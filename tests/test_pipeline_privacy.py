"""Tests for the privacy rules of normalization: credentials, e-mail addresses, telephones.

Values that look real are assembled in the tests, so that this file holds
no meeting code, address or number that a scanner of the repository would
report. The Localist recordings already carry placeholders (``REDACTED``,
``redacted@stanford.edu``, zeros); the tests on them assert the structure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT
from factories import REFERENCE_NOW, make_draft, make_source, make_window
from seminars.config import Settings, load_settings
from seminars.models import dump_events
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.pipeline.privacy import (
    EMAIL_REMOVED,
    LINK_REMOVED,
    PHONE_REMOVED,
    VALUE_REMOVED,
    has_private_address,
    is_private_link,
    scrub_link,
    scrub_private,
    strip_markers,
)
from seminars.pipeline.text import paragraphs

LOCALIST = Path(__file__).parent / "fixtures" / "localist-stanford"
CODE = "48" + "1516"
WORD_CODE = "Zx9" + "LqT4"
PERSON = "jdoe" + "@" + "stanford.edu"
PHONE = "(650) " + "723-" + "4321"
MEETING = "984 " + "6689 " + "7972"
ACCENTED = "Ana\u00efs Vo\u015fki"


def scrub(text: str) -> str:
    return scrub_private(text)


class TestLinks:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                f"Join https://stanford.zoom.us/j/98479731295?pwd={WORD_CODE}.",
                "Join https://stanford.zoom.us/j/98479731295.",
            ),
            (
                f"https://stanford.zoom.us/j/1?pwd={WORD_CODE}&uname=Jane+Doe&from=addon",
                "https://stanford.zoom.us/j/1?from=addon",
            ),
            (
                "https://stanford.zoom.us/j/95136885017?pwd=REDACTED#success",
                "https://stanford.zoom.us/j/95136885017#success",
            ),
            (f"https://stanford.zoom.us/w/99?tk={WORD_CODE}.x", "https://stanford.zoom.us/w/99"),
            (f"https://example.edu/live?Token={WORD_CODE}", "https://example.edu/live"),
            (f"https://example.edu/live?PASS={CODE}&a=1", "https://example.edu/live?a=1"),
            (f"https://example.edu/live#passcode={CODE}", "https://example.edu/live"),
            (f"stanford.zoom.us/j/123?pwd={WORD_CODE} now", "stanford.zoom.us/j/123 now"),
            (
                f"https://stanford.webex.com/stanford/j.php?MTID=m{CODE}abcdef",
                "https://stanford.webex.com/stanford/j.php",
            ),
            (f"(https://zoom.us/j/7?pwd={CODE})", "(https://zoom.us/j/7)"),
        ],
    )
    def test_credentials_leave_a_link_and_the_link_stays(self, text: str, expected: str) -> None:
        assert scrub(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            f"https://user:{WORD_CODE}@example.edu/talk",
            f"https://example.edu/rsvp?email={PERSON}",
            "https://nam10.safelinks.protection.outlook.com/?url=https%3A%2F%2Fx.org"
            "&data=05%7C02%7Credacted%40ufl.edu%7C55df",
        ],
    )
    def test_a_link_with_a_login_or_an_address_goes(self, text: str) -> None:
        assert scrub(f"See {text} today") == f"See {LINK_REMOVED} today"
        assert has_private_address(text)

    @pytest.mark.parametrize("text", ["http://[::1/talk", "http://[fe80::1%25eth0/?pwd=1"])
    def test_a_malformed_link_goes_and_never_raises(self, text: str) -> None:
        assert scrub(f"See {text} today") == f"See {LINK_REMOVED} today"

    @pytest.mark.parametrize(
        "text",
        [
            "https://example.edu/a?b=1&c=2",
            "https://arxiv.org/abs/2401.12345",
            "https://med.stanford.edu/anesthesia/education/grand-rounds.html",
            "https://stanford.zoom.us/j/98466897972",
            "https://medium.com/@someone/post",
            "see example.edu/talks/1 or e.g./i.e. and 1.5/2",
        ],
    )
    def test_other_links_are_untouched(self, text: str) -> None:
        assert scrub(text) == text
        assert not has_private_address(text)
        assert not is_private_link(text)

    @pytest.mark.parametrize(
        "text",
        [
            f"https://www.google.com/url?q=https://stanford.zoom.us/j/912?pwd%3D{WORD_CODE}&sa=D",
            "https://www.google.com/url?q=https%3A%2F%2Fstanford.zoom.us%2Fj%2F912%3Fpwd%3D"
            f"{WORD_CODE}&sa=D&ust=1700000000000000",
            "https://www.google.com/url?q=https%253A%252F%252Fstanford.zoom.us%252Fj%252F912"
            f"%253Fpwd%253D{WORD_CODE}",
            "https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fstanford.zoom.us"
            f"%2Fj%2F912%3Fpwd%3D{WORD_CODE}&data=05%7C02%7C%7C55df&reserved=0",
            "https://urldefense.proofpoint.com/v2/url?u=https-3A__stanford.zoom.us_j_9123-3Fpwd-3D"
            f"{WORD_CODE}&d=DwMFaQ&c=abc&r=def&m=ghi&s=jkl&e=",
            "https://urldefense.proofpoint.com/v2/url?u=https-3A__example.edu_live-26pass-3D"
            f"{CODE}&d=DwMFaQ&e=",
            f"https://www.google.com/url?q=https://user:{WORD_CODE}@example.edu/talk",
            f"https://urldefense.proofpoint.com/v2/url?u=https-3A__user-3A{WORD_CODE}"
            "-40example.edu_talk&d=x&e=",
            "https://urldefense.proofpoint.com/v2/url?u=https-3A__example.edu_rsvp-3Femail-3D"
            "jdoe-40stanford.edu&d=x&e=",
        ],
    )
    def test_a_link_that_wraps_a_credential_or_an_address_goes(self, text: str) -> None:
        assert scrub(f"Join: {text}.") == f"Join: {LINK_REMOVED}."
        assert is_private_link(text)

    @pytest.mark.parametrize(
        "text",
        [
            "https://www.google.com/url?q=https://example.edu/talks/1&sa=D",
            "https://www.google.com/url?q=https%3A%2F%2Fexample.edu%2Ftalks%3Fpage%3D2&sa=D",
            "https://urldefense.proofpoint.com/v2/url?u=https-3A__example.edu_talks_1&d=x&e=",
            "https://example.edu/a?b=1;c=2",
            "https://example.edu/a?b=1&&c=2",
            "https://example.edu/search?q=pin%3D5",
            "https://example.edu/events/talk-3Fpin-3D",
        ],
    )
    def test_a_link_that_wraps_nothing_private_stays(self, text: str) -> None:
        assert scrub_link(text) == text
        assert not is_private_link(text)

    @pytest.mark.parametrize(
        "text",
        [
            "https://biohub.org/wp-content/uploads/2024/05/adrian-jacobo@2x.png",
            "https://calendar.google.com/calendar/ical/c_abc123%40group.calendar.google.com"
            "/public/basic.ics",
            "https://calendar.google.com/calendar/embed?src=c_abc%40group.calendar.google.com",
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.skype/0",
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0",
        ],
    )
    def test_file_names_calendar_ids_and_chat_threads_are_no_address(self, text: str) -> None:
        assert not has_private_address(text)
        assert scrub(f"See {text} today") == f"See {text} today"

    def test_a_personal_calendar_is_an_address_also_in_the_path(self) -> None:
        text = "https://calendar.google.com/calendar/ical/" + "jdoe%40" + "stanford.edu/basic.ics"
        assert has_private_address(text)

    def test_an_encoded_credential_parameter_goes_and_the_link_stays(self) -> None:
        text = f"https://stanford.zoom.us/j/912?pwd%3D{WORD_CODE}"
        assert scrub(text) == "https://stanford.zoom.us/j/912"
        assert scrub_link(f"https://zoom.us/j/9?a=1&pwd%253D{CODE}") == "https://zoom.us/j/9?a=1"


class TestWrittenCodes:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"Passcode: {CODE}", f"Passcode: {VALUE_REMOVED}"),
            (f"Password: {WORD_CODE}", f"Password: {VALUE_REMOVED}"),
            (f"Password:\u00a0{CODE}", f"Password:\u00a0{VALUE_REMOVED}"),
            (
                f"Meeting ID: {MEETING} Passcode: {CODE}",
                f"Meeting ID: {MEETING} Passcode: {VALUE_REMOVED}",
            ),
            (f"Password if prompted: {CODE}", f"Password if prompted: {VALUE_REMOVED}"),
            (f"Zoom webinar (passcode: {CODE})", f"Zoom webinar (passcode: {VALUE_REMOVED})"),
            (f"Meeting password: {WORD_CODE}!", f"Meeting password: {VALUE_REMOVED}!"),
            ("pwd=REDACTED", f"pwd={VALUE_REMOVED}"),
            (f"The passcode is {CODE}.", f"The passcode is {VALUE_REMOVED}."),
            (f"Passcode {CODE}", f"Passcode {VALUE_REMOVED}"),
            (f"password {WORD_CODE}", f"password {VALUE_REMOVED}"),
            ("PIN: 123 456 789#", f"PIN: {VALUE_REMOVED}"),
            (f"Webinar Pass Code - {CODE}", f"Webinar Pass Code - {VALUE_REMOVED}"),
            (f"passcode\uff1a{CODE}", f"passcode\uff1a{VALUE_REMOVED}"),
            (f'Passcode: "{CODE}"', f"Passcode: {VALUE_REMOVED}"),
            (f"Password: '{WORD_CODE}'.", f"Password: {VALUE_REMOVED}."),
            (f"Passcode: \u201c{CODE}\u201d", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: \u2018{WORD_CODE}\u2019", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: ({CODE})", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: [{CODE}]", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: <{CODE}>", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: {{{CODE}}}", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode \u2013 {CODE}", f"Passcode \u2013 {VALUE_REMOVED}"),
            (f"Passcode\u2014{CODE}", f"Passcode\u2014{VALUE_REMOVED}"),
            (f"Passcodes: {CODE}", f"Passcodes: {VALUE_REMOVED}"),
            (f"Passwords: {CODE}", f"Passwords: {VALUE_REMOVED}"),
            (
                f"The passcode for the meeting is {CODE}.",
                f"The passcode for the meeting is {VALUE_REMOVED}.",
            ),
            (f"Passcode: {CODE[:2]} {CODE[2:4]} {CODE[4:]}", f"Passcode: {VALUE_REMOVED}"),
            (f"Passcode: {CODE} 2 pm", f"Passcode: {VALUE_REMOVED} 2 pm"),
            (f"PIN: {CODE[:4]}", f"PIN: {VALUE_REMOVED}"),
            (
                f"Passcode (case sensitive): {WORD_CODE}",
                f"Passcode (case sensitive): {VALUE_REMOVED}",
            ),
        ],
    )
    def test_the_code_goes_and_the_label_stays(self, text: str, expected: str) -> None:
        assert scrub(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Password: abc", f"Password: {VALUE_REMOVED}"),
            ("Password: seminar", f"Password: {VALUE_REMOVED}"),
            ("Password = seminar", f"Password = {VALUE_REMOVED}"),
            ("pw: seminar", f"pw: {VALUE_REMOVED}"),
            (
                f"Meeting ID: {MEETING}\nPassword: colloquium",
                f"Meeting ID: {MEETING}\nPassword: {VALUE_REMOVED}",
            ),
            (
                f"Meeting ID: {MEETING} Password: colloquium",
                f"Meeting ID: {MEETING} Password: {VALUE_REMOVED}",
            ),
            (
                "Zoom link: https://stanford.zoom.us/j/912 Password: physics",
                f"Zoom link: https://stanford.zoom.us/j/912 Password: {VALUE_REMOVED}",
            ),
            (
                "Password: physics https://stanford.zoom.us/j/912",
                f"Password: {VALUE_REMOVED} https://stanford.zoom.us/j/912",
            ),
            ("The meeting password is stanford", f"The meeting password is {VALUE_REMOVED}"),
            ("Passcode 'abc'", f"Passcode {VALUE_REMOVED}"),
            ("Use password stanford to join", f"Use password {VALUE_REMOVED} to join"),
            (
                "Password: seminar. Join five minutes early.",
                f"Password: {VALUE_REMOVED}. Join five minutes early.",
            ),
            ("(Password: seminar)", f"(Password: {VALUE_REMOVED})"),
            (
                "Password: seminar, Passcode: physics",
                f"Password: {VALUE_REMOVED}, Passcode: {VALUE_REMOVED}",
            ),
        ],
    )
    def test_a_code_without_a_digit_goes_where_it_ends_the_line(
        self, text: str, expected: str
    ) -> None:
        assert scrub(text) == expected

    def test_written_codes_are_scrubbed_in_every_field(self) -> None:
        cases = [
            f'Passcode: "{CODE}"',
            f"Password: '{WORD_CODE}'",
            f"Passcode: ({CODE})",
            f"Passcode \u2013 {CODE}",
            f"Passcodes: {CODE}",
            f"passcode for the meeting is {CODE}",
            f"Passcode: {CODE[:2]} {CODE[2:4]} {CODE[4:]}",
        ]
        for case in cases:
            draft = make_draft(title=f"Seminar {case}", abstract=case, location=f"Zoom, {case}")
            (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
            dumped = dump_events([candidate.event])
            for value in (CODE, CODE[2:4] + " " + CODE[4:], WORD_CODE):
                assert value not in dumped, case

    @pytest.mark.parametrize(
        "text",
        [
            "A talk on password hashing and passkeys",
            "We show that the password is the weakest link",
            "Password managers: usability and security",
            "Pin Chen (MIT) and Pat Pin speak on spin: 1/2 systems",
            "Students who pass the exam",
            "Meeting ID: 984 6689 7972",
            "Webinar ID 912 345 6789",
            "PIN: auxin efflux carriers in root development",
            "PIN: 2 roles in auxin transport",
            "Choosing the password: a history",
            "The passcode for the meeting is on the course page.",
            "Zoom password protected.",
            "The meeting password is required",
            "Passwords: past, present and future",
            "PIN (protein interaction network): a model of signaling",
            "The passcode is in the calendar invite",
            "Password: the weakest link",
            "PW: Physics Week",
        ],
    )
    def test_ordinary_text_and_meeting_ids_stay(self, text: str) -> None:
        assert scrub(text) == text


class TestAddresses:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"Contact {PERSON} for details", f"Contact {EMAIL_REMOVED} for details"),
            (f"Questions: mailto:{PERSON}", f"Questions: {EMAIL_REMOVED}"),
            ("jane.doe+talks" + "@cs.stanford.edu.", f"{EMAIL_REMOVED}."),
            ("jdoe [at] stanford [dot] edu", EMAIL_REMOVED),
            (
                "jdoe(at)stanford.edu or jdoe {at} cs {dot} stanford {dot} edu",
                f"{EMAIL_REMOVED} or {EMAIL_REMOVED}",
            ),
            (f"Jane Doe ({PERSON}), MIT", "Jane Doe, MIT"),
            (f"Jane Doe <{PERSON}>", "Jane Doe"),
            ("Join by SIP 98479731295" + "@zoomcrc.com", f"Join by SIP {EMAIL_REMOVED}"),
            ("redacted@stanford.edu", EMAIL_REMOVED),
            ("jdoe at stanford dot edu", EMAIL_REMOVED),
            ("Write to jane.doe AT cs DOT stanford DOT edu.", f"Write to {EMAIL_REMOVED}."),
            (f"E-mail: {PERSON}", f"E-mail: {EMAIL_REMOVED}"),
            ("jdoe @ stanford.edu", EMAIL_REMOVED),
            ("Contact: jdoe\uff20stanford.edu", f"Contact: {EMAIL_REMOVED}"),
            ("Contact: jdoe at stanford.edu", f"Contact: {EMAIL_REMOVED}"),
            ("Email jdoe at cs.stanford.edu.", f"Email {EMAIL_REMOVED}."),
            ("RSVP to jdoe at stanford.edu", f"RSVP to {EMAIL_REMOVED}"),
            ("Jane Doe (j.doe at stanford.edu)", f"Jane Doe ({EMAIL_REMOVED})"),
            ("jdoe2 at stanford.edu", EMAIL_REMOVED),
        ],
    )
    def test_every_address_goes(self, text: str, expected: str) -> None:
        assert scrub(text) == expected

    def test_words_joined_by_a_removed_address_are_scrubbed_too(self) -> None:
        text = f"pass ({PERSON}) code: {CODE}"
        assert scrub(text) == f"pass code: {VALUE_REMOVED}"

    @pytest.mark.parametrize(
        "text",
        [
            "Follow @StanfordHAI",
            "user @ host",
            "f@g in category theory",
            "Aim at stanford dot",
            "Slides are available at arxiv.org and at example dot",
            "We meet at Clark dot Center",
            "Silicon Valley at the dot com boom",
            "Register at events.stanford.edu",
            "Slides at cs.stanford.edu/talks",
            "Contact info at events.stanford.edu",
            "Contact us at stanford.edu.",
            "photo: adrian-jacobo@2x.png",
            "Subscribe to c_abc123@group.calendar.google.com",
            "Thread 19:meeting_x@thread.skype",
        ],
    )
    def test_things_that_are_not_addresses_stay(self, text: str) -> None:
        assert scrub(text) == text


class TestTelephones:
    @pytest.mark.parametrize(
        "number",
        [
            PHONE,
            "650-723-" + "4321",
            "650.723." + "4321",
            "+1 650 723 " + "4321",
            "+1650723" + "4321",
            "1-800-555-" + "0199",
            "+44 20 7946 " + "0958",
            "+86 10 1234 " + "5678",
            "(650)723-" + "4321 ext. 5",
            "+16507249799,,98479731295#,,,,*" + CODE + "#",
            "(000) 000-0000",
            "650723" + "4321",
            "650-723" + "4321",
            "650 723" + "4321",
            "(650) 723" + "4321",
        ],
    )
    def test_numbers_go(self, number: str) -> None:
        assert scrub(f"Reach us at {number} or come by") == (
            f"Reach us at {PHONE_REMOVED} or come by"
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Phone: 3-" + "4321", f"Phone: {PHONE_REMOVED}"),
            ("Tel. 650 723" + "4321.", f"Tel. {PHONE_REMOVED}."),
            ("tel:+1650723" + "4321", f"tel:{PHONE_REMOVED}"),
            ("Fax: 650.725." + "0000", f"Fax: {PHONE_REMOVED}"),
            ("text code to (000) 000-0000 to confirm", f"text code to {PHONE_REMOVED} to confirm"),
            ("Cell: 723 " + "4321", f"Cell: {PHONE_REMOVED}"),
            ("cell phone 723 " + "4321", f"cell phone {PHONE_REMOVED}"),
            ("Contact Jane Doe at 723-" + "4321", f"Contact Jane Doe at {PHONE_REMOVED}"),
            ("Call Ann at 723." + "4321.", f"Call Ann at {PHONE_REMOVED}."),
            ("Questions? Reach Ann at 723-" + "4321", f"Questions? Reach Ann at {PHONE_REMOVED}"),
            ("contact 723-" + "4321", f"contact {PHONE_REMOVED}"),
            ("Contact: 650-723-" + "4321", f"Contact: {PHONE_REMOVED}"),
        ],
    )
    def test_numbers_after_a_cue_go(self, text: str, expected: str) -> None:
        assert scrub(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Tuesday, 2026-11-17, 4:00-5:00pm",
            "Fellows 2019-2021 and the 2026-2027 cycle",
            "doi:10.1038/s41586-021-03819-2 and arXiv:2401.12345",
            "Meeting ID: 912 345 6789",
            "Meeting number (access code): 2620 123 4567",
            "pH 10.5-11.5 buffer",
            "Call for papers 2026; Call: 2026-2027",
            "Room 380-381T, Building 160-325",
            "H.323: 162.255.37.11 (US West)",
            "ISBN 978-0-306-40615-7",
            "Cell (2019) 176: 1234-1245",
            "In Cell 2019 2020 and Nature 2021",
            "Cell. 2019;176(5):1234-1245",
            "ISBN 0306406152, timestamp 1700000000",
            "NCT0123456789 and grant R01GM1234567",
            "https://stanford.zoom.us/j/9123456789",
            "Webinar ID: 9123456789",
            "about 1.5551234567 units",
            "Lunch at 930-1130",
            "We meet at 723-4321 Gates",
            "contact angle 120 degrees and contact 2019-2021",
            "ISBN 978 0131103627",
            "Room 650 1234 5678",
        ],
    )
    def test_dates_ids_and_numbers_of_science_stay(self, text: str) -> None:
        assert scrub(text) == text


class TestStripMarkers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"Jane Doe, {PERSON}", "Jane Doe"),
            (f"Stanford, {PERSON}", "Stanford"),
            (f"{PERSON}, Stanford", "Stanford"),
            (f"Stanford ({PERSON}, {PHONE})", "Stanford"),
            (f"Stanford, E-mail: {PERSON}, Phone: {PHONE}", "Stanford"),
            (f"Phone: {PHONE}", ""),
            (f"Stanford, https://example.edu/rsvp?email={PERSON}", "Stanford"),
            (f"MIT; passcode: {CODE}", "MIT"),
            (f"Department of Chemistry,\n{PERSON}\nMIT", "Department of Chemistry\nMIT"),
            ("Jane Doe, Stanford", "Jane Doe, Stanford"),
            ("Stanford,\n(MIT)", "Stanford,\n(MIT)"),
        ],
    )
    def test_markers_go_with_their_cues(self, text: str, expected: str) -> None:
        assert strip_markers(scrub(text)) == expected

    def test_text_without_a_marker_is_unchanged(self) -> None:
        for text in ("Jane Doe", " Stanford, ", "Contact:", "(MIT)", ACCENTED):
            assert strip_markers(text) == text


class TestZoomInvitation:
    INVITATION = (
        "Join Zoom Meeting\n"
        f"https://stanford.zoom.us/j/98466897972?pwd={WORD_CODE}\n"
        f"Meeting ID: {MEETING}\n"
        f"Passcode: {CODE}\n"
        "One tap mobile\n"
        f"+16507249799,,98466897972#,,,,*{CODE}# US (San Jose)\n"
        "Dial by your location\n"
        "+1 650 724 9799 US (San Jose)\n"
        "+1 833 302 1841 US Toll-free\n"
        f"Meeting ID: {MEETING}\n"
        f"Find your local number: https://stanford.zoom.us/u/{WORD_CODE}\n"
        "Join by SIP\n"
        "98466897972@zoomcrc.com\n"
    )

    def test_only_the_meeting_and_how_to_find_it_are_left(self) -> None:
        result = scrub(self.INVITATION)
        assert CODE not in result
        assert result.count(VALUE_REMOVED) == 1
        assert result.count(PHONE_REMOVED) == 3
        assert result.count(MEETING) == 2
        assert "https://stanford.zoom.us/j/98466897972\n" in result
        assert "zoomcrc" not in result
        assert result.splitlines()[5] == f"{PHONE_REMOVED} US (San Jose)"


def localist_records() -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(LOCALIST.glob("events-*-page*.json")):
        for item in json.loads(path.read_text(encoding="utf-8"))["events"]:
            event = item["event"]
            yield str(event["event_instances"][0]["event_instance"]["id"]), event


RECORDS = dict(localist_records())
PRIVATE = {
    "54004063149104": "stream_url with pwd",
    "54445649547777": "pwd link and Password: in stream_info",
    "54435561022415": "pwd= as text, Passcode:, a mailto address",
    "54420408876947": "pwd links in description and stream_info",
    "54435582365420": "Password if prompted:",
    "54294379845960": "(passcode: ...) and a pwd link",
    "54287642110827": "two addresses and a telephone number",
}


def texts(record: dict[str, Any]) -> list[str]:
    """Every text of a record as normalization sees it (HTML fields as text)."""
    found = [record.get("title") or "", record.get("description_text") or ""]
    for name in ("description", "stream_info"):
        found.append(paragraphs(record.get(name), limit=20000, is_html=True) or "")
    found.append(record.get("stream_url") or "")
    return found


class TestLocalistRecordings:
    @pytest.mark.parametrize("instance", sorted(PRIVATE))
    def test_the_recorded_cases_are_present(self, instance: str) -> None:
        assert instance in RECORDS

    @pytest.mark.parametrize("instance", sorted(PRIVATE))
    def test_nothing_private_is_left(self, instance: str) -> None:
        for text in texts(RECORDS[instance]):
            result = scrub(text)
            assert "pwd=" not in result.replace(f"pwd={VALUE_REMOVED}", "")
            assert "REDACTED" not in result
            assert "redacted" not in result.casefold()
            assert "000000" not in result
            assert "(000) 000-0000" not in result
            assert scrub(result) == result

    def test_meeting_ids_and_links_survive(self) -> None:
        stream = paragraphs(RECORDS["54445649547777"]["stream_info"], limit=9000, is_html=True)
        result = scrub(stream or "")
        assert "Zoom link: https://stanford.zoom.us/j/98479731295\n" in result
        assert "Meeting ID: 98479731295" in result
        assert f"Password: {VALUE_REMOVED}" in result
        info = paragraphs(RECORDS["54435561022415"]["stream_info"], limit=9000, is_html=True)
        assert scrub(info or "").splitlines() == [
            f"Zoom Link: https://stanford.zoom.us/j/98466897972 pwd={VALUE_REMOVED}",
            "Meeting ID: 984 6689 7972",
            f"Passcode: {VALUE_REMOVED}",
        ]

    def test_contact_lines_lose_addresses_and_numbers(self) -> None:
        text = RECORDS["54287642110827"]["description_text"]
        assert scrub(text).endswith(
            f"please contact {EMAIL_REMOVED} or Vivian Sming, {EMAIL_REMOVED}, {PHONE_REMOVED}."
        )

    def test_every_recorded_text_is_scrubbed_idempotently(self) -> None:
        for record in RECORDS.values():
            for text in texts(record):
                once = scrub(text)
                assert scrub(once) == once
                assert "pwd=REDACTED" not in once
                assert "redacted@" not in once


SETTINGS: Settings = load_settings(REPO_ROOT / "config" / "settings.yaml")


def context() -> NormalizeContext:
    return NormalizeContext(settings=SETTINGS, now=REFERENCE_NOW, window=make_window())


class TestNormalization:
    def test_every_free_text_field_of_an_event_is_scrubbed(self) -> None:
        draft = make_draft(
            title=f"Seminar (RSVP {PERSON})",
            abstract=f"Zoom https://stanford.zoom.us/j/1?pwd={WORD_CODE} Passcode: {CODE}",
            location=f"Room 101, call {PHONE}",
            series=f"Series, questions to {PERSON}",
            host_org=f"Physics (tel. {PHONE})",
            speakers=[{"name": "Pin Chen", "affiliation": f"MIT, {PERSON}"}],
            speaker_text=f"{ACCENTED} ({PERSON})",
            categories=[f"Contact {PERSON}"],
            abstract_html=None,
        )
        result = normalize_drafts([draft], make_source(), context())
        (candidate,) = result.candidates
        dumped = dump_events([candidate.event]) + candidate.hints.model_dump_json()
        for value in (PERSON, CODE, WORD_CODE, "723-4321", "pwd="):
            assert value not in dumped
        event = candidate.event
        assert [speaker.name for speaker in event.speakers] == ["Pin Chen", ACCENTED]
        assert event.speakers[0].affiliation == "MIT"
        assert event.speakers[1].affiliation is None
        assert event.abstract == (f"Zoom https://stanford.zoom.us/j/1 Passcode: {VALUE_REMOVED}")
        assert result.redactions >= 7

    def test_html_abstracts_are_scrubbed_after_the_markup_is_gone(self) -> None:
        html = (
            f'<p>Questions: <a href="mailto:{PERSON}">{PERSON}</a></p>'
            f'<p>Zoom: <a href="https://zoom.us/j/9?pwd={CODE}">https://zoom.us/j/9?pwd={CODE}</a>'
            f"</p><p>Password:&nbsp;{CODE}</p>"
        )
        draft = make_draft(abstract=None, abstract_html=html)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert candidate.event.abstract == (
            f"Questions: {EMAIL_REMOVED}\nZoom: https://zoom.us/j/9\nPassword: {VALUE_REMOVED}"
        )

    def test_a_stream_address_with_any_credential_is_dropped(self) -> None:
        streams = [
            f"https://stanford.webex.com/stanford/j.php?MTID=m{CODE}&pass={CODE}",
            f"https://stanford.zoom.us/j/912?pwd%3D{WORD_CODE}",
            f"https://www.google.com/url?q=https://stanford.zoom.us/j/912?pwd%3D{WORD_CODE}&sa=D",
        ]
        for stream in streams:
            draft = make_draft(stream_url=stream, location="Zoom")
            result = normalize_drafts([draft], make_source(), context())
            (candidate,) = result.candidates
            assert candidate.event.stream_url is None, stream
            assert candidate.event.is_online
            assert result.redactions >= 1

    def test_an_event_address_with_a_credential_gives_way_to_the_source(self) -> None:
        page = f"https://example.edu/talk?pin={CODE[:4]}&x=1"
        draft = make_draft(url=page, native_id=None)
        source = make_source(homepage="https://bioengineering.stanford.edu/events")
        (candidate,) = normalize_drafts([draft], source, context()).candidates
        event = candidate.event
        assert str(event.url) == "https://bioengineering.stanford.edu/events"
        assert [str(url) for url in event.source_urls] == [str(event.url)]
        (key,) = event.source_ids
        assert key.startswith("sites-bioengineering:day:")
        assert CODE[:4] not in dump_events([event])

    def test_a_speaker_address_with_a_credential_is_dropped(self) -> None:
        speaker = {"name": "Jane Doe", "url": f"https://example.edu/people/jdoe?pw={WORD_CODE}"}
        draft = make_draft(speaker_text=None, speakers=[speaker])
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        (person,) = candidate.event.speakers
        assert (person.name, person.url) == ("Jane Doe", None)

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (f"Jane Doe, {PERSON}", [("Jane Doe", None)]),
            (f"Jane Doe (Stanford), {PERSON}", [("Jane Doe", "Stanford")]),
            (f"Jane Doe, Stanford, E-mail: {PERSON}", [("Jane Doe", "Stanford")]),
            (f"Contact: {PHONE}", []),
            (f"Jane Doe (Stanford); Contact: {PHONE}", [("Jane Doe", "Stanford")]),
            (f"Jane Doe (Stanford, {PERSON}, {PHONE})", [("Jane Doe", "Stanford")]),
            (
                f"Jane Doe, Phone: {PHONE}\nJohn Roe (MIT)",
                [("Jane Doe", None), ("John Roe", "MIT")],
            ),
        ],
    )
    def test_a_speaker_line_keeps_no_marker_and_no_label(
        self, line: str, expected: list[tuple[str, str | None]]
    ) -> None:
        draft = make_draft(speaker_text=line)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        found = [(person.name, person.affiliation) for person in candidate.event.speakers]
        assert found == expected

    def test_a_structured_affiliation_keeps_no_marker(self) -> None:
        speakers = [
            {"name": "Jane Doe", "affiliation": f"Stanford, Phone: {PHONE}"},
            {"name": "John Roe", "affiliation": f"E-mail: {PERSON}"},
            {"name": "Contact:", "affiliation": PHONE},
        ]
        draft = make_draft(speaker_text=None, speakers=speakers)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        found = [(person.name, person.affiliation) for person in candidate.event.speakers]
        assert found == [("Jane Doe", "Stanford"), ("John Roe", None)]

    def test_an_address_with_an_e_mail_is_not_used(self) -> None:
        draft = make_draft(url=f"https://example.edu/people/{PERSON}", native_id="7")
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert PERSON not in str(candidate.event.url)

    def test_a_page_with_an_image_name_or_a_calendar_id_is_used(self) -> None:
        pages = [
            "https://biohub.org/wp-content/uploads/adrian-jacobo@2x.png",
            "https://calendar.google.com/calendar/event?eid=x&ctz=c_abc%40group.calendar.google.com",
        ]
        for page in pages:
            draft = make_draft(url=page, native_id="7")
            (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
            assert str(candidate.event.url) == page

    @pytest.mark.parametrize(
        "name",
        [
            "Jane Doe",
            "Pin Chen",
            "Jean-Pierre Serre",
            ACCENTED,
            "Henri Poincar\u00e9",
            "Shing-Tung Yau",
            "O'Neil 3rd",
            "Pat Text",
            "Call Me Maybe",
        ],
    )
    def test_speaker_names_are_never_changed(self, name: str) -> None:
        assert scrub(name) == name
        draft = make_draft(speaker_text=None, speakers=[{"name": name}])
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert [speaker.name for speaker in candidate.event.speakers] == [name]
