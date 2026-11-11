"""A lone surrogate in scraped text must not stop a run: no entry point may fail on one.

``json.loads`` turns an unpaired ``\\ud83d`` escape into a lone surrogate, a
character that a Python string can hold and no UTF-8 file can. These tests
feed one to everything that stores, hashes or sends text.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

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


DEPTH = 2000
"""Nesting no function can follow by calling itself, and that JSON decoding still takes."""

TOO_DEEP = 100_000
"""Nesting at which ``json.loads`` gives up with a RecursionError of its own."""


def wrapped(leaf: object, kind: str, depth: int = DEPTH) -> Any:
    """``leaf`` inside ``depth`` containers with one item each; dictionary keys end in HIGH."""
    value = leaf
    for level in range(depth):
        shape = ("list", "tuple", "dict")[level % 3] if kind == "mixed" else kind
        if shape == "list":
            value = [value]
        elif shape == "tuple":
            value = (value,)
        else:
            value = {"k" + HIGH: value}
    return value


def unwrapped(value: Any) -> tuple[int, set[str], object]:
    """The number of one-item containers around the innermost value, their keys, that value."""
    depth, keys = 0, set()
    while isinstance(value, list | tuple | dict) and len(value) == 1:
        if isinstance(value, dict):
            keys.update(value)
            value = next(iter(value.values()))
        else:
            value = value[0]
        depth += 1
    return depth, keys, value


def reference(value: Any) -> Any:
    """``without_surrogates`` written the short way, which only works for shallow data."""
    if isinstance(value, str):
        return strip_surrogates(value)
    if isinstance(value, dict):
        return {reference(key): reference(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reference(item) for item in value]
    if type(value) is tuple:
        return tuple(reference(item) for item in value)
    return value


class Pair(tuple[Any, ...]):
    """A tuple of another type: left alone, like every value that is not plain data."""


scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), hostile)
keys = st.one_of(hostile, st.integers(), st.tuples(hostile, st.integers()))
plain_data = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.lists(inner, max_size=3).map(tuple),
        st.dictionaries(keys, inner, max_size=4),
    ),
    max_leaves=20,
)


class Loose(Model):
    """A model with a field that takes any data."""

    data: Any = None


class TestDeepNesting:
    """Data nested deeper than the recursion limit is data like any other."""

    def test_the_depth_tested_is_beyond_the_recursion_limit(self) -> None:
        assert sys.getrecursionlimit() < DEPTH

    @pytest.mark.parametrize("kind", ["list", "tuple", "dict", "mixed"])
    def test_without_surrogates_walks_data_of_any_depth(self, kind: str) -> None:
        value = wrapped("a" + HIGH, kind)
        cleaned = without_surrogates(value)
        depth, keys, leaf = unwrapped(cleaned)
        assert (depth, leaf) == (DEPTH, "a")
        assert keys == ({"k"} if kind in ("dict", "mixed") else set())
        assert type(cleaned) is type(value)
        assert unwrapped(value)[2] == "a" + HIGH  # the input was copied, not changed

    @given(plain_data)
    def test_the_walk_gives_what_the_short_recursive_form_gives(self, value: Any) -> None:
        assert without_surrogates(value) == reference(value)
        assert json.dumps(without_surrogates(value), default=str, skipkeys=True).isascii()

    def test_containers_are_copied_and_other_values_are_passed_on(self) -> None:
        inner = ["x" + HIGH]
        pair = Pair(("p" + HIGH,))
        value = {"list": inner, "pair": pair, "tuple": (inner, 1), ("t" + HIGH, 2): None}
        cleaned = without_surrogates(value)
        assert cleaned == {"list": ["x"], "pair": pair, "tuple": (["x"], 1), ("t", 2): None}
        assert cleaned["pair"] is pair
        assert cleaned["list"] is not inner
        assert inner == ["x" + HIGH]
        assert list(cleaned) == ["list", "pair", "tuple", ("t", 2)]

    def test_of_two_keys_that_become_one_the_later_one_counts(self) -> None:
        value = {"a" + HIGH: ["first"], "a": "second", "b": 1, "b" + LOW: {"x": 2}}
        value |= {"c": "one", "c" + HIGH: "two", "d" + HIGH: (1,), "d" + LOW: [2], "d": 3}
        cleaned = without_surrogates(value)
        assert cleaned == {"a": "second", "b": {"x": 2}, "c": "two", "d": 3}
        assert list(cleaned) == ["a", "b", "c", "d"]
        assert cleaned == reference(value)

    def test_data_that_contains_itself_is_a_value_error_not_an_endless_walk(self) -> None:
        loop: list[Any] = ["a" + HIGH]
        loop.append(loop)
        ring: dict[str, Any] = {"items": [1, 2, ("x", [])]}
        ring["items"][2][1].append(ring)
        wide: list[Any] = []
        wide.extend([wide] * 1000)
        for value in (loop, ring, wide, {"k": (loop,)}):
            with pytest.raises(ValueError, match="contains itself"):
                without_surrogates(value)
        with pytest.raises(ValidationError):
            Loose(data=loop)
        with pytest.raises(ValidationError):
            Bag.model_validate({"nested": [ring]})

    def test_a_value_that_occurs_twice_is_not_data_that_contains_itself(self) -> None:
        shared = ["x" + HIGH]
        value = {"a": shared, "b": [shared, (shared, shared)], "c": shared}
        assert without_surrogates(value) == {"a": ["x"], "b": [["x"], (["x"], ["x"])], "c": ["x"]}
        assert without_surrogates([shared, shared]) == [["x"], ["x"]]

    @pytest.mark.parametrize("kind", ["list", "dict"])
    def test_parse_json_reads_deeply_nested_json(self, kind: str) -> None:
        leaf = json.dumps("a" + HIGH)
        if kind == "list":
            body = "[" * DEPTH + leaf + "]" * DEPTH
        else:
            body = ("{" + json.dumps("k" + HIGH) + ":") * DEPTH + leaf + "}" * DEPTH
        assert body.isascii()
        data = make_document("https://x.example.edu/api", body).parse_json()
        assert unwrapped(data) == (DEPTH, {"k"} if kind == "dict" else set(), "a")

    @pytest.mark.parametrize(("opening", "closing"), [("[", "]"), ('{"k":', "}")])
    def test_json_that_is_nested_too_deeply_to_decode_is_a_value_error(
        self, opening: str, closing: str
    ) -> None:
        body = opening * TOO_DEEP + "1" + closing * TOO_DEEP
        with pytest.raises(RecursionError):
            json.loads(body)
        document = make_document("https://x.example.edu/api", body)
        with pytest.raises(ValueError, match="JSON is nested too deeply"):
            document.parse_json()

    def test_json_that_is_no_json_is_still_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="Expecting"):
            make_document(text="[" * DEPTH).parse_json()

    @pytest.mark.parametrize("kind", ["list", "dict", "mixed"])
    def test_a_model_given_deeply_nested_data_fails_validation_as_usual(self, kind: str) -> None:
        nested = wrapped("x", kind)
        with pytest.raises(ValidationError, match="text"):
            make_document(text=nested)
        with pytest.raises(ValidationError, match="title"):
            make_draft(title=nested)
        with pytest.raises(ValidationError):
            Bag.model_validate(nested)

    def test_a_model_keeps_deeply_nested_data_in_a_field_that_takes_it(self) -> None:
        loose = Loose(data=wrapped("a" + HIGH, "mixed"))
        depth, keys, leaf = unwrapped(loose.data)
        assert (depth, keys, leaf) == (DEPTH, {"k"}, "a")


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
