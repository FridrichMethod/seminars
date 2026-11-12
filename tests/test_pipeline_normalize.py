"""Tests for normalization (drafts to candidates) and for source keys and uids."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from conftest import REPO_ROOT
from factories import PACIFIC, REFERENCE_NOW, make_draft, make_source, make_window
from seminars.config import Settings, load_settings
from seminars.models import (
    UID_PATTERN,
    Candidate,
    EventDraft,
    SourceConfig,
    SpeakerDraft,
    UidMap,
    dump_events,
)
from seminars.pipeline.ids import (
    canonical_url,
    draft_key,
    resolve_uid,
    safe_native_id,
    shared_urls,
    slug,
    uid_for_key,
)
from seminars.pipeline.normalize import (
    NormalizeContext,
    Normalized,
    merge_speakers,
    normalize_drafts,
)
from seminars.pipeline.secret_scan import REDACTED, build_secret_scanner
from seminars.textrules import data_charset_problems
from test_pipeline_secret_scan import FORGED

SETTINGS: Settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
SCANNER = build_secret_scanner()


def context(**overrides: Any) -> NormalizeContext:
    values: dict[str, Any] = {
        "settings": SETTINGS,
        "now": REFERENCE_NOW,
        "window": make_window(),
        "redact": SCANNER.redact,
        "is_secret": SCANNER.has_secret,
    }
    return NormalizeContext(**{**values, **overrides})


def normalize(
    *drafts: EventDraft, source: SourceConfig | None = None, **overrides: Any
) -> Normalized:
    return normalize_drafts(list(drafts), source or make_source(), context(**overrides))


def one(draft: EventDraft, source: SourceConfig | None = None, **overrides: Any) -> Candidate:
    result = normalize(draft, source=source, **overrides)
    assert result.invalid == ()
    (candidate,) = result.candidates
    return candidate


def no_secret(text: str) -> bool:
    return False


class TestBasics:
    def test_a_draft_becomes_an_event(self) -> None:
        candidate = one(make_draft())
        event = candidate.event
        assert event.uid == "sites-bioengineering.1234"
        assert event.source_ids == ["sites-bioengineering:1234"]
        assert event.title == "Protein design with diffusion models"
        assert [(s.name, s.affiliation) for s in event.speakers] == [
            ("Jane Doe", "Stanford University")
        ]
        assert event.host_org == "Stanford Bioengineering"
        assert event.institution == "stanford"
        assert event.event_type == "seminar"
        assert event.start.isoformat() == "2026-11-17T16:00:00-08:00"
        assert event.end is not None
        assert event.end.isoformat() == "2026-11-17T17:00:00-08:00"
        assert event.location == "Shriram Center, Room 104"
        assert str(event.url) == "https://bioengineering.stanford.edu/events/protein-design-seminar"
        assert [str(url) for url in event.source_urls] == [str(event.url)]
        assert (event.fields, event.field_tag_method) == ([], "source")
        assert (event.status, event.extraction) == ("confirmed", "feed")
        assert event.first_seen == event.last_changed == REFERENCE_NOW
        assert (event.missing_runs, event.sequence, event.picks_score) == (0, 0, 0.0)

    def test_hints_come_from_the_source_and_the_draft(self) -> None:
        draft = make_draft(
            field_hints=["chem"], categories=[" Chemistry <b>Dept</b> ", "", "Talks"]
        )
        hints = one(draft, make_source(default_fields=["bio", "med"])).hints
        assert hints.source_ids == ["sites-bioengineering"]
        assert hints.default_fields == ["bio", "med"]
        assert hints.field_hints == ["chem"]
        assert hints.categories == ["Chemistry Dept", "Talks"]
        assert hints.tags == []

    def test_tags_are_cleaned_like_categories_and_kept_apart(self) -> None:
        draft = make_draft(categories=["Talks"], tags=[" protein <b>design</b> ", "", "Talks"])
        hints = one(draft, make_source()).hints
        assert hints.categories == ["Talks"]
        assert hints.tags == ["Talks", "protein design"]

    def test_source_defaults_fill_what_the_draft_leaves_out(self) -> None:
        source = make_source(series="Bioengineering Seminar", default_event_type="colloquium")
        event = one(make_draft(), source).event
        assert (event.series, event.event_type) == ("Bioengineering Seminar", "colloquium")
        draft = make_draft(series="Special Lecture", host_org="Chem-H", event_type_hint="lecture")
        event = one(draft, source).event
        assert (event.series, event.host_org, event.event_type) == (
            "Special Lecture",
            "Chem-H",
            "lecture",
        )

    def test_a_draft_without_an_address_points_at_its_source(self) -> None:
        source = make_source(homepage="https://bioengineering.stanford.edu/events")
        assert str(one(make_draft(url=None), source).event.url) == source.homepage
        assert str(one(make_draft(url=None)).event.url) == make_source().url

    def test_candidates_keep_the_order_of_their_drafts(self) -> None:
        drafts = [make_draft(native_id=str(number)) for number in (3, 1, 2)]
        result = normalize(*drafts)
        assert [c.event.uid.rsplit(".", 1)[1] for c in result.candidates] == ["3", "1", "2"]

    def test_times_are_stored_as_local_wall_time(self) -> None:
        event = one(make_draft(start=datetime(2026, 11, 18, 0, 0, tzinfo=UTC), end=None)).event
        assert event.start.isoformat() == "2026-11-17T16:00:00-08:00"
        assert event.end is None
        assert '"start": "2026-11-17T16:00:00-08:00"' in dump_events([event])

    def test_the_time_zone_of_the_draft_wins_over_the_source(self) -> None:
        draft = make_draft(start=datetime(2026, 11, 18, 2, 0), end=None, timezone="UTC")
        assert one(draft).event.start.isoformat() == "2026-11-17T18:00:00-08:00"
        source = make_source(timezone="America/New_York")
        event = one(make_draft(start=datetime(2026, 11, 17, 19, 0), end=None), source).event
        assert event.start.isoformat() == "2026-11-17T16:00:00-08:00"

    def test_an_end_at_the_same_time_on_the_next_day_is_not_kept(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Instance 53759577652952 of the campus calendar as the API serves it: the Wu Tsai
        # Neurosciences Seminar at noon, "12pm to 12pm PT" on its page.
        source = make_source(
            id="localist-stanford",
            group="localist",
            adapter="localist",
            url="https://events.stanford.edu/api/2/events",
        )
        drafts = [
            make_draft(
                source_id="localist-stanford",
                native_id=native_id,
                title="Jonathan Kipnis - Navigating turbulent CNS waters",
                start="2026-12-03T12:00:00-08:00",
                end="2026-12-04T12:00:00-08:00",
                all_day=False,
            )
            for native_id in ("53759577652952", "53759577710301")
        ]
        with caplog.at_level("WARNING", logger="seminars.pipeline.normalize"):
            result = normalize(*drafts, source=source)
        events = [candidate.event for candidate in result.candidates]
        assert [(event.start.isoformat(), event.end, event.all_day) for event in events] == [
            ("2026-12-03T12:00:00-08:00", None, False)
        ] * 2
        (record,) = caplog.records
        assert record.getMessage() == (
            "localist-stanford: the end of 2 timed event(s) is not kept (the same time on a"
            " later day, or more than 7 days after the start): "
            "localist-stanford.53759577652952, localist-stanford.53759577710301"
        )

    def test_at_most_a_few_uids_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        drafts = [
            make_draft(
                native_id=str(index),
                start=datetime(2026, 11, 17 + index, 16, 0, tzinfo=PACIFIC),
                end=datetime(2026, 11, 18 + index, 16, 0, tzinfo=PACIFIC),
            )
            for index in range(7)
        ]
        with caplog.at_level("WARNING", logger="seminars.pipeline.normalize"):
            result = normalize(*drafts)
        assert all(candidate.event.end is None for candidate in result.candidates)
        (record,) = caplog.records
        assert record.getMessage().endswith(", ...")
        assert record.getMessage().count("sites-bioengineering.") == 5

    def test_an_all_day_draft(self) -> None:
        draft = make_draft(start=date(2026, 11, 17), end=date(2026, 11, 19))
        event = one(draft).event
        assert event.all_day is True
        assert event.start.isoformat() == "2026-11-17T00:00:00-08:00"
        assert event.end is not None
        assert event.end.isoformat() == "2026-11-20T00:00:00-08:00"


class TestWindow:
    def test_events_outside_the_window_are_counted_and_dropped(self) -> None:
        window = make_window()
        inside = make_draft(native_id="in")
        early = make_draft(native_id="early", start=window.start - timedelta(minutes=1), end=None)
        late = make_draft(native_id="late", start=window.end, end=None)
        first = make_draft(native_id="first", start=window.start, end=None)
        result = normalize(inside, early, late, first)
        assert [c.event.uid for c in result.candidates] == [
            "sites-bioengineering.in",
            "sites-bioengineering.first",
        ]
        assert (result.outside_window, result.invalid) == (2, ())


class TestInvalidDrafts:
    def test_an_unusable_start_is_reported_and_the_rest_goes_on(self) -> None:
        bad = make_draft(native_id="bad", title="Broken <b>talk</b>", start="no idea", end=None)
        result = normalize(bad, make_draft())
        assert len(result.candidates) == 1
        (excluded,) = result.invalid
        assert (excluded.reason, excluded.title) == ("invalid", "Broken talk")
        assert excluded.source_ids == ["sites-bioengineering"]
        assert excluded.detail is not None
        assert excluded.detail.startswith("start: ")
        assert excluded.url == "https://bioengineering.stanford.edu/events/protein-design-seminar"

    def test_a_draft_of_another_source_is_invalid(self) -> None:
        (excluded,) = normalize(make_draft(source_id="sites-other", title=None)).invalid
        assert excluded.title == "(no title)"
        assert excluded.detail == "source_id: the draft belongs to another source"

    def test_a_value_the_model_refuses_is_invalid(self) -> None:
        draft = make_draft(native_id=None, url=None, speaker_text=None, title="x")
        key = "sites-bioengineering:day:2026-11-17:x"
        broken = UidMap.model_construct(schema_version=1, aliases={key: "not a uid!"})
        result = normalize_drafts([draft], make_source(), context(uid_map=broken))
        (excluded,) = result.invalid
        assert excluded.detail == "not a valid event: uid"

    def test_the_title_of_an_invalid_draft_is_cleaned_and_redacted(self) -> None:
        token = FORGED["github-token"]
        draft = make_draft(title=f"Leak {token} \U0001f680", start="???", end=None)
        (excluded,) = normalize(draft).invalid
        assert token not in excluded.model_dump_json()
        assert excluded.title == f"Leak {REDACTED}"


class TestText:
    def test_markup_whitespace_and_unicode(self) -> None:
        draft = make_draft(
            title="  Poincar\u00e9\u00a0duality <em>and</em>\n e\ufb03ciency \U0001f389 ",
            series="Topology&nbsp;Seminar",
            location="Room\t380-W,\nSloan Hall",
            abstract=None,
            abstract_html="<p>First &amp; foremost.</p><p>Second<br>third\u200b.</p>",
        )
        event = one(draft).event
        assert event.title == "Poincar\u00e9 duality and efficiency"
        assert event.series == "Topology Seminar"
        assert event.location == "Room 380-W, Sloan Hall"
        assert event.abstract == "First & foremost.\nSecond\nthird."
        assert data_charset_problems(dump_events([event])) == []

    def test_the_plain_abstract_wins_over_the_html_one(self) -> None:
        draft = make_draft(abstract=" Plain  text ", abstract_html="<p>HTML text</p>")
        assert one(draft).event.abstract == "Plain text"

    @pytest.mark.parametrize("placeholder", ["Abstract", "<p><strong>Abstract</strong></p>", "TBA"])
    def test_an_abstract_that_says_nothing_is_none(self, placeholder: str) -> None:
        # The Stanford Sites body of a talk without an abstract yet is often just its heading.
        draft = make_draft(abstract=None, abstract_html=placeholder)
        assert one(draft).event.abstract is None

    def test_an_empty_plain_abstract_gives_way_to_the_html_one(self) -> None:
        draft = make_draft(abstract="Abstract", abstract_html="<p>We study knots.</p>")
        assert one(draft).event.abstract == "We study knots."

    def test_the_abstract_is_cut_to_the_configured_length(self) -> None:
        event = one(make_draft(abstract="word " * 2000)).event
        assert event.abstract is not None
        assert len(event.abstract) <= SETTINGS.events.abstract_max_chars == 4000
        assert event.abstract.endswith("...")

    def test_long_fields_are_bounded(self) -> None:
        draft = make_draft(title="t" * 5000, location="l" * 5000, series="s" * 5000)
        event = one(draft).event
        assert (len(event.title), len(event.location or ""), len(event.series or "")) == (
            300,
            300,
            200,
        )

    @pytest.mark.parametrize("title", [None, "", "  ", "TBA", "Title: TBD", "to be announced"])
    def test_a_missing_title_is_tba(self, title: str | None) -> None:
        event = one(make_draft(title=title)).event
        assert event.title == "TBA"
        assert event.is_tba

    @pytest.mark.parametrize(
        "title",
        [
            "\u86cb\u767d\u8d28\u8bbe\u8ba1\u7684\u65b0\u65b9\u6cd5",
            "\u0422\u0435\u043e\u0440\u0438\u044f \u0447\u0438\u0441\u0435\u043b",
            "\u03a4\u03bf\u03c0\u03bf\u03bb\u03bf\u03b3\u03af\u03b1",
            "\u03c0",
        ],
    )
    def test_a_title_in_another_script_is_kept(self, title: str) -> None:
        event = one(make_draft(title=title)).event
        assert (event.title, event.is_tba) == (title, False)

    @pytest.mark.parametrize(
        "abstract",
        [
            "\u8fd9\u662f\u4e00\u4e2a\u5173\u4e8e\u86cb\u767d\u8d28\u8bbe\u8ba1\u7684\u62a5\u544a",
            "Abstract: \u86cb\u767d\u8d28\u8bbe\u8ba1",
        ],
    )
    def test_an_abstract_in_another_script_is_kept(self, abstract: str) -> None:
        assert one(make_draft(abstract=abstract)).event.abstract == abstract

    def test_a_cancelled_marker_sets_the_status(self) -> None:
        event = one(make_draft(title="CANCELLED: Protein design")).event
        assert (event.title, event.status) == ("Protein design", "cancelled")
        event = one(make_draft(title="Protein design (postponed)")).event
        assert (event.title, event.status) == ("Protein design", "cancelled")

    def test_status_hints(self) -> None:
        assert one(make_draft(status_hint="tentative")).event.status == "tentative"
        assert one(make_draft(status_hint="cancelled")).event.status == "cancelled"
        hinted = make_draft(title="Cancelled - Knots", status_hint="confirmed")
        assert one(hinted).event.status == "cancelled"


class TestSpeakers:
    def test_a_speaker_line_is_split(self) -> None:
        draft = make_draft(speaker_text="Speakers: Jane Doe (MIT) and Prof. John Roe, Stanford")
        assert [(s.name, s.affiliation) for s in one(draft).event.speakers] == [
            ("Jane Doe", "MIT"),
            ("John Roe", "Stanford"),
        ]

    def test_structured_speakers_come_first_and_are_not_repeated(self) -> None:
        draft = make_draft(
            speakers=[
                SpeakerDraft(name="Dr. Jane  Doe, PhD", url="https://example.edu/~doe"),
                SpeakerDraft(name="Jos\u00e9 Garc\u00eda", affiliation=" <i>UCSF</i> "),
            ],
            speaker_text="jane doe (Stanford University); Jose Garcia; TBA",
        )
        speakers = one(draft).event.speakers
        assert [(s.name, s.affiliation) for s in speakers] == [
            ("Jane Doe", "Stanford University"),
            ("Jos\u00e9 Garc\u00eda", "UCSF"),
        ]
        assert str(speakers[0].url) == "https://example.edu/~doe"

    def test_no_speaker(self) -> None:
        assert one(make_draft(speaker_text=None)).event.speakers == []
        assert one(make_draft(speaker_text="TBA")).event.speakers == []

    @pytest.mark.parametrize(
        ("line", "people"),
        [
            (
                "\u5f20\u4e09 (\u6e05\u534e\u5927\u5b66)",
                [("\u5f20\u4e09", "\u6e05\u534e\u5927\u5b66")],
            ),
            (
                "\u5f20\u4e09\uff08\u6e05\u534e\u5927\u5b66\uff09",
                [("\u5f20\u4e09", "\u6e05\u534e\u5927\u5b66")],
            ),
            (
                "\u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432"
                " (\u041c\u0413\u0423)",
                [
                    (
                        "\u0418\u0432\u0430\u043d \u0418\u0432\u0430\u043d\u043e\u0432",
                        "\u041c\u0413\u0423",
                    )
                ],
            ),
            (
                "\u03a3\u03c9\u03ba\u03c1\u03ac\u03c4\u03b7\u03c2 (\u0395\u039a\u03a0\u0391)",
                [("\u03a3\u03c9\u03ba\u03c1\u03ac\u03c4\u03b7\u03c2", "\u0395\u039a\u03a0\u0391")],
            ),
            ("\u674e\u96f7 and Jane Doe (MIT)", [("\u674e\u96f7", None), ("Jane Doe", "MIT")]),
            (
                "\u674e\u56db, \u738b\u4e94 (\u5317\u4eac\u5927\u5b66)",
                [("\u674e\u56db", None), ("\u738b\u4e94", "\u5317\u4eac\u5927\u5b66")],
            ),
            (
                "\u674e\u56db\u3001\u738b\u4e94\uff08\u5317\u4eac\u5927\u5b66\uff09",
                [("\u674e\u56db", None), ("\u738b\u4e94", "\u5317\u4eac\u5927\u5b66")],
            ),
            (
                "\u5f20\u4e09\uff0c\u6e05\u534e\u5927\u5b66",
                [("\u5f20\u4e09", "\u6e05\u534e\u5927\u5b66")],
            ),
        ],
    )
    def test_names_in_any_script_are_kept(
        self, line: str, people: list[tuple[str, str | None]]
    ) -> None:
        speakers = one(make_draft(speaker_text=line)).event.speakers
        assert [(s.name, s.affiliation) for s in speakers] == people

    def test_structured_names_in_any_script_are_kept(self) -> None:
        draft = make_draft(
            speakers=[
                SpeakerDraft(name="\u738b\u5c0f\u660e", affiliation="\u5317\u4eac\u5927\u5b66"),
                SpeakerDraft(name="\u041f\u0451\u0442\u0440 \u041f\u0435\u0442\u0440\u043e\u0432"),
                SpeakerDraft(name="TBA"),
            ],
            speaker_text=None,
        )
        assert [(s.name, s.affiliation) for s in one(draft).event.speakers] == [
            ("\u738b\u5c0f\u660e", "\u5317\u4eac\u5927\u5b66"),
            ("\u041f\u0451\u0442\u0440 \u041f\u0435\u0442\u0440\u043e\u0432", None),
        ]

    def test_merge_speakers_is_idempotent_and_bounded(self) -> None:
        many = [SpeakerDraft(name=f"Person Number{chr(65 + index)}") for index in range(26)]
        speakers = one(make_draft(speakers=many, speaker_text=None)).event.speakers
        assert len(speakers) == 20
        assert merge_speakers(speakers) == speakers


class TestOnline:
    def test_a_stream_address_marks_the_event_as_online(self) -> None:
        event = one(make_draft(stream_url="https://stanford.zoom.us/j/123456")).event
        assert event.is_online is True
        assert str(event.stream_url) == "https://stanford.zoom.us/j/123456"
        assert event.location == "Shriram Center, Room 104"

    @pytest.mark.parametrize(
        "url",
        [
            "https://stanford.zoom.us/j/123456?pwd=abcDEF",
            "https://stanford.zoom.us/j/123456?PASSCODE=1",
            "https://example.edu/live?tk=1",
            "https://example.edu/live#token=1",
            "https://user:secret@example.edu/live",
        ],
    )
    def test_a_stream_address_with_a_credential_is_dropped(self, url: str) -> None:
        result = normalize(make_draft(stream_url=url))
        event = result.candidates[0].event
        assert event.stream_url is None
        assert event.is_online is True
        assert "pwd" not in dump_events([event])
        assert result.redactions == 1

    def test_an_online_only_location(self) -> None:
        for location in ("Zoom", "Online", "Virtual event", "Online via Zoom", "Zoom Webinar"):
            event = one(make_draft(location=location)).event
            assert (event.location, event.is_online) == (None, True), location

    def test_a_hybrid_location(self) -> None:
        event = one(make_draft(location="Room 104 and Zoom")).event
        assert (event.location, event.is_online) == ("Room 104 and Zoom", True)

    def test_an_ordinary_location(self) -> None:
        for location in ("Zoomer Hall 3", "Remote Sensing Lab", "Virtual Reality Lab"):
            event = one(make_draft(location=location)).event
            assert (event.location, event.is_online) == (location, False), location

    def test_a_location_that_is_a_link(self) -> None:
        event = one(make_draft(location="https://stanford.zoom.us/j/99")).event
        assert (event.location, event.is_online) == (None, True)
        assert str(event.stream_url) == "https://stanford.zoom.us/j/99"
        private = one(make_draft(location="https://stanford.zoom.us/j/99?pwd=x")).event
        assert (private.location, private.is_online, private.stream_url) == (None, True, None)

    def test_what_the_source_says_explicitly_wins(self) -> None:
        event = one(make_draft(location="Zoom", is_online=False)).event
        assert (event.location, event.is_online) == ("Zoom", False)
        assert one(make_draft(is_online=True)).event.is_online is True

    def test_an_unannounced_location_is_none(self) -> None:
        assert one(make_draft(location="TBA")).event.location is None
        assert one(make_draft(location="Location TBD")).event.location is None
        assert one(make_draft(location="TBA", is_online=False)).event.location is None

    def test_a_country_alone_is_no_location(self) -> None:
        # The address block of a Stanford Sites card can hold just the country.
        for country in ("United States", "USA", "United States of America"):
            assert one(make_draft(location=country)).event.location is None, country
            assert one(make_draft(location=country, is_online=False)).event.location is None
        kept = "PAB 102/103, 452 Lomita Mall, Stanford, CA 94305, United States"
        assert one(make_draft(location=kept)).event.location == kept


class TestPrivacyAndSecrets:
    def test_meeting_passwords_are_removed_from_every_text(self) -> None:
        draft = make_draft(
            abstract="Join https://stanford.zoom.us/j/1?pwd=abc Passcode: 987654",
            location="Zoom https://stanford.zoom.us/j/1?pwd=abc and Room 1",
        )
        result = normalize(draft)
        text = dump_events([c.event for c in result.candidates])
        assert "pwd=" not in text
        assert "987654" not in text
        assert result.redactions == 2

    @pytest.mark.parametrize("name", ["github-token", "anthropic-oauth", "jwt", "bearer"])
    def test_credential_shaped_text_never_reaches_an_event(self, name: str) -> None:
        token = FORGED[name]
        draft = make_draft(
            title=f"Token {token}",
            abstract=f"<p>{token}</p>",
            location=f"Room {token}",
            series=token,
            speaker_text=f"Jane Doe ({token})",
            categories=[token],
        )
        result = normalize(draft)
        (candidate,) = result.candidates
        dumped = candidate.model_dump_json()
        assert token not in dumped
        assert REDACTED in candidate.event.title
        assert SCANNER.scan_text(dumped, origin="event") == []
        assert result.redactions >= 4

    def test_a_secret_shaped_address_is_not_used(self) -> None:
        token = FORGED["github-token"]
        draft = make_draft(native_id=None, url=f"https://example.edu/{token}", speaker_text=None)
        event = one(draft).event
        assert token not in event.model_dump_json()
        assert str(event.url) == make_source().url

    def test_a_secret_shaped_native_id_is_hashed(self) -> None:
        token = FORGED["github-token"]
        event = one(make_draft(native_id=token)).event
        assert token not in event.model_dump_json()
        assert event.source_ids[0].startswith("sites-bioengineering:sha256:")

    def test_an_address_with_a_credential_is_not_published(self) -> None:
        event = one(make_draft(url="https://example.edu/talk?token=abc")).event
        assert str(event.url) == make_source().url


class TestSourceKeys:
    def key(self, draft: EventDraft, **overrides: Any) -> str:
        values: dict[str, Any] = {
            "start": datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC),
            "speaker": "Jane Doe",
            "title": "Protein design",
            "shared": frozenset(),
            "is_secret": no_secret,
        }
        return draft_key(draft, **{**values, **overrides})

    def test_the_native_id_comes_first(self) -> None:
        assert self.key(make_draft(native_id=" 44475127680618 ")) == (
            "sites-bioengineering:44475127680618"
        )

    def test_then_the_detail_page(self) -> None:
        draft = make_draft(native_id=None, url="HTTPS://Example.edu/Talks/7/#top")
        assert self.key(draft) == "sites-bioengineering:url:https://example.edu/Talks/7"

    def test_a_page_shared_by_several_drafts_is_not_a_detail_page(self) -> None:
        source = make_source(homepage="https://example.edu/seminar")
        series = [make_draft(native_id=None, url="https://example.edu/series/") for _ in range(2)]
        listed = make_draft(native_id=None, url="https://example.edu/seminar")
        own = make_draft(native_id=None, url="https://example.edu/talks/7")
        shared = shared_urls([*series, listed, own], source)
        assert shared == {
            "https://example.edu/series",
            "https://example.edu/seminar",
            "https://bioengineering.stanford.edu/events/all/rss",
        }
        assert self.key(series[0], shared=shared) == (
            "sites-bioengineering:day:2026-11-17:jane-doe"
        )
        assert self.key(listed, shared=shared).startswith("sites-bioengineering:day:")
        assert self.key(own, shared=shared).startswith("sites-bioengineering:url:")

    def test_then_the_day_with_the_speaker_the_title_or_the_time(self) -> None:
        draft = make_draft(native_id=None, url=None)
        assert self.key(draft) == "sites-bioengineering:day:2026-11-17:jane-doe"
        assert self.key(draft, speaker=None) == "sites-bioengineering:day:2026-11-17:protein-design"
        assert self.key(draft, speaker=None, title=None) == (
            "sites-bioengineering:day:2026-11-17:t1600"
        )

    def test_keys_are_ascii_whatever_the_names_are(self) -> None:
        draft = make_draft(native_id="caf\u00e9-7", url=None)
        assert self.key(draft) == "sites-bioengineering:caf\\xe9-7"
        named = make_draft(native_id=None, url=None)
        assert self.key(named, speaker="Jos\u00e9 Garc\u00eda").endswith(":jose-garcia")
        assert self.key(named, speaker="\u5f20\u4f1f", title="\u7fa4\u8bba").endswith(":t1600")

    def test_long_and_secret_shaped_native_ids_are_hashed(self) -> None:
        assert safe_native_id(" short ", no_secret) == "short"
        hashed = safe_native_id("x" * 500, no_secret)
        assert re.fullmatch(r"sha256:[0-9a-f]{24}", hashed)
        assert safe_native_id("x" * 500, no_secret) == hashed
        assert safe_native_id("abc", lambda text: True).startswith("sha256:")

    def test_helpers(self) -> None:
        assert canonical_url("HTTPS://X.edu/Events/a/?q=1#frag") == "https://x.edu/Events/a?q=1"
        assert slug("Jos\u00e9 Garc\u00eda-L\u00f3pez!") == "jose-garcia-lopez"
        assert slug("  ") == ""


class TestUids:
    @pytest.mark.parametrize(
        ("key", "uid"),
        [
            ("localist-stanford:44475127680618", "localist-stanford.44475127680618"),
            (
                "sites-chemistry:0b9c1c36-8a7e-4f7b-9a53-0d2a1b3c4d5e",
                "sites-chemistry.0b9c1c36-8a7e-4f7b-9a53-0d2a1b3c4d5e",
            ),
            ("rsorg-x:talk_7.v2", "rsorg-x.talk_7.v2"),
        ],
    )
    def test_a_simple_native_id_is_readable(self, key: str, uid: str) -> None:
        assert uid_for_key(key) == uid

    @pytest.mark.parametrize(
        "key",
        [
            "sites-x:url:https://example.edu/talks/7",
            "sites-x:day:2026-11-17:jane-doe",
            "sites-x:guid with spaces",
            "sites-x:_leading",
            "sites-x:" + "a" * 64,
            "sites-x:caf\\xe9",
            "sites-x:",
        ],
    )
    def test_anything_else_gets_a_digest(self, key: str) -> None:
        uid = uid_for_key(key)
        assert re.fullmatch(r"sites-x\._[0-9a-f]{24}", uid)
        assert uid == uid_for_key(key)

    def test_uids_are_valid_and_distinct(self) -> None:
        keys = [
            "a-b:c",
            "a:b-c",
            "a:b.c",
            "a-b:url:c",
            "a:" + "x" * 63,
            "s" * 64 + ":" + "n" * 63,
            "a:_" + "0" * 24,
        ]
        uids = [uid_for_key(key) for key in keys]
        assert len(set(uids)) == len(keys)
        assert all(re.fullmatch(UID_PATTERN, uid) for uid in uids)

    def test_a_known_key_keeps_its_uid(self) -> None:
        uid_map = UidMap(aliases={"sites-x:7": "localist-stanford.1"})
        assert resolve_uid("sites-x:7", uid_map) == "localist-stanford.1"
        assert resolve_uid("sites-x:8", uid_map) == "sites-x.8"


class TestUidStability:
    """The uid of an event must survive everything a source does to it."""

    def test_the_same_input_gives_the_same_uids(self) -> None:
        drafts = [
            make_draft(),
            make_draft(native_id=None, url="https://example.edu/talks/7"),
            make_draft(native_id=None, url=None),
            make_draft(native_id=None, url=None, speaker_text=None),
        ]
        first = [c.event.uid for c in normalize(*drafts).candidates]
        second = [c.event.uid for c in normalize(*drafts).candidates]
        assert first == second
        assert len(set(first)) == 4

    def test_title_time_and_speaker_changes_do_not_change_a_native_uid(self) -> None:
        before = one(make_draft(title="TBA", speaker_text=None)).event
        after = one(
            make_draft(
                title="Protein design",
                start=datetime(2026, 11, 19, 10, 0, tzinfo=PACIFIC),
                end=None,
                url="https://example.edu/moved",
            )
        ).event
        assert before.uid == after.uid == "sites-bioengineering.1234"

    def test_a_detail_page_uid_survives_title_and_time_changes(self) -> None:
        tba = make_draft(native_id=None, title="TBA")
        announced = make_draft(
            native_id=None,
            title="Protein design",
            start=datetime(2026, 12, 1, 9, 0, tzinfo=PACIFIC),
        )
        assert one(tba).event.uid == one(announced.evolve(end=None)).event.uid

    def test_a_derived_uid_survives_a_title_change_while_the_speaker_is_known(self) -> None:
        tba = make_draft(native_id=None, url=None, title="TBA")
        announced = make_draft(native_id=None, url=None, title="Protein design")
        assert one(tba).event.uid == one(announced).event.uid

    def test_the_uid_map_decides_before_the_derivation(self) -> None:
        uid_map = UidMap(aliases={"sites-bioengineering:1234": "localist-stanford.99"})
        assert one(make_draft(), uid_map=uid_map).event.uid == "localist-stanford.99"

    def test_two_sources_never_share_a_derived_uid(self) -> None:
        other = make_source(id="sites-bio", url="https://bio.example.edu/rss")
        first = one(make_draft()).event.uid
        second = one(make_draft(source_id="sites-bio"), other).event.uid
        assert first != second
