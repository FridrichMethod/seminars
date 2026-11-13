"""Browser tests of site/calendar.js: a calendar file of chosen talks, cut out of a feed.

The module runs in the served talk list, imported by the test (a dynamic import in
``page.evaluate``), against the sample feed that the site serves as feeds/all.ics with
CRLF line endings. What it makes is checked in Python against the same file: a cut of
the feed made here line by line (``expected_calendar``) and the feed validator of the
pipeline (``seminars.outputs.ics.validate_ics``). Calendars written by hand cover what
the sample feed does not hold: folded UIDs, alarms, UIDs without a domain and broken
text. The server, the browser and the sessions are those of test_site_browser.py; its
fixtures are bound to names of this module, so pytest finds them here as well. Marked
``browser`` like that module: ``uv run pytest -m browser``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import test_site_browser as harness
from seminars.outputs.ics import validate_ics
from seminars.textio import read_text
from site_sample import UID_DOMAIN
from test_site_browser import Session, sample
from test_site_browser_search import paused

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

CRLF = "\r\n"
FEED_PATH = "feeds/all.ics"
FILE_NAME = "seminars-selected.ics"
CALENDAR_TYPE = "text/calendar;charset=utf-8"
FETCH_TIMEOUT_MS = 30 * 1000  # how long fetchCalendar waits for a feed (calendar.js)
KEPT = ("VERSION", "PRODID", "CALSCALE", "METHOD", "X-WR-TIMEZONE")
SUBSCRIPTION_ONLY = ("X-WR-CALNAME", "X-WR-CALDESC", "REFRESH-INTERVAL", "X-PUBLISHED-TTL")
SAMPLE_UIDS = [event["uid"] for event in sample("events.json")["events"]]
MAX_LINE_OCTETS = 75
LONG_UID = "talk-" + "x" * 115  # 120 characters, the UID line is folded
UID_LINE = re.compile(r"^UID:(.*)$", re.MULTILINE)
UNFOLD = re.compile(r"\r?\n[ \t]")

EXPORTS = "async () => Object.keys(await import('./calendar.js')).sort()"

PARSE = """async (text) => {
  const { parseCalendar } = await import("./calendar.js");
  return parseCalendar(text);
}"""

PARSE_FEED = """async (path) => {
  const { fetchCalendar, parseCalendar } = await import("./calendar.js");
  const parsed = parseCalendar(await fetchCalendar(path));
  const values = [
    parsed,
    parsed.properties,
    parsed.timezones,
    parsed.events,
    ...parsed.properties,
    ...parsed.timezones,
    ...parsed.events,
    ...parsed.events.map((event) => event.lines),
  ];
  return { ...parsed, frozen: values.every((value) => Object.isFrozen(value)) };
}"""

# ``text`` null: the served feed. ``asSet``: the uids are handed over as a Set.
CUT = """async ({ text, uids, asSet }) => {
  const { calendarFor, fetchCalendar, parseCalendar } = await import("./calendar.js");
  const parsed = parseCalendar(text ?? (await fetchCalendar("feeds/all.ics")));
  const result = calendarFor(parsed, asSet ? new Set(uids) : uids);
  const frozen = [result, result.included, result.missing].every((v) => Object.isFrozen(v));
  return { ...result, frozen };
}"""

THROWN = """async (text) => {
  const { parseCalendar } = await import("./calendar.js");
  try {
    parseCalendar(text);
    return null;
  } catch (error) {
    return { error: error instanceof Error, message: String(error.message) };
  }
}"""

FETCH = """async (path) => {
  const { fetchCalendar } = await import("./calendar.js");
  try {
    return { text: await fetchCalendar(path) };
  } catch (error) {
    return { error: error instanceof Error, message: String(error.message) };
  }
}"""

# The requests the page makes through fetch, with the options it gives each.
RECORD_FETCHES = """() => {
  window.__fetches = [];
  const original = window.fetch;
  window.fetch = (input, init) => {
    window.__fetches.push({
      path: new URL(String(input), document.baseURI).pathname,
      cache: init?.cache ?? null,
      signal: init?.signal instanceof AbortSignal,
    });
    return original.call(window, input, init);
  };
}"""

# Start fetchCalendar(path) and leave its outcome in window.__fetched.
START_FETCH = """(path) => {
  window.__fetched = null;
  import("./calendar.js")
    .then(({ fetchCalendar }) => fetchCalendar(path))
    .then(
      (text) => { window.__fetched = { text }; },
      (error) => {
        window.__fetched = { error: error instanceof Error, message: String(error.message) };
      },
    );
}"""

# Object URLs made and given up by the page, with the media type of each file.
TRACK_URLS = """() => {
  window.__urls = { created: [], types: [], revoked: [] };
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (blob) => {
    const address = create(blob);
    window.__urls.created.push(address);
    window.__urls.types.push(blob.type);
    return address;
  };
  URL.revokeObjectURL = (address) => {
    window.__urls.revoked.push(address);
    revoke(address);
  };
}"""

SAVE = """async ({ text, name, type }) => {
  const { saveFile } = await import("./calendar.js");
  if (type === null) {
    saveFile(text, name);
  } else {
    saveFile(text, name, type);
  }
  return { ...window.__urls, anchors: document.querySelectorAll("a[download]").length };
}"""

SAVE_REFUSED = """async ({ text, name }) => {
  const { saveFile } = await import("./calendar.js");
  try {
    saveFile(text, name);
    return null;
  } catch (error) {
    return { type: error instanceof TypeError, created: window.__urls.created.length };
  }
}"""


# --- a reference cut of a calendar, in Python ------------------------------------------------


@dataclass(frozen=True)
class Parts:
    """The top-level parts of a calendar, each as its physical lines."""

    properties: list[tuple[str, list[str]]]
    timezones: list[list[str]]
    events: list[tuple[str, list[str]]]


def property_name(line: str) -> str:
    return re.split(r"[;:]", line, maxsplit=1)[0].upper()


def site_uid(lines: list[str]) -> str:
    """The UID of a VEVENT without nested components, up to its last "@"."""
    (value,) = UID_LINE.findall(UNFOLD.sub("", "\n".join(lines)))
    return value.rpartition("@")[0] or value


def parts(text: str) -> Parts:
    """Split a calendar with CRLF line endings into its top-level parts."""
    lines = text.split(CRLF)
    assert lines.pop() == ""
    assert (lines[0], lines[-1]) == ("BEGIN:VCALENDAR", "END:VCALENDAR")
    found = Parts(properties=[], timezones=[], events=[])
    block: list[str] = []
    depth = 0
    for line in lines[1:-1]:
        if depth == 0 and not line.startswith("BEGIN:"):
            if line.startswith((" ", "\t")):
                found.properties[-1][1].append(line)
            else:
                found.properties.append((property_name(line), [line]))
            continue
        block.append(line)
        depth += line.startswith("BEGIN:") - line.startswith("END:")
        if depth == 0:
            if block[0] == "BEGIN:VTIMEZONE":
                found.timezones.append(block)
            elif block[0] == "BEGIN:VEVENT":
                found.events.append((site_uid(block), block))
            block = []
    return found


def expected_calendar(feed: str, uids: Iterable[str]) -> str:
    """The calendar of the talks ``uids`` that calendar.js has to cut out of ``feed``."""
    found, wanted = parts(feed), set(uids)
    lines = [
        "BEGIN:VCALENDAR",
        *(line for name, kept in found.properties if name in KEPT for line in kept),
        *(line for timezone in found.timezones for line in timezone),
        *(line for uid, event in found.events if uid in wanted for line in event),
        "END:VCALENDAR",
    ]
    return CRLF.join(lines) + CRLF


def block_of(feed: str, uid: str) -> str:
    """The VEVENT of ``uid`` in ``feed``, as the text it is stored as."""
    (lines,) = [lines for found, lines in parts(feed).events if found == uid]
    return CRLF.join(lines) + CRLF


def crlf_only(text: str) -> bool:
    rest = text.replace(CRLF, "")
    return text.endswith(CRLF) and "\r" not in rest and "\n" not in rest


# --- calendars written by hand ---------------------------------------------------------------


def fold(line: str, continuation: str = " ") -> list[str]:
    """An ASCII content line folded after 75 octets, as RFC 5545 asks."""
    folded = [line[:MAX_LINE_OCTETS]]
    rest = line[MAX_LINE_OCTETS:]
    while rest:
        folded.append(continuation + rest[: MAX_LINE_OCTETS - 1])
        rest = rest[MAX_LINE_OCTETS - 1 :]
    return folded


def event(*inner: str, uid: str | None = "talk", uid_lines: Iterable[str] = ()) -> list[str]:
    """A VEVENT with ``inner`` lines first, then its UID (``uid_lines`` when given)."""
    own = list(uid_lines) or ([] if uid is None else [f"UID:{uid}@{UID_DOMAIN}"])
    return [
        "BEGIN:VEVENT",
        *inner,
        *own,
        "DTSTAMP:20261110T120000Z",
        "DTSTART:20261117T160000Z",
        "DTEND:20261117T170000Z",
        "SUMMARY:A talk",
        "END:VEVENT",
    ]


def calendar(*components: list[str], header: Iterable[str] | None = None) -> str:
    """A calendar of ``components`` with CRLF line endings."""
    if header is None:
        header = ["VERSION:2.0", "PRODID:-//seminars.example.org//tests//EN"]
    lines = ["BEGIN:VCALENDAR", *header, *(line for part in components for line in part)]
    return CRLF.join([*lines, "END:VCALENDAR"]) + CRLF


ALARM = [
    "BEGIN:VALARM",
    "ACTION:DISPLAY",
    "TRIGGER:-PT15M",
    "DESCRIPTION:Reminder",
    f"UID:alarm@{UID_DOMAIN}",
    "END:VALARM",
]


@pytest.fixture
def current(session: Callable[..., Session]) -> Session:
    return session().open("index.html")


@pytest.fixture(scope="module")
def feed(site_dir: Path) -> str:
    """The feed as the site serves it."""
    text = read_text(site_dir / FEED_PATH)
    assert crlf_only(text)
    return text


def cut(current: Session, uids: list[str], text: str | None = None, *, as_set: bool = False) -> Any:
    return current.page.evaluate(CUT, {"text": text, "uids": uids, "asSet": as_set})


# --- tests -------------------------------------------------------------------------------------


def test_the_module_exports_its_four_functions(current: Session) -> None:
    exports = current.page.evaluate(EXPORTS)
    assert exports == ["calendarFor", "fetchCalendar", "parseCalendar", "saveFile"]
    current.assert_clean()


class TestParseCalendar:
    def test_the_served_feed_is_split_into_its_parts(self, current: Session, feed: str) -> None:
        parsed = current.page.evaluate(PARSE_FEED, FEED_PATH)
        reference = parts(feed)
        assert parsed["frozen"]
        assert [found["uid"] for found in parsed["events"]] == SAMPLE_UIDS
        assert [(found["uid"], found["lines"]) for found in parsed["events"]] == reference.events
        assert parsed["timezones"] == reference.timezones
        assert parsed["timezones"]
        assert all(zone[0] == "BEGIN:VTIMEZONE" for zone in parsed["timezones"])
        names = [property_name(kept[0]) for kept in parsed["properties"]]
        assert names == list(KEPT)
        assert parsed["properties"] == [kept for name, kept in reference.properties if name in KEPT]
        assert set(SUBSCRIPTION_ONLY) <= {name for name, _ in reference.properties}
        assert not set(SUBSCRIPTION_ONLY) & set(names)
        current.assert_clean()

    def test_lf_line_endings_give_the_same_parts(self, current: Session, feed: str) -> None:
        with_crlf = current.page.evaluate(PARSE, feed)
        with_lf = current.page.evaluate(PARSE, feed.replace(CRLF, "\n"))
        assert with_lf == with_crlf
        assert len(with_lf["events"]) == len(SAMPLE_UIDS)
        current.assert_clean()

    @pytest.mark.parametrize("continuation", [" ", "\t"], ids=["space", "tab"])
    def test_a_folded_uid_is_read_unfolded_and_kept_folded(
        self, current: Session, continuation: str
    ) -> None:
        folded = fold(f"UID:{LONG_UID}@{UID_DOMAIN}", continuation)
        assert len(folded) == 2
        text = calendar(event(uid_lines=folded), event(uid="short"))
        parsed = current.page.evaluate(PARSE, text)
        assert [found["uid"] for found in parsed["events"]] == [LONG_UID, "short"]
        assert parsed["events"][0]["lines"] == event(uid_lines=folded)
        result = cut(current, [LONG_UID], text)
        assert result["included"] == [LONG_UID]
        assert result["text"] == calendar(event(uid_lines=folded))
        assert validate_ics(result["text"]) == []
        current.assert_clean()

    def test_an_alarm_stays_inside_its_event(self, current: Session) -> None:
        """The UID of the alarm, before the UID of the event, is not the event's."""
        text = calendar(event(*ALARM, uid="talk"), event(uid="other"))
        parsed = current.page.evaluate(PARSE, text)
        assert [found["uid"] for found in parsed["events"]] == ["talk", "other"]
        assert parsed["events"][0]["lines"] == event(*ALARM, uid="talk")
        result = cut(current, ["talk", "alarm"], text)
        assert (result["included"], result["missing"]) == (["talk"], ["alarm"])
        assert result["text"] == calendar(event(*ALARM, uid="talk"))
        assert validate_ics(result["text"]) == []
        current.assert_clean()

    def test_the_site_uid_is_the_uid_up_to_its_last_at_sign(self, current: Session) -> None:
        text = calendar(
            event(uid_lines=["UID:no-domain"]),
            event(uid_lines=["UID:a@b@seminars.example.org"]),
            event(uid_lines=['UID;X-NOTE="a:b@c":quoted@other.example.net']),
            event(uid_lines=["uid:lower-case@seminars.example.org"]),
        )
        parsed = current.page.evaluate(PARSE, text)
        uids = [found["uid"] for found in parsed["events"]]
        assert uids == ["no-domain", "a@b", "quoted", "lower-case"]
        result = cut(current, ["no-domain"], text)
        assert result["included"] == ["no-domain"]
        assert result["text"] == calendar(event(uid_lines=["UID:no-domain"]))
        current.assert_clean()

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("", "holds no VCALENDAR"),
            (CRLF.join(event()) + CRLF, "starts with BEGIN:VCALENDAR"),
            ("not a calendar" + CRLF, "not a content line"),
            (" UID:x" + CRLF + calendar(event()), "continues no line"),
            (calendar(event()).removesuffix("END:VCALENDAR" + CRLF), "never closed"),
            (calendar(event()[:-1]), "does not close BEGIN:VEVENT"),
            (calendar(["BEGIN:VEVENT", "END:VTIMEZONE"]), "does not close BEGIN:VEVENT"),
            (calendar(["END:VEVENT"]), "does not close BEGIN:VCALENDAR"),
            (calendar(event()) + calendar(event()), "content after END:VCALENDAR"),
            (calendar(event(uid=None)), "exactly one UID"),
            (calendar(event(uid_lines=["UID:a@x.example", "UID:b@x.example"])), "exactly one UID"),
            (calendar(event(uid_lines=[f"UID:@{UID_DOMAIN}"])), "exactly one UID"),
            (calendar(event(), header=["PRODID:-//x//EN"]), "no VERSION"),
            (calendar(event(), header=["VERSION:2.0"]), "no PRODID"),
        ],
        ids=[
            "empty",
            "no-calendar",
            "not-a-content-line",
            "continuation-first",
            "calendar-never-closed",
            "event-never-closed",
            "end-of-another-component",
            "end-without-begin",
            "two-calendars",
            "event-without-uid",
            "event-with-two-uids",
            "empty-site-uid",
            "no-version",
            "no-prodid",
        ],
    )
    def test_text_that_is_not_one_calendar_throws(
        self, current: Session, text: str, reason: str
    ) -> None:
        thrown = current.page.evaluate(THROWN, text)
        assert thrown is not None
        assert thrown["error"]
        assert reason in thrown["message"]
        current.assert_clean()

    def test_only_text_is_parsed(self, current: Session) -> None:
        thrown = current.page.evaluate(THROWN, None)
        assert thrown == {"error": True, "message": "a calendar is text"}
        current.assert_clean()


class TestCalendarFor:
    def test_chosen_talks_are_copied_out_of_the_served_feed(
        self, current: Session, feed: str
    ) -> None:
        requested = ["sample-tba", "sample-xss", "gone-b", "sample-xss", "gone-a", "sample-pick"]
        result = cut(current, requested)
        assert result["frozen"]
        assert result["included"] == ["sample-xss", "sample-pick", "sample-tba"]
        assert result["missing"] == ["gone-b", "gone-a"]
        text = result["text"]
        assert text == expected_calendar(feed, requested)
        assert validate_ics(text) == []
        assert crlf_only(text)
        for uid in result["included"]:
            assert block_of(feed, uid) in text
        assert [uid for uid, _ in parts(text).events] == result["included"]
        names = {name for name, _ in parts(text).properties}
        assert names == set(KEPT)
        current.assert_clean()

    def test_every_talk_gives_the_feed_without_its_subscription_properties(
        self, current: Session, feed: str
    ) -> None:
        result = cut(current, list(reversed(SAMPLE_UIDS)))
        assert result["included"] == SAMPLE_UIDS
        assert result["missing"] == []
        kept = [
            line
            for name, lines in parts(feed).properties
            if name not in SUBSCRIPTION_ONLY
            for line in lines
        ]
        header = CRLF.join(["BEGIN:VCALENDAR", *kept]) + CRLF
        body = feed[feed.index("BEGIN:VTIMEZONE") :]
        assert result["text"] == header + body
        assert validate_ics(result["text"]) == []
        current.assert_clean()

    def test_cancelled_and_tentative_talks_are_copied_too(
        self, current: Session, feed: str
    ) -> None:
        requested = ["sample-cancelled", "sample-tentative-physics"]
        result = cut(current, requested)
        assert result["included"] == requested
        assert "STATUS:CANCELLED" in result["text"]
        assert "STATUS:TENTATIVE" in result["text"]
        assert result["text"] == expected_calendar(feed, requested)
        current.assert_clean()

    def test_uids_may_come_as_a_set(self, current: Session, feed: str) -> None:
        result = cut(current, ["sample-arc", "sample-online"], as_set=True)
        assert result["included"] == ["sample-online", "sample-arc"]
        assert result["text"] == expected_calendar(feed, ["sample-arc", "sample-online"])
        current.assert_clean()

    def test_lf_line_endings_give_the_same_file(self, current: Session, feed: str) -> None:
        requested = ["sample-hybrid-med", "sample-all-day"]
        with_lf = cut(current, requested, feed.replace(CRLF, "\n"))
        assert with_lf["text"] == expected_calendar(feed, requested)
        assert crlf_only(with_lf["text"])
        current.assert_clean()

    @pytest.mark.parametrize(
        ("requested", "missing"),
        [
            (["gone", "also-gone", "gone"], ["gone", "also-gone"]),
            ([], []),
            ([f"sample-xss@{UID_DOMAIN}"], [f"sample-xss@{UID_DOMAIN}"]),
        ],
        ids=["unknown", "nothing", "full-uid"],
    )
    def test_no_talk_of_the_feed_gives_no_text(
        self, current: Session, requested: list[str], missing: list[str]
    ) -> None:
        result = cut(current, requested)
        assert result == {"text": None, "included": [], "missing": missing, "frozen": True}
        current.assert_clean()


class TestFetchCalendar:
    def test_the_feed_is_fetched_as_served(self, current: Session, feed: str) -> None:
        page = current.page
        page.evaluate(RECORD_FETCHES)
        assert page.evaluate(FETCH, FEED_PATH) == {"text": feed}
        # Revalidated with the server, not taken from the HTTP cache, and given a time limit.
        fetches = page.evaluate("window.__fetches")
        assert fetches == [{"path": f"/{FEED_PATH}", "cache": "no-cache", "signal": True}]
        current.assert_clean()

    def test_a_feed_that_does_not_arrive_in_time_rejects(
        self, session: Callable[..., Session]
    ) -> None:
        current = paused(session)
        page = current.page
        held: list[Any] = []
        page.route(f"**/{FEED_PATH}", lambda route: held.append(route))
        with page.expect_request(f"**/{FEED_PATH}"):
            page.evaluate(START_FETCH, FEED_PATH)
        page.clock.run_for(FETCH_TIMEOUT_MS - 1)
        assert page.evaluate("window.__fetched") is None
        page.clock.run_for(1)
        page.wait_for_function("window.__fetched !== null")
        assert page.evaluate("window.__fetched") == {
            "error": True,
            "message": f"{FEED_PATH}: no answer within {FETCH_TIMEOUT_MS} ms",
        }
        assert len(held) == 1
        current.assert_clean()

    @pytest.mark.parametrize(
        "path",
        [
            "events.json",
            "feeds/all.json",
            "feeds/all.ics?x=1",
            "feeds/all.ics#top",
            "feeds/all.ics\n",
            " feeds/all.ics",
            "/feeds/all.ics",
            "./feeds/all.ics",
            "feeds/../events.json",
            "feeds/sub/all.ics",
            "feeds/All.ics",
            "feeds/-all.ics",
            "feeds/.ics",
            "//other.example/feeds/all.ics",
            "https://other.example/feeds/all.ics",
            "http://127.0.0.2/feeds/all.ics",
            "javascript:alert(1)//feeds/all.ics",
            None,
        ],
    )
    def test_anything_but_a_feed_of_the_site_is_refused_unfetched(
        self, current: Session, path: str | None
    ) -> None:
        page = current.page
        page.evaluate(EXPORTS)  # the module is loaded before requests are counted
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        refused = page.evaluate(FETCH, path)
        assert refused["error"]
        assert "feeds/<id>.ics" in refused["message"]
        assert requests == []
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "status"),
        [("feeds/gone.ics", 404), (FEED_PATH, 503)],
        ids=["missing", "server-error"],
    )
    def test_an_answer_that_is_not_ok_rejects(
        self, current: Session, path: str, status: int
    ) -> None:
        if status != 404:
            current.page.route(f"**/{path}", lambda route: route.fulfill(status=status, body=""))
        refused = current.page.evaluate(FETCH, path)
        assert refused == {"error": True, "message": f"{path}: HTTP status {status}"}
        assert current.page_errors == []
        assert current.dialogs == []
        assert current.foreign_requests == []
        assert current.violations() == []


class TestSaveFile:
    @pytest.mark.parametrize("type_", [None, "text/plain;charset=utf-8"], ids=["default", "given"])
    def test_the_file_is_downloaded_with_its_name_and_text(
        self, current: Session, feed: str, tmp_path: Path, type_: str | None
    ) -> None:
        page = current.page
        text = cut(current, ["sample-tba", "sample-pick"])["text"]
        assert not text.isascii()  # the accented name of the TBA talk
        page.evaluate(TRACK_URLS)
        with page.expect_download() as info:
            state = page.evaluate(SAVE, {"text": text, "name": FILE_NAME, "type": type_})
        download = info.value
        assert download.suggested_filename == FILE_NAME
        saved = tmp_path / "saved.ics"
        download.save_as(saved)
        assert saved.read_bytes() == text.encode("utf-8")
        assert state["types"] == [type_ or CALENDAR_TYPE]
        assert len(state["created"]) == 1
        assert state["anchors"] == 0
        assert page.url == current.base_url + "index.html"
        current.assert_clean()

    def test_the_object_url_is_revoked_after_the_click(
        self, session: Callable[..., Session]
    ) -> None:
        current = paused(session)
        page = current.page
        page.evaluate(TRACK_URLS)
        with page.expect_download() as info:
            state = page.evaluate(SAVE, {"text": "x", "name": FILE_NAME, "type": None})
        info.value.path()
        assert len(state["created"]) == 1
        assert state["revoked"] == []
        page.clock.run_for(1)
        assert page.evaluate("window.__urls.revoked") == []
        page.clock.run_for(60 * 1000)
        assert page.evaluate("window.__urls.revoked") == state["created"]
        current.assert_clean()

    @pytest.mark.parametrize("name", ["", None], ids=["empty", "none"])
    def test_a_file_without_a_name_is_refused(self, current: Session, name: str | None) -> None:
        current.page.evaluate(TRACK_URLS)
        refused = current.page.evaluate(SAVE_REFUSED, {"text": "x", "name": name})
        assert refused == {"type": True, "created": 0}
        current.assert_clean()
