"""Tests that scrubbing leaves ordinary titles and abstracts alone.

The privacy rules (tests/test_pipeline_privacy.py) must not damage what a
talk is about: a course at a video site is no address, and a talk on how
people use passwords has no meeting code in its title. Values that look
real are assembled in the tests, as in the other privacy tests.
"""

from __future__ import annotations

import time

import pytest

from conftest import REPO_ROOT
from factories import REFERENCE_NOW, make_draft, make_source, make_window
from seminars.config import Settings, load_settings
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.pipeline.privacy import (
    EMAIL_REMOVED,
    VALUE_REMOVED,
    scrub_private,
    scrub_title,
)

CODE = "48" + "1516"
SETTINGS: Settings = load_settings(REPO_ROOT / "config" / "settings.yaml")


def context() -> NormalizeContext:
    return NormalizeContext(settings=SETTINGS, now=REFERENCE_NOW, window=make_window())


class TestSpelledAddresses:
    @pytest.mark.parametrize(
        "text",
        [
            "Recordings of CS224N at youtube.com.",
            "Registration closes at 5pm at eventbrite.com",
            "Registration closes at 5:30pm at stanford.edu.",
            "Slides will be posted on Nov. 12 at stanford.edu.",
            "Register for EE380 at events.stanford.edu",
            "Course notes for CS229 at cs229.stanford.edu.",
            "Watch lecture 3 at youtube.com or session 2b at zoom.us",
            "Tickets for Day.2 at eventbrite.com",
            "Doors open at 9am at stanford.edu",
            "Materials for CS224N at cs.stanford.edu.",
            "Office hours for CS106B at cs.stanford.edu",
            "Recordings of EE380 at www.stanford.edu",
            "Slides are online, e.g. at stanford.edu.",
            "Watch talk.mp4 at youtu.be",
            "Learn node.js at github.com",
            "Slides for A.B at zoom.us",
        ],
    )
    def test_a_word_at_a_web_host_is_no_address(self, text: str) -> None:
        assert scrub_private(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Jane Doe (jane.doe at cs.stanford.edu)", f"Jane Doe ({EMAIL_REMOVED})"),
            ("Questions: jane.doe at cs.stanford.edu", f"Questions: {EMAIL_REMOVED}"),
            ("jdoe2 at cs.stanford.edu", EMAIL_REMOVED),
            ("jane.doe at med.stanford.edu", EMAIL_REMOVED),
            ("jane.doe at slac.stanford.edu", EMAIL_REMOVED),
            ("jane.doe at csail.mit.edu", EMAIL_REMOVED),
            ("jane.doe at eng.cam.ac.uk", EMAIL_REMOVED),
            ("jane.doe at ethz.ch", EMAIL_REMOVED),
            ("jane.doe at inf.ethz.ch.", f"{EMAIL_REMOVED}."),
            ("j_doe at tuebingen.mpg.de", EMAIL_REMOVED),
            ("Jane.Doe at CS.Stanford.EDU", EMAIL_REMOVED),
            ("Host: Jane Doe, j_doe at genentech.com", f"Host: Jane Doe, {EMAIL_REMOVED}"),
        ],
    )
    def test_a_mailbox_at_a_department_or_a_named_host_goes(self, text: str, expected: str) -> None:
        assert scrub_private(text) == expected
        assert scrub_title(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("jdoe2 at stanford.edu", EMAIL_REMOVED),
            ("Jane Doe (j.doe at stanford.edu)", f"Jane Doe ({EMAIL_REMOVED})"),
            ("Write j_doe at berkeley.edu.", f"Write {EMAIL_REMOVED}."),
            ("jane.doe at gmail.com", EMAIL_REMOVED),
            ("j.doe at ox.ac.uk", EMAIL_REMOVED),
            ("Email jdoe at cs.stanford.edu.", f"Email {EMAIL_REMOVED}."),
            ("Contact: CS224N at youtube.com", f"Contact: {EMAIL_REMOVED}"),
        ],
    )
    def test_a_mailbox_at_a_mail_host_or_after_a_cue_still_goes(
        self, text: str, expected: str
    ) -> None:
        assert scrub_private(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "12 at " * 40_000,
            "5pm at " * 30_000,
            "a1 at x" * 30_000,
            "j.doe at cs.cs.cs." * 5_000,
            "jdoe2 at a-b.c-d." * 5_000,
        ],
    )
    def test_hostile_text_is_scrubbed_in_linear_time(self, text: str) -> None:
        started = time.perf_counter()
        scrub_private(text)
        assert time.perf_counter() - started < 1.0


class TestWordsAfterAPasswordLabel:
    @pytest.mark.parametrize(
        "text",
        [
            "Why do people not use password managers?",
            "Can users use passwords safely?",
            "How users enter PIN codes",
            "Users type passwords carefully",
            "Do you use password hashing?",
            "Is the passcode secret?",
            "We ask users to enter the PIN twice.",
            "Users who use password managers.",
            "Why users never type the password correctly",
        ],
    )
    def test_prose_about_passwords_stays(self, text: str) -> None:
        assert scrub_private(text) == text
        assert scrub_title(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Use password stanford to join", f"Use password {VALUE_REMOVED} to join"),
            ("Enter the PIN seminar.", f"Enter the PIN {VALUE_REMOVED}."),
            ("Password: seminar?", f"Password: {VALUE_REMOVED}?"),
        ],
    )
    def test_a_code_without_a_digit_still_goes_from_an_abstract(
        self, text: str, expected: str
    ) -> None:
        assert scrub_private(text) == expected


class TestTitles:
    @pytest.mark.parametrize(
        "title",
        [
            "Why do people not use password managers?",
            "Can users use passwords safely?",
            "How users enter PIN codes",
            "Password: a history of authentication",
            "The meeting password is dead",
            "Passcode Leaks and Zoom Bombing",
            "Use password stanford to join",
        ],
    )
    def test_a_title_keeps_its_words(self, title: str) -> None:
        assert scrub_title(title) == title
        draft = make_draft(title=title, series=title)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert candidate.event.title == title
        assert candidate.event.series == title

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (f"Seminar, Passcode: {CODE}", f"Seminar, Passcode: {VALUE_REMOVED}"),
            (f"Talk (password {CODE})", f"Talk (password {VALUE_REMOVED})"),
            (
                f"Join https://stanford.zoom.us/j/1?pwd={CODE}",
                "Join https://stanford.zoom.us/j/1",
            ),
            ("Office hours (jdoe" + "@stanford.edu)", "Office hours"),
        ],
    )
    def test_a_title_loses_links_codes_with_digits_and_addresses(
        self, title: str, expected: str
    ) -> None:
        assert scrub_title(title) == expected
        draft = make_draft(title=title)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert candidate.event.title == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Seminar - Zoom passcode: neuro", f"Seminar - Zoom passcode: {VALUE_REMOVED}"),
            ("Meeting passcode: protein", f"Meeting passcode: {VALUE_REMOVED}"),
            (
                "Talk (Zoom passcode: neuro) Room 101",
                f"Talk (Zoom passcode: {VALUE_REMOVED}) Room 101",
            ),
            ('Webinar passcode: "protein".', f"Webinar passcode: {VALUE_REMOVED}."),
        ],
    )
    def test_a_title_loses_the_one_word_after_a_code_label_at_its_end(
        self, title: str, expected: str
    ) -> None:
        assert scrub_title(title) == expected
        draft = make_draft(title=title, series=title)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert (candidate.event.title, candidate.event.series) == (expected, expected)

    @pytest.mark.parametrize(
        "title",
        [
            "Passcodes: Security",
            "Passcodes: A Usability Study",
            "Zoom passcode: required",
            "Passcode: Hashing",
            "Passcode: neuro - Room 101",
        ],
    )
    def test_a_title_keeps_a_word_of_prose_or_one_followed_by_more(self, title: str) -> None:
        assert scrub_title(title) == title

    def test_an_abstract_with_the_same_words_is_scrubbed_fully(self) -> None:
        draft = make_draft(
            title="Use password stanford to join", abstract="Use password stanford to join"
        )
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        assert candidate.event.title == "Use password stanford to join"
        assert candidate.event.abstract == f"Use password {VALUE_REMOVED} to join"

    def test_scrubbing_a_title_twice_changes_nothing(self) -> None:
        for text in (f"Passcode: {CODE} and https://zoom.us/j/1?pwd=x", "PIN: 1234 5678"):
            once = scrub_title(text)
            assert scrub_title(once) == once
