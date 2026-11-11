"""The Adapter protocol and the adapter registry.

An adapter turns one configured source into event drafts in two steps::

    payload = adapter.fetch(http, source, window=window)   # all I/O, pagination included
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
(``adapter: my_format`` in config/sources.d/*.yaml). An adapter is named
after the module that defines it (see GENERIC_ADAPTERS for the generic ones).

Rules for adapters:

* ``fetch`` uses nothing but the given ``HttpClient`` for I/O and returns
  every document it received. It may read a response as far as needed to
  find the next request (pagination, detail links) but leaves the
  interpretation of events to ``parse``. It must issue the same URLs offline
  as live, because offline mode looks fixtures up by URL. ``window`` tells it
  which period to ask the source for; query parameters derived from it must
  be recorded as volatile (see tests/fixtures/README.md).
* ``fetch`` stays on the hosts of its source. It requests a URL only when
  ``on_source_host(source, url)`` is true: the host is one of the source's
  ``url`` / ``urls`` / ``homepage``, or it is listed under
  ``params.extra_hosts`` of the source. Links found in a page (detail
  pages, the next page) are checked before they are fetched and skipped
  otherwise; ``fetch_urls`` does the check and raises ``ForeignHostError``.
  The pauses and request budgets of the HTTP client are kept per host
  name, so a page that lists links to other hosts could otherwise steer
  requests at third parties or use up the budget of another source.
* ``parse`` is a pure function of the payload and the source entry. Use
  ``payload.fetched_at`` as "now" (for example to infer a missing year).
  Copy what the source says into ``EventDraft``; cleaning is not its job.
* Times and URLs are the exception, because only the adapter knows the page
  and the format they came from. ``EventDraft`` documents what a date, a
  date-only string and the end of an all-day event mean; convert the
  source's convention to that one. Every URL in a draft is absolute:
  resolve each link with ``absolute_url(document.final_url, link)``, which
  gives ``None`` for anything that is not a web page.
* Raise ``ParseError`` when a response cannot be understood at all; skip
  single malformed items instead of failing the whole source. A draft that
  breaks the rules above raises ``pydantic.ValidationError`` when it is
  built; that is a malformed item too.
* Adapter classes take no constructor arguments and keep no state between
  calls.
* Decorate the class in the module that defines it. Discovery relies on
  that to find the adapters of a module that was imported earlier, at a time
  when a test had replaced the registry.

Thread safety: ``get_adapter``, ``adapter_names`` and ``discover_adapters``
may be called from several threads at once, for example from one worker
thread per source. The first caller imports the adapter modules while the
others wait, so every caller sees the complete registry. An adapter module
should not call them while it is being imported; if it does, it sees the
adapters registered so far.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
import re
import threading
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

from seminars.httpbase import HttpClient
from seminars.httpurl import canonical_host
from seminars.models import (
    EventDraft,
    FetchedDocument,
    RawPayload,
    SourceConfig,
    TimeWindow,
    ascii_url,
    check_http_url,
)
from seminars.textrules import ascii_printable

ADAPTER_PACKAGES: Final = ("seminars.sources.generic", "seminars.sources.custom")
"""Packages whose modules are imported by adapter discovery."""

ADAPTER_NAME_PATTERN: Final = r"[a-z][a-z0-9_]*"

GENERIC_ADAPTERS: Final = (
    "localist",  # Localist JSON API (events.stanford.edu)
    "drupal_jsonapi",  # Drupal JSON:API, node type hs_event
    "ics",  # iCalendar feeds
    "rss",  # RSS / Atom feeds, optionally with detail pages
    "jsonld",  # schema.org/Event JSON-LD embedded in pages
    "html_list",  # HTML lists driven by selectors in ``params``
    "llm_page",  # pages without structure: fetched only, read by the LLM page extractor
)
"""Names reserved for the generic adapters.

Each one is registered by the module of the same name in
``seminars.sources.generic``. Any other adapter is named after its own
module (``seminars.sources.custom.<name>``), or ``<module name>_<variant>``
when a module holds several, which keeps names unique without coordination.

``llm_page`` is an ordinary adapter for the registry and for the pipeline.
``seminars.sources.generic.llm_page`` registers it (next to
``build_page_extractor``): its ``fetch`` is ``fetch_urls`` and its ``parse``
always returns an empty list. The events come from the LLM fallback that the
pipeline applies to every source with ``wants_llm_fallback`` whose adapter
produced no drafts (see ``seminars.contracts.PageExtractor``), so nothing
ever special-cases the adapter name.
"""


class AdapterError(Exception):
    """Base class of adapter and registry errors."""


class ParseError(AdapterError):
    """An adapter could not make sense of a response."""


class UnknownAdapterError(AdapterError, LookupError):
    """No adapter is registered under the requested name."""


class DuplicateAdapterError(AdapterError):
    """Two different adapters were registered under one name."""


class ForeignHostError(AdapterError):
    """An adapter was about to request a URL on a host that its source does not name."""

    def __init__(self, source_id: str, url: str) -> None:
        self.source_id = source_id
        self.url = url
        super().__init__(
            f"source {source_id}: {ascii_printable(url)} is not on a host of this source; "
            "an adapter only requests URLs on the hosts of its url, urls and homepage. "
            "Name any other host under params.extra_hosts of the source"
        )


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
        """Download everything needed for ``source`` and return it unparsed.

        An adapter requests only URLs for which ``on_source_host(source,
        url)`` is true. A source that needs another host (detail pages on a
        campus-wide calendar, for example) names it in ``params.extra_hosts``.
        """
        ...

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        """Turn a payload into drafts without any I/O."""
        ...


AdapterFactory = Callable[[], Adapter]
"""A zero-argument callable returning an adapter; adapter classes qualify."""


_DECLARED: Final[dict[str, dict[str, AdapterFactory]]] = {}
"""Everything ``register_adapter`` ever registered: defining module -> {adapter name: factory}.

A module body runs only once per process, so its decorators register in
whichever registry is current at that moment. When that is a registry a
test installed, the process-wide one would never hear of the adapter. This
table is not replaced with the registry; discovery reads it for every module
it imports and registers what is missing.
"""


class AdapterRegistry:
    """Maps adapter names to factories and imports the adapter packages on demand.

    ``discover`` is safe to call from several threads; see the module
    docstring. ``register`` deliberately takes no lock: it runs while a
    module is being imported, and waiting for a lock there could deadlock
    with a thread that holds the lock and waits for that very import.
    """

    def __init__(self, packages: Sequence[str] = ADAPTER_PACKAGES) -> None:
        self._packages = tuple(packages)
        self._factories: dict[str, AdapterFactory] = {}
        self._discovery_lock = threading.RLock()
        self._failures: dict[str, str] | None = None  # None until discovery has finished
        self._collecting: dict[str, str] | None = None  # failures so far, while discovery runs

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

        Calls from other threads block until the imports are done. A call
        made by an adapter module while it is being imported does not start
        a second round; it reports the failures collected so far. Discovery
        that was interrupted (``KeyboardInterrupt``) starts over next time.
        """
        with self._discovery_lock:
            if self._failures is None and self._collecting is None:
                collected: dict[str, str] = {}
                self._collecting = collected
                try:
                    for package_name in self._packages:
                        for module_name in _import_tree(package_name, collected):
                            self._adopt(module_name, collected)
                    self._failures = collected
                finally:
                    self._collecting = None
            known = self._failures if self._failures is not None else self._collecting
            failures = dict(known or {})
        if failures and strict:
            raise AdapterDiscoveryError(failures)
        return failures

    def _adopt(self, module_name: str, failures: dict[str, str]) -> None:
        """Register what ``module_name`` declared while another registry was current.

        A no-op for a module that was imported by this discovery, because
        registering the same factory again changes nothing.
        """
        for name, factory in _DECLARED.get(module_name, {}).items():
            try:
                self.register(name)(factory)
            except DuplicateAdapterError as error:
                failures[module_name] = f"{type(error).__name__}: {error}"


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


def _import_tree(package_name: str, failures: dict[str, str]) -> list[str]:
    """Import ``package_name`` and everything below it; return the modules that imported."""
    package = _import(package_name, failures)
    if package is None:
        return []
    imported = [package_name]
    if not hasattr(package, "__path__"):
        return imported
    children = sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name)
    for info in children:
        child = f"{package_name}.{info.name}"
        if info.ispkg:
            imported.extend(_import_tree(child, failures))
        elif _import(child, failures) is not None:
            imported.append(child)
    return imported


REGISTRY = AdapterRegistry()
"""The process-wide registry behind the functions below.

They look the name up when they are called, so a test can replace it with an
empty ``AdapterRegistry`` (``monkeypatch.setattr(base, "REGISTRY", ...)``;
the ``fake_registry`` fixture does it). An adapter module that is imported
for the first time while a replacement is installed registers there; this
registry adopts its adapters the next time it discovers.
"""


def register_adapter[F: AdapterFactory](name: str) -> Callable[[F], F]:
    """Class decorator: register an adapter in the process-wide registry.

    The registration is also remembered for the module that defines the
    class, so that a registry which discovers that module later still gets
    the adapter (see ``_DECLARED``).
    """
    register = REGISTRY.register(name)

    def decorate(factory: F) -> F:
        registered = register(factory)
        module_name = getattr(factory, "__module__", None)
        if isinstance(module_name, str):
            _DECLARED.setdefault(module_name, {})[name] = factory
        return registered

    return decorate


def discover_adapters(*, strict: bool = True) -> dict[str, str]:
    """Import all adapter modules (idempotent, thread-safe); see ``AdapterRegistry.discover``."""
    return REGISTRY.discover(strict=strict)


def adapter_names() -> list[str]:
    """Names of all adapters that could be loaded, after discovery."""
    REGISTRY.discover(strict=False)
    return REGISTRY.names()


def get_adapter(name: str) -> Adapter:
    """A new instance of the adapter called ``name``, discovering adapters first.

    A module that fails to import only makes its own adapters unknown; the
    ``UnknownAdapterError`` then names the modules that failed. Safe to call
    from several threads.
    """
    REGISTRY.discover(strict=False)
    return REGISTRY.create(name)


def absolute_url(base: str, link: str | None) -> str | None:
    """``link`` as an absolute http(s) URL, or ``None`` when it does not name a web page.

    ``base`` is the address of the document the link was found in; pass
    ``document.final_url`` so that links on a redirected page resolve
    correctly. Relative links are resolved against it, surrounding
    whitespace is dropped, and whatever a URL may not contain as it is gets
    percent-encoded (``seminars.models.ascii_url``): characters outside
    printable ASCII, spaces, and the double quote, ``<``, ``>``, backslash,
    ``^``, backtick, ``{``, ``|`` and ``}``, so a link cannot carry markup
    into a draft. Escapes that are there already stay as they are.

    Missing and empty links, other schemes (``mailto:``, ``tel:``,
    ``javascript:``), links that cannot be parsed and links with a user
    name or password (``https://user:secret@host/``) give ``None``. Use it
    for every URL that goes into an ``EventDraft``.
    """
    if link is None or not link.strip():
        return None
    try:
        url = check_http_url(ascii_url(urljoin(base, link.strip())))
        return None if "@" in urlsplit(url).netloc else url
    except ValueError:
        return None


def _host_of(url: str) -> str | None:
    """The host of ``url`` in canonical form; ``None`` when the text has no host."""
    try:
        return canonical_host(urlsplit(url).hostname or "")
    except ValueError:
        return None


def source_hosts(source: SourceConfig) -> frozenset[str]:
    """The hosts ``source`` is fetched from, in the spelling the HTTP client uses.

    These are the hosts of its ``url`` / ``urls`` and of its ``homepage``,
    plus the host names listed under ``params.extra_hosts``::

        params: {extra_hosts: [events.stanford.edu]}

    An entry of that list which is not a host name (a URL, a path, a
    number) adds nothing, and neither does a value that is not a list.
    """
    urls = [*source.all_urls, *([source.homepage] if source.homepage else [])]
    hosts = {_host_of(url) for url in urls}
    extra = source.params.get("extra_hosts", [])
    for name in extra if isinstance(extra, list) else []:
        if isinstance(name, str):
            with contextlib.suppress(ValueError):
                hosts.add(canonical_host(name))
    return frozenset(host for host in hosts if host is not None)


def on_source_host(source: SourceConfig, url: str) -> bool:
    """Whether ``url`` is on one of the hosts of ``source`` (``source_hosts``).

    This is the test every adapter makes before it requests a URL that it
    did not take from the configuration: a detail link, the next page, a
    feed that a page points to. Only the host counts, whatever the scheme,
    the port and the path are; ``www.example.edu`` and ``example.edu`` are
    two hosts. Text that is no absolute URL is on no host.
    """
    return _host_of(url) in source_hosts(source)


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
    ``label`` is attached to every document. Raises ``ForeignHostError``,
    before anything is requested, when one of ``urls`` is not on a host of
    the source (``on_source_host``).
    """
    targets = list(source.all_urls if urls is None else urls)
    hosts = source_hosts(source)
    for url in targets:
        if _host_of(url) not in hosts:
            raise ForeignHostError(source.id, url)
    documents = [http.get(url) for url in targets]
    if label is not None:
        documents = [document.evolve(label=label) for document in documents]
    return make_payload(source, documents, window=window)
