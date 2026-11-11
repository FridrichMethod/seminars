"""Turning a response body into text. Pure functions, no I/O.

Re-exported by ``seminars.http``; import ``decode_body`` from there.

The body and the label that names its encoding both come from the site. A
label is therefore only ever used to pick one of the encodings web pages
are written in (``_WEB_CODECS``, the list browsers support). Python knows
many more codecs under that name space, and some of them must never run on
foreign input: ``punycode`` and ``idna`` take time quadratic in the length
of the body, ``unicode_escape`` turns plain ASCII into any character at all
(lone surrogates and NUL included), UTF-7 and HZ let ASCII text smuggle
other text, and ``base64``, ``zlib`` and the like are not text encodings.
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
_WIDE_CODEC_PREFIXES: Final = ("utf-16", "utf-32")
_WEB_CODECS: Final = frozenset(
    {
        "utf-8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        *(f"cp{number}" for number in (866, 874, 932, 949, *range(1250, 1259))),
        *(f"iso8859-{part}" for part in (*range(2, 12), *range(13, 17))),
        "tis-620",
        "koi8-r",
        "koi8-u",
        "mac-roman",
        "mac-cyrillic",
        "gbk",
        "gb18030",
        "big5",
        "big5hkscs",
        "euc_jp",
        "shift_jis",
        "iso2022_jp",
        "euc_kr",
    }
)
"""Every encoding a label may select, by the name ``codecs.lookup`` gives it.

These are the encodings of the WHATWG Encoding Standard that Python has.
All of them decode in time linear in the input and accept the "replace"
error handler. UTF-32 is not among them (browsers do not support it); a
body in UTF-32 is still read when it starts with a byte order mark.
"""
_READ_AS: Final = {"iso8859-1": "cp1252", "ascii": "cp1252", "gb2312": "gbk"}
"""Labels that browsers read as a larger encoding, because pages mislabel themselves."""


def _text_codec(label: str | None) -> str | None:
    """Python's name of the web encoding called ``label``; ``None`` for any other label.

    ISO-8859-1 and ASCII labels are read as windows-1252 and GB 2312 as GBK,
    as browsers do: pages labelled that way routinely contain characters of
    the larger encoding. The label only selects from ``_WEB_CODECS``; it
    never reaches a decoder by itself.
    """
    if not label:
        return None
    try:
        codec = codecs.lookup(label).name
    except (LookupError, ValueError):
        return None
    codec = _READ_AS.get(codec, codec)
    return codec if codec in _WEB_CODECS else None


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
    hand-written pages use. A label that does not name an encoding of the
    web (see the module docstring) is skipped as if it were absent, and
    bytes that are invalid in the chosen encoding become U+FFFD.
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
    return content.decode(encoding, errors="replace")
