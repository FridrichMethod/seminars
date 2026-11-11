"""Tests for the Adapter protocol, the registry and adapter auto-discovery."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factories import REFERENCE_NOW, make_document, make_draft, make_source, make_window
from fakes import FakeHttpClient
from seminars.http import FixtureNotFoundError, HttpClient
from seminars.models import (
    LLM_PAGE_ADAPTER,
    EventDraft,
    RawPayload,
    SourceConfig,
    TimeWindow,
)
from seminars.sources import base
from seminars.sources.base import (
    ADAPTER_PACKAGES,
    GENERIC_ADAPTERS,
    Adapter,
    AdapterDiscoveryError,
    AdapterRegistry,
    DuplicateAdapterError,
    ParseError,
    UnknownAdapterError,
    absolute_url,
    fetch_urls,
    make_payload,
)

ADAPTER_MODULE = """\
from seminars.models import EventDraft
from seminars.sources.base import fetch_urls, register_adapter


@register_adapter("{name}")
class GeneratedAdapter:
    def fetch(self, http, source, *, window):
        return fetch_urls(http, source, window=window)

    def parse(self, payload, source):
        return [
            EventDraft(source_id=source.id, extraction="html", start="2026-11-17", title="{name}")
        ]
"""


SLOW_MODULE = "import time\n\ntime.sleep(0.2)\n" + ADAPTER_MODULE
"""An adapter module whose import takes long enough for other threads to pile up."""

REENTRANT_MODULE = (
    "from seminars.sources import base\n\n"
    "names_during_import = base.adapter_names()\n"
    "failures_during_import = base.discover_adapters(strict=False)\n" + ADAPTER_MODULE
)
"""An adapter module that asks the registry for its content while it is being imported."""


class EchoAdapter:
    """A minimal adapter written the way real ones are."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        return [
            EventDraft(source_id=source.id, extraction="html", start="2026-11-17", title=doc.text)
            for doc in payload.documents
        ]


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AdapterRegistry]:
    """Install an empty registry over ``packages`` as the process-wide one."""

    def build(*packages: str) -> AdapterRegistry:
        fresh = AdapterRegistry(packages)
        monkeypatch.setattr(base, "REGISTRY", fresh)
        return fresh

    return build


@pytest.fixture
def registry(install: Callable[..., AdapterRegistry]) -> AdapterRegistry:
    """An empty process-wide registry without any packages to discover."""
    return install()


@pytest.fixture
def make_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, str]], str]]:
    """Build an importable package from ``{relative path: source}`` and return its name."""
    created: list[str] = []
    monkeypatch.syspath_prepend(str(tmp_path))

    def build(files: dict[str, str]) -> str:
        name = f"adapters_{uuid.uuid4().hex[:10]}"
        for relative, content in {"__init__.py": "", **files}.items():
            path = tmp_path / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        importlib.invalidate_caches()
        created.append(name)
        return name

    yield build
    for module_name in [key for key in sys.modules if key.split(".")[0] in created]:
        del sys.modules[module_name]


class TestRegistration:
    def test_register_and_create(self, registry: AdapterRegistry) -> None:
        decorated = base.register_adapter("echo")(EchoAdapter)
        assert decorated is EchoAdapter
        assert registry.names() == ["echo"]
        assert "echo" in registry
        first, second = registry.create("echo"), registry.create("echo")
        assert isinstance(first, EchoAdapter)
        assert isinstance(first, Adapter)
        assert first is not second

    def test_duplicate_name_is_an_error(self, registry: AdapterRegistry) -> None:
        registry.register("echo")(EchoAdapter)

        class OtherAdapter(EchoAdapter):
            pass

        with pytest.raises(DuplicateAdapterError) as caught:
            registry.register("echo")(OtherAdapter)
        message = str(caught.value)
        assert "'echo' is already registered by test_registry.EchoAdapter" in message
        assert "OtherAdapter cannot reuse it" in message
        assert isinstance(registry.create("echo"), EchoAdapter)

    def test_registering_the_same_object_twice_is_harmless(self, registry: AdapterRegistry) -> None:
        registry.register("echo")(EchoAdapter)
        registry.register("echo")(EchoAdapter)
        assert registry.names() == ["echo"]

    @pytest.mark.parametrize("name", ["", "Echo", "my-adapter", "1st", "with space"])
    def test_invalid_names_are_rejected(self, registry: AdapterRegistry, name: str) -> None:
        with pytest.raises(ValueError, match="invalid adapter name"):
            registry.register(name)

    def test_unknown_adapter(self, registry: AdapterRegistry) -> None:
        registry.register("echo")(EchoAdapter)
        with pytest.raises(
            UnknownAdapterError, match=r"unknown adapter 'rss' \(registered: echo\)"
        ):
            registry.create("rss")
        assert isinstance(UnknownAdapterError("x"), LookupError)

    def test_a_factory_function_can_be_registered(self, registry: AdapterRegistry) -> None:
        @registry.register("made")
        def make() -> Adapter:
            return EchoAdapter()

        assert isinstance(registry.create("made"), EchoAdapter)


PackageBuilder = Callable[[dict[str, str]], str]
Installer = Callable[..., AdapterRegistry]


class TestDiscovery:
    def test_modules_are_imported_recursively(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "generic/__init__.py": "",
                "generic/alpha.py": ADAPTER_MODULE.format(name="alpha"),
                "generic/nested/__init__.py": "",
                "generic/nested/beta.py": ADAPTER_MODULE.format(name="beta"),
                "custom/__init__.py": "",
                "custom/gamma.py": ADAPTER_MODULE.format(name="gamma"),
                "custom/_helpers.py": "VALUE = 1\n",
            }
        )
        registry = install(f"{package}.generic", f"{package}.custom")
        assert registry.names() == []
        assert registry.discover() == {}
        assert registry.names() == ["alpha", "beta", "gamma"]
        assert f"{package}.custom._helpers" in sys.modules

    def test_discovery_is_idempotent(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        registry = install(make_package({"alpha.py": ADAPTER_MODULE.format(name="alpha")}))
        registry.discover()
        registry.discover()
        assert registry.names() == ["alpha"]

    def test_new_adapter_needs_no_shared_file(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        install(make_package({"alpha.py": ADAPTER_MODULE.format(name="alpha")}))
        assert base.adapter_names() == ["alpha"]
        adapter = base.get_adapter("alpha")
        assert isinstance(adapter, Adapter)
        source = make_source()
        http = FakeHttpClient({source.all_urls[0]: "<rss/>"})
        drafts = adapter.parse(adapter.fetch(http, source, window=make_window()), source)
        assert [draft.title for draft in drafts] == ["alpha"]
        with pytest.raises(UnknownAdapterError):
            base.get_adapter("beta")

    def test_broken_module_is_reported_and_others_still_load(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "alpha.py": ADAPTER_MODULE.format(name="alpha"),
                "broken.py": "import module_that_does_not_exist_anywhere\n",
            }
        )
        registry = install(package)
        with pytest.raises(AdapterDiscoveryError) as caught:
            registry.discover()
        assert list(caught.value.failures) == [f"{package}.broken"]
        assert "ModuleNotFoundError" in str(caught.value)
        assert registry.names() == ["alpha"]
        with pytest.raises(AdapterDiscoveryError):
            registry.discover()
        assert isinstance(base.get_adapter("alpha"), Adapter)
        with pytest.raises(UnknownAdapterError, match=f"failed to import: {package}.broken"):
            base.get_adapter("broken")

    def test_lenient_discovery_returns_the_failures(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "alpha.py": ADAPTER_MODULE.format(name="alpha"),
                "broken.py": "raise RuntimeError('boom')\n",
            }
        )
        install(package)
        expected = {f"{package}.broken": "RuntimeError: boom"}
        assert base.discover_adapters(strict=False) == expected
        assert base.discover_adapters(strict=False) == expected
        assert base.adapter_names() == ["alpha"]

    def test_duplicate_names_across_modules_are_an_error(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "one.py": ADAPTER_MODULE.format(name="same"),
                "two.py": ADAPTER_MODULE.format(name="same"),
            }
        )
        with pytest.raises(AdapterDiscoveryError, match="DuplicateAdapterError"):
            install(package).discover()

    def test_missing_package_is_reported(self, install: Installer) -> None:
        failures = install("package_that_does_not_exist_anywhere").discover(strict=False)
        assert list(failures) == ["package_that_does_not_exist_anywhere"]


class TestConcurrentDiscovery:
    def test_lookups_from_several_threads_wait_for_discovery(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        threads = 4
        install(make_package({"slow_one.py": SLOW_MODULE.format(name="slow_one")}))
        barrier = threading.Barrier(threads)

        def lookup() -> Adapter:
            barrier.wait(timeout=5)
            return base.get_adapter("slow_one")

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(lookup) for _ in range(threads)]
            adapters = [future.result(timeout=10) for future in futures]
        assert all(isinstance(adapter, Adapter) for adapter in adapters)
        assert len({id(adapter) for adapter in adapters}) == threads

    def test_names_and_failures_are_complete_for_every_thread(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        threads = 4
        package = make_package(
            {
                "broken.py": "raise RuntimeError('boom')\n",
                "slow_one.py": SLOW_MODULE.format(name="slow_one"),
            }
        )
        install(package)
        barrier = threading.Barrier(threads)

        def inspect() -> tuple[list[str], dict[str, str]]:
            barrier.wait(timeout=5)
            return base.adapter_names(), base.discover_adapters(strict=False)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(inspect) for _ in range(threads)]
            results = [future.result(timeout=10) for future in futures]
        expected = (["slow_one"], {f"{package}.broken": "RuntimeError: boom"})
        assert results == [expected] * threads

    def test_an_adapter_module_may_query_the_registry_while_it_is_imported(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "alpha.py": ADAPTER_MODULE.format(name="alpha"),
                "beta.py": REENTRANT_MODULE.format(name="beta"),
                "aaa_broken.py": "raise RuntimeError('boom')\n",
            }
        )
        install(package)
        failures = {f"{package}.aaa_broken": "RuntimeError: boom"}
        assert base.discover_adapters(strict=False) == failures
        assert base.adapter_names() == ["alpha", "beta"]
        beta = sys.modules[f"{package}.beta"]
        assert beta.names_during_import == ["alpha"]
        assert beta.failures_during_import == failures

    def test_an_interrupted_discovery_is_repeated(
        self, install: Installer, make_package: PackageBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interrupt_once = (
            "import os\n\n"
            "if os.environ.pop('SEMINARS_TEST_INTERRUPT', ''):\n"
            "    raise KeyboardInterrupt\n"
        )
        package = make_package(
            {"a_stop.py": interrupt_once, "beta.py": ADAPTER_MODULE.format(name="beta")}
        )
        registry = install(package)
        monkeypatch.setenv("SEMINARS_TEST_INTERRUPT", "1")
        with pytest.raises(KeyboardInterrupt):
            registry.discover()
        assert registry.names() == []
        assert registry.discover() == {}
        assert registry.names() == ["beta"]


class TestModulesImportedUnderAnotherRegistry:
    """A module body runs once, whichever registry is installed at that moment."""

    def test_discovery_adopts_adapters_that_registered_in_a_replaced_registry(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package({"alpha.py": ADAPTER_MODULE.format(name="alpha")})
        temporary = install()  # what the fake_registry fixture of some test does
        importlib.import_module(f"{package}.alpha")
        assert temporary.names() == ["alpha"]
        owner = install(package)  # the registry that discovers the package, as REGISTRY does
        assert owner.names() == []
        assert base.discover_adapters(strict=False) == {}
        assert base.adapter_names() == ["alpha"]
        adapter = base.get_adapter("alpha")
        assert isinstance(adapter, Adapter)
        assert type(adapter).__module__ == f"{package}.alpha"

    def test_adopted_adapters_do_not_leak_into_registries_of_other_packages(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package({"alpha.py": ADAPTER_MODULE.format(name="alpha")})
        install()
        importlib.import_module(f"{package}.alpha")
        other = install(make_package({"beta.py": ADAPTER_MODULE.format(name="beta")}))
        assert other.discover() == {}
        assert other.names() == ["beta"]

    def test_a_name_clash_found_while_adopting_is_reported_for_the_module(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        package = make_package(
            {
                "one.py": ADAPTER_MODULE.format(name="same"),
                "two.py": ADAPTER_MODULE.format(name="same"),
            }
        )
        for module in ("one", "two"):  # each under its own short-lived registry
            install()
            importlib.import_module(f"{package}.{module}")
        owner = install(package)
        failures = owner.discover(strict=False)
        assert list(failures) == [f"{package}.two"]
        assert "DuplicateAdapterError" in failures[f"{package}.two"]
        assert type(owner.create("same")).__module__ == f"{package}.one"

    def test_registering_by_hand_in_a_registry_is_not_remembered(
        self, install: Installer, make_package: PackageBuilder
    ) -> None:
        """Only ``register_adapter`` is replayed; ``registry.register`` stays local."""
        package = make_package({"helpers.py": "class Local:\n    pass\n"})
        temporary = install()
        module = importlib.import_module(f"{package}.helpers")
        temporary.register("local")(module.Local)
        owner = install(package)
        assert owner.discover() == {}
        assert owner.names() == []


class TestRealAdapterPackages:
    def test_default_packages(self) -> None:
        assert ADAPTER_PACKAGES == ("seminars.sources.generic", "seminars.sources.custom")

    def test_every_adapter_module_imports_and_names_are_unique(self) -> None:
        assert base.discover_adapters(strict=False) == {}

    def test_every_registered_adapter_follows_the_protocol(self) -> None:
        for name in base.adapter_names():
            assert isinstance(base.get_adapter(name), Adapter), name

    def test_adapters_are_named_after_their_module(self) -> None:
        for name in base.adapter_names():
            module = type(base.get_adapter(name)).__module__.rsplit(".", 1)[-1]
            assert name == module or name.startswith(f"{module}_"), (name, module)

    def test_generic_adapter_names_are_reserved(self) -> None:
        assert GENERIC_ADAPTERS == (
            "localist",
            "drupal_jsonapi",
            "ics",
            "rss",
            "jsonld",
            "html_list",
            "llm_page",
        )
        assert LLM_PAGE_ADAPTER in GENERIC_ADAPTERS
        for name in base.adapter_names():
            module = type(base.get_adapter(name)).__module__
            if name in GENERIC_ADAPTERS:
                assert module == f"seminars.sources.generic.{name}"

    @pytest.mark.parametrize("name", GENERIC_ADAPTERS)
    def test_a_generic_module_registers_the_adapter_named_after_it(self, name: str) -> None:
        module = f"seminars.sources.generic.{name}"
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} is not implemented yet")
        assert name in base.adapter_names()
        assert type(base.get_adapter(name)).__module__ == module

    def test_llm_page_adapter_only_fetches(self) -> None:
        """The events of an llm_page source come from the pipeline's LLM fallback."""
        if importlib.util.find_spec(f"seminars.sources.generic.{LLM_PAGE_ADAPTER}") is None:
            pytest.skip("seminars.sources.generic.llm_page is not implemented yet")
        source = make_source(adapter=LLM_PAGE_ADAPTER)
        assert source.wants_llm_fallback
        adapter = base.get_adapter(source.adapter)
        http = FakeHttpClient(dict.fromkeys(source.all_urls, "<html>talks</html>"))
        payload = adapter.fetch(http, source, window=make_window())
        assert http.requests == source.all_urls
        assert [document.url for document in payload.documents] == source.all_urls
        assert adapter.parse(payload, source) == []


class TestFetchHelpers:
    def test_fetch_urls_fetches_all_source_urls(self) -> None:
        source = make_source(urls=["https://bioengineering.stanford.edu/events"])
        http = FakeHttpClient(dict.fromkeys(source.all_urls, "body"))
        window = make_window()
        payload = fetch_urls(http, source, window=window, label="index")
        assert http.requests == source.all_urls
        assert payload.source_id == source.id
        assert payload.window == window
        assert payload.fetched_at == REFERENCE_NOW
        assert [document.label for document in payload.documents] == ["index", "index"]

    def test_fetch_urls_with_explicit_urls(self) -> None:
        source = make_source()
        http = FakeHttpClient({"https://example.edu/a": "a", "https://example.edu/b": "b"})
        payload = fetch_urls(http, source, window=make_window(), urls=["https://example.edu/b"])
        assert [document.text for document in payload.documents] == ["b\n"]
        assert payload.documents[0].label is None

    def test_http_errors_propagate(self) -> None:
        with pytest.raises(FixtureNotFoundError):
            fetch_urls(FakeHttpClient(), make_source(), window=make_window())

    def test_make_payload_uses_the_latest_fetch_time(self) -> None:
        later = REFERENCE_NOW.replace(hour=22)
        documents = [make_document(), make_document(fetched_at=later)]
        payload = make_payload(make_source(), documents, window=make_window())
        assert payload.fetched_at == later
        with pytest.raises(ValueError, match="at least one document"):
            make_payload(make_source(), [], window=make_window())

    def test_parse_error_is_an_adapter_error(self) -> None:
        assert issubclass(ParseError, base.AdapterError)


PAGE_URL = "https://dept.example.edu/events/upcoming?page=2"
OTHER_SCHEMES = ["mailto:jane@example.edu", "tel:+16505551234", "javascript:void(0)"]
OTHER_SCHEMES += ["ftp://example.edu/file", "data:text/plain,hello"]


class TestAbsoluteUrl:
    @pytest.mark.parametrize(
        ("link", "expected"),
        [
            ("https://other.example.org/talk", "https://other.example.org/talk"),
            ("/events/42", "https://dept.example.edu/events/42"),
            ("42", "https://dept.example.edu/events/42"),
            ("../people/jane", "https://dept.example.edu/people/jane"),
            ("?page=3", "https://dept.example.edu/events/upcoming?page=3"),
            ("//cdn.example.org/x", "https://cdn.example.org/x"),
            ("#abstract", "https://dept.example.edu/events/upcoming?page=2#abstract"),
            ("  /events/42\n", "https://dept.example.edu/events/42"),
            ("/events/my talk", "https://dept.example.edu/events/my%20talk"),
            ("/caf" + chr(0xE9), "https://dept.example.edu/caf%C3%A9"),
            ("/a%20b?q=1&r=%C3%A9", "https://dept.example.edu/a%20b?q=1&r=%C3%A9"),
        ],
    )
    def test_links_are_resolved_against_the_page(self, link: str, expected: str) -> None:
        assert absolute_url(PAGE_URL, link) == expected
        assert make_draft(url=absolute_url(PAGE_URL, link)).url == expected

    @pytest.mark.parametrize("link", [None, "", "   ", "http://[broken", *OTHER_SCHEMES])
    def test_links_that_are_not_web_pages_give_none(self, link: str | None) -> None:
        assert absolute_url(PAGE_URL, link) is None
        assert make_draft(url=absolute_url(PAGE_URL, link)).url is None

    def test_a_relative_link_needs_an_absolute_base(self) -> None:
        assert absolute_url("/events", "42") is None
        assert absolute_url("", "https://example.edu/x") == "https://example.edu/x"

    def test_markup_in_a_link_is_percent_encoded(self) -> None:
        link = 'x"><script>alert(1)</script>'
        assert absolute_url("https://base.example/dir/page", link) == (
            "https://base.example/dir/x%22%3E%3Cscript%3Ealert(1)%3C/script%3E"
        )
        assert absolute_url(PAGE_URL, '/x#"><img src=x onerror=alert(1)>') == (
            "https://dept.example.edu/x#%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E"
        )

    @pytest.mark.parametrize(
        ("char", "escape"),
        [
            ('"', "%22"),
            ("<", "%3C"),
            (">", "%3E"),
            ("\\", "%5C"),
            ("`", "%60"),
            ("{", "%7B"),
            ("|", "%7C"),
            ("}", "%7D"),
            ("^", "%5E"),
            (" ", "%20"),
            ("\x7f", "%7F"),
        ],
    )
    def test_what_a_url_may_not_contain_as_it_is_gets_encoded(self, char: str, escape: str) -> None:
        expected = f"https://dept.example.edu/a{escape}b?q={escape}c#f{escape}g"
        assert absolute_url(PAGE_URL, f"/a{char}b?q={char}c#f{char}g") == expected
        assert absolute_url(PAGE_URL, expected) == expected

    @pytest.mark.parametrize(
        "link",
        [
            "/a%20b%22?q=%3Cb%3E&r=%C3%A9#%7Bx%7D",
            "/100%/x?rate=5%&odd=%zz",
            "/a!$&'()*+,;=:@~._-/x?page[limit]=50&filter[a]=b#frag",
            "/people/@jane?mail=jane@example.edu",
        ],
    )
    def test_escapes_and_the_characters_a_url_may_contain_stay_as_written(self, link: str) -> None:
        assert absolute_url(PAGE_URL, link) == f"https://dept.example.edu{link}"

    @pytest.mark.parametrize(
        "link",
        [
            "http://user:pw@evil.example/",
            "https://user@evil.example/x",
            "//user:pw@cdn.example.org/x",
            "http://@evil.example/",
            "http://:pw@evil.example/",
            "http://a.example\\@evil.example/",
            "http://dept.example.edu%40evil.example@other.example/",
        ],
    )
    def test_a_link_with_a_user_name_or_password_gives_none(self, link: str) -> None:
        assert absolute_url(PAGE_URL, link) is None

    def test_a_base_with_a_user_name_gives_none_for_relative_links(self) -> None:
        assert absolute_url("https://user:pw@dept.example.edu/events", "42") is None
        assert absolute_url("https://user:pw@dept.example.edu/events", "https://x.example/") == (
            "https://x.example/"
        )

    @given(st.text(max_size=60))
    def test_whatever_the_link_the_result_is_none_or_a_plain_ascii_url(self, link: str) -> None:
        url = absolute_url(PAGE_URL, link)
        if url is None:
            return
        assert all("!" <= char <= "~" for char in url)
        assert not set(url) & set('"<>\\^`{|}')
        assert url.startswith(("http://", "https://"))
        assert "@" not in urlsplit(url).netloc
        assert absolute_url(PAGE_URL, url) == url
        assert make_draft(url=url).url == url

    def test_the_base_of_a_redirected_page_is_its_final_url(self) -> None:
        document = make_document(
            "https://dept.example.edu/events", final_url="https://www.example.edu/dept/events/"
        )
        assert absolute_url(document.final_url, "talk-1") == (
            "https://www.example.edu/dept/events/talk-1"
        )
