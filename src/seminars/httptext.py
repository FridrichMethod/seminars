"""Turning a response body into text. Pure functions, no I/O.

Re-exported by ``seminars.http``; import ``decode_body`` from there.
"""

from __future__ import annotations

import codecs
import re
from typing import Final

_SNIFF_BYTES: Final = 2048
_FALLBACK_ENCODING: Final = "cp1252"
_BOM_ENCODINGS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE, whose mark is a prefix of this one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_LABEL: Final = r"[A-Za-z0-9._:-]+"
_HEADER_CHARSET: Final = re.compile(rf"""charset\s*=\s*["']?({_LABEL})""", re.IGNORECASE)
_XML_ENCODING: Final = re.compile(
    rf"""^\s*<\?xml[^>]*?\bencoding\s*=\s*["']({_LABEL})""".encode("ascii")
)
_META_CHARSET: Final = re.compile(
    rf"""<meta\b[^>]*?\bcharset\s*=\s*["']?\s*({_LABEL})""".encode("ascii"), re.IGNORECASE
)
_LATIN1_CODECS: Final = frozenset({"iso8859-1", "ascii"})
_REFUSED_CODECS: Final = frozenset({"utf-7"})  # obsolete; lets ASCII text smuggle other text
_WIDE_CODEC_PREFIXES: Final = ("utf-16", "utf-32")


def _text_codec(label: str | None) -> str | None:
    """Python's name of the text encoding called ``label``; ``None`` when it is not usable.

    ISO-8859-1 and ASCII labels are read as windows-1252, as browsers do:
    pages labelled that way routinely contain windows-1252 punctuation.
    UTF-7 is refused, also as in browsers.
    """
    if not label:
        return None
    try:
        b"".decode(label)  # also rejects codecs that are not text encodings (base64, ...)
        codec = codecs.lookup(label).name
    except (LookupError, ValueError):
        return None
    if codec in _REFUSED_CODECS:
        return None
    return _FALLBACK_ENCODING if codec in _LATIN1_CODECS else codec


def _declared_encoding(head: bytes) -> str | None:
    """The encoding an XML declaration or an HTML ``<meta>`` tag in ``head`` names."""
    for pattern in (_XML_ENCODING, _META_CHARSET):
        match = pattern.search(head)
        if match is None:
            continue
        codec = _text_codec(match.group(1).decode("ascii"))
        # A declaration that can be read as ASCII is not in a 16 or 32 bit encoding.
        if codec is not None and not codec.startswith(_WIDE_CODEC_PREFIXES):
            return codec
    return None


def decode_body(content: bytes, content_type: str | None) -> str:
    """Decode a response body to text.

    The encoding is taken from, in this order: a byte order mark (which is
    consumed); the ``charset`` parameter of the Content-Type header; the
    ``encoding`` of an XML declaration or the charset of an HTML ``<meta>``
    tag within the first 2048 bytes. A body that states nothing is UTF-8 if
    it is valid UTF-8 and windows-1252 otherwise, which is what old
    hand-written pages use. Unknown labels are skipped, and bytes that are
    invalid in the chosen encoding become U+FFFD.
    """
    encoding = next((name for mark, name in _BOM_ENCODINGS if content.startswith(mark)), None)
    if encoding is None:
        header = _HEADER_CHARSET.search(content_type or "")
        encoding = _text_codec(header.group(1) if header is not None else None)
    if encoding is None:
        encoding = _declared_encoding(content[:_SNIFF_BYTES])
    if encoding is None:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            encoding = _FALLBACK_ENCODING
    try:
        return content.decode(encoding, errors="replace")
    except (LookupError, ValueError):  # exotic codecs that refuse the "replace" handler
        return content.decode("utf-8", errors="replace")
