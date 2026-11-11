"""Static checks of the web site in site/ and of its messages in locales/.

The site is plain HTML, CSS and JavaScript without a build step. These tests
read the files as text, so they run without a browser:

* every page carries the Content-Security-Policy meta tag before anything it
  loads, loads only its own module scripts and style sheet, and has no inline
  script, style or event handler;
* no file contains interface text (text nodes or labelled attributes in the
  HTML), a raw HTML sink (``innerHTML`` and friends) or an absolute http(s)
  URL, so nothing is loaded from or linked to another host by the code;
* every message key the pages use exists in both languages, every key below
  ``site`` is used, and both languages have the same keys and placeholders.

tests/test_site_browser.py checks the same promises on the rendered pages.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, get_args

import pytest

from seminars.i18n import flatten, placeholders
from seminars.models import EventStatus, EventType, Institution, PublicSourceState

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = REPO_ROOT / "site"
LOCALES_DIR = REPO_ROOT / "locales"

PAGES = ("index.html", "status.html")
REQUIRED_FILES = {
    "index.html",
    "status.html",
    "style.css",
    "i18n.js",
    "dom.js",
    "time.js",
    "filters.js",
    "page.js",
    "app.js",
    "subscribe.js",
    "status.js",
}
ALLOWED_SUFFIXES = {".html", ".css", ".js"}

CSP_DIRECTIVES = {
    "default-src": ["'none'"],
    "script-src": ["'self'"],
    "style-src": ["'self'"],
    "img-src": ["'self'", "data:"],
    "connect-src": ["'self'"],
    "base-uri": ["'none'"],
    "form-action": ["'none'"],
}

# Attributes that browsers show or read aloud; their text has to come from the locales.
LABEL_ATTRIBUTES = {"title", "alt", "placeholder", "aria-label", "label", "value"}
# The attributes data-i18n-attr may fill in (mirrors ATTRIBUTES in site/i18n.js).
TRANSLATABLE_ATTRIBUTES = {"aria-label", "placeholder", "title"}
FORBIDDEN_ELEMENTS = {"iframe", "object", "embed", "img", "form", "base", "frame", "style"}
VOID_ELEMENTS = {"meta", "link", "input", "br", "hr", "img", "source", "col", "wbr", "area"}

RAW_HTML_SINKS = re.compile(
    r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function"
    r"|srcdoc|createContextualFragment|DOMParser|setHTMLUnsafe|parseHTMLUnsafe"
    r"|javascript:",
    re.IGNORECASE,
)
ABSOLUTE_HTTP_URL = re.compile(r"https?://", re.IGNORECASE)
KEY_LITERAL = re.compile(r'"((?:site|fields|feeds)\.[A-Za-z0-9_.-]*)"')
OTHER_QUOTED_KEY = re.compile(r"['`](?:site|fields|feeds)\.")
JS_IMPORT = re.compile(r'^\s*import\s[^;]*?from\s+"([^"]+)";', re.MULTILINE)

# Messages that are the same in both languages on purpose.
SAME_IN_BOTH_LANGUAGES = {
    "site.event.time_range",
    "site.institutions.slac",
    "site.institutions.biohub",
    "site.status.groups.researchseminars",
}


class ParsedPage(HTMLParser):
    """Elements (in document order) and visible text of one HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.texts: list[str] = []
        self.raw_bodies: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {name: value or "" for name, value in attrs}))
        if tag not in VOID_ELEMENTS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            while self._open.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self._open and self._open[-1] in ("script", "style"):
            self.raw_bodies.append(data)
        elif data.strip():
            self.texts.append(data.strip())

    def find(self, tag: str) -> list[dict[str, str]]:
        return [attrs for name, attrs in self.elements if name == tag]


def parse_page(name: str) -> ParsedPage:
    parser = ParsedPage()
    parser.feed((SITE_DIR / name).read_text(encoding="utf-8"))
    parser.close()
    return parser


def site_files() -> list[Path]:
    return sorted(path for path in SITE_DIR.rglob("*") if path.is_file())


def script_files() -> list[Path]:
    return [path for path in site_files() if path.suffix == ".js"]


def catalog(lang: str) -> dict[str, str]:
    tree = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    return flatten(tree)


def site_messages(lang: str) -> dict[str, str]:
    return {key: value for key, value in catalog(lang).items() if key.startswith("site.")}


def child_ids(messages: dict[str, str], prefix: str) -> list[str]:
    """Ids directly below ``prefix`` in file order, as ``children`` in site/i18n.js returns them."""
    ids: list[str] = []
    for key in messages:
        if key.startswith(f"{prefix}."):
            name = key.removeprefix(f"{prefix}.").split(".")[0]
            if name not in ids:
                ids.append(name)
    return ids


def html_keys() -> set[str]:
    """Message keys named by data-i18n and data-i18n-attr in the pages."""
    keys: set[str] = set()
    for name in PAGES:
        for _, attrs in parse_page(name).elements:
            if "data-i18n" in attrs:
                keys.add(attrs["data-i18n"])
            for pair in filter(None, attrs.get("data-i18n-attr", "").split(";")):
                keys.add(pair.split(":", 1)[1].strip())
    return keys


def script_literals() -> set[str]:
    """Message keys and key prefixes (ending with ".") spelled out in the scripts."""
    return {
        literal
        for path in script_files()
        for literal in KEY_LITERAL.findall(path.read_text(encoding="utf-8"))
    }


class TestFiles:
    def test_the_site_has_its_pages_scripts_and_style_sheet(self) -> None:
        names = {path.relative_to(SITE_DIR).as_posix() for path in site_files()}
        assert names >= REQUIRED_FILES

    def test_only_html_css_and_javascript(self) -> None:
        """No images, fonts or anything else that could have come from another site."""
        assert [path.name for path in site_files() if path.suffix not in ALLOWED_SUFFIXES] == []

    @pytest.mark.parametrize("path", site_files(), ids=lambda path: path.name)
    def test_files_are_ascii(self, path: Path) -> None:
        """Interface text lives in the locales; the code holds neither Chinese nor symbols."""
        assert path.read_bytes().isascii()

    @pytest.mark.parametrize("path", site_files(), ids=lambda path: path.name)
    def test_no_raw_html_sink(self, path: Path) -> None:
        """Untrusted text reaches the page through textContent and DOM construction only."""
        assert RAW_HTML_SINKS.findall(path.read_text(encoding="utf-8")) == []

    @pytest.mark.parametrize("path", site_files(), ids=lambda path: path.name)
    def test_no_absolute_http_url(self, path: Path) -> None:
        """Every address in the code is relative, so nothing points at another host."""
        assert ABSOLUTE_HTTP_URL.findall(path.read_text(encoding="utf-8")) == []

    def test_style_sheet_loads_nothing(self) -> None:
        """No web fonts, imports or background images, from anywhere."""
        css = (SITE_DIR / "style.css").read_text(encoding="utf-8")
        assert re.findall(r"url\(|@import|@font-face", css) == []

    @pytest.mark.parametrize("path", script_files(), ids=lambda path: path.name)
    def test_scripts_import_only_their_neighbours(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        targets = JS_IMPORT.findall(text)
        assert all(re.fullmatch(r"\./[a-z0-9]+\.js", target) for target in targets), targets
        assert all((SITE_DIR / target).is_file() for target in targets)
        assert "import(" not in text

    @pytest.mark.parametrize("path", script_files(), ids=lambda path: path.name)
    def test_scripts_spell_message_keys_with_double_quotes(self, path: Path) -> None:
        """Keeps the key checks below complete: they only look for double-quoted keys."""
        assert OTHER_QUOTED_KEY.findall(path.read_text(encoding="utf-8")) == []


class TestPages:
    @pytest.mark.parametrize("name", PAGES)
    def test_content_security_policy(self, name: str) -> None:
        page = parse_page(name)
        policies = [
            attrs
            for attrs in page.find("meta")
            if attrs.get("http-equiv", "").lower() == "content-security-policy"
        ]
        assert len(policies) == 1
        directives = {}
        for part in filter(None, (part.strip() for part in policies[0]["content"].split(";"))):
            directive, *sources = part.split()
            directives[directive] = sources
        assert directives == CSP_DIRECTIVES

    @pytest.mark.parametrize("name", PAGES)
    def test_policy_comes_before_anything_that_loads(self, name: str) -> None:
        tags = [
            (tag, attrs.get("http-equiv", "").lower()) for tag, attrs in parse_page(name).elements
        ]
        policy = tags.index(("meta", "content-security-policy"))
        loading = [index for index, (tag, _) in enumerate(tags) if tag in ("script", "link")]
        assert loading
        assert policy < min(loading)

    @pytest.mark.parametrize("name", PAGES)
    def test_scripts_are_local_modules_without_inline_code(self, name: str) -> None:
        page = parse_page(name)
        scripts = page.find("script")
        assert scripts
        for attrs in scripts:
            assert attrs.get("type") == "module"
            assert re.fullmatch(r"[a-z]+\.js", attrs.get("src", "")), attrs
        assert [body for body in page.raw_bodies if body.strip()] == []

    @pytest.mark.parametrize("name", PAGES)
    def test_links_load_the_local_style_sheet_and_no_icon_from_elsewhere(self, name: str) -> None:
        links = parse_page(name).find("link")
        assert {(attrs["rel"], attrs["href"]) for attrs in links} == {
            ("stylesheet", "style.css"),
            ("icon", "data:,"),
        }

    @pytest.mark.parametrize("name", PAGES)
    def test_no_inline_style_handler_or_embedding(self, name: str) -> None:
        page = parse_page(name)
        assert {tag for tag, _ in page.elements} & FORBIDDEN_ELEMENTS == set()
        offending = [
            (tag, attribute)
            for tag, attrs in page.elements
            for attribute in attrs
            if attribute == "style" or attribute.startswith("on")
        ]
        assert offending == []

    @pytest.mark.parametrize("name", PAGES)
    def test_no_interface_text_in_the_markup(self, name: str) -> None:
        page = parse_page(name)
        assert page.texts == []
        labelled = [
            (tag, attribute)
            for tag, attrs in page.elements
            for attribute in attrs
            if attribute in LABEL_ATTRIBUTES and tag != "meta"
        ]
        assert labelled == []

    @pytest.mark.parametrize("name", PAGES)
    def test_page_language_and_viewport(self, name: str) -> None:
        page = parse_page(name)
        assert page.find("html") == [{"lang": "en"}]
        names = {attrs.get("name"): attrs.get("content") for attrs in page.find("meta")}
        assert names["viewport"] == "width=device-width, initial-scale=1"
        assert names["color-scheme"] == "light dark"
        assert names["referrer"] == "no-referrer"
        assert {"charset": "utf-8"} in page.find("meta")

    @pytest.mark.parametrize("name", PAGES)
    def test_only_translatable_attributes_are_filled_in(self, name: str) -> None:
        for _, attrs in parse_page(name).elements:
            for pair in filter(None, attrs.get("data-i18n-attr", "").split(";")):
                attribute, _, key = pair.partition(":")
                assert attribute.strip() in TRANSLATABLE_ATTRIBUTES
                assert key.strip()

    def test_translatable_attributes_match_the_script(self) -> None:
        text = (SITE_DIR / "i18n.js").read_text(encoding="utf-8")
        match = re.search(r"const ATTRIBUTES = Object\.freeze\(\[([^\]]*)\]\)", text)
        assert match is not None
        assert set(re.findall(r'"([a-z-]+)"', match.group(1))) == TRANSLATABLE_ATTRIBUTES


class TestMessageKeys:
    def test_keys_in_the_markup_exist_in_both_languages(self) -> None:
        keys = html_keys()
        assert keys
        for lang in ("en", "zh-CN"):
            assert sorted(keys - set(catalog(lang))) == []

    def test_keys_in_the_scripts_exist(self) -> None:
        """A literal is a message, the base of a plural pair, or a prefix ending with "."."""
        messages = catalog("en")
        unknown = []
        for literal in sorted(script_literals()):
            if literal.endswith("."):
                known = any(key.startswith(literal) for key in messages)
            else:
                known = literal in messages or (
                    f"{literal}_one" in messages and f"{literal}_other" in messages
                )
            if not known:
                unknown.append(literal)
        assert unknown == []

    def test_every_site_message_is_used(self) -> None:
        literals = script_literals() | html_keys()
        prefixes = tuple(literal for literal in literals if literal.endswith("."))
        unused = [
            key
            for key in site_messages("en")
            if key not in literals
            and not key.startswith(prefixes)
            and re.sub(r"_(one|other)$", "", key) not in literals
        ]
        assert unused == []

    def test_site_has_no_dynamic_prefix_that_covers_everything(self) -> None:
        """Only leaf families may be addressed by prefix, or the check above proves nothing."""
        prefixes = {literal for literal in script_literals() if literal.endswith(".")}
        assert prefixes <= {
            "fields.",
            "feeds.",
            "site.institutions.",
            "site.event_types.",
            "site.filters.range_options.",
            "site.status.groups.",
            "site.status.states.",
            "site.subscribe.apple.steps.",
        }


class TestSiteMessages:
    def test_site_namespace_is_filled(self) -> None:
        assert len(site_messages("en")) >= 60

    def test_same_keys_in_both_languages(self) -> None:
        assert list(site_messages("en")) == list(site_messages("zh-CN"))

    def test_same_placeholders_in_both_languages(self) -> None:
        english, chinese = site_messages("en"), site_messages("zh-CN")
        assert {key: placeholders(english[key]) for key in english} == {
            key: placeholders(chinese[key]) for key in english
        }

    def test_chinese_is_translated(self) -> None:
        english, chinese = site_messages("en"), site_messages("zh-CN")
        untranslated = [
            key
            for key in english
            if key not in SAME_IN_BOTH_LANGUAGES and (chinese[key] == english[key])
        ]
        assert untranslated == []
        assert sorted(key for key in SAME_IN_BOTH_LANGUAGES if chinese[key] != english[key]) == []

    @pytest.mark.parametrize("lang", ["en", "zh-CN"])
    def test_messages_hold_no_markup_or_addresses(self, lang: str) -> None:
        """The site shows messages with textContent, so markup would appear literally."""
        bad = [
            key
            for key, message in site_messages(lang).items()
            if re.search(r"[<>]|https?:|&[a-z]+;", message)
        ]
        assert bad == []

    def test_plural_messages_come_in_pairs(self) -> None:
        keys = set(site_messages("en"))
        plural = re.compile(r"_(one|other)$")
        bases = {plural.sub("", key) for key in keys if plural.search(key)}
        assert bases
        assert sorted(base for base in bases if {f"{base}_one", f"{base}_other"} - keys) == []
        for base in bases:
            assert "count" in placeholders(site_messages("en")[f"{base}_other"])

    @pytest.mark.parametrize(
        ("prefix", "vocabulary"),
        [
            ("site.institutions", Institution),
            ("site.event_types", EventType),
            ("site.status.states", PublicSourceState),
        ],
    )
    def test_vocabularies_have_a_label_for_every_value(self, prefix: str, vocabulary: Any) -> None:
        assert child_ids(site_messages("en"), prefix) == list(get_args(vocabulary))

    def test_event_status_labels(self) -> None:
        """Confirmed events carry no label; the other states do."""
        statuses = [status for status in get_args(EventStatus) if status != "confirmed"]
        assert all(f"site.event.{status}" in site_messages("en") for status in statuses)

    def test_range_options_match_the_filter_code(self) -> None:
        text = (SITE_DIR / "filters.js").read_text(encoding="utf-8")
        match = re.search(r"export const RANGES = Object\.freeze\(\{([^}]*)\}\)", text)
        assert match is not None
        ranges = re.findall(r"([a-z_]+):", match.group(1))
        assert child_ids(site_messages("en"), "site.filters.range_options") == ranges

    def test_apple_calendar_steps_are_numbered_in_order(self) -> None:
        steps = child_ids(site_messages("en"), "site.subscribe.apple.steps")
        assert len(steps) >= 4
        assert steps == sorted(steps)
