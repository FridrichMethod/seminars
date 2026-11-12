#!/usr/bin/env python3
"""Charset gate for this repository.

Scans every git-tracked file and enforces the charset rules of the project:

* every text file is UTF-8 without BOM, uses LF line endings and ends with a
  newline;
* outside the allow-list a file may only contain printable ASCII (0x20-0x7E)
  and newlines (tabs are tolerated in Makefiles only);
* the allow-list is ``*.zh-CN.md``, ``locales/zh-CN.json``, ``data/**`` and
  ``tests/fixtures/**``;
* ``*.zh-CN.md``, ``locales/zh-CN.json`` and ``data/**`` may hold non-ASCII
  text (Chinese, accented names, mathematical and scientific symbols) but
  no emoji, no decorative or invisible code points (ligatures, no-break and
  typographic spaces, zero width characters, line separators, blank fillers,
  private use characters, ...) and no control characters. ``data/**`` is
  published output: the pipeline cleans scraped text before it is stored,
  and this check catches whatever slips through;
* ``tests/fixtures/**`` holds verbatim snapshots of real pages. They are test
  input and are never published, and the cleaning code has to be tested
  against exactly what the sites serve, so anything that is valid UTF-8 is
  accepted there, a stray NUL byte included.

Binary files are skipped; a file is binary when its name ends in one of
``BINARY_SUFFIXES`` (images, fonts, archives, PDF). Every other file is text
and must be UTF-8: a byte order mark of UTF-16 or UTF-32 is reported with the
name of the encoding, and a NUL byte as not UTF-8 text. In ``tests/fixtures/**``
a NUL byte is reported only when the file reads like UTF-16 or UTF-32 text
without a byte order mark, which has a NUL at every second or every fourth
byte (``WIDE_NUL_SHARE``). A binary file of another kind fails the gate until
its suffix is added to the list. Violations are printed as
``path:line:col U+XXXX NAME`` and the exit status is non-zero.

``--commit-msg FILE`` checks a commit message instead of the tree, which is
what the commit-msg hook of pre-commit runs: printable ASCII and newlines
only. Comment lines (starting with ``#``) and everything below the scissors
line of ``git commit --verbose`` are not part of the message and are not
checked.

The script uses the standard library only and runs under any Python 3, so it
cannot import the project. EMOJI_RANGES and DECORATIVE_RANGES are therefore
repeated in ``src/seminars/textrules.py`` for the code that cleans and checks
text at run time; tests/test_textrules.py fails when the two copies differ.
Usage: ``python3 scripts/check_ascii.py [--root DIR]`` or
``python3 scripts/check_ascii.py --commit-msg FILE``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import unicodedata

MODE_STRICT = "strict"  # printable ASCII only
MODE_CLEAN = "clean"  # any script, but no emoji, decorative or control characters
MODE_FREE = "free"  # any valid UTF-8

FREE_PREFIXES = ("tests/fixtures/",)
CLEAN_PREFIXES = ("data/",)
CLEAN_PATHS = ("locales/zh-CN.json",)
CLEAN_SUFFIX = ".zh-CN.md"
TAB_BASENAMES = ("Makefile", "makefile", "GNUmakefile")
TAB_SUFFIXES = (".mk",)

BOM = b"\xef\xbb\xbf"
OTHER_BOMS = (  # longest first: the UTF-32 LE mark starts with the UTF-16 LE one
    (b"\xff\xfe\x00\x00", "UTF-32 LE"),
    (b"\x00\x00\xfe\xff", "UTF-32 BE"),
    (b"\xff\xfe", "UTF-16 LE"),
    (b"\xfe\xff", "UTF-16 BE"),
)
BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".7z",
    ".mp3",
    ".mp4",
    ".webm",
    ".ogg",
    ".wav",
)
WIDE_SAMPLE_BYTES = 4096
WIDE_NUL_SHARE = 0.5  # of the even or of the odd bytes of the sample
MAX_REPORTS_PER_FILE = 20
COMMENT_PREFIX = "#"
SCISSORS = ">8"

# Inclusive code point ranges treated as emoji. Mirrored in src/seminars/textrules.py.
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # Mahjong tiles .. Symbols and Pictographs Extended-A
    # Miscellaneous Symbols and Dingbats (U+2600-27BF) without the characters that are
    # notation in the sciences: U+2609 sun, U+263F-2647 planets (with female and male),
    # U+266D-266F musical flat, natural and sharp.
    (0x2600, 0x2608),
    (0x260A, 0x263E),
    (0x2648, 0x266C),
    (0x2670, 0x27BF),
    # Characters that are text by default but are shown as emoji by most systems:
    # U+203C double and U+2049 question exclamation mark, U+2139 information source,
    # U+24C2 circled M.
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2139, 0x2139),
    (0x24C2, 0x24C2),
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

# Inclusive ranges that are not emoji but are still decorative or invisible.
# Mirrored in src/seminars/textrules.py.
DECORATIVE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00A0, 0x00A0),  # no-break space
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x061C, 0x061C),  # Arabic letter mark
    (0x115F, 0x1160),  # Hangul initial and medial filler
    (0x17B4, 0x17B5),  # Khmer inherent vowels, which are not written
    (0x180B, 0x180F),  # Mongolian free variation selectors and vowel separator
    (0x2000, 0x200F),  # typographic spaces, zero width characters and direction marks
    (0x2028, 0x202F),  # line and paragraph separator, bidi controls, narrow no-break space
    (0x205F, 0x2064),  # medium mathematical space, word joiner and invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0x2800, 0x2800),  # Braille pattern blank, a blank filler
    (0x3164, 0x3164),  # Hangul filler
    (0xE000, 0xF8FF),  # private use area (Symbol font bullets pasted from Word, icon fonts)
    (0xFB00, 0xFB06),  # Latin ligatures
    (0xFE00, 0xFE0E),  # variation selectors 1 to 14 and the text variation selector
    (0xFEFF, 0xFEFF),  # zero width no-break space
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xFFF9, 0xFFFC),  # interlinear annotation marks, object replacement character
    (0xE0001, 0xE0001),  # language tag
    (0xE0100, 0xE01EF),  # variation selectors supplement
    (0xF0000, 0xFFFFD),  # supplementary private use area A (icon fonts)
    (0x100000, 0x10FFFD),  # supplementary private use area B
)


def _in_ranges(code_point: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= code_point <= high for low, high in ranges)


def is_emoji(char: str) -> bool:
    """Return True when ``char`` is an emoji or an emoji modifier."""
    return _in_ranges(ord(char), EMOJI_RANGES)


def is_decorative(char: str) -> bool:
    """Return True for ligatures, invisible and unprintable characters and no-break spaces."""
    return _in_ranges(ord(char), DECORATIVE_RANGES)


def classify(path: str) -> str:
    """Map a repository-relative POSIX path to its charset mode."""
    if path.startswith(FREE_PREFIXES):
        return MODE_FREE
    if path.startswith(CLEAN_PREFIXES) or path in CLEAN_PATHS or path.endswith(CLEAN_SUFFIX):
        return MODE_CLEAN
    return MODE_STRICT


def allows_tab(path: str) -> bool:
    """Tabs are only acceptable where the file format requires them."""
    name = os.path.basename(path)
    return name in TAB_BASENAMES or name.endswith(TAB_SUFFIXES)


def is_binary(path: str) -> bool:
    """Whether ``path`` names a binary file: its suffix is one of ``BINARY_SUFFIXES``."""
    return path.lower().endswith(BINARY_SUFFIXES)


def reads_like_wide_text(data: bytes) -> bool:
    """Whether ``data`` reads like UTF-16 or UTF-32 text: NUL is half its even or odd bytes."""
    sample = data[:WIDE_SAMPLE_BYTES]
    halves = [half for half in (sample[0::2], sample[1::2]) if half]
    return any(half.count(0) >= WIDE_NUL_SHARE * len(half) for half in halves)


def _encoding_problem(path: str, data: bytes, mode: str) -> str | None:
    """Why ``data`` is no UTF-8 text at all (another encoding, a NUL byte); else ``None``."""
    for mark, name in OTHER_BOMS:
        if data.startswith(mark):
            return f"{path}: not UTF-8 ({name} byte order mark)"
    nul = data.find(b"\0")
    if nul >= 0 and (mode != MODE_FREE or reads_like_wide_text(data)):
        return f"{path}: NUL byte at byte offset {nul}: not UTF-8 text"
    return None


def _is_bad_char(char: str, mode: str, tab_ok: bool) -> bool:
    if char == "\n":
        return False
    if mode == MODE_STRICT:
        if char == "\t":
            return not tab_ok
        return not 0x20 <= ord(char) <= 0x7E
    if is_emoji(char) or is_decorative(char):
        return True
    return unicodedata.category(char) == "Cc"


def _describe(char: str) -> str:
    name = unicodedata.name(char, "")
    label = f"U+{ord(char):04X}"
    return f"{label} {name}" if name else label


def _scan_chars(path: str, text: str, mode: str) -> list[str]:
    messages: list[str] = []
    tab_ok = allows_tab(path)
    for line_number, line in enumerate(text.split("\n"), start=1):
        if line.endswith("\r"):
            line = line[:-1]  # reported once as a CRLF line ending
        for column, char in enumerate(line, start=1):
            if _is_bad_char(char, mode, tab_ok):
                messages.append(f"{path}:{line_number}:{column} {_describe(char)}")
    return messages


def check_bytes(path: str, data: bytes) -> list[str]:
    """Return the list of violation messages for one file."""
    if not data or is_binary(path):
        return []
    mode = classify(path)
    problem = _encoding_problem(path, data, mode)
    if problem is not None:
        return [problem]
    messages: list[str] = []
    if data.startswith(BOM):
        messages.append(f"{path}:1:1 U+FEFF byte order mark at start of file")
        data = data[len(BOM) :]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        messages.append(f"{path}: not valid UTF-8 (byte offset {error.start})")
        return messages
    crlf_lines = [n for n, line in enumerate(text.split("\n"), start=1) if line.endswith("\r")]
    if crlf_lines:
        count = len(crlf_lines)
        messages.append(f"{path}:{crlf_lines[0]}: CRLF line ending ({count} line(s) in total)")
    if not text.endswith("\n"):
        messages.append(f"{path}: missing final newline")
    if mode != MODE_FREE:
        messages.extend(_scan_chars(path, text, mode))
    return messages


def check_commit_message(path: str, data: bytes) -> list[str]:
    """Return the violation messages for a commit message read from ``path``.

    The message may only hold printable ASCII and newlines. Comment lines
    and the part below the scissors line are skipped; line numbers count
    every line of the file.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return [f"{path}: not valid UTF-8 (byte offset {error.start})"]
    messages: list[str] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        if line.startswith(COMMENT_PREFIX):
            if SCISSORS in line and set(line) <= set("#- >8"):
                break
            continue
        for column, char in enumerate(line.rstrip("\r"), start=1):
            if _is_bad_char(char, MODE_STRICT, False):
                messages.append(f"{path}:{line_number}:{column} {_describe(char)}")
    return messages


def _check_commit_message_file(path: str) -> int:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        print(f"check_ascii: cannot read the commit message: {error}", file=sys.stderr)
        return 2
    messages = check_commit_message(path, data)
    for message in messages:
        print(message)
    if messages:
        count = len(messages)
        print(f"check_ascii: {count} violation(s) in the commit message", file=sys.stderr)
        return 1
    return 0


def tracked_files(root: str) -> list[str]:
    """List git-tracked files below ``root`` as POSIX paths."""
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    names = [name for name in output.decode("utf-8", "surrogateescape").split("\0") if name]
    return sorted(names)


def check_tree(root: str) -> tuple[int, list[tuple[str, list[str]]]]:
    """Check every tracked file. Returns (number of files checked, messages per file)."""
    checked = 0
    failures: list[tuple[str, list[str]]] = []
    for name in tracked_files(root):
        full_path = os.path.join(root, name)
        if os.path.islink(full_path) or not os.path.isfile(full_path):
            continue
        with open(full_path, "rb") as handle:
            data = handle.read()
        checked += 1
        messages = check_bytes(name, data)
        if messages:
            failures.append((name, messages))
    return checked, failures


def _print_failures(failures: list[tuple[str, list[str]]]) -> int:
    total = 0
    for _name, messages in failures:
        total += len(messages)
        for message in messages[:MAX_REPORTS_PER_FILE]:
            print(message)
        hidden = len(messages) - MAX_REPORTS_PER_FILE
        if hidden > 0:
            print(f"    ... and {hidden} more in this file")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the charset rules of the repository.")
    parser.add_argument("--root", default=".", help="directory inside the git work tree")
    parser.add_argument(
        "--commit-msg", metavar="FILE", help="check this commit message instead of the tree"
    )
    args = parser.parse_args(argv)
    if args.commit_msg is not None:
        return _check_commit_message_file(args.commit_msg)
    try:
        toplevel = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=args.root)
        checked, failures = check_tree(toplevel.decode("utf-8").strip())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"check_ascii: cannot list git-tracked files: {error}", file=sys.stderr)
        return 2
    if failures:
        total = _print_failures(failures)
        files = len(failures)
        print(f"check_ascii: {total} violation(s) in {files} of {checked} file(s)", file=sys.stderr)
        return 1
    print(f"check_ascii: OK ({checked} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
