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
variation selectors, line separators and direction marks. Accented letters,
CJK text, Greek and mathematical symbols (including arrows, superscripts and
double-struck letters) are ordinary text and are neither emoji nor decorative.

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
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Final

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
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x2000, 0x200F),  # typographic spaces, zero width characters and direction marks
    (0x2028, 0x202F),  # line and paragraph separator, bidi controls, narrow no-break space
    (0x205F, 0x2064),  # medium mathematical space, word joiner and invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFB00, 0xFB06),  # Latin ligatures
    (0xFE0E, 0xFE0E),  # text variation selector
    (0xFEFF, 0xFEFF),  # zero width no-break space
)
"""Code points that are not emoji but are still decorative or invisible."""


def _in_ranges(code_point: int, ranges: tuple[CodePointRange, ...]) -> bool:
    return any(low <= code_point <= high for low, high in ranges)


def _character_class(ranges: tuple[CodePointRange, ...]) -> str:
    """The body of a regular expression character class that matches ``ranges``."""
    return "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in ranges)


_TABLES: Final = _character_class(EMOJI_RANGES) + _character_class(DECORATIVE_RANGES)
_CONTROLS: Final = "\x00-\x09\x0b-\x1f\x7f-\x9f"  # category Cc without the newline
_UNWANTED: Final = re.compile(f"[{_TABLES}]")
_REJECTED_IN_DATA: Final = re.compile(f"[{_TABLES}{_CONTROLS}]")

_LIGATURE_LETTERS: Final = ("ff", "fi", "fl", "ffi", "ffl", "st", "st")  # U+FB00 .. U+FB06
_SPACES: Final = (0x0009, 0x00A0, *range(0x2000, 0x200B), 0x202F, 0x205F)
_LINE_BREAKS: Final = (0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x0085, 0x2028, 0x2029)
_EMOJI_GENDER: Final = re.compile("\u200d[\u2640\u2642]")
"""Female or male sign joined to an emoji (a gendered emoji); alone they are ordinary text."""


def _cleaning_table() -> dict[int, str | None]:
    """Translation table of ``clean_text``: delete by default, replace where that reads better."""
    table: dict[int, str | None] = {}
    for low, high in (*EMOJI_RANGES, *DECORATIVE_RANGES, (0x00, 0x09), (0x0B, 0x1F), (0x7F, 0x9F)):
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
    """True for ligatures, invisible characters and no-break spaces."""
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
    * emoji, the other decorative and invisible characters and the remaining
      control characters are removed.

    Letters with accents, CJK text and symbols stay exactly as they are; no
    name is transliterated. Whitespace is neither collapsed nor stripped, so
    removing a character can leave two spaces next to each other; tidying
    whitespace is the caller's next step. The function is idempotent, and
    ``decorative_characters(clean_text(x))`` is empty for every ``x``.
    """
    text = unicodedata.normalize("NFC", text)
    text = _EMOJI_GENDER.sub("", text).replace("\r\n", "\n").translate(_CLEANING)
    return unicodedata.normalize("NFC", text)  # removals can bring a letter and its accent together


def ascii_safe(text: str) -> str:
    """Escape non-ASCII characters so that a message is safe for logs and reports.

    Every character outside ASCII becomes a backslash escape (``\\xe9``,
    ``\\u2609``, ``\\U0001f680``); ASCII text is returned unchanged, so the
    function is idempotent.
    """
    return text.encode("ascii", "backslashreplace").decode("ascii")


def data_charset_problems(text: str) -> list[str]:
    """What the charset gate would report for a file below data/ with this content.

    One entry per offending character, as ``<line>:<column> U+XXXX`` (both
    counted from 1), plus ``missing final newline`` when that applies. Emoji,
    decorative characters and control characters other than the newline are
    offending, which includes a byte order mark and the CR of a CRLF line
    ending. An empty list means the gate accepts the text.
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
