"""Which characters count as emoji or as decorative, for cleaning and for checks.

One definition serves three users:

* normalization removes or replaces these characters in scraped text;
* output tests assert that feeds, site data and digests are free of them;
* ``scripts/check_ascii.py``, the charset gate, rejects them in ``data/**``,
  ``*.zh-CN.md`` and ``locales/zh-CN.json``.

The gate is a stand-alone script that cannot import this package, so it holds
its own copy of the two tables. tests/test_textrules.py fails when the copies
differ; change both together.

"Decorative" means code points that carry no content of their own: ligatures,
no-break and other special spaces, soft hyphens, zero width characters and
direction marks. Accented letters, CJK text, Greek and mathematical symbols
(including arrows, superscripts and double-struck letters) are ordinary text
and are neither emoji nor decorative.
"""

from __future__ import annotations

from typing import Final

CodePointRange = tuple[int, int]
"""Inclusive range of code points ``(first, last)``."""

EMOJI_RANGES: Final[tuple[CodePointRange, ...]] = (
    (0x1F000, 0x1FAFF),  # Mahjong tiles .. Symbols and Pictographs Extended-A
    (0x2600, 0x27BF),  # Miscellaneous Symbols and Dingbats
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
    (0x2007, 0x2007),  # figure space
    (0x200B, 0x200F),  # zero width characters and direction marks
    (0x202A, 0x202F),  # bidi embedding controls, narrow no-break space
    (0x2060, 0x2064),  # word joiner and invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFB00, 0xFB06),  # Latin ligatures
    (0xFEFF, 0xFEFF),  # zero width no-break space
)
"""Code points that are not emoji but are still decorative or invisible."""


def _in_ranges(code_point: int, ranges: tuple[CodePointRange, ...]) -> bool:
    return any(low <= code_point <= high for low, high in ranges)


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
    found = dict.fromkeys(char for char in text if is_emoji(char) or is_decorative(char))
    return list(found)
