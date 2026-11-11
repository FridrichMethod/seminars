"""A lone surrogate in scraped text must not stop a run: no entry point may fail on one.

``json.loads`` turns an unpaired ``\\ud83d`` escape into a lone surrogate, a
character that a Python string can hold and no UTF-8 file can. These tests
feed one to everything that stores, hashes or sends text.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from factories import make_document, make_draft, make_event
from fakes import FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.contracts import llm_cache_key
from seminars.fixtures import FixtureStore, OfflineHttpClient
from seminars.http import HttpError, PoliteClient, UnsupportedSchemeError, split_url
from seminars.models import (
    ExcludedEvent,
    Model,
    ascii_url,
    dump_ascii_json,
    dump_event_lines,
    dump_events,
    dump_json,
    load_events,
    read_events,
    source_key,
    split_source_key,
    write_events,
    write_model,
)
from seminars.sources.base import absolute_url
from seminars.textrules import (
    ascii_safe,
    clean_text,
    data_charset_problems,
    strip_surrogates,
    without_surrogates,
)

HIGH = "\ud83d"  # the first half of an emoji: what an unpaired escape decodes to
LOW = "\ude80"
SURROGATES = [HIGH, LOW, "\ud800", "\udbff", "\udc00", "\udfff"]

hostile = st.text(
    alphabet=st.one_of(
        st.characters(categories=["Cs"]),
        st.characters(exclude_categories=[]),
        st.sampled_from(["a", " ", "\n", "\u00e9"]),
    )
)
"""Arbitrary text that is dense in lone surrogates."""


def encodes(text: str) -> bool:
    """True when ``text`` can be written to a UTF-8 file."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class Bag(Model):
    """A model with strings in every kind of container."""

    text: str = ""
    names: list[str] = []
    table: dict[str, list[str]] = {}
    pair: tuple[str, str] = ("", "")
    nested: list[Bag] = []


class TestThePremise:
    def test_json_decoding_produces_lone_surrogates(self) -> None:
        text = json.loads('"a\\ud83db"')
        assert text == "a" + HIGH + "b"
        assert not encodes(text)

    def test_a_complete_pair_is_an_ordinary_character(self) -> None:
        assert json.loads('"\\ud83d\\ude80"') == "\U0001f680"
        assert encodes(json.loads('"\\ud83d\\ude80"'))


class TestTextRules:
    @pytest.mark.parametrize("char", SURROGATES)
    def test_clean_text_removes_them(self, char: str) -> None:
        assert clean_text("semi" + char + "nar") == "seminar"
        assert clean_text(char) == ""

    def test_two_halves_side_by_side_are_not_a_character(self) -> None:
        assert clean_text("a" + HIGH + LOW + "b") == "ab"

    @pytest.mark.parametrize("char", SURROGATES)
    def test_strip_surrogates_removes_only_them(self, char: str) -> None:
        kept = "Poincar\u00e9 \U0001f680" + chr(0xA0)  # letters, an emoji, a no-break space
        assert strip_surrogates(kept + char + "\n") == kept + "\n"
        assert strip_surrogates("plain") == "plain"

    def test_without_surrogates_walks_json_data(self) -> None:
        value = {"a" + HIGH: ["x" + LOW, {"k": HIGH, "n": 1, "f": 1.5, "b": True, "z": None}]}
        assert without_surrogates(value) == {
            "a": ["x", {"k": "", "n": 1, "f": 1.5, "b": True, "z": None}]
        }
        assert without_surrogates(("a" + HIGH, 2)) == ("a", 2)
        assert without_surrogates(7) == 7

    def test_the_data_check_reports_them(self) -> None:
        assert data_charset_problems("ab" + HIGH + "c\n" + LOW + "\n") == [
            "1:3 U+D83D",
            "2:1 U+DE80",
        ]

    def test_ascii_safe_escapes_them(self) -> None:
        assert ascii_safe("a" + HIGH) == "a\\ud83d"

    @given(hostile)
    def test_cleaned_text_can_always_be_written(self, text: str) -> None:
        cleaned = clean_text(text)
        assert encodes(cleaned)
        assert data_charset_problems(cleaned + "\n") == []
        assert encodes(strip_surrogates(text))
        assert encodes(ascii_safe(text))


class TestModels:
    def test_every_string_of_a_model_loses_them(self) -> None:
        bag = Bag(
            text="a" + HIGH,
            names=["b" + LOW, "c"],
            table={"k" + HIGH: ["v" + HIGH]},
            pair=("p" + HIGH, LOW),
            nested=[Bag(text=HIGH + "inner")],
        )
        assert bag == Bag(
            text="a",
            names=["b", "c"],
            table={"k": ["v"]},
            pair=("p", ""),
            nested=[Bag(text="inner")],
        )
        assert Bag.model_validate({"nested": [{"names": [HIGH]}]}).nested[0].names == [""]
        assert bag.evolve(text="z" + LOW).text == "z"

    def test_a_draft_built_from_decoded_json_is_clean(self) -> None:
        item = json.loads('{"title": "Deep learning \\ud83d", "id": "ev-\\ude80"}')
        draft = make_draft(
            title=item["title"],
            native_id=item["id"],
            abstract="first" + HIGH + " line",
            speakers=[{"name": "Jane" + LOW + " Doe", "affiliation": HIGH}],
            categories=["bio" + HIGH],
            source_text=LOW + "page",
        )
        assert draft.title == "Deep learning "
        assert draft.native_id == "ev-"
        assert draft.abstract == "first line"
        assert (draft.speakers[0].name, draft.speakers[0].affiliation) == ("Jane Doe", "")
        assert (draft.categories, draft.source_text) == (["bio"], "page")
        assert encodes(dump_json(draft))

    def test_an_event_with_one_is_stored_without_it(self, tmp_path: Path) -> None:
        event = make_event(
            title="Protein" + HIGH + " design",
            abstract="de novo" + LOW,
            location=HIGH,
            series="Seminar" + HIGH,
            speakers=[{"name": "Jane Doe", "affiliation": "MIT" + LOW}],
            picks_reasons=["keyword" + HIGH],
        )
        assert (event.title, event.abstract, event.location) == ("Protein design", "de novo", "")
        path = tmp_path / "data" / "events.json"
        write_events(path, [event])
        assert read_events(path) == [event]
        assert encodes(dump_event_lines([event]))

    def test_an_excluded_event_can_be_reported(self, tmp_path: Path) -> None:
        excluded = ExcludedEvent(
            source_ids=["sites-a:" + HIGH],
            title=HIGH + "Talk",
            url="https://x.example.edu/a" + HIGH + "?q=" + LOW,
            reason="invalid",
            detail="no usable start" + LOW,
        )
        assert excluded.title == "Talk"
        assert excluded.url == "https://x.example.edu/a?q="
        assert excluded.detail == "no usable start"
        assert excluded.source_ids == ["sites-a:"]
        write_model(tmp_path / "report.json", excluded)
        assert (tmp_path / "report.json").read_bytes().isascii()

    def test_a_fetched_document_never_carries_one(self, tmp_path: Path) -> None:
        document = make_document("https://x.example.edu/feed", "<p>a" + HIGH + "b</p>")
        assert document.text == "<p>ab</p>\n"
        store = FixtureStore(tmp_path / "sites-x")
        store.add(document)
        assert OfflineHttpClient([store]).get(document.url).text == "<p>ab</p>\n"

    def test_parse_json_drops_them_from_keys_and_values(self) -> None:
        body = '{"events": [{"title": "a\\ud83db", "tags\\ude80": ["x\\ud83d"], "n": 3}]}'
        document = make_document("https://x.example.edu/api", body)
        assert document.parse_json() == {"events": [{"title": "ab", "tags": ["x"], "n": 3}]}
        assert make_document(text='"\\ud83d\\ude80"').parse_json() == "\U0001f680"

    @given(hostile, hostile)
    def test_any_model_can_be_dumped_and_written(self, text: str, key: str) -> None:
        bag = Bag(text=text, names=[text], table={key: [text]})
        dumped = dump_json(bag)
        assert encodes(dumped)
        assert Bag.model_validate_json(dumped) == bag


class TestKeysAndUrls:
    def test_source_keys_stay_ascii(self) -> None:
        key = source_key("sites-a", "id" + HIGH + "\u00e9")
        assert key == "sites-a:id\\ud83d\\xe9"
        assert split_source_key(key) == ("sites-a", "id\\ud83d\\xe9")

    def test_ascii_url_drops_them(self) -> None:
        assert ascii_url("https://x.example.edu/a" + HIGH + "b") == "https://x.example.edu/ab"
        assert (
            ascii_url("https://x.example.edu/caf\u00e9" + LOW) == "https://x.example.edu/caf%C3%A9"
        )

    def test_absolute_url_drops_them(self) -> None:
        assert absolute_url("https://x.example.edu/", "/a" + HIGH) == "https://x.example.edu/a"

    def test_the_cache_key_is_defined_for_them(self) -> None:
        key = llm_cache_key("v1", "tag", "a" + HIGH)
        assert len(key) == 64
        assert key == llm_cache_key("v1", "tag", "a" + HIGH)
        assert key != llm_cache_key("v1", "tag", "a")
        assert key != llm_cache_key("v1", "tag", "a" + LOW)
        assert key != llm_cache_key("v1", "tag", "a\\ud83d")

    def test_the_cache_key_of_ordinary_text_is_unchanged(self) -> None:
        material = json.dumps(["v1", "tag", "caf\u00e9"], ensure_ascii=False).encode("utf-8")
        assert llm_cache_key("v1", "tag", "caf\u00e9") == hashlib.sha256(material).hexdigest()


class TestDumps:
    def test_ascii_json_keeps_them_as_escapes_and_gives_them_back(self) -> None:
        value = {"answer": "a" + HIGH, "k" + LOW: [LOW + HIGH, HIGH + "-" + LOW]}
        text = dump_ascii_json(value)
        assert text.isascii()
        assert json.loads(text) == value

    def test_two_halves_in_pair_order_are_the_character_to_json(self) -> None:
        """JSON writes a character beyond U+FFFF as such a pair, so it cannot tell them apart."""
        assert json.loads(dump_ascii_json([HIGH + LOW])) == ["\U0001f680"]

    def test_readable_dumps_never_contain_one(self) -> None:
        class Plain(BaseModel):
            text: str

        dumped = dump_json(Plain(text="a" + HIGH + "b"))
        assert encodes(dumped)
        assert json.loads(dumped) == {"text": "ab"}

    def test_events_dump_and_load(self) -> None:
        event = make_event(abstract="x" + HIGH)
        text = dump_events([event])
        assert encodes(text)
        assert load_events(text) == [event]


class TestHttp:
    URL = "https://dept.example.edu/events/" + HIGH

    def test_a_url_with_one_is_not_a_url(self) -> None:
        with pytest.raises(UnsupportedSchemeError, match="lone surrogate") as caught:
            split_url(self.URL)
        assert str(caught.value).isascii()
        assert isinstance(caught.value, HttpError)

    def test_the_live_client_refuses_it_without_a_request(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        client = PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )
        with pytest.raises(UnsupportedSchemeError):
            client.get(self.URL)
        assert respx_router.calls.call_count == 0

    def test_the_offline_client_refuses_it(self, tmp_path: Path) -> None:
        client = OfflineHttpClient([FixtureStore(tmp_path / "sites-x")])
        with pytest.raises(UnsupportedSchemeError):
            client.get(self.URL)
        assert client.requests == []
