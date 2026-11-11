"""Tests for the fixture store, the offline HTTP client and the recorder."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
import respx

from factories import REFERENCE_NOW, make_document
from fakes import FakeClock, StubResolver
from seminars.config import ENV_HTTP_CACHE_DIR, HttpSettings
from seminars.fixtures import (
    MANIFEST_NAME,
    MAX_FIXTURE_BYTES,
    FixtureError,
    FixtureManifest,
    FixtureStore,
    check_tree,
    default_file_name,
    fixture_key,
    fixture_sources,
    main,
    record,
    reference_time,
)
from seminars.http import (
    FixtureNotFoundError,
    HttpClient,
    HttpStatusError,
    OfflineHttpClient,
    PoliteClient,
    RobotsDisallowedError,
    UnsupportedSchemeError,
    offline_client,
)
from seminars.textio import normalize_body

REPO_ROOT = Path(__file__).resolve().parents[1]
API = "https://events.example.edu/api/2/events"
PAGE_1 = f"{API}?start=2026-10-27&end=2027-03-10&pp=100&page=1&type[]=1&type[]=2"
BOM = chr(0xFEFF)


@pytest.fixture
def store(tmp_path: Path) -> FixtureStore:
    return FixtureStore(tmp_path / "fixtures" / "localist-example")


class TestFixtureKey:
    def test_parameter_order_and_encoding_do_not_matter(self) -> None:
        left = "https://Events.Example.edu/api/2/events?type[]=1&pp=100&page=1"
        right = "https://events.example.edu/api/2/events?page=1&pp=100&type%5B%5D=1#top"
        assert fixture_key(left) == fixture_key(right)

    def test_different_values_do_not_match(self) -> None:
        assert fixture_key(f"{API}?page=1") != fixture_key(f"{API}?page=2")
        assert fixture_key(f"{API}?page=1") != fixture_key(API)
        assert fixture_key("https://a.example.edu/x") != fixture_key("http://a.example.edu/x")

    def test_default_ports_are_dropped(self) -> None:
        plain = fixture_key("https://a.example.edu/x")
        assert fixture_key("https://a.example.edu:443/x") == plain
        assert fixture_key("https://a.example.edu:8443/x") != plain
        assert fixture_key("http://a.example.edu:80/x") == fixture_key("http://a.example.edu/x")

    def test_repeated_parameters_are_kept(self) -> None:
        assert fixture_key(f"{API}?type[]=1&type[]=2") != fixture_key(f"{API}?type[]=1")
        assert fixture_key(f"{API}?type[]=2&type[]=1") == fixture_key(f"{API}?type[]=1&type[]=2")

    def test_volatile_parameters_match_any_value(self) -> None:
        volatile = ["start", "end"]
        recorded = fixture_key(f"{API}?start=2026-10-27&end=2027-03-10&page=1", volatile)
        later = fixture_key(f"{API}?page=1&end=2027-06-01&start=2027-01-01", volatile)
        assert recorded == later
        assert recorded != fixture_key(f"{API}?page=1&end=2027-06-01", volatile)
        assert recorded != fixture_key(f"{API}?start=x&end=y&page=2", volatile)

    def test_path_is_percent_decoded_and_empty_path_is_root(self) -> None:
        assert fixture_key("https://a.example.edu/a%20b") == fixture_key(
            "https://a.example.edu/a b"
        )
        assert fixture_key("https://a.example.edu") == fixture_key("https://a.example.edu/")


class TestHelpers:
    def test_normalize_body(self) -> None:
        assert normalize_body(BOM + "BEGIN\r\nEND\r\n") == "BEGIN\nEND\n"
        assert normalize_body("a\rb") == "a\nb\n"
        assert normalize_body("") == "\n"
        assert normalize_body("ok\n") == "ok\n"
        odd = BOM + BOM + "a\r\r\nb"
        assert normalize_body(odd) == "a\n\nb\n"
        assert normalize_body(normalize_body(odd)) == normalize_body(odd)

    @pytest.mark.parametrize(
        ("url", "content_type", "expected_prefix", "extension"),
        [
            (API, "application/json", "api-2-events-", ".json"),
            (
                "https://x.example.edu/events/all/rss",
                "application/rss+xml",
                "events-all-rss-",
                ".xml",
            ),
            ("https://x.example.edu/", "text/html", "index-", ".html"),
            ("https://x.example.edu/seminar/Abc/ics", "text/calendar", "seminar-abc-ics-", ".ics"),
            ("https://x.example.edu/data", None, "data-", ".txt"),
        ],
    )
    def test_default_file_name(
        self, url: str, content_type: str | None, expected_prefix: str, extension: str
    ) -> None:
        name = default_file_name(url, content_type)
        assert name.startswith(expected_prefix)
        assert name.endswith(extension)
        assert name == default_file_name(url, content_type)
        assert name != default_file_name(url + "?page=2", content_type)


class TestStore:
    def test_add_writes_body_and_manifest(self, store: FixtureStore) -> None:
        document = make_document(PAGE_1, '{"events": []}', content_type="application/json")
        entry = store.add(document, name="page1.json")
        assert (store.directory / "page1.json").read_text(encoding="utf-8") == '{"events": []}\n'
        manifest = json.loads((store.directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest == {
            "schema_version": 1,
            "source_id": "localist-example",
            "entries": {
                PAGE_1: {
                    "file": "page1.json",
                    "status": 200,
                    "content_type": "application/json",
                    "fetched_at": "2026-11-10T20:00:00Z",
                    "final_url": None,
                    "volatile_params": [],
                }
            },
        }
        assert entry.file == "page1.json"
        assert FixtureStore(store.directory).manifest == store.manifest

    def test_read_returns_the_recorded_document(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "<rss/>", final_url=f"{API}/moved"))
        document = store.read(PAGE_1)
        assert document is not None
        assert document.url == PAGE_1
        assert document.final_url == f"{API}/moved"
        assert document.text == "<rss/>\n"
        assert document.status == 200
        assert document.content_type == "application/rss+xml"
        assert document.fetched_at == REFERENCE_NOW
        assert store.read(f"{API}?page=9") is None

    def test_recording_again_is_idempotent(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "old"))
        first_files = sorted(path.name for path in store.directory.iterdir())
        later = REFERENCE_NOW + timedelta(hours=1)
        store.add(make_document(PAGE_1, "new", fetched_at=later))
        assert sorted(path.name for path in store.directory.iterdir()) == first_files
        assert len(store.manifest.entries) == 1
        document = store.read(PAGE_1)
        assert document is not None
        assert (document.text, document.fetched_at) == ("new\n", later)

    def test_renaming_removes_the_old_body(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "body"), name="old.json")
        store.add(make_document(PAGE_1, "body"), name="new.json")
        assert sorted(path.name for path in store.directory.iterdir()) == [
            MANIFEST_NAME,
            "new.json",
        ]

    def test_manifest_entries_are_sorted_by_url(self, store: FixtureStore) -> None:
        store.add(make_document(f"{API}?page=2", "b"))
        store.add(make_document(f"{API}?page=1", "a"))
        assert list(store.manifest.entries) == [f"{API}?page=1", f"{API}?page=2"]

    def test_volatile_parameters_are_matched_loosely(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "page one"), volatile_params=["start", "end"])
        later = f"{API}?type[]=1&type[]=2&page=1&pp=100&start=2027-02-01&end=2027-06-15"
        document = store.read(later)
        assert document is not None
        assert (document.url, document.text) == (later, "page one\n")
        assert store.read(later.replace("page=1", "page=2")) is None

    @pytest.mark.parametrize("name", ["../escape.txt", "sub/dir.txt", MANIFEST_NAME, "README.md"])
    def test_unsafe_or_reserved_names_are_rejected(self, store: FixtureStore, name: str) -> None:
        with pytest.raises(FixtureError):
            store.add(make_document(PAGE_1, "body"), name=name)
        assert not (store.directory.parent / "escape.txt").exists()

    def test_file_name_cannot_be_shared_by_two_urls(self, store: FixtureStore) -> None:
        store.add(make_document(f"{API}?page=1", "a"), name="page.json")
        with pytest.raises(FixtureError, match="already used by another URL"):
            store.add(make_document(f"{API}?page=2", "b"), name="page.json")

    @pytest.mark.parametrize(
        ("text", "content_type"), [("PNG\x00\x01", "text/html"), ("fake", "image/png")]
    )
    def test_binary_content_is_refused(
        self, store: FixtureStore, text: str, content_type: str
    ) -> None:
        with pytest.raises(FixtureError, match="binary"):
            store.add(make_document(PAGE_1, text, content_type=content_type))
        assert not store.directory.exists()

    def test_invalid_directory_name_or_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(FixtureError, match="not a valid source id"):
            FixtureStore(tmp_path / "Not_A_Source")
        directory = tmp_path / "sites-broken"
        directory.mkdir()
        (directory / MANIFEST_NAME).write_text("{}", encoding="utf-8")
        with pytest.raises(FixtureError, match="invalid fixture manifest"):
            FixtureStore(directory)


class TestOfflineClient:
    def test_serves_recorded_urls(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "page one"))
        client = OfflineHttpClient([store])
        assert isinstance(client, HttpClient)
        assert client.get(PAGE_1, headers={"Accept": "application/json"}).text == "page one\n"
        assert client.requests == [PAGE_1]

    def test_unrecorded_url_raises_a_clear_error(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "page one"))
        client = OfflineHttpClient([store])
        missing = f"{API}?page=2"
        with pytest.raises(FixtureNotFoundError) as caught:
            client.get(missing)
        message = str(caught.value)
        assert "offline mode: no fixture recorded in [localist-example]" in message
        assert "python -m seminars.fixtures record" in message
        assert missing in message
        assert isinstance(caught.value, LookupError)

    def test_recorded_error_status_behaves_like_the_live_client(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "gone", status=404))
        client = OfflineHttpClient([store])
        with pytest.raises(HttpStatusError) as caught:
            client.get(PAGE_1)
        assert caught.value.status == 404
        assert client.get(PAGE_1, allow_statuses=[404]).text == "gone\n"

    def test_only_http_urls(self, store: FixtureStore) -> None:
        with pytest.raises(UnsupportedSchemeError):
            OfflineHttpClient([store]).get("file:///etc/passwd")

    def test_factory_scopes_the_client_to_sources(self, tmp_path: Path) -> None:
        fixtures_dir = tmp_path / "fixtures"
        url = "https://shared.example.edu/feed"
        FixtureStore(fixtures_dir / "sites-a").add(make_document(url, "from a"))
        FixtureStore(fixtures_dir / "sites-b").add(make_document(url, "from b"))
        (fixtures_dir / "_site").mkdir()
        assert fixture_sources(fixtures_dir) == ["sites-a", "sites-b"]
        assert offline_client("sites-b", fixtures_dir=fixtures_dir).get(url).text == "from b\n"
        assert offline_client(fixtures_dir=fixtures_dir).get(url).text == "from a\n"
        with pytest.raises(FixtureNotFoundError, match=r"\[sites-c\]"):
            offline_client("sites-c", fixtures_dir=fixtures_dir).get(url)

    def test_conftest_fixture(
        self, offline_http: Callable[..., OfflineHttpClient], tmp_path: Path
    ) -> None:
        FixtureStore(tmp_path / "sites-a").add(make_document("https://a.example.edu/x", "hello"))
        client = offline_http("sites-a", fixtures_dir=tmp_path)
        assert client.get("https://a.example.edu/x").text == "hello\n"
        assert isinstance(offline_http(), OfflineHttpClient)


class TestReferenceTime:
    def test_latest_fetch_time_of_all_sources(self, tmp_path: Path) -> None:
        assert reference_time(tmp_path / "missing") is None
        later = REFERENCE_NOW + timedelta(days=1)
        FixtureStore(tmp_path / "sites-a").add(make_document("https://a.example.edu/x", "a"))
        FixtureStore(tmp_path / "sites-b").add(
            make_document("https://b.example.edu/x", "b", fetched_at=later)
        )
        assert reference_time(tmp_path) == later


class TestCheck:
    def test_consistent_tree_has_no_problems(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE_1, "body"))
        (store.directory / "README.md").write_text("trimmed to 3 events\n", encoding="utf-8")
        assert check_tree(store.directory.parent) == []

    def test_problems_are_reported(self, store: FixtureStore) -> None:
        store.add(make_document(f"{API}?page=1", "a"), name="page1.json")
        store.add(make_document(f"{API}?page=2", "b"), name="page2.json")
        store.add(make_document(f"{API}?page=3", "c"), name="page3.json")
        (store.directory / "page1.json").unlink()
        (store.directory / "page2.json").write_bytes(b"x" * (MAX_FIXTURE_BYTES + 1))
        (store.directory / "page3.json").write_bytes(b"\x00binary")
        (store.directory / "stray.html").write_text("orphan\n", encoding="utf-8")
        assert check_tree(store.directory.parent) == [
            "localist-example: page1.json is missing",
            "localist-example: page2.json is larger than 500000 bytes; trim it",
            "localist-example: page3.json is binary",
            "localist-example: stray.html is not listed in the manifest",
        ]

    def test_ambiguous_entries_and_wrong_source_id(self, tmp_path: Path) -> None:
        directory = tmp_path / "sites-a"
        directory.mkdir()
        entry = {"file": "a.json", "fetched_at": "2026-11-10T20:00:00Z"}
        manifest = FixtureManifest.model_validate(
            {
                "source_id": "sites-other",
                "entries": {
                    "https://a.example.edu/x?b=1&a=2": entry,
                    "https://a.example.edu/x?a=2&b=1": {**entry, "file": "b.json"},
                },
            }
        )
        (directory / MANIFEST_NAME).write_text(manifest.model_dump_json(), encoding="utf-8")
        (directory / "a.json").write_text("{}\n", encoding="utf-8")
        (directory / "b.json").write_text("{}\n", encoding="utf-8")
        problems = check_tree(tmp_path)
        assert problems[0] == "sites-a: manifest source_id is 'sites-other'"
        assert "match the same requests" in problems[1]
        assert len(problems) == 2

    def test_broken_manifest_is_a_problem_not_a_crash(self, tmp_path: Path) -> None:
        directory = tmp_path / "sites-a"
        directory.mkdir()
        (directory / MANIFEST_NAME).write_text("not json", encoding="utf-8")
        (problem,) = check_tree(tmp_path)
        assert problem.startswith("sites-a: ")
        assert "invalid fixture manifest" in problem


class TestRecorder:
    @pytest.fixture
    def live(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> PoliteClient:
        return PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver
        )

    def test_record_through_the_polite_client_and_load_back(
        self,
        live: PoliteClient,
        store: FixtureStore,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        robots = respx_router.get("https://events.example.edu/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 30\nDisallow: /admin/\n"
        )
        respx_router.get(url__startswith=API).respond(
            200,
            content=(BOM + '{"events": []}\r\n').encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        entry = record(live, store, PAGE_1, volatile_params=["start", "end"])
        record(live, store, PAGE_1.replace("page=1", "page=2"), name="page2.json")
        assert robots.call_count == 1
        assert fake_clock.sleeps == [30.0, 30.0]
        assert entry.content_type == "application/json"
        assert entry.volatile_params == ["end", "start"]
        assert entry.fetched_at == REFERENCE_NOW + timedelta(seconds=30)

        offline = OfflineHttpClient([FixtureStore(store.directory)])
        document = offline.get(PAGE_1.replace("2026-10-27", "2026-12-01"))
        assert document.text == '{"events": []}\n'
        assert document.parse_json() == {"events": []}
        assert offline.get(PAGE_1.replace("page=1", "page=2")).status == 200
        assert check_tree(store.directory.parent) == []

    @pytest.mark.parametrize(
        "raw",
        [
            (BOM + '{"events": [],\r\n "page": 1}').encode(),
            b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            b"<html>\r<body>old mac line ends</body>\r</html>",
            b"plain\n",
            b"",
        ],
        ids=["bom-json", "crlf-ics", "cr-html", "clean", "empty"],
    )
    def test_live_and_offline_documents_have_the_same_text(
        self, live: PoliteClient, store: FixtureStore, respx_router: respx.MockRouter, raw: bytes
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(url__startswith=API).respond(
            200, content=raw, headers={"Content-Type": "text/plain; charset=utf-8"}
        )
        fetched = live.get(PAGE_1)
        store.add(fetched)
        replayed = OfflineHttpClient([FixtureStore(store.directory)]).get(PAGE_1)
        assert fetched.text == replayed.text
        assert not fetched.text.startswith(BOM)
        assert "\r" not in fetched.text
        assert fetched.text.endswith("\n")
        assert (store.directory / store.manifest.entries[PAGE_1].file).read_bytes() == (
            fetched.text.encode()
        )

    def test_live_json_with_a_byte_order_mark_parses(
        self, live: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(url__startswith=API).respond(
            200,
            content=(BOM + '{"events": []}').encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert live.get(PAGE_1).parse_json() == {"events": []}

    def test_disallowed_url_is_not_recorded(
        self, live: PoliteClient, store: FixtureStore, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(
            200, text="User-agent: *\nDisallow: /api/\n"
        )
        with pytest.raises(RobotsDisallowedError):
            record(live, store, PAGE_1)
        assert not store.directory.exists()

    def test_command_line_record(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(url__startswith=API).respond(
            200, text="<rss/>", headers={"Content-Type": "application/rss+xml"}
        )
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example"]
        code = main(
            [*arguments, PAGE_1, "--name", "feed.xml", "--volatile-param", "start"], http=live
        )
        assert code == 0
        assert "recorded localist-example/feed.xml (HTTP 200, 7 bytes)" in capsys.readouterr().out
        recorded = FixtureStore(tmp_path / "tests" / "fixtures" / "localist-example")
        assert recorded.manifest.entries[PAGE_1].volatile_params == ["start"]
        assert main(["--root", str(tmp_path), "check"]) == 0
        assert "fixtures OK" in capsys.readouterr().out

    def test_command_line_reports_errors(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(url__startswith=API).respond(
            200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
        )
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example", PAGE_1]
        assert main(arguments, http=live) == 1
        assert "refusing binary content type image/png" in capsys.readouterr().err
        bad_id = ["--root", str(tmp_path), "record", "--source-id", "Bad_Id", PAGE_1]
        assert main(bad_id, http=live) == 1
        assert "not a valid source id" in capsys.readouterr().err

    def test_command_line_records_several_urls_one_after_another(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        robots = respx_router.get("https://events.example.edu/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 30\n"
        )
        respx_router.get(url__startswith=API).respond(200, text="{}")
        pages = [PAGE_1.replace("page=1", f"page={number}") for number in (1, 2, 3)]
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example"]
        assert main([*arguments, *pages, "--volatile-param", "start"], http=live) == 0
        assert capsys.readouterr().out.count("recorded localist-example/") == 3
        assert robots.call_count == 1
        assert fake_clock.sleeps == [30.0, 30.0, 30.0]
        recorded = FixtureStore(tmp_path / "tests" / "fixtures" / "localist-example")
        assert sorted(recorded.manifest.entries) == sorted(pages)
        assert all(e.volatile_params == ["start"] for e in recorded.manifest.entries.values())

    def test_command_line_goes_on_after_a_url_that_fails(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(f"{API}/missing").respond(404)
        respx_router.get(f"{API}/present").respond(200, text="{}")
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example"]
        assert main([*arguments, f"{API}/missing", f"{API}/present"], http=live) == 1
        captured = capsys.readouterr()
        assert "error: HTTP 404" in captured.err
        assert captured.out.count("recorded localist-example/") == 1
        recorded = FixtureStore(tmp_path / "tests" / "fixtures" / "localist-example")
        assert list(recorded.manifest.entries) == [f"{API}/present"]

    def test_command_line_refuses_one_file_name_for_several_urls(
        self, live: PoliteClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example"]
        with pytest.raises(SystemExit) as caught:
            main([*arguments, PAGE_1, f"{API}/other", "--name", "page.json"], http=live)
        assert caught.value.code == 2
        assert "--name names a single file" in capsys.readouterr().err
        assert not (tmp_path / "tests").exists()

    def test_command_line_builds_its_client_on_the_shared_cache_directory(
        self,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        monkeypatch: pytest.MonkeyPatch,
        resolver: StubResolver,
    ) -> None:
        """Without ``http`` the recorder is live: settings and cache come from the project."""
        settings = (REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
        settings = settings.replace("min_interval_seconds: 1.0", "min_interval_seconds: 0")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.yaml").write_text(settings, encoding="utf-8")
        shared = tmp_path / "shared-http-cache"
        monkeypatch.setenv(ENV_HTTP_CACHE_DIR, str(shared))
        robots = respx_router.get("https://events.example.edu/robots.txt").respond(404)
        respx_router.get(url__startswith=API).respond(200, text="{}")
        arguments = ["--root", str(tmp_path), "record", "--source-id", "localist-example"]
        assert main([*arguments, PAGE_1], resolver=resolver) == 0
        assert set(resolver.calls) == {"events.example.edu"}
        expected = "seminars-bot/1.0 (+https://fridrichmethod.github.io/seminars/)"
        assert robots.calls.last.request.headers["user-agent"] == expected
        assert (shared / "hosts" / "events.example.edu.json").is_file()
        assert not (tmp_path / ".cache").exists()

    def test_command_line_check_reports_problems(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = FixtureStore(tmp_path / "tests" / "fixtures" / "sites-a")
        store.add(make_document("https://a.example.edu/x", "body"), name="x.html")
        (store.directory / "x.html").unlink()
        assert main(["--root", str(tmp_path), "check", "--source-id", "sites-a"]) == 1
        output = capsys.readouterr().out
        assert "sites-a: x.html is missing" in output
        assert "1 problem(s) found" in output

    def test_module_is_runnable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "seminars.fixtures", "check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "fixtures OK" in result.stdout
        assert result.stderr == ""


class TestRepositoryFixtures:
    def test_every_committed_fixture_directory_is_consistent(self) -> None:
        assert check_tree(REPO_ROOT / "tests" / "fixtures") == []

    def test_readme_is_ascii(self) -> None:
        assert (REPO_ROOT / "tests" / "fixtures" / "README.md").read_bytes().isascii()
