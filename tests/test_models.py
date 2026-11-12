"""Tests for the event, draft, classification, report, health and public models."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from factories import (
    PACIFIC,
    REFERENCE_NOW,
    make_candidate,
    make_document,
    make_draft,
    make_event,
    make_payload,
    make_window,
)
from seminars.models import (
    FEED_IDS,
    FIELD_IDS,
    HEALTH_HISTORY_LIMIT,
    TBA_TITLE,
    Event,
    EventDraft,
    EventHints,
    ExcludedEvent,
    ExclusionReason,
    FieldId,
    HealthFile,
    LLMHealthEntry,
    LLMProviderResult,
    LLMUsage,
    PublicEvent,
    PublicEventsPayload,
    PublicStatusPayload,
    RuleExclusionReason,
    RunCounts,
    RunReport,
    SourceHealthEntry,
    SourceRunResult,
    Speaker,
    SpeakerDraft,
    TimeWindow,
    UidMap,
    canonical_fields,
    dump_json,
    feed_path,
    source_key,
    split_source_key,
    url_has_credential,
)
from seminars.models.base import ascii_url
from seminars.textrules import data_charset_problems

RELATIVE_LINKS = ["/events/relative-path", "events/1", "//example.edu/x", "example.edu/events"]
NOT_WEB_PAGES = ["mailto:jane@example.edu", "javascript:void(0)", "ftp://example.edu/x", ""]
NBSP = chr(0xA0)
ZERO_WIDTH_SPACE = chr(0x200B)
ROCKET = chr(0x1F680)
E_ACUTE = chr(0xE9)

EXPECTED_EVENT_FIELDS = [
    "uid",
    "title",
    "speakers",
    "series",
    "host_org",
    "institution",
    "event_type",
    "start",
    "end",
    "all_day",
    "location",
    "is_online",
    "stream_url",
    "abstract",
    "url",
    "source_urls",
    "source_ids",
    "fields",
    "field_tag_method",
    "picks_score",
    "picks_reasons",
    "status",
    "extraction",
    "first_seen",
    "last_changed",
    "missing_runs",
    "sequence",
]


BOM = chr(0xFEFF)


class TestVocabularies:
    def test_field_ids(self) -> None:
        assert FIELD_IDS == ("math", "cs", "ai-ml", "chem", "bio", "physics", "med")
        assert get_args(FieldId) == FIELD_IDS

    def test_nine_feeds(self) -> None:
        assert FEED_IDS == ("all", "picks", "math", "cs", "ai-ml", "chem", "bio", "physics", "med")
        assert feed_path("ai-ml") == "feeds/ai-ml.ics"

    def test_out_of_scope_is_an_exclusion_reason(self) -> None:
        assert "out_of_scope" in get_args(ExclusionReason)

    def test_switched_off_defenses_have_their_own_exclusion_reason(self) -> None:
        assert "defense" in get_args(ExclusionReason)
        assert "defense" not in get_args(RuleExclusionReason)
        assert set(get_args(RuleExclusionReason)) < set(get_args(ExclusionReason))
        event = make_event(event_type="defense")
        assert ExcludedEvent.from_event(event, "defense").reason == "defense"

    def test_canonical_fields(self) -> None:
        assert canonical_fields(["med", "math", "med"]) == ["math", "med"]
        with pytest.raises(ValueError, match="unknown field id"):
            canonical_fields(["history"])

    def test_source_key(self) -> None:
        key = source_key("localist-stanford", "44475127680618")
        assert key == "localist-stanford:44475127680618"
        assert split_source_key(key) == ("localist-stanford", "44475127680618")
        assert split_source_key("rsorg-x:uid:with:colons") == ("rsorg-x", "uid:with:colons")
        with pytest.raises(ValueError, match="not a source key"):
            split_source_key("no-separator")

    def test_source_key_is_plain_ascii_whatever_the_native_id_holds(self) -> None:
        native = "caf" + E_ACUTE + NBSP + "talk" + ROCKET
        key = source_key("rsorg-x", native)
        assert key == "rsorg-x:caf\\xe9\\xa0talk\\U0001f680"
        assert key.isascii()
        assert source_key("rsorg-x", native) == key
        assert source_key("rsorg-x", "caf" + E_ACUTE) != source_key("rsorg-x", "cafe")
        assert split_source_key(key) == ("rsorg-x", "caf\\xe9\\xa0talk\\U0001f680")
        uid_map = UidMap().with_aliases({key: "evt-1"})
        assert uid_map.resolve(source_key("rsorg-x", native)) == "evt-1"
        assert data_charset_problems(dump_json(uid_map)) == []

    def test_ascii_url(self) -> None:
        plain = "https://example.edu/events?id=1&tag=a%20b#top"
        assert ascii_url(plain) == plain
        encoded = ascii_url("https://example.edu/caf" + E_ACUTE + "/" + ROCKET + " talk" + NBSP)
        assert encoded == "https://example.edu/caf%C3%A9/%F0%9F%9A%80%20talk%C2%A0"
        assert ascii_url(encoded) == encoded

    def test_ascii_url_encodes_what_a_url_may_not_contain_as_it_is(self) -> None:
        """RFC 3986: these nine printable characters have no place in a URL."""
        hostile = 'https://example.edu/a"b<c>d\\e^f`g{h|i}j'
        encoded = "https://example.edu/a%22b%3Cc%3Ed%5Ce%5Ef%60g%7Bh%7Ci%7Dj"
        assert ascii_url(hostile) == encoded
        assert ascii_url(encoded) == encoded
        allowed = "https://user@example.edu:8443/a-b._~c/!$&'()*+,;=:@?q[0]=%41%zz&r=100%#top"
        assert ascii_url(allowed) == allowed

    def test_an_excluded_event_never_carries_markup_in_its_url(self) -> None:
        excluded = ExcludedEvent(
            title="x", url='https://example.edu/"><script>alert(1)</script>', reason="not_a_talk"
        )
        assert excluded.url == "https://example.edu/%22%3E%3Cscript%3Ealert(1)%3C/script%3E"


class TestEventSchema:
    def test_fields_are_exactly_the_agreed_ones(self) -> None:
        assert list(Event.model_fields) == EXPECTED_EVENT_FIELDS
        assert "last_seen" not in Event.model_fields
        assert list(Speaker.model_fields) == ["name", "affiliation", "url"]

    def test_defaults(self) -> None:
        defaults = {
            name: field.default
            for name, field in Event.model_fields.items()
            if not field.is_required()
        }
        assert defaults == {
            "speakers": [],
            "all_day": False,
            "is_online": False,
            "picks_score": 0.0,
            "picks_reasons": [],
            "status": "confirmed",
            "missing_runs": 0,
            "sequence": 0,
        }

    def test_nullable_fields_are_required(self) -> None:
        for name in ("series", "end", "location", "stream_url", "abstract"):
            assert Event.model_fields[name].is_required()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("institution", "mit"),
            ("event_type", "party"),
            ("status", "postponed"),
            ("field_tag_method", "manual"),
            ("extraction", "ocr"),
            ("fields", ["history"]),
            ("uid", "has space"),
            ("uid", "a@b"),
            ("uid", ""),
            ("title", ""),
            ("url", "ftp://example.edu/x"),
            ("url", "javascript:alert(1)"),
            ("start", datetime(2026, 11, 17, 16, 0)),
            ("first_seen", "2026-11-10T12:00:00"),
            ("source_ids", []),
            ("missing_runs", -1),
            ("picks_score", float("nan")),
        ],
    )
    def test_rejects_invalid_values(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError):
            make_event(**{field: value})

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError, match="last_seen"):
            make_event(last_seen=REFERENCE_NOW)

    def test_is_frozen(self) -> None:
        event = make_event()
        with pytest.raises(ValidationError):
            event.title = "changed"

    def test_evolve_returns_a_validated_copy(self) -> None:
        event = make_event()
        changed = event.evolve(title="New title", sequence=event.sequence + 1)
        assert (changed.title, changed.sequence) == ("New title", 1)
        assert event.title == "Protein design with diffusion models"
        with pytest.raises(ValidationError):
            event.evolve(fields=["history"])
        with pytest.raises(ValidationError):
            event.evolve(no_such_field=1)


class TestEventCanonicalForm:
    def test_fields_are_deduplicated_in_canonical_order(self) -> None:
        assert make_event(fields=["med", "bio", "math", "bio"]).fields == ["math", "bio", "med"]

    def test_source_ids_and_urls_are_sorted_and_unique(self) -> None:
        event = make_event(
            source_ids=["localist-stanford:9", "sites-chemistry:abc", "localist-stanford:9"],
            source_urls=["https://b.example.edu/x", "https://a.example.edu/y"],
        )
        assert event.source_ids == ["localist-stanford:9", "sites-chemistry:abc"]
        assert [str(url) for url in event.source_urls] == [
            "https://a.example.edu/y",
            "https://b.example.edu/x",
        ]

    def test_speaker_and_reason_order_is_preserved(self) -> None:
        speakers = [Speaker(name="Zed"), Speaker(name="Amy")]
        event = make_event(speakers=speakers, picks_reasons=["title: enzyme design", "a"])
        assert [speaker.name for speaker in event.speakers] == ["Zed", "Amy"]
        assert event.picks_reasons == ["title: enzyme design", "a"]

    def test_timestamps_drop_microseconds(self) -> None:
        event = make_event(first_seen=datetime(2026, 11, 10, 20, 0, 0, 123456, tzinfo=UTC))
        assert event.first_seen == REFERENCE_NOW

    def test_end_before_start_is_rejected(self) -> None:
        start = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
        with pytest.raises(ValidationError, match="end must not be earlier"):
            make_event(start=start, end=start - timedelta(minutes=1))
        assert make_event(start=start, end=start).end == start

    def test_stream_url_with_credential_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="credential"):
            make_event(stream_url="https://stanford.zoom.us/j/123?pwd=abc")
        assert make_event(stream_url="https://stanford.zoom.us/j/123").stream_url is not None

    def test_is_tba(self) -> None:
        assert TBA_TITLE == "TBA"
        assert make_event(title="TBA").is_tba
        assert not make_event(title="TBA: details to follow").is_tba

    def test_is_pick(self) -> None:
        event = make_event(picks_score=3.0)
        assert event.is_pick(3.0)
        assert not event.is_pick(3.5)


class TestUrlHasCredential:
    @pytest.mark.parametrize(
        "url",
        [
            "https://stanford.zoom.us/j/123?pwd=abc",
            "https://stanford.zoom.us/j/123?PWD=abc",
            "https://zoom.us/j/1?passcode=1",
            "https://zoom.us/w/1?tk=xyz",
            "https://example.edu/live?a=1&token=xyz",
            "https://example.edu/live#pwd=abc",
            "https://user:secret@example.edu/live",
        ],
    )
    def test_detects_credentials(self, url: str) -> None:
        assert url_has_credential(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://stanford.zoom.us/j/123",
            "https://example.edu/live?page=2",
            "https://example.edu/tokens/intro",
        ],
    )
    def test_accepts_plain_urls(self, url: str) -> None:
        assert not url_has_credential(url)


class TestEventDraft:
    def test_minimal_draft(self) -> None:
        draft = EventDraft(source_id="custom-nlp", extraction="html", start="Nov 12, 2026 11am")
        assert draft.title is None
        assert draft.end is None
        assert draft.speakers == []
        assert draft.field_hints == []

    def test_datetimes_and_dates_keep_their_type(self) -> None:
        naive = datetime(2026, 11, 17, 16, 0)
        aware = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
        assert make_draft(start=naive).start == naive
        assert make_draft(start=aware).start == aware
        day = make_draft(start=date(2026, 11, 17)).start
        assert day == date(2026, 11, 17)
        assert not isinstance(day, datetime)

    def test_iso_strings_get_their_typed_form(self) -> None:
        assert make_draft(start="2026-11-17T16:00:00-08:00").start == datetime(
            2026, 11, 17, 16, 0, tzinfo=PACIFIC
        )
        assert make_draft(start="2026-11-17T16:00").start == datetime(2026, 11, 17, 16, 0)
        assert make_draft(start="2026-11-17 16:00:00Z").start == datetime(
            2026, 11, 17, 16, 0, tzinfo=UTC
        )
        assert make_draft(start=" 2026-11-17 ").start == date(2026, 11, 17)
        assert make_draft(end="2026-11-19").end == date(2026, 11, 19)

    def test_a_date_only_string_means_all_day_and_never_midnight(self) -> None:
        draft = make_draft(start="2026-11-17", end=None)
        assert draft.start == date(2026, 11, 17)
        assert not isinstance(draft.start, datetime)
        assert draft.is_all_day
        assert draft == make_draft(start=date(2026, 11, 17), end=None)

    def test_other_strings_are_left_for_normalization(self) -> None:
        for text in ("Nov 12, 2026 11am", "Tuesday 4:15 PM", "1700000000", "17/11/2026"):
            assert make_draft(start=text).start == text
            assert (
                EventDraft.model_validate_json(make_draft(start=text).model_dump_json()).start
                == text
            )

    @pytest.mark.parametrize("value", [None, 1700000000, 12.5, "", "   ", ["2026-11-17"]])
    def test_start_must_be_a_time(self, value: object) -> None:
        with pytest.raises(ValidationError):
            make_draft(start=value)
        with pytest.raises(ValidationError):
            make_draft(end="")
        assert make_draft(end=None).end is None

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("2026-11-17", None),
            ("2026-11-17", "2026-11-19"),
            (date(2026, 11, 17), date(2026, 11, 19)),
            ("2026-11-17T16:00:00-08:00", "2026-11-17T17:00:00-08:00"),
            (datetime(2026, 11, 17, 16, 0), None),
            ("Nov 12, 2026 11am", "noon"),
        ],
    )
    def test_a_draft_is_the_same_however_it_is_loaded(self, start: object, end: object) -> None:
        """The LLM cache stores drafts as JSON; they must come back with the same types."""
        draft = make_draft(start=start, end=end, source_text="page text", extraction="llm")
        from_json = EventDraft.model_validate_json(draft.model_dump_json())
        from_dict = EventDraft.model_validate(json.loads(draft.model_dump_json()))
        from_python = EventDraft.model_validate(draft.model_dump())
        assert from_json == from_dict == from_python == draft
        assert type(from_json.start) is type(from_dict.start) is type(draft.start)
        assert type(from_json.end) is type(from_dict.end) is type(draft.end)

    def test_all_day_drafts(self) -> None:
        timed = make_draft()
        assert not timed.is_all_day
        assert make_draft(all_day=True).is_all_day
        assert make_draft(start=date(2026, 11, 17), end=date(2026, 11, 19)).is_all_day
        assert not make_draft(start="next Tuesday").is_all_day
        workshop = make_draft(start="2026-11-17", end="2026-11-19")
        assert (workshop.start, workshop.end) == (date(2026, 11, 17), date(2026, 11, 19))

    def test_text_fields_are_raw(self) -> None:
        draft = make_draft(
            title="<h2><a href='/x'>Talk &amp; discussion</a></h2>",
            stream_url="https://stanford.zoom.us/j/1?pwd=secret",
        )
        assert draft.title == "<h2><a href='/x'>Talk &amp; discussion</a></h2>"
        assert draft.stream_url == "https://stanford.zoom.us/j/1?pwd=secret"

    @pytest.mark.parametrize("link", [*RELATIVE_LINKS, *NOT_WEB_PAGES, "https://example.edu/a b"])
    def test_urls_must_be_absolute_http_urls(self, link: str) -> None:
        with pytest.raises(ValidationError, match=r"absolute http\(s\) URL|whitespace"):
            make_draft(url=link)
        with pytest.raises(ValidationError):
            make_draft(stream_url=link)
        with pytest.raises(ValidationError):
            SpeakerDraft(name="Jane Doe", url=link)

    def test_urls_are_kept_as_written_and_may_be_missing(self) -> None:
        url = "HTTPS://Example.edu/Events?id=1#top"
        draft = make_draft(url=url, stream_url=None, speakers=[{"name": "Jane Doe", "url": url}])
        assert draft.url == url
        assert draft.speakers[0].url == url
        assert make_draft(url=None).url is None
        assert SpeakerDraft(name="Jane Doe").url is None

    def test_llm_extraction_requires_source_text(self) -> None:
        with pytest.raises(ValidationError, match="source_text"):
            make_draft(extraction="llm")
        with pytest.raises(ValidationError, match="source_text"):
            make_draft(extraction="llm", source_text="   ")
        assert make_draft(extraction="llm", source_text="Talk by Jane Doe").extraction == "llm"

    def test_rejects_unknown_keys_and_bad_hints(self) -> None:
        with pytest.raises(ValidationError):
            make_draft(speaker="Jane Doe")
        with pytest.raises(ValidationError):
            make_draft(field_hints=["history"])
        with pytest.raises(ValidationError):
            make_draft(timezone="Mars/Olympus")

    def test_a_zone_name_too_long_for_a_file_name_is_unknown(self) -> None:
        with pytest.raises(ValidationError, match="unknown IANA time zone"):
            make_draft(timezone="America/" + "x" * 300)


class TestTimeWindow:
    def test_default_window_spans_whole_local_days(self) -> None:
        window = make_window()
        assert window.start.isoformat() == "2026-10-27T00:00:00-07:00"
        assert window.end.isoformat() == "2027-03-11T00:00:00-08:00"

    def test_window_uses_the_local_date_of_now(self) -> None:
        late_evening_utc = datetime(2026, 11, 11, 7, 30, tzinfo=UTC)  # still Nov 10 in PT
        window = TimeWindow.around(
            late_evening_utc, days_back=0, days_ahead=0, timezone="America/Los_Angeles"
        )
        assert window.start.isoformat() == "2026-11-10T00:00:00-08:00"
        assert window.end.isoformat() == "2026-11-11T00:00:00-08:00"

    def test_contains_is_half_open(self) -> None:
        window = make_window()
        assert window.contains(window.start)
        assert not window.contains(window.end)
        assert window.contains(REFERENCE_NOW)

    def test_rejects_empty_window(self) -> None:
        with pytest.raises(ValidationError):
            TimeWindow(start=REFERENCE_NOW, end=REFERENCE_NOW)


class TestPayload:
    def test_document_text_is_always_in_canonical_form(self) -> None:
        document = make_document(text=BOM + "BEGIN\r\nSUMMARY:x\rEND")
        assert document.text == "BEGIN\nSUMMARY:x\nEND\n"
        assert document.evolve(label="index").text == document.text
        assert make_document(text="").text == "\n"
        assert make_document(text="already fine\n").text == "already fine\n"

    def test_document_with_a_byte_order_mark_parses_as_json(self) -> None:
        assert make_document(text=BOM + '{"a": 1}').parse_json() == {"a": 1}

    def test_document_json_and_labels(self) -> None:
        index = make_document(text='{"a": 1}', label="index")
        detail = make_document(url="https://x.example.edu/1", text="<p>hi</p>", label="detail")
        payload = make_payload(index, detail)
        assert index.parse_json() == {"a": 1}
        assert payload.labelled("detail") == [detail]
        with pytest.raises(ValueError, match="Expecting value"):
            detail.parse_json()


class TestClassificationModels:
    def test_hints_merge_is_a_union(self) -> None:
        left = EventHints(
            source_ids=["sites-chemistry"], default_fields=["chem"], categories=["Chemistry"]
        )
        right = EventHints(
            source_ids=["localist-stanford"], field_hints=["bio", "chem"], categories=["Seminar"]
        )
        merged = left.merge(right)
        assert merged == right.merge(left)
        assert merged.merge(merged) == merged
        assert merged.source_ids == ["localist-stanford", "sites-chemistry"]
        assert merged.suggested_fields == ["chem", "bio"]
        assert merged.categories == ["Chemistry", "Seminar"]

    def test_hints_keep_tags_apart_from_categories(self) -> None:
        left = EventHints(categories=["Seminar"], tags=["robotics"])
        right = EventHints(categories=["Physics"], tags=["Seminar", "optics"])
        merged = left.merge(right)
        assert merged == right.merge(left)
        assert merged.categories == ["Physics", "Seminar"]
        assert merged.tags == ["Seminar", "optics", "robotics"]
        assert merged.labels == ["Physics", "Seminar", "optics", "robotics"]

    def test_candidate_defaults_to_empty_hints(self) -> None:
        candidate = make_candidate()
        assert candidate.event.fields == []
        assert candidate.hints.default_fields == ["bio"]

    def test_excluded_event_from_event(self) -> None:
        event = make_event()
        excluded = ExcludedEvent.from_event(event, "out_of_scope", detail="no field matched")
        assert excluded.reason == "out_of_scope"
        assert excluded.title == event.title
        assert excluded.url == str(event.url)
        with pytest.raises(ValidationError):
            ExcludedEvent(title="x", reason="boring")  # type: ignore[arg-type]

    def test_excluded_event_cleans_raw_text_of_an_invalid_draft(self) -> None:
        """A draft that failed normalization is reported with the text of its source."""
        excluded = ExcludedEvent(
            source_ids=[source_key("custom-x", "id" + NBSP + "1")],
            title="Of\ufb01ce" + NBSP + "hours " + ROCKET + ZERO_WIDTH_SPACE + "Poincar" + E_ACUTE,
            url="https://example.edu/talks/" + ROCKET + "?q=a" + NBSP + "b",
            reason="invalid",
            detail="no usable start in " + chr(0x201C) + "next" + NBSP + "week" + chr(0x201D),
        )
        assert excluded.title == "Office hours Poincar" + E_ACUTE
        assert excluded.url == "https://example.edu/talks/%F0%9F%9A%80?q=a%C2%A0b"
        assert excluded.detail == "no usable start in " + chr(0x201C) + "next week" + chr(0x201D)
        assert excluded.source_ids == ["custom-x:id\\xa01"]
        assert ExcludedEvent.model_validate(excluded.model_dump()) == excluded
        report = RunReport(
            started_at=REFERENCE_NOW,
            finished_at=REFERENCE_NOW,
            mode="offline",
            ok=True,
            excluded=[excluded, ExcludedEvent(title="", reason="invalid")],
        )
        assert data_charset_problems(dump_json(report)) == []


class TestRunReport:
    def test_to_last_run_condenses_the_report(self) -> None:
        report = RunReport(
            started_at=REFERENCE_NOW,
            finished_at=REFERENCE_NOW + timedelta(minutes=3),
            mode="live",
            ok=True,
            sources=[
                SourceRunResult(source_id="sites-physics", status="failed", error_kind="http"),
                SourceRunResult(source_id="sites-chemistry", status="ok", events=12),
                SourceRunResult(source_id="custom-nlp", status="failed", error_kind="parse"),
            ],
            counts=RunCounts(sources=3, sources_ok=1, sources_failed=2, events=12),
            llm=LLMUsage(
                enabled=True,
                provider="claude-cli",
                calls=2,
                providers=[LLMProviderResult(provider="claude-cli", status="ok", calls=2)],
            ),
        )
        last_run = report.to_last_run()
        assert last_run.failed_sources == ["custom-nlp", "sites-physics"]
        assert last_run.llm_provider == "claude-cli"
        assert last_run.counts.events == 12
        assert last_run.finished_at == report.finished_at

    def test_counts_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            RunCounts(events=-1)

    def test_charset_findings_have_their_own_field(self) -> None:
        report = RunReport(
            started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, mode="live", ok=False
        )
        assert report.charset_findings == []
        assert report.secret_findings == []
        flagged = report.evolve(charset_findings=["data/events.json:12:40 U+00A0"])
        assert '"charset_findings": [\n    "data/events.json:12:40 U+00A0"\n  ]' in dump_json(
            flagged
        )


def _source_entry(minute: int, status: str = "ok", events: int = 3) -> SourceHealthEntry:
    return SourceHealthEntry.model_validate(
        {"at": REFERENCE_NOW + timedelta(minutes=minute), "status": status, "events": events}
    )


class TestHealth:
    def test_history_is_capped_at_thirty_runs(self) -> None:
        health = HealthFile()
        for minute in range(HEALTH_HISTORY_LIMIT + 5):
            health = health.with_source_entry("sites-chemistry", _source_entry(minute))
        history = health.sources["sites-chemistry"].history
        assert HEALTH_HISTORY_LIMIT == 30
        assert len(history) == 30
        assert history[0].at == REFERENCE_NOW + timedelta(minutes=5)
        assert history[-1].at == REFERENCE_NOW + timedelta(minutes=34)

    def test_loading_an_overlong_history_fails(self) -> None:
        entries = [_source_entry(minute).model_dump(mode="json") for minute in range(31)]
        with pytest.raises(ValidationError):
            HealthFile.model_validate({"sources": {"sites-chemistry": {"history": entries}}})

    def test_trailing_counts_consecutive_statuses(self) -> None:
        health = HealthFile()
        for minute, status in enumerate(["ok", "failed", "empty", "failed", "failed"]):
            entry = _source_entry(minute, status, 0 if status != "ok" else 4)
            health = health.with_source_entry("custom-nlp", entry)
        source = health.sources["custom-nlp"]
        assert source.trailing("failed") == 2
        assert source.trailing("failed", "empty") == 4
        assert source.trailing("ok") == 0
        assert source.last_success_at == REFERENCE_NOW
        assert source.last is not None
        assert source.last.status == "failed"

    def test_llm_history(self) -> None:
        health = HealthFile()
        for minute, status in enumerate(["ok", "failed", "failed"]):
            entry = LLMHealthEntry.model_validate(
                {"at": REFERENCE_NOW + timedelta(minutes=minute), "status": status}
            )
            health = health.with_llm_entry("claude-cli", entry)
        assert health.llm["claude-cli"].trailing("failed") == 2
        assert health.llm["claude-cli"].last_success_at == REFERENCE_NOW

    def test_keys_are_sorted_for_stable_dumps(self) -> None:
        health = HealthFile().with_source_entry("sites-b", _source_entry(0))
        health = health.with_source_entry("sites-a", _source_entry(0))
        assert list(health.sources) == ["sites-a", "sites-b"]


class TestUidMap:
    def test_resolve_and_rebind(self) -> None:
        uid_map = UidMap().with_aliases({"sites-chemistry:b": "evt-2", "localist-x:1": "evt-1"})
        assert list(uid_map.aliases) == ["localist-x:1", "sites-chemistry:b"]
        assert uid_map.resolve("localist-x:1") == "evt-1"
        assert uid_map.resolve("unknown:1") is None
        merged = uid_map.with_aliases({"sites-chemistry:b": "evt-1"})
        assert merged.resolve("sites-chemistry:b") == "evt-1"
        assert uid_map.resolve("sites-chemistry:b") == "evt-2"

    def test_values_must_be_valid_uids(self) -> None:
        with pytest.raises(ValidationError):
            UidMap(aliases={"a:1": "not a uid"})


INTERNAL_EVENT_FIELDS = {
    "missing_runs",
    "sequence",
    "source_ids",
    "source_urls",
    "field_tag_method",
    "extraction",
    "first_seen",
    "last_changed",
    "picks_score",
}


class TestPublicPayloads:
    def test_public_event_never_exposes_internal_state(self) -> None:
        assert not INTERNAL_EVENT_FIELDS & set(PublicEvent.model_fields)
        assert set(PublicEvent.model_fields) - set(Event.model_fields) == {"is_pick"}

    def test_from_event(self) -> None:
        event = make_event(picks_score=4.0, picks_reasons=["title: protein design"], missing_runs=2)
        public = PublicEvent.from_event(event, picks_threshold=3.0)
        assert public.is_pick
        assert [reason.text for reason in public.picks_reasons] == ["title: protein design"]
        assert public.uid == event.uid
        assert "missing_runs" not in public.model_dump()
        assert not PublicEvent.from_event(event, picks_threshold=5.0).is_pick

    def test_events_payload_defaults(self) -> None:
        payload = PublicEventsPayload(generated_at=REFERENCE_NOW, timezone="America/Los_Angeles")
        assert payload.schema_version == 1
        assert payload.fields == list(FIELD_IDS)
        assert [feed.id for feed in payload.feeds] == list(FEED_IDS)
        assert payload.feeds[0].path == "feeds/all.ics"

    def test_status_payload_has_a_schema_version(self) -> None:
        payload = PublicStatusPayload(generated_at=REFERENCE_NOW)
        assert payload.model_dump(mode="json")["schema_version"] == 1
        with pytest.raises(ValidationError):
            PublicStatusPayload(generated_at=REFERENCE_NOW, schema_version=2)  # type: ignore[arg-type]
