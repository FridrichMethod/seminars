"""The Adapter protocol and the adapter registry.

An adapter turns one configured source into event drafts in two steps::

    payload = adapter.fetch(http, source, window=window)   # I/O, no parsing decisions
    drafts = adapter.parse(payload, source)                # pure, no I/O

Writing an adapter means adding ONE module below ``seminars.sources.generic``
(configuration-driven, reusable formats) or ``seminars.sources.custom`` (one
odd site each) and decorating a class::

    from seminars.sources.base import fetch_urls, register_adapter

    @register_adapter("my_format")
    class MyFormatAdapter:
        def fetch(self, http, source, *, window):
            return fetch_urls(http, source, window=window)

        def parse(self, payload, source):
            return [EventDraft(source_id=source.id, extraction="html", ...)]

Every module in those two packages is imported automatically (recursively),
so no shared file has to be edited. A source selects its adapter by name
(``adapter: my_format`` in config/sources.d/*.yaml).

Rules for adapters:

* ``fetch`` uses nothing but the given ``HttpClient`` for I/O and returns
  every document it received; it must issue the same URLs offline as live,
  because offline mode looks fixtures up by URL. ``window`` tells it which
  period to ask the source for.
* ``parse`` is a pure function of the payload and the source entry. Use
  ``payload.fetched_at`` as "now" (for example to infer a missing year).
  Copy what the source says into ``EventDraft``; cleaning is not its job.
* Raise ``ParseError`` when a response cannot be understood at all; skip
  single malformed items instead of failing the whole source.
* Adapter classes take no constructor arguments and keep no state between
  calls.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Final, Protocol, runtime_checkable

from seminars.httpbase import HttpClient
from seminars.models import EventDraft, FetchedDocument, RawPayload, SourceConfig, TimeWindow

ADAPTER_PACKAGES: Final = ("seminars.sources.generic", "seminars.sources.custom")
"""Packages whose modules are imported by adapter discovery."""

ADAPTER_NAME_PATTERN: Final = r"[a-z][a-z0-9_]*"


class AdapterError(Exception):
    """Base class of adapter and registry errors."""


class ParseError(AdapterError):
    """An adapter could not make sense of a response."""


class UnknownAdapterError(AdapterError, LookupError):
    """No adapter is registered under the requested name."""


class DuplicateAdapterError(AdapterError):
    """Two different adapters were registered under one name."""


class AdapterDiscoveryError(AdapterError):
    """One or more adapter modules failed to import."""

    def __init__(self, failures: dict[str, str]) -> None:
        self.failures = failures
        details = "; ".join(f"{module}: {reason}" for module, reason in sorted(failures.items()))
        super().__init__(f"adapter modules failed to import: {details}")


@runtime_checkable
class Adapter(Protocol):
    """What every source adapter implements; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        """Download everything needed for ``source`` and return it unparsed."""
        ...

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        """Turn a payload into drafts without any I/O."""
        ...


AdapterFactory = Callable[[], Adapter]
"""A zero-argument callable returning an adapter; adapter classes qualify."""


class AdapterRegistry:
    """Maps adapter names to factories and imports the adapter packages on demand."""

    def __init__(self, packages: Sequence[str] = ADAPTER_PACKAGES) -> None:
        self._packages = tuple(packages)
        self._factories: dict[str, AdapterFactory] = {}
        self._failures: dict[str, str] | None = None  # None until discovery has run

    def register[F: AdapterFactory](self, name: str) -> Callable[[F], F]:
        """Class decorator that registers an adapter under ``name``.

        Registering a second, different adapter under the same name raises
        ``DuplicateAdapterError``; registering the same object again is a no-op.
        """
        if re.fullmatch(ADAPTER_NAME_PATTERN, name) is None:
            raise ValueError(f"invalid adapter name {name!r}: use lower-case letters, digits, '_'")

        def decorate(factory: F) -> F:
            existing = self._factories.get(name)
            if existing is not None and existing is not factory:
                raise DuplicateAdapterError(
                    f"adapter name {name!r} is already registered by "
                    f"{_describe(existing)}; {_describe(factory)} cannot reuse it"
                )
            self._factories[name] = factory
            return factory

        return decorate

    def names(self) -> list[str]:
        """Registered adapter names, sorted."""
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def create(self, name: str) -> Adapter:
        """A new instance of the adapter registered as ``name``."""
        factory = self._factories.get(name)
        if factory is None:
            known = ", ".join(self.names()) or "none"
            broken = ", ".join(sorted(self._failures or {}))
            hint = f"; modules that failed to import: {broken}" if broken else ""
            raise UnknownAdapterError(f"unknown adapter {name!r} (registered: {known}){hint}")
        return factory()

    def discover(self, *, strict: bool = True) -> dict[str, str]:
        """Import every module below the registry's packages so that adapters register.

        Only the first call imports anything; later calls report the same
        result. Import failures are collected per module as
        ``{module name: error}``. With ``strict`` they raise
        ``AdapterDiscoveryError`` (after all modules were tried); otherwise
        they are returned and the healthy modules stay usable.
        """
        if self._failures is None:
            failures: dict[str, str] = {}
            self._failures = failures
            for package_name in self._packages:
                _import_tree(package_name, failures)
        if self._failures and strict:
            raise AdapterDiscoveryError(dict(self._failures))
        return dict(self._failures)


def _describe(factory: AdapterFactory) -> str:
    module = getattr(factory, "__module__", "?")
    name = getattr(factory, "__qualname__", repr(factory))
    return f"{module}.{name}"


def _import(name: str, failures: dict[str, str]) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except Exception as error:  # a broken adapter module must not take the others down
        failures[name] = f"{type(error).__name__}: {error}"
        return None


def _import_tree(package_name: str, failures: dict[str, str]) -> None:
    package = _import(package_name, failures)
    if package is None or not hasattr(package, "__path__"):
        return
    children = sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name)
    for info in children:
        child = f"{package_name}.{info.name}"
        if info.ispkg:
            _import_tree(child, failures)
        else:
            _import(child, failures)


REGISTRY = AdapterRegistry()
"""The process-wide registry behind the functions below.

They look the name up when they are called, so a test can replace it with an
empty ``AdapterRegistry`` (``monkeypatch.setattr(base, "REGISTRY", ...)``).
"""


def register_adapter[F: AdapterFactory](name: str) -> Callable[[F], F]:
    """Class decorator: register an adapter in the process-wide registry."""
    return REGISTRY.register(name)


def discover_adapters(*, strict: bool = True) -> dict[str, str]:
    """Import all adapter modules (idempotent); see ``AdapterRegistry.discover``."""
    return REGISTRY.discover(strict=strict)


def adapter_names() -> list[str]:
    """Names of all adapters that could be loaded, after discovery."""
    REGISTRY.discover(strict=False)
    return REGISTRY.names()


def get_adapter(name: str) -> Adapter:
    """A new instance of the adapter called ``name``, discovering adapters first.

    A module that fails to import only makes its own adapters unknown; the
    ``UnknownAdapterError`` then names the modules that failed.
    """
    REGISTRY.discover(strict=False)
    return REGISTRY.create(name)


def make_payload(
    source: SourceConfig, documents: Sequence[FetchedDocument], *, window: TimeWindow
) -> RawPayload:
    """Wrap fetched documents into the payload of ``source``.

    ``fetched_at`` is the time of the latest document, so it is the recording
    time when the documents come from fixtures.
    """
    if not documents:
        raise ValueError(f"source {source.id}: a payload needs at least one document")
    return RawPayload(
        source_id=source.id,
        fetched_at=max(document.fetched_at for document in documents),
        window=window,
        documents=list(documents),
    )


def fetch_urls(
    http: HttpClient,
    source: SourceConfig,
    *,
    window: TimeWindow,
    urls: Sequence[str] | None = None,
    label: str | None = None,
) -> RawPayload:
    """Fetch ``urls`` (default: all URLs of the source) into one payload.

    This is the complete ``fetch`` step of adapters that need no pagination.
    ``label`` is attached to every document.
    """
    documents = [http.get(url) for url in (source.all_urls if urls is None else urls)]
    if label is not None:
        documents = [document.evolve(label=label) for document in documents]
    return make_payload(source, documents, window=window)
