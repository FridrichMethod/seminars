"""Text handling of normalization: markup, whitespace and the phrases it recognises.

Adapters copy what a source says. Everything in here turns that into the
text an ``Event`` holds: plain text without markup, cleaned by
``seminars.textrules.clean_text``, with tidy whitespace and a bounded
length. The functions are pure and never raise on odd input.

A field can be as large as a page (several megabytes), and a broken or
hostile page must not stall a run. ``one_line`` and ``paragraphs`` therefore
cut what they are given to ``RAW_FACTOR`` times their limit before anything
else happens, and every pattern applied to text runs in time linear in its
length.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Final

from selectolax.lexbor import LexborHTMLParser

from seminars.models import TBA_TITLE, EventStatus
from seminars.textrules import clean_text

ELLIPSIS: Final = "..."
RAW_FACTOR: Final = 8
"""How much raw text, markup included, a field may hold per character of its limit."""

_BLOCK_TAGS: Final = (
    "address|article|aside|blockquote|br|dd|div|dl|dt|figcaption|figure|footer|h[1-6]|header"
    "|hr|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot|th|thead|tr|ul"
)
_INLINE_TAGS: Final = (
    "a|abbr|b|bdi|bdo|big|center|cite|code|del|dfn|em|font|i|img|ins|kbd|label|mark|q|s|samp"
    "|small|span|strike|strong|sub|sup|time|tt|u|var|wbr"
)
_BLOCK_TAG: Final = re.compile(rf"</?(?:{_BLOCK_TAGS})(?:\s[^<>]*)?/?>", re.IGNORECASE)
_KNOWN_TAG: Final = re.compile(
    rf"</?(?:{_BLOCK_TAGS}|{_INLINE_TAGS})(?:\s[^<>]*)?/?>", re.IGNORECASE
)
_HIDDEN_OPENER: Final = re.compile(r"<(?:(script|style)\b[^<>]*>|!--)", re.IGNORECASE)
_HIDDEN_CLOSERS: Final[dict[str, re.Pattern[str]]] = {
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
    "--": re.compile(r"-->"),
}
_HIDDEN_TAGS: Final = ["script", "style", "noscript", "template", "head"]

_DASHES: Final = r"[\-\u2013\u2014]"
_AFTER_PREFIX: Final = rf"(?:\s*[:!|]+|\s+{_DASHES}+(?=\s))\s*"
_BEFORE_SUFFIX: Final = rf"(?:\s*[:|]+\s*|\s+{_DASHES}+\s+)"
_TBA_WORDS: Final = (
    "tba|tbd|tbc|to be announced|to be determined|to be confirmed|to come|forthcoming"
    "|coming soon|pending|untitled|no title|no title yet"
)
_CJK_TBA_WORDS: Final = (
    "\u5f85\u5b9a|\u672a\u5b9a|\u5f85\u516c\u5e03|\u5f85\u786e\u5b9a|\u5f85\u78ba\u5b9a"
    "|\u53e6\u884c\u901a\u77e5|\ubbf8\uc815"
)
"""Chinese and Japanese for "to be determined", "undetermined", "to be announced", "to be
confirmed" (simplified and traditional) and "to be notified separately"; Korean for
"undecided"."""
_CJK_TITLE_WORDS: Final = (
    "\u9898\u76ee|\u984c\u76ee|\u6807\u9898|\u6a19\u984c|\u62a5\u544a\u9898\u76ee"
    "|\u5831\u544a\u984c\u76ee|\u8bb2\u9898|\u8b1b\u984c|\u30bf\u30a4\u30c8\u30eb|\uc81c\ubaa9"
)
"""Chinese, Japanese and Korean for "title", "title of the talk" and "title of the lecture"."""
_CJK_KIND_WORDS: Final = (
    "\u8bb2\u5ea7|\u8b1b\u5ea7|\u7814\u8ba8\u4f1a|\u7814\u8a0e\u6703|\u62a5\u544a|\u5831\u544a"
)
"""Chinese for "lecture", "seminar" and "talk" (simplified and traditional), the words that
may come before a placeholder like "talk" and "seminar" do in English."""
_ENGLISH_TBA: Final = (
    rf"(?:(?:talk|seminar|lecture|colloquium) )?(?:title (?:is |and abstract )?)?(?:{_TBA_WORDS})"
    r"|title"
)
_CJK_PREFIX: Final = rf"(?:(?:{_CJK_KIND_WORDS}) ?)?(?:(?:{_CJK_TITLE_WORDS}) ?)?"
_CJK_TBA: Final = rf"{_CJK_PREFIX}(?:{_CJK_TBA_WORDS})"
_CJK_THEN_ENGLISH: Final = rf"{_CJK_PREFIX}(?:{_TBA_WORDS})"
_TBA: Final = re.compile(
    rf"(?:{_ENGLISH_TBA}|{_CJK_THEN_ENGLISH})(?: ?(?:{_CJK_TBA}))?"
    rf"|(?:{_CJK_TBA})(?: ?(?:{_ENGLISH_TBA}))?"
)
"""A placeholder in English or in CJK, or one of each side by side ("TBA (to be
determined)" with the second in Chinese, also without a space between them). The words
for "title" and for the kind of event come before either one in CJK ("title: TBA",
"lecture: to be determined"), as "title" and "seminar" do in English."""
_ABSTRACT_HEADING: Final = re.compile(
    r"^(?:(?:abstract|summary|description)(?: and title)?\b"
    "|\u6458\u8981|\u7b80\u4ecb|\u7c21\u4ecb|\u6982\u8981|\u8981\u65e8|\ucd08\ub85d|\uc694\uc57d"
    r")\s*"
)
"""A heading in English, or Chinese, Japanese or Korean for "abstract" and "summary"."""
_NO_ABSTRACT: Final = re.compile(
    r"n a|na|none|nil|no abstract(?: (?:yet|available|provided))?|not available"
    "|(?:\u6682\u65e0|\u66ab\u7121|\u65e0|\u7121)(?:\u6458\u8981|\u7b80\u4ecb|\u7c21\u4ecb)?"
    "|\u306a\u3057|\uc5c6\uc74c"
)
"""Placeholders of an abstract; the last are Chinese for "none (yet)", optionally with
"abstract" or "summary", and Japanese and Korean for "none"."""
_HONORIFIC: Final = re.compile(
    r"^(?:(?:dr|prof|professor|mr|mrs|ms|mx|sir|dame)\b\.?\s*)+", re.IGNORECASE
)
_DEGREE: Final = re.compile(
    r"^(?:ph\.?\s?d|d\.?\s?phil|sc\.?\s?d|m\.?\s?d|m\.?\s?sc?|b\.?\s?sc?|m\.?\s?eng|mph|mba"
    r"|jr|sr|ii|iii|iv)\.?$",
    re.IGNORECASE,
)


def _status_patterns(words: str) -> tuple[re.Pattern[str], ...]:
    """Ways a title carries a status word: as a prefix, as a suffix, in brackets, alone."""
    word = rf"(?:this\s+)?(?:event\s+|talk\s+|seminar\s+)?(?:has\s+been\s+|is\s+)?(?:{words})"
    return (
        re.compile(rf"^\s*[\[(]\s*{word}\s*[\])](?:{_AFTER_PREFIX}|\s*)", re.IGNORECASE),
        re.compile(rf"^\s*{word}{_AFTER_PREFIX}", re.IGNORECASE),
        re.compile(rf"\s*[\[(]\s*{word}\s*[\])]\s*$", re.IGNORECASE),
        re.compile(rf"{_BEFORE_SUFFIX}{word}\s*!*\s*$", re.IGNORECASE),
        re.compile(rf"^\s*{word}\s*!*\s*$", re.IGNORECASE),
    )


_STATUS_MARKERS: Final[tuple[tuple[EventStatus, tuple[re.Pattern[str], ...]], ...]] = (
    ("cancelled", _status_patterns("cancell?ed|postponed")),
    ("tentative", _status_patterns("tentative")),
)


def word_set(text: str) -> frozenset[str]:
    """The words of ``text`` as a set; how the word lists of normalization are written."""
    return frozenset(text.split())


def html_to_text(markup: str) -> str:
    """The text of an HTML fragment; block elements and ``<br>`` end a line.

    Scripts, styles and comments are dropped and entities are decoded. The
    result still needs ``paragraphs`` or ``one_line``.
    """
    marked = _BLOCK_TAG.sub(lambda match: "\n" + match.group(0), markup)
    tree = LexborHTMLParser(marked)
    tree.strip_tags(_HIDDEN_TAGS)
    body = tree.body
    return body.text(deep=True, separator="") if body is not None else ""


def drop_hidden(text: str) -> str:
    """``text`` without scripts, styles and comments; each becomes one space.

    An opener without its closer stays as it is: ``a <!-- b`` in a title
    is text. Once the closer of a kind was not found, it is not searched
    for again, so the time is linear in the length of ``text``.
    """
    pieces: list[str] = []
    position = 0
    unclosed: set[str] = set()
    while (opener := _HIDDEN_OPENER.search(text, position)) is not None:
        kind = (opener.group(1) or "--").lower()
        closer = None if kind in unclosed else _HIDDEN_CLOSERS[kind].search(text, opener.end())
        if closer is None:
            unclosed.add(kind)
            pieces.append(text[position : opener.end()])
        else:
            pieces.append(text[position : opener.start()] + " ")
        position = opener.end() if closer is None else closer.end()
    pieces.append(text[position:])
    return "".join(pieces)


def bounded(text: str, limit: int) -> str:
    """At most ``RAW_FACTOR * limit`` characters of ``text``, without a tag cut in half."""
    size = RAW_FACTOR * max(limit, 1)
    if len(text) <= size:
        return text
    cut = text[:size]
    tail = cut.rfind("<")
    return cut[:tail] if tail > cut.rfind(">") else cut


def strip_markup(text: str) -> str:
    """Plain text that may hold stray tags and entities, without them.

    Only tags with a known HTML name are removed, so ``a < b`` and
    ``<x, y>`` in a title about mathematics stay as they are. A block tag
    leaves a line break behind.
    """
    without_hidden = drop_hidden(text)
    with_breaks = _BLOCK_TAG.sub("\n", without_hidden)
    return html.unescape(_KNOWN_TAG.sub("", with_breaks))


def truncate(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters; a cut ends in ``...``."""
    if len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        return text[:limit]
    return text[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def one_line(text: str | None, *, limit: int) -> str | None:
    """A single-line text field: markup removed, cleaned, whitespace collapsed, bounded.

    ``None`` and text without content give ``None``.
    """
    if text is None:
        return None
    collapsed = " ".join(clean_text(strip_markup(bounded(text, limit))).split())
    return truncate(collapsed, limit) or None


def paragraphs(text: str | None, *, limit: int, is_html: bool = False) -> str | None:
    """A multi-line text field: one paragraph per line, no empty lines, bounded.

    With ``is_html`` the text is an HTML fragment; otherwise stray markup is
    removed. ``None`` and text without content give ``None``.
    """
    if text is None:
        return None
    raw = bounded(text, limit)
    plain = clean_text(html_to_text(raw) if is_html else strip_markup(raw))
    lines = (" ".join(line.split()) for line in plain.split("\n"))
    return truncate("\n".join(line for line in lines if line), limit) or None


def _phrase(text: str) -> str:
    """``text`` as the words the phrases of this module are written in.

    That is ``fold``, with the dots of "T.B.A." removed first and Korean
    syllables, which ``fold`` takes apart into letters, put together again.
    Letters of every script are words, so a title or an abstract in Chinese
    or Greek, or one that is a single accented letter, is never empty.
    """
    return unicodedata.normalize("NFC", fold(text.replace(".", "")))


def is_tba(title: str | None) -> bool:
    """Whether ``title`` says that the title is not announced yet (or says nothing).

    Nothing is said by a title without a letter or a digit of any script
    ("--", "?!"); a title in Chinese, or the Greek letter pi alone, is one.
    The placeholders are English ("TBA", "Title to come") and Chinese,
    Japanese or Korean (for "to be determined", alone or after the word
    for "title" or for the kind of event), also one of each side by side,
    with or without a space, and an English one after the CJK word for
    "title".
    """
    if title is None:
        return True
    words = _phrase(title)
    return not words or _TBA.fullmatch(words) is not None


def is_empty_abstract(text: str | None) -> bool:
    """Whether an abstract is only a heading or says that there is none yet.

    "Abstract", "Abstract: TBA", "Summary", "TBD", "N/A" and "No abstract"
    say nothing about the talk, and neither do the same in Chinese,
    Japanese or Korean; "Abstract: we study knots", "More information
    forthcoming" and any other abstract in any script do.
    """
    if text is None:
        return True
    rest = _ABSTRACT_HEADING.sub("", _phrase(text), count=1)
    return not rest or _TBA.fullmatch(rest) is not None or _NO_ABSTRACT.fullmatch(rest) is not None


def split_status(title: str) -> tuple[str, EventStatus | None]:
    """Take a status word off a title: ``"CANCELLED: Knots"`` -> ``("Knots", "cancelled")``.

    Recognised are "cancelled", "canceled" and "postponed" (both mean the
    talk does not take place at this time) and "tentative", as a prefix or
    a suffix set off by punctuation or brackets, or as the whole title.
    The word inside a sentence ("Cancelled flights and how to avoid them")
    is left alone. A title that was nothing but the marker becomes "TBA".
    """
    for status, patterns in _STATUS_MARKERS:
        for pattern in patterns:
            stripped, count = pattern.subn("", title, count=1)
            if count:
                return stripped.strip() or TBA_TITLE, status
    return title, None


def tidy_name(name: str) -> str:
    """A person's name without honorifics ("Prof. Dr.") and degrees (", PhD")."""
    parts = [part.strip() for part in _HONORIFIC.sub("", name.strip()).split(",")]
    kept = [part for part in parts if part and _DEGREE.fullmatch(part) is None]
    words = " ".join(kept).split()
    while len(words) > 1 and _DEGREE.fullmatch(words[-1]) is not None:
        words = words[:-1]
    return " ".join(words)


def is_degree(text: str) -> bool:
    """Whether ``text`` is an academic degree or a name suffix such as "PhD" or "Jr."."""
    return _DEGREE.fullmatch(text.strip()) is not None


def fold(text: str) -> str:
    """``text`` for comparisons: no accents, no case, no punctuation, single spaces.

    Used to compare names and titles from different sources. It is never
    stored: names keep their accents in every output.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    letters = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w\s]|_", " ", letters.casefold()).split())


def canonical_name(name: str) -> str:
    """The form in which two spellings of one person's name are equal."""
    return fold(tidy_name(name))
