"""The researchseminars adapter: series feeds of researchseminars.org.

The recorded feeds of the two configured sources come first, then the layout
of DESCRIPTION, the speakers and the edge cases with hand-written feeds.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.pipeline.text import one_line
from seminars.sources import base
from seminars.sources.base import ParseError
from seminars.sources.custom import researchseminars
from seminars.sources.custom.researchseminars import (
    Description,
    ResearchSeminarsAdapter,
    parse_speakers,
    read_description,
    talk_counter,
)
from seminars.sources.generic.html_list import AdapterConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDED = datetime(2026, 11, 11, 16, 16, tzinfo=UTC)
TOPOS = "rsorg-topos-colloquium"
AGSTANFORD = "rsorg-stanford-ag"
TOPOS_FEED = "https://researchseminars.org/seminar/ToposInstituteColloquium/ics"
TOPOS_TALK = "https://researchseminars.org/talk/ToposInstituteColloquium"
AG_TALK = "https://researchseminars.org/talk/agstanford"


@functools.cache
def configured() -> dict[str, SourceConfig]:
    sources = load_sources(REPO_ROOT / "config" / "sources.d")
    return {source.id: source for source in sources if source.adapter == "researchseminars"}


@functools.cache
def recorded(source_id: str) -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    """Requests and drafts of a configured source, read from its recording."""
    source = configured()[source_id]
    http = offline_client(source_id)
    adapter = ResearchSeminarsAdapter()
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(RECORDED)), source)
    return tuple(http.requests), tuple(drafts)


def talk(source_id: str, native_id: str) -> EventDraft:
    (draft,) = [item for item in recorded(source_id)[1] if item.native_id == native_id]
    return draft


def speakers(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


# --- registration and configuration --------------------------------------------------------


def test_the_module_registers_the_adapter() -> None:
    assert isinstance(base.get_adapter("researchseminars"), ResearchSeminarsAdapter)
    assert "researchseminars" in base.adapter_names()


def test_both_configured_sources_use_the_adapter_and_keep_their_state() -> None:
    sources = configured()
    assert set(sources) == {TOPOS, AGSTANFORD}
    assert sources[TOPOS].enabled
    assert not sources[AGSTANFORD].enabled
    assert sources[TOPOS].params == {"series_id": "ToposInstituteColloquium"}
    assert sources[AGSTANFORD].params == {"series_id": "agstanford"}


# --- the recorded feeds --------------------------------------------------------------------


class TestRecordedTopos:
    def test_fetch_requests_the_feed_and_nothing_else(self) -> None:
        requests, drafts = recorded(TOPOS)
        assert requests == (TOPOS_FEED,)
        assert len(drafts) == 233
        assert {draft.extraction for draft in drafts} == {"feed"}
        assert {draft.source_id for draft in drafts} == {TOPOS}

    def test_an_upcoming_online_talk(self) -> None:
        draft = talk(TOPOS, "ToposInstituteColloquium/232")
        assert draft.title == "What are (infty,infty)-categories?"
        assert draft.start == datetime(2026, 11, 12, 17, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
        assert not draft.is_all_day
        assert draft.timezone is None
        assert speakers(draft) == [("Viktoriya Ozornova", None)]
        assert draft.speaker_text is None
        assert draft.location is None
        assert draft.url == f"{TOPOS_TALK}/232/"
        assert draft.series == "Topos Institute Colloquium"
        assert draft.is_online is True
        assert draft.stream_url == "https://www.youtube.com/live/fTbxbYiH-IE"
        assert draft.abstract is not None
        assert draft.abstract.startswith("I will report on joint work with Martina Rovelli")
        assert draft.abstract.endswith("as well as intermediate localizations.")
        assert draft.abstract_html is None

    def test_a_talk_without_a_title_yet(self) -> None:
        draft = talk(TOPOS, "ToposInstituteColloquium/254")
        assert draft.title is None
        assert draft.start == datetime(2027, 5, 20, 17, 0, tzinfo=UTC)
        assert speakers(draft) == [("Jacob Tsimerman", None)]
        assert draft.abstract is None
        assert draft.is_online is True
        assert draft.stream_url is None
        assert draft.url == f"{TOPOS_TALK}/254/"

    def test_a_past_talk_has_no_livestream(self) -> None:
        draft = talk(TOPOS, "ToposInstituteColloquium/2")
        assert draft.title == "Comodels of an algebraic theory"
        assert speakers(draft) == [("Richard Garner", None)]
        assert draft.is_online is None
        assert draft.stream_url is None
        assert draft.abstract is not None
        assert draft.abstract.startswith("In 1991 Eugenio Moggi introduced")
        assert "\n\nAround 2000, Plotkin and Power" in draft.abstract

    def test_several_speakers_in_one_talk(self) -> None:
        by_speakers = {len(draft.speakers): draft for draft in recorded(TOPOS)[1]}
        assert speakers(by_speakers[5]) == [
            ("Riehl", None),
            ("Bradley", None),
            ("Cheng", None),
            ("Dancstep", None),
            ("Lugg", None),
        ]
        assert speakers(by_speakers[2]) == [
            ("Eswaran Subrahmanian", None),
            ("Yves Keraron", None),
        ]

    def test_no_meeting_link_password_or_location_url_reaches_a_draft(self) -> None:
        for draft in recorded(TOPOS)[1]:
            texts = [draft.url, draft.stream_url, draft.location, draft.abstract, draft.title]
            joined = " ".join(text for text in texts if text)
            assert "zoom.us" not in joined
            assert "pwd" not in joined
            assert "Password hint" not in joined
            assert "livestream" not in joined
            assert draft.location is None
            counter = (draft.native_id or "").rpartition("/")[2]
            assert draft.url == f"{TOPOS_TALK}/{counter}/"

    def test_upcoming_talks_are_online_and_titled_when_announced(self) -> None:
        upcoming = [
            draft
            for draft in recorded(TOPOS)[1]
            if isinstance(draft.start, datetime) and draft.start >= RECORDED
        ]
        assert len(upcoming) == 24
        assert all(draft.is_online for draft in upcoming)
        assert sum(draft.title is None for draft in upcoming) == 21


class TestRecordedStanfordAg:
    def test_every_talk_is_returned_whatever_its_date(self) -> None:
        requests, drafts = recorded(AGSTANFORD)
        assert requests == ("https://researchseminars.org/seminar/agstanford/ics",)
        assert len(drafts) == 148
        starts = [draft.start for draft in drafts if isinstance(draft.start, datetime)]
        assert len(starts) == len(drafts)
        assert max(starts) < RECORDED

    def test_a_talk_with_a_room_and_an_affiliation(self) -> None:
        draft = talk(AGSTANFORD, "agstanford/152")
        assert draft.title == "(Distinguished Lecture) Algebraic geometry vs Kahler geometry"
        assert draft.start == datetime(2024, 12, 4, 22, 0, tzinfo=UTC)
        assert draft.end == datetime(2024, 12, 4, 23, 0, tzinfo=UTC)
        assert speakers(draft) == [("Christopher Hacon", "University of Utah")]
        assert draft.location == "383-N"
        assert draft.url == f"{AG_TALK}/152/"
        assert draft.series == "Stanford algebraic geometry seminar"
        assert draft.is_online is None
        assert draft.abstract is not None
        assert draft.abstract.startswith("Algebraic geometry and analytic geometry")

    @pytest.mark.parametrize(
        ("native_id", "location"),
        [
            ("agstanford/96", "Room 383-N"),
            ("agstanford/67", "383-N"),
            ("agstanford/128", None),  # "Lecture held in TBA."
            ("agstanford/139", "384-I (unusual date and location)"),
            ("agstanford/1", None),  # no room line
        ],
    )
    def test_rooms(self, native_id: str, location: str | None) -> None:
        assert talk(AGSTANFORD, native_id).location == location

    def test_every_room_was_read_after_unfolding(self) -> None:
        rooms = {draft.location for draft in recorded(AGSTANFORD)[1]} - {None}
        assert "380-W (unusual room!)" in rooms
        assert all("  " not in room and " -" not in room for room in rooms if room)
        assert not any(room.endswith(".") for room in rooms if room)

    @pytest.mark.parametrize(
        ("native_id", "expected"),
        [
            ("agstanford/136", [("Hannah Larson", "Berkeley, Clay Mathematical Institute")]),
            ("agstanford/110", [("Hannah Larson", "Harvard/Berkeley")]),
        ],
    )
    def test_affiliations_with_commas_and_slashes(
        self, native_id: str, expected: list[tuple[str, str | None]]
    ) -> None:
        assert speakers(talk(AGSTANFORD, native_id)) == expected

    def test_an_affiliation_with_and_is_one_speaker(self) -> None:
        (draft,) = [
            item for item in recorded(AGSTANFORD)[1] if "Princeton and IAS" in str(item.speakers)
        ]
        assert speakers(draft) == [("Remy van Dobben de Bruyn", "Princeton and IAS")]

    def test_the_by_line_is_taken_by_position(self) -> None:
        draft = talk(AGSTANFORD, "agstanford/6")
        assert draft.title == "Bertini irreducibility theorems via statistics"
        assert speakers(draft) == [("Bjorn Poonen", "MIT")]
        assert draft.abstract is not None
        assert "\nby reducing to the case of a finite field" in draft.abstract

    def test_an_abstract_with_latex_and_a_raw_ampersand_in_a_title(self) -> None:
        titles = {draft.title for draft in recorded(AGSTANFORD)[1]}
        assert "38406501359372282063949 & all that: Monodromy of Fano problems" in titles
        draft = talk(AGSTANFORD, "agstanford/136")
        assert draft.abstract is not None
        assert "$J^d_g$ and $\\mathscr{J}^d_g$" in draft.abstract


# --- the layout of DESCRIPTION -------------------------------------------------------------

LINK = '<a href="https://researchseminars.org/talk/S/1/">'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f"Title: {LINK}A talk</a>\nby Jane Doe (MIT) as part of Series\n\n\nAbstract\nText",
            Description(
                title="A talk",
                speakers_text="Jane Doe (MIT)",
                series="Series",
                abstract="Text",
            ),
            id="title-by-abstract",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nAbstract: TBA",
            Description(speakers_text="Jane Doe", series="Series"),
            id="tba-directly-after-the-by-line",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nInteractive livestream: https://x.zoom.us/j/1?pwd=X"
            "\nPassword hint: 12345\nAbstract: TBA",
            Description(speakers_text="Jane Doe", series="Series", online=True),
            id="interactive-only",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nView-only livestream: https://youtu.be/abc\n"
            "Lecture held in Room 383-N.\n\nAbstract\nOne\n\nTwo\n\nNote the room.",
            Description(
                speakers_text="Jane Doe",
                series="Series",
                location="Room 383-N",
                online=True,
                view_only_link="https://youtu.be/abc",
                abstract="One\n\nTwo\n\nNote the room.",
            ),
            id="hybrid-with-paragraphs-and-comments",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nAbstract: TBA\n\nThe talk will be recorded.",
            Description(
                speakers_text="Jane Doe", series="Series", abstract="The talk will be recorded."
            ),
            id="comments-after-a-tba-abstract",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nAbstract: A short one.",
            Description(speakers_text="Jane Doe", series="Series", abstract="A short one."),
            id="abstract-on-the-marker-line",
        ),
        pytest.param(
            "by Jane Doe as part of Series\n\nMeeting ID: 123 456\nLecture held in 380-W\n\n"
            "Abstract\nText",
            Description(
                speakers_text="Jane Doe", series="Series", location="380-W", abstract="Text"
            ),
            id="unknown-header-lines-are-ignored",
        ),
        pytest.param(
            "by Jane Doe\n\nLecture held in TBD.",
            Description(speakers_text="Jane Doe"),
            id="no-series-no-abstract-room-tbd",
        ),
        pytest.param(
            f"Title: {LINK}Groups &amp; rings &lt;3</a>\nby A as part of B",
            Description(title="Groups &amp; rings &lt;3", speakers_text="A", series="B"),
            id="html-entities-in-the-title-are-left-to-normalization",
        ),
        pytest.param(
            f"Title: {LINK}On $p<q$ bounds</a>\nby A as part of B",
            Description(title="On $p<q$ bounds", speakers_text="A", series="B"),
            id="a-less-than-sign-before-a-letter",
        ),
        pytest.param(
            f"Title: {LINK}x<y and y>z</a>",
            Description(title="x<y and y>z"),
            id="text-that-looks-like-a-tag",
        ),
        pytest.param(
            f"Title: {LINK}Links </a> in </a>titles</a>",
            Description(title="Links </a> in </a>titles"),
            id="up-to-the-last-closing-tag",
        ),
        pytest.param(
            f"Title:   {LINK}  Spaced\ttitle  </a>  ",
            Description(title="Spaced title"),
            id="whitespace-around-the-link",
        ),
        pytest.param(
            f"Title: {LINK}</a>\nby A",
            Description(speakers_text="A"),
            id="an-empty-link",
        ),
        pytest.param(
            "Title: A talk without a link, $x<y$\nby A as part of B",
            Description(title="A talk without a link, $x<y$", speakers_text="A", series="B"),
            id="title-without-a-link",
        ),
        pytest.param(
            "Title: <a href='https://researchseminars.org/talk/S/1/'>Single &amp; quoted</a>",
            Description(title="Single & quoted"),
            id="a-link-of-another-shape-is-read-as-html",
        ),
        pytest.param(
            f"Title: {LINK}Never closed",
            Description(title="Never closed"),
            id="a-link-without-its-closing-tag-is-read-as-html",
        ),
        pytest.param(
            "Title: <abbr>AG</abbr> seminar",
            Description(title="<abbr>AG</abbr> seminar"),
            id="a-tag-that-only-starts-like-a-link",
        ),
        pytest.param(
            "Abstract\nOnly an abstract.\nby somebody else",
            Description(abstract="Only an abstract.\nby somebody else"),
            id="no-by-line",
        ),
        pytest.param("", Description(), id="empty"),
        pytest.param(None, Description(), id="missing"),
    ],
)
def test_read_description(text: str | None, expected: Description) -> None:
    assert read_description(text) == expected


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Groups &amp; rings &lt;3", "Groups & rings <3"),
        ("On $p<q$ bounds", "On $p<q$ bounds"),
        ("x<y and y>z", "x<y and y>z"),
        ("<i>Lie</i> groups & more", "Lie groups & more"),
    ],
)
def test_raw_titles_are_finished_by_normalization(raw: str, normalized: str) -> None:
    title = read_description(f"Title: {LINK}{raw}</a>").title
    assert title == raw
    assert one_line(title, limit=300) == normalized


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jane Doe", [("Jane Doe", None)]),
        ("Jane Doe (MIT)", [("Jane Doe", "MIT")]),
        ("  Jane   Doe  ( MIT ) ", [("Jane Doe", "MIT")]),
        ("Jane Doe (Princeton and IAS)", [("Jane Doe", "Princeton and IAS")]),
        ("Jane Doe (Berkeley, Clay)", [("Jane Doe", "Berkeley, Clay")]),
        ("Jane Doe (Univ. (Paris))", [("Jane Doe", "Univ. (Paris)")]),
        ("Jane Doe (MIT) and John Roe (Stanford)", [("Jane Doe", "MIT"), ("John Roe", "Stanford")]),
        ("Jane Doe & John Roe", [("Jane Doe", None), ("John Roe", None)]),
        ("A, B, and C", [("A", None), ("B", None), ("C", None)]),
        ("A, B AND C", [("A", None), ("B", None), ("C", None)]),
        ("Jane Doe, Jr.", [("Jane Doe, Jr.", None)]),
        ("Jane Doe (MIT", [("Jane Doe (MIT", None)]),
        ("Jane Doe MIT)", [("Jane Doe MIT)", None)]),
        ("(MIT)", [("(MIT)", None)]),
        ("Texas A&M (Texas A&M University)", [("Texas A&M", "Texas A&M University")]),
        (
            "Jos\u00e9 Garc\u00eda (Universidad de Chile)",
            [("Jos\u00e9 Garc\u00eda", "Universidad de Chile")],
        ),
        ("", []),
        (None, []),
    ],
)
def test_parse_speakers(text: str | None, expected: list[tuple[str, str | None]]) -> None:
    assert [(item.name, item.affiliation) for item in parse_speakers(text)] == expected


@pytest.mark.parametrize(
    ("uid", "expected"),
    [
        ("S1/12", "12"),
        ("S1/0", "0"),
        ("S1/-59", None),  # the site's copy of a rescheduled talk
        ("Other/12", None),
        ("S1/12/x", None),
        ("S1", None),
        ("", None),
        (None, None),
    ],
)
def test_talk_counter(uid: str | None, expected: str | None) -> None:
    assert talk_counter(uid, "S1") == expected


# --- hand-written feeds --------------------------------------------------------------------

FEED = "https://researchseminars.org/seminar/S1/ics"


def series_source(**overrides: Any) -> SourceConfig:
    values: dict[str, Any] = {
        "id": "rsorg-example",
        "name": "Example series",
        "group": "researchseminars",
        "adapter": "researchseminars",
        "url": FEED,
        "institution": "other",
        "host_org": "Example",
        "timezone": "UTC",
        "aggregator": True,
        "default_fields": ["math"],
        "series": "Example series",
        "params": {"series_id": "S1"},
    }
    return make_source(**{**values, **overrides})


def calendar(*events: str) -> str:
    head = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:researchseminars.org\n"
    return head + "".join(events) + "END:VCALENDAR\n"


def vevent(*lines: str, uid: str | None = "S1/7", start: str | None = "20261117T170000Z") -> str:
    head = ([f"UID:{uid}"] if uid else []) + ([f"DTSTART:{start}"] if start else [])
    body = [*head, *lines]
    return "BEGIN:VEVENT\n" + "".join(f"{line}\n" for line in body) + "END:VEVENT\n"


DESCRIPTION = (
    'DESCRIPTION:Title: <a href="https://researchseminars.org/talk/S1/7/">A talk</a>\\n'
    "by Jane Doe (MIT) as part of Example series\\n\\n\\nAbstract\\nText"
)


def parse(text: str, source: SourceConfig | None = None) -> list[EventDraft]:
    source = source or series_source()
    document = make_document(url=FEED, text=text, content_type="text/calendar")
    payload = make_payload(document, source_id=source.id, window=make_window(RECORDED))
    return ResearchSeminarsAdapter().parse(payload, source)


def parse_one(*lines: str, **kwargs: Any) -> EventDraft:
    (draft,) = parse(calendar(vevent(*lines, **kwargs)))
    return draft


class TestHandWrittenFeeds:
    def test_a_complete_talk(self) -> None:
        draft = parse_one("SUMMARY:Jane Doe (MIT)", "DTEND:20261117T180000Z", DESCRIPTION)
        assert draft.native_id == "S1/7"
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 17, 17, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 17, 18, 0, tzinfo=UTC)
        assert speakers(draft) == [("Jane Doe", "MIT")]
        assert draft.url == "https://researchseminars.org/talk/S1/7/"
        assert draft.abstract == "Text"

    def test_location_and_url_properties_are_never_copied(self) -> None:
        draft = parse_one(
            "SUMMARY:Jane Doe",
            DESCRIPTION,
            "LOCATION:https://x.zoom.us/j/1?pwd=SECRET",
            "URL:https://x.zoom.us/j/1?pwd=SECRET",
        )
        assert draft.location is None
        assert draft.url == "https://researchseminars.org/talk/S1/7/"
        assert draft.stream_url is None
        assert draft.is_online is None

    def test_speakers_fall_back_to_the_summary(self) -> None:
        draft = parse_one("SUMMARY:Jane Doe (MIT)\\, John Roe and Mary Major")
        assert speakers(draft) == [
            ("Jane Doe", "MIT"),
            ("John Roe", None),
            ("Mary Major", None),
        ]
        assert draft.title is None
        assert draft.abstract is None

    def test_a_non_ascii_speaker(self) -> None:
        draft = parse_one("SUMMARY:Jos\u00e9 Garc\u00eda (Universit\u00e9 Paris)")
        assert speakers(draft) == [("Jos\u00e9 Garc\u00eda", "Universit\u00e9 Paris")]

    def test_an_all_day_talk_ends_on_its_last_day(self) -> None:
        draft = parse_one(
            "SUMMARY:Workshop", "DTEND;VALUE=DATE:20261120", start="20261117", uid="S1/8"
        )
        assert draft.start == date(2026, 11, 17)
        assert draft.end == date(2026, 11, 19)
        assert draft.is_all_day

    def test_a_one_day_all_day_talk_has_no_end(self) -> None:
        draft = parse_one("DTEND;VALUE=DATE:20261118", start="20261117")
        assert draft.start == date(2026, 11, 17)
        assert draft.end is None
        assert draft.is_all_day

    def test_a_multi_day_timed_talk(self) -> None:
        draft = parse_one("DTEND:20261119T010000Z", start="20261117T170000Z")
        assert draft.end == datetime(2026, 11, 19, 1, 0, tzinfo=UTC)
        assert not draft.is_all_day

    def test_a_cancelled_talk(self) -> None:
        assert parse_one("STATUS:CANCELLED").status_hint == "cancelled"

    def test_a_vevent_without_start_or_with_a_broken_one_is_skipped(self) -> None:
        drafts = parse(
            calendar(
                vevent(uid="S1/1", start=None),
                vevent(uid="S1/2", start="not-a-date"),
                vevent(uid="S1/3"),
            )
        )
        assert [draft.native_id for draft in drafts] == ["S1/3"]

    def test_vevents_of_other_series_and_without_uid_are_skipped(self) -> None:
        drafts = parse(calendar(vevent(uid="Other/1"), vevent(uid=None), vevent(uid="S1/4")))
        assert [draft.native_id for draft in drafts] == ["S1/4"]

    def test_a_feed_without_any_talk_of_the_series_is_not_understood(self) -> None:
        with pytest.raises(ParseError, match=r"rsorg-example.*series S1"):
            parse(calendar(vevent(uid="Other/1"), vevent(uid="Other/2")))

    def test_an_empty_calendar_is_fine(self) -> None:
        assert parse(calendar()) == []

    def test_a_body_that_is_no_calendar_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match="source rsorg-example"):
            parse("<html><body>Not found</body></html>\n")

    def test_a_feed_that_breaks_the_calendar_library_is_a_parse_error(self) -> None:
        broken = (
            "BEGIN:VCALENDAR\nBEGIN:VTIMEZO;E\nTZID:Custom/Zone\nBEGIN:STANDARD\n"
            "TZOFFSETFROM:-0800\nTZOFFSETTO:-0800\nEND:STANDARD\nEND:VTIMEZONE\n"
            + vevent()
            + "END:VCALENDAR\n"
        )
        with pytest.raises(ParseError, match="source rsorg-example: the iCalendar feed"):
            parse(broken)

    def test_a_repeated_uid_is_kept_once(self) -> None:
        drafts = parse(calendar(vevent("SUMMARY:First"), vevent("SUMMARY:Second")))
        assert len(drafts) == 1
        assert speakers(drafts[0]) == [("First", None)]

    def test_a_talk_that_cannot_be_a_draft_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = researchseminars.occurrence_draft

        def refuse_one(occurrence: Any, *args: Any, **kwargs: Any) -> EventDraft:
            if occurrence.native_id == "S1/1":
                raise ValueError("broken")
            return real(occurrence, *args, **kwargs)

        monkeypatch.setattr(researchseminars, "occurrence_draft", refuse_one)
        drafts = parse(calendar(vevent(uid="S1/1"), vevent(uid="S1/2")))
        assert [draft.native_id for draft in drafts] == ["S1/2"]

    def test_an_invalid_view_only_link_is_dropped(self) -> None:
        draft = parse_one("DESCRIPTION:by A as part of B\\n\\nView-only livestream: javascript:x")
        assert draft.stream_url is None
        assert draft.is_online is True

    def test_all_texts_are_copied_without_cleaning(self) -> None:
        draft = parse_one(
            "DESCRIPTION:by A as part of B\\n\\nAbstract\\nSee <b>this</b> &amp; that\\, $x<y$"
        )
        assert draft.abstract == "See <b>this</b> &amp; that, $x<y$"
        assert draft.abstract_html is None


class TestParams:
    def test_fetch_requests_only_the_configured_feed(self) -> None:
        http = FakeHttpClient({FEED: calendar(vevent())})
        payload = ResearchSeminarsAdapter().fetch(http, series_source(), window=make_window())
        assert http.requests == [FEED]
        assert [document.url for document in payload.documents] == [FEED]

    def test_an_unknown_key_is_named(self) -> None:
        source = series_source(params={"series_id": "S1", "seriesid": "S1"})
        with pytest.raises(AdapterConfigError, match=r"params\.seriesid: unknown key"):
            ResearchSeminarsAdapter().fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(AdapterConfigError, match=r"params\.seriesid"):
            parse(calendar(), source)

    def test_series_id_is_required_and_checked(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.series_id"):
            parse(calendar(), series_source(params={}))
        with pytest.raises(AdapterConfigError, match=r"params\.series_id"):
            parse(calendar(), series_source(params={"series_id": "a/b"}))

    def test_extra_hosts_is_left_to_the_host_check(self) -> None:
        source = series_source(params={"series_id": "S1", "extra_hosts": ["example.org"]})
        assert len(parse(calendar(vevent()), source)) == 1


def test_the_module_has_no_site_specific_ids() -> None:
    text = Path(researchseminars.__file__).read_text(encoding="utf-8")
    for name in ("ToposInstituteColloquium", "agstanford", "rsorg-"):
        assert name not in text
