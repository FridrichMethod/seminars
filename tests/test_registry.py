"""Tests for the Adapter protocol, the registry and adapter auto-discovery."""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from factories import REFERENCE_NOW, make_document, make_source, make_window
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
        assert [document.text for document in payload.documents] == ["b"]
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
