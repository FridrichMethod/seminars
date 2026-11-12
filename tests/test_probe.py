"""The format probe behind ``seminars add-source``: one request, one guess, a YAML entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from fakes import FakeHttpClient
from seminars.contracts import SourceProber, load_implementation
from seminars.http import offline_client
from seminars.models import ProbeResult, SourceConfig
from seminars.sources.generic.html_list import load_params
from seminars.sources.generic.rss import RssParams
from seminars.sources.probe import probe_source

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def recorded_url(source_id: str, file_name: str) -> str:
    manifest = json.loads((FIXTURES / source_id / "manifest.json").read_text(encoding="utf-8"))
    [url] = [url for url, entry in manifest["entries"].items() if entry["file"] == file_name]
    return url


def probe_recorded(source_id: str, file_name: str) -> tuple[ProbeResult, list[str]]:
    http = offline_client(source_id)
    url = recorded_url(source_id, file_name)
    result = probe_source(http, url)
    assert http.requests == [url]  # one request, never more
    return result, http.requests


def probe_text(
    text: str, content_type: str | None, url: str = "https://lab.example.edu/x"
) -> ProbeResult:
    http = FakeHttpClient()
    http.add(url, text, content_type=content_type)
    result = probe_source(http, url)
    assert http.requests == [url]
    return result


def suggested_source(result: ProbeResult) -> SourceConfig:
    """The suggested entry loads as a source as it is, and is plain ASCII."""
    assert result.suggested_yaml.isascii()
    [entry] = yaml.safe_load(result.suggested_yaml)["sources"]
    return SourceConfig.model_validate(entry)


def test_the_contract_points_here() -> None:
    prober: SourceProber = load_implementation("source_probe")
    assert prober is probe_source


class TestFormats:
    def test_localist_api(self) -> None:
        result, _ = probe_recorded("localist-stanford", "events-types-page1.json")
        assert result.format == "localist"
        assert result.content_type == "application/json"
        assert suggested_source(result).adapter == "localist"

    def test_localist_calendar_page(self) -> None:
        result, _ = probe_recorded("sites-bioengineering", "detail-localist-redirect.html")
        assert result.format == "localist"
        source = suggested_source(result)
        assert source.url == "https://events.stanford.edu/api/2/events"
        assert any("JSON-LD" in note for note in result.notes)

    def test_drupal_jsonapi(self) -> None:
        result, _ = probe_recorded("sites-mathematics", "hs-event-page1.json")
        assert result.format == "drupal_jsonapi"
        assert any("node--hs_event" in note for note in result.notes)
        assert suggested_source(result).adapter == "drupal_jsonapi"

    def test_rss_with_markup_titles(self) -> None:
        result, requests = probe_recorded("sites-cheme", "events-all-rss.xml")
        assert result.format == "rss"
        source = suggested_source(result)
        assert source.url == requests[0]
        params = load_params(RssParams, source)
        assert (params.feed.start, params.feed.title_is_markup) == ("pubDate", True)
        assert any("2 item(s)" in note for note in result.notes)

    def test_atom(self) -> None:
        atom = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'
            "<entry><title>A</title></entry></feed>"
        )
        result = probe_text(atom, "application/atom+xml")
        assert result.format == "rss"
        assert load_params(RssParams, suggested_source(result)).feed.start == "published"

    def test_ics(self) -> None:
        result, _ = probe_recorded("rsorg-topos-colloquium", "series.ics")
        assert result.format == "ics"
        assert suggested_source(result).adapter == "ics"
        assert result.notes[0].startswith("iCalendar with ")

    def test_ics_without_a_content_type(self) -> None:
        assert probe_text("BEGIN:VCALENDAR\nEND:VCALENDAR", None).format == "ics"

    def test_jsonld(self) -> None:
        block = json.dumps({"@type": "Event", "name": "Talk", "startDate": "2026-11-17"})
        text = f'<html><head><script type="application/ld+json">{block}</script></head></html>'
        result = probe_text(text, "text/html")
        assert result.format == "jsonld"
        assert suggested_source(result).adapter == "jsonld"

    def test_json_ld_document(self) -> None:
        text = json.dumps({"@graph": [{"@type": "Event", "name": "T", "startDate": "2026-11-17"}]})
        assert probe_text(text, "application/ld+json").format == "jsonld"

    def test_html_lists_its_feeds_but_writes_no_selectors(self) -> None:
        result, _ = probe_recorded("custom-rain-seminar", "index-9ec536ca.html")
        assert result.format == "html"
        assert suggested_source(result).adapter == "llm_page"
        assert "the page links https://rain.stanford.edu/feed.xml" in result.notes
        assert "selectors" not in result.suggested_yaml
        assert any("html_list needs selectors" in note for note in result.notes)

    def test_html_of_a_drupal_site(self) -> None:
        text = (
            '<!DOCTYPE html><html><head><meta name="Generator" content="Drupal 10">'
            '<link rel="alternate" type="text/calendar" href="/events/ical"></head>'
            '<body><a href="webcal://x">x</a><a href="cal.ics">ics</a></body></html>'
        )
        result = probe_text(text, None, url="https://dept.example.edu/events")
        assert result.format == "html"
        assert result.notes[:2] == [
            "the page links https://dept.example.edu/events/ical",
            "the page links https://dept.example.edu/cal.ics",
        ]
        assert any("jsonapi/node/hs_event" in note for note in result.notes)

    @pytest.mark.parametrize(
        ("text", "content_type", "note"),
        [
            ("plain words", "text/plain", "content type text/plain is not a known format"),
            ("{broken", "application/json", "not valid JSON"),
            ('{"items": []}', "application/json", "neither Localist nor Drupal"),
            ('<?xml version="1.0"?><urlset></urlset>', "application/xml", "no RSS or Atom feed"),
        ],
    )
    def test_unknown(self, text: str, content_type: str, note: str) -> None:
        result = probe_text(text, content_type)
        assert result.format == "unknown"
        assert any(note in line for line in result.notes)
        assert suggested_source(result).adapter == "llm_page"


class TestSuggestedYaml:
    def test_the_entry_names_what_to_change(self) -> None:
        result = probe_text(
            "BEGIN:VCALENDAR", "text/calendar", url="https://www.lab.example.edu/cal"
        )
        source = suggested_source(result)
        assert source.id == "new-www-lab-example-edu"
        assert source.group == "new"
        assert source.name == "CHANGE www.lab.example.edu"
        assert source.default_fields == []

    def test_a_redirect_suggests_the_final_address(self) -> None:
        url = "https://cs.example.edu/events/rss"
        final = "https://www.cs.example.edu/events/rss"
        http = FakeHttpClient()
        feed = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        http.add(url, feed, content_type="application/rss+xml", final_url=final)
        result = probe_source(http, url)
        assert http.requests == [url]
        assert result.url == url
        assert suggested_source(result).url == final
        assert any(f"redirected to {final}" in note for note in result.notes)

    def test_no_redirect_note_without_a_redirect(self) -> None:
        result = probe_text("BEGIN:VCALENDAR", "text/calendar")
        assert suggested_source(result).url == "https://lab.example.edu/x"
        assert not any("redirected" in note for note in result.notes)

    def test_urls_outside_ascii_are_escaped(self) -> None:
        url = "https://lab.example.edu/\u00e9v\u00e9nements"
        result = probe_text("BEGIN:VCALENDAR", "text/calendar", url=url)
        assert suggested_source(result).url == "https://lab.example.edu/%C3%A9v%C3%A9nements"

    def test_notes_are_printable_ascii(self) -> None:
        link = '<link rel="alternate" type="application/rss+xml" href="/f\u00e9ed\n">'
        text = f"<html><head>{link}</head></html>"
        result = probe_text(text, "text/html")
        assert all(note.isascii() and note.isprintable() for note in result.notes)

    def test_errors_of_the_request_propagate(self) -> None:
        http = FakeHttpClient()
        with pytest.raises(LookupError):
            probe_source(http, "https://lab.example.edu/missing")


def test_every_result_validates() -> None:
    payload: Any = probe_text("BEGIN:VCALENDAR", "text/calendar")
    assert ProbeResult.model_validate(payload.model_dump()) == payload
