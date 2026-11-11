"""Which characters count as emoji or as decorative, and the code that acts on it.

One definition serves three users:

* normalization removes or replaces these characters in scraped text
  (``clean_text``);
* output tests assert that feeds, site data and digests are free of them
  (``decorative_characters``);
* ``scripts/check_ascii.py``, the charset gate, rejects them in ``data/**``,
  ``*.zh-CN.md`` and ``locales/zh-CN.json``. ``data_tree_problems`` is the
  same check for use at run time.

The gate is a stand-alone script that cannot import this package, so it holds
its own copy of the two tables. tests/test_textrules.py fails when the copies
differ; change both together.

"Decorative" means code points that carry no content of their own: ligatures,
no-break and other typographic spaces, soft hyphens, zero width characters,
variation selectors, line separators and direction marks. Characters that
cannot be shown count as well: the blank Hangul fillers, the interlinear
annotation marks, the object replacement character and the private use area
U+E000-F8FF, whose characters mean something only in the font of the page
they came from (the bullet of the Symbol font that Word pastes as U+F0B7,
icon fonts) and are boxes everywhere else. Accented letters, CJK text, Greek
and mathematical symbols (including arrows, superscripts and double-struck
letters) are ordinary text and are neither emoji nor decorative; so is the
replacement character U+FFFD, which marks a byte that could not be decoded.

"Emoji" covers the pictograph blocks and the whole Miscellaneous Symbols and
Dingbats blocks (U+2600-27BF: stars, check marks, hearts, warning signs,
ornamental arrows), which pages use as decoration. A few characters of those
blocks are notation in the sciences and count as ordinary text: the sun
U+2609 (solar mass and radius), the planet signs U+263F-2647 (planetary
masses; female U+2640 and male U+2642 in biology) and the musical flat,
natural and sharp U+266D-266F (the musical isomorphisms of geometry).

The rule for everything written below data/
-------------------------------------------

Every file below data/ is committed, published and checked by the gate: no
emoji, no decorative or invisible character, no raw control character. Text
taken from a web page, a feed or an LLM answer can contain all of them, so
whoever writes such text below data/ has to do one of two things first:

* pass it through ``clean_text`` (what normalization does for every text
  field of an event), or
* write the file as ASCII-only JSON, with every other character as a
  ``\\uXXXX`` escape (``seminars.models.dump_ascii_json``; what the LLM cache
  does, because its values must come back exactly as they went in).

Identifiers copied from a source are a third case: ``ascii_safe`` turns them
into plain ASCII without losing information (``seminars.models.source_key``
does it for the keys of events and of the uid map).

``seminars run`` checks its own output with ``data_tree_problems`` before it
finishes; see the flow in ``seminars.contracts``.

Lone surrogates
---------------

A Python string can hold half of a UTF-16 pair (U+D800 to U+DFFF) on its own:
``json.loads`` returns one for an unpaired escape such as ``"\\ud83d"``, which
sites produce when they cut a string in the middle of an emoji. No UTF-8 file
can hold such a character, so writing it raises ``UnicodeEncodeError``. It is
therefore removed wherever text enters or leaves the program: ``clean_text``
deletes it, every model drops it from its strings on validation
(``seminars.models.Model``), ``FetchedDocument.parse_json`` drops it from the
decoded data, the readable JSON writers never emit it, and
``data_charset_problems`` reports it. ``strip_surrogates`` and
``without_surrogates`` are the helpers behind that. ``ascii_safe`` and
ASCII-only JSON keep it as an escape, which is plain ASCII.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Final

CodePointRange = tuple[int, int]
"""Inclusive range of code points ``(first, last)``."""

EMOJI_RANGES: Final[tuple[CodePointRange, ...]] = (
    (0x1F000, 0x1FAFF),  # Mahjong tiles .. Symbols and Pictographs Extended-A
    # Miscellaneous Symbols and Dingbats (U+2600-27BF) without the characters that are
    # notation in the sciences: U+2609 sun, U+263F-2647 planets (with female and male),
    # U+266D-266F musical flat, natural and sharp.
    (0x2600, 0x2608),
    (0x260A, 0x263E),
    (0x2648, 0x266C),
    (0x2670, 0x27BF),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x20E3, 0x20E3),  # combining enclosing keycap
    (0xFE0F, 0xFE0F),  # emoji variation selector
    (0xE0020, 0xE007F),  # tag characters
)
"""Code points treated as emoji, including the modifiers that build emoji sequences."""

DECORATIVE_RANGES: Final[tuple[CodePointRange, ...]] = (
    (0x00A0, 0x00A0),  # no-break space
    (0x00AD, 0x00AD),  # soft hyphen
    (0x061C, 0x061C),  # Arabic letter mark
    (0x115F, 0x1160),  # Hangul initial and medial filler
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x2000, 0x200F),  # typographic spaces, zero width characters and direction marks
    (0x2028, 0x202F),  # line and paragraph separator, bidi controls, narrow no-break space
    (0x205F, 0x2064),  # medium mathematical space, word joiner and invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0x3164, 0x3164),  # Hangul filler
    (0xE000, 0xF8FF),  # private use area (Symbol font bullets pasted from Word, icon fonts)
    (0xFB00, 0xFB06),  # Latin ligatures
    (0xFE0E, 0xFE0E),  # text variation selector
    (0xFEFF, 0xFEFF),  # zero width no-break space
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xFFF9, 0xFFFC),  # interlinear annotation marks, object replacement character
)
"""Code points that are not emoji but are still decorative or invisible."""


def _in_ranges(code_point: int, ranges: tuple[CodePointRange, ...]) -> bool:
    return any(low <= code_point <= high for low, high in ranges)


def _character_class(ranges: tuple[CodePointRange, ...]) -> str:
    """The body of a regular expression character class that matches ``ranges``."""
    return "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in ranges)


_SURROGATE_RANGE: Final[CodePointRange] = (0xD800, 0xDFFF)
"""Halves of UTF-16 pairs. Alone they are not characters and cannot be encoded."""

_TABLES: Final = _character_class(EMOJI_RANGES) + _character_class(DECORATIVE_RANGES)
_CONTROLS: Final = "\x00-\x09\x0b-\x1f\x7f-\x9f"  # category Cc without the newline
_SURROGATE_CLASS: Final = _character_class((_SURROGATE_RANGE,))
_UNWANTED: Final = re.compile(f"[{_TABLES}]")
_REJECTED_IN_DATA: Final = re.compile(f"[{_TABLES}{_CONTROLS}{_SURROGATE_CLASS}]")
_SURROGATES: Final = re.compile(f"[{_SURROGATE_CLASS}]")

_LIGATURE_LETTERS: Final = ("ff", "fi", "fl", "ffi", "ffl", "st", "st")  # U+FB00 .. U+FB06
_SPACES: Final = (0x0009, 0x00A0, *range(0x2000, 0x200B), 0x202F, 0x205F)
_LINE_BREAKS: Final = (0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x0085, 0x2028, 0x2029)
_EMOJI_GENDER: Final = re.compile("\u200d[\u2640\u2642]")
"""Female or male sign joined to an emoji (a gendered emoji); alone they are ordinary text."""


def _cleaning_table() -> dict[int, str | None]:
    """Translation table of ``clean_text``: delete by default, replace where that reads better."""
    table: dict[int, str | None] = {}
    controls = ((0x00, 0x09), (0x0B, 0x1F), (0x7F, 0x9F))
    for low, high in (*EMOJI_RANGES, *DECORATIVE_RANGES, *controls, _SURROGATE_RANGE):
        table.update(dict.fromkeys(range(low, high + 1)))
    table.update({0xFB00 + offset: letters for offset, letters in enumerate(_LIGATURE_LETTERS)})
    table.update(dict.fromkeys(_SPACES, " "))
    table.update(dict.fromkeys(_LINE_BREAKS, "\n"))
    return table


_CLEANING: Final = _cleaning_table()


def is_emoji(char: str) -> bool:
    """True when the single character ``char`` is an emoji or an emoji modifier."""
    return _in_ranges(ord(char), EMOJI_RANGES)


def is_decorative(char: str) -> bool:
    """True for ligatures, invisible and unprintable characters and no-break spaces."""
    return _in_ranges(ord(char), DECORATIVE_RANGES)


def decorative_characters(text: str) -> list[str]:
    """The emoji and decorative characters in ``text``: each once, in order of appearance.

    An empty result means the text passes the charset gate's rule for
    ``data/**`` as far as these two tables are concerned (the gate also
    rejects raw control characters, which JSON never contains unescaped).
    """
    return list(dict.fromkeys(_UNWANTED.findall(text)))


def clean_text(text: str) -> str:
    """Scraped text without anything the charset rule for ``data/**`` rejects.

    This is the Unicode cleaning of normalization, and the one function to
    use for any text that is stored or published:

    * the text is brought into NFC (never NFKC, which would flatten
      double-struck letters, superscripts and other mathematical notation);
    * the Latin ligatures U+FB00 to U+FB06 become their letters;
    * no-break spaces, the typographic spaces U+2000 to U+200A, U+202F and
      U+205F and the tab become an ordinary space;
    * every kind of line break (CR LF, CR, vertical tab, form feed, NEL,
      U+2028, U+2029) becomes a newline;
    * emoji, the other decorative and invisible characters (private use
      characters and blank fillers among them) and the remaining control
      characters are removed;
    * lone surrogates (U+D800 to U+DFFF) are removed, so the result can
      always be encoded as UTF-8 and written to a file.

    Letters with accents, CJK text and symbols stay exactly as they are; no
    name is transliterated. Whitespace is neither collapsed nor stripped, so
    removing a character can leave two spaces next to each other; tidying
    whitespace is the caller's next step. The function is idempotent, and
    ``decorative_characters(clean_text(x))`` is empty for every ``x``.
    """
    text = unicodedata.normalize("NFC", text)
    text = _EMOJI_GENDER.sub("", text).replace("\r\n", "\n").translate(_CLEANING)
    return unicodedata.normalize("NFC", text)  # removals can bring a letter and its accent together


def strip_surrogates(text: str) -> str:
    """``text`` without lone surrogates (U+D800 to U+DFFF); everything else is kept.

    The result can be encoded as UTF-8 whatever the input was. Use it for
    text that must stay as it is otherwise (a URL, a response body);
    ``clean_text`` already includes it.
    """
    return _SURROGATES.sub("", text)


def without_surrogates(value: Any) -> Any:
    """Plain data with ``strip_surrogates`` applied to every string in it.

    Walks dictionaries (keys and values), lists and tuples, which is what
    decoded JSON and the input of a model consist of; any other value is
    returned as it is. Containers are copied, never changed in place.

    Data may be nested to any depth. The walk keeps a list of what is
    still to do instead of calling itself once per level, so JSON that is
    nested deeper than the recursion limit of the interpreter is cleaned
    like any other and never ends in a ``RecursionError``.
    """
    result: list[Any] = [None]
    # Each entry: a container that is being built, a place in it, the value that belongs
    # there, and whether that value is the finished list of items of a tuple.
    todo: list[tuple[Any, Any, Any, bool]] = [(result, 0, value, False)]
    while todo:
        target, place, item, is_tuple_items = todo.pop()
        if is_tuple_items:
            target[place] = tuple(item)
        elif isinstance(item, str):
            target[place] = _SURROGATES.sub("", item)
        elif isinstance(item, dict):
            copy: dict[Any, Any] = {}
            target[place] = copy
            entries = []
            for key, inner in item.items():
                if isinstance(key, str):
                    key = _SURROGATES.sub("", key)
                elif type(key) is tuple:
                    key = without_surrogates(key)  # a key is hashable, so it is not deep
                copy[key] = None  # keeps the keys in their order
                entries.append((copy, key, inner, False))
            # The last entry added is the next one taken, so the first one goes in last.
            # Entries are then set in their order: of two keys that became one, the later
            # one counts, as in a dictionary that is built in one go.
            todo.extend(reversed(entries))
        elif isinstance(item, list) or type(item) is tuple:
            items: list[Any] = [None] * len(item)
            if isinstance(item, list):
                target[place] = items
            else:
                todo.append((target, place, items, True))  # its turn comes after its items
            todo.extend((items, index, inner, False) for index, inner in enumerate(item))
        else:
            target[place] = item
    return result[0]


def ascii_safe(text: str) -> str:
    """Escape non-ASCII characters so that a message is safe for logs and reports.

    Every character outside ASCII becomes a backslash escape (``\\xe9``,
    ``\\u2609``, ``\\U0001f680``; a lone surrogate becomes ``\\ud83d``); ASCII
    text is returned unchanged, so the function is idempotent.
    """
    return text.encode("ascii", "backslashreplace").decode("ascii")


def data_charset_problems(text: str) -> list[str]:
    """What the charset gate would report for a file below data/ with this content.

    One entry per offending character, as ``<line>:<column> U+XXXX`` (both
    counted from 1), plus ``missing final newline`` when that applies. Emoji,
    decorative characters and control characters other than the newline are
    offending, which includes a byte order mark and the CR of a CRLF line
    ending. A lone surrogate is offending as well: text that contains one
    cannot be written at all. An empty list means the gate accepts the text.
    """
    problems = []
    if _REJECTED_IN_DATA.search(text) is not None:
        for number, line in enumerate(text.split("\n"), start=1):
            for match in _REJECTED_IN_DATA.finditer(line):
                problems.append(f"{number}:{match.start() + 1} U+{ord(match.group()):04X}")
    if text and not text.endswith("\n"):
        problems.append("missing final newline")
    return problems


def data_tree_problems(data_dir: Path) -> list[str]:
    """Charset problems of every file below ``data_dir``; empty when all of them pass.

    The run-time twin of the gate's rule for ``data/**``, for the code that
    writes those files: ``seminars run`` calls it on its data directory after
    persisting and treats any problem like a finding of the secret scan, and
    ``seminars validate`` reports the same list. Entries look like the gate's
    output, ``data/events.json:12:40 U+00A0``, with the path relative to the
    parent of ``data_dir``, and never contain file content. A missing
    directory has no problems.
    """
    problems: list[str] = []
    for path in sorted(data_dir.rglob("*")) if data_dir.is_dir() else []:
        if not path.is_file():
            continue
        name = path.relative_to(data_dir.parent).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{name}: not valid UTF-8")
            continue
        problems.extend(f"{name}:{problem}" for problem in data_charset_problems(text))
    return problems
