"""Recorded HTTP fixtures: the store, the offline client and the recorder.

Layout (see tests/fixtures/README.md for the full convention)::

    tests/fixtures/<source_id>/
        manifest.json     maps every recorded request URL to its body file
        <file>            one recorded response body per URL, text only
        README.md         optional notes, e.g. what was trimmed and why

``FixtureStore`` reads and writes one such directory. ``OfflineHttpClient``
is an ``HttpClient`` that serves recorded bodies by URL and raises
``FixtureNotFoundError`` for anything that was not recorded; it backs
``seminars run --offline`` and every adapter test. The command line fetches
through the polite client, so robots.txt and Crawl-delay are honoured while
researching sources too::

    uv run python -m seminars.fixtures record --source-id ID URL [URL ...] [--name FILE]
    uv run python -m seminars.fixtures fetch URL [URL ...] [--save-dir DIR]
    uv run python -m seminars.fixtures check [--source-id ID]

``record`` stores what it fetched as a fixture. ``fetch`` is for looking
around: it shows status, final URL, content type and size of every answer
and writes no fixture and no manifest; with ``--save-dir`` it also puts each
body into a scratch directory (``.scratch/<topic>`` is ignored by git, and
it is the only place inside the project that ``fetch`` writes to).
``--source-id`` takes a source id, never a path.
Both are the only way this project talks to a source site by hand: never
use curl or another direct network tool, which would skip robots.txt, the
crawl delay and the fixed User-Agent.

Give ``record`` all URLs of one host in a single call: they are fetched one
after another with the pause the host asks for. Calls that run at the same
time and share a cache directory wait for each other. All git worktrees of
one repository share it by default; separate clones only do when
``SEMINARS_HTTP_CACHE_DIR`` names one directory for all of them (see
``seminars.config.ProjectPaths.http_cache_dir``). The command prints the
directory it uses on stderr before its first request. When that directory
cannot be created or locked, ``record`` and ``fetch`` say so and end with
status 1 without fetching anything: they never run unprotected.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self, TextIO
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from pydantic import AfterValidator, ValidationError

from seminars.config import ENV_HTTP_CACHE_DIR, ProjectPaths, find_project_root, load_settings
from seminars.httpbase import (
    HttpClient,
    HttpError,
    HttpStatusError,
    Resolver,
    is_binary_media_type,
)
from seminars.httpcache import lock_problem
from seminars.httpurl import split_url
from seminars.models import SOURCE_ID_PATTERN, FetchedDocument, Model, dump_json
from seminars.models.base import SortedStrList, SourceId, Timestamp, sorted_dict
from seminars.textio import read_text, write_text_atomic
from seminars.textrules import ascii_printable, ascii_safe

if TYPE_CHECKING:
    from seminars.http import PoliteClient

MANIFEST_NAME: Final = "manifest.json"
MAX_FIXTURE_BYTES: Final = 500_000
"""Bodies larger than this must be trimmed by hand (and the trim noted in a README)."""

_RESERVED_NAMES: Final = frozenset({MANIFEST_NAME})
_MAX_SOURCE_ID_LENGTH: Final = 64
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_NOTE_PREFIX: Final = "README"
_EXTENSIONS: Final[dict[str, str]] = {
    "application/json": ".json",
    "application/vnd.api+json": ".json",
    "application/ld+json": ".json",
    "application/rss+xml": ".xml",
    "application/atom+xml": ".xml",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/calendar": ".ics",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
}

FIXTURE_EXTENSIONS: Final = tuple(sorted({*_EXTENSIONS.values(), ".md"}))
"""What the name of a file in a fixture directory ends in: a body type, or ``.md`` for notes.

A body is whatever a site sent, so it must never become a file that is
executed. pytest imports every ``conftest.py`` and ``test_*.py`` it comes
across; with these extensions only, nothing in a fixture directory is a
program. ``FixtureStore.add`` writes no other file and ``check`` reports
any other.
"""

_FILE_NAME: Final = re.compile(
    "[A-Za-z0-9][A-Za-z0-9._-]*(?:" + "|".join(map(re.escape, FIXTURE_EXTENSIONS)) + ")"
)


def is_fixture_file_name(name: str) -> bool:
    """Whether ``name`` can be a file of a fixture directory.

    It starts with a letter or a digit, consists of letters, digits, ``.``,
    ``_`` and ``-``, and ends in one of ``FIXTURE_EXTENSIONS`` (lower case).
    """
    return _FILE_NAME.fullmatch(name) is not None


def _fixture_file_name(name: str) -> str:
    if not is_fixture_file_name(name):
        allowed = ", ".join(FIXTURE_EXTENSIONS)
        raise ValueError(
            f"{name[:80]!a} is not a fixture file name: use letters, digits, '.', '_' and '-' "
            f"and one of the extensions {allowed}"
        )
    return name


FileName = Annotated[str, AfterValidator(_fixture_file_name)]


class FixtureError(ValueError):
    """A fixture directory is inconsistent or a body cannot be stored."""


class FixtureNotFoundError(HttpError, LookupError):
    """Offline mode was asked for a URL that has no recorded fixture."""


class FixtureEntry(Model):
    """Manifest record of one recorded request."""

    file: FileName  # body file inside the source directory
    status: int = 200
    content_type: str | None = None
    fetched_at: Timestamp
    final_url: str | None = None  # set only when the request was redirected
    volatile_params: SortedStrList = []  # query parameters whose value is ignored on lookup


class FixtureManifest(Model):
    """Content of tests/fixtures/<source_id>/manifest.json."""

    schema_version: Literal[1] = 1
    source_id: SourceId
    entries: Annotated[dict[str, FixtureEntry], AfterValidator(sorted_dict)] = {}


def fixture_key(url: str, volatile_params: Iterable[str] = ()) -> str:
    """Canonical form of a URL for fixture lookup.

    Scheme and host are lower-cased, a default port is dropped, the path is
    percent-decoded, the fragment is dropped and the query parameters are
    sorted, so that the order and the encoding of parameters do not matter.
    Parameters named in ``volatile_params`` match any value (their value
    becomes ``*``).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    volatile = set(volatile_params)
    pairs = sorted(
        (name, "*" if name in volatile else value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    query = f"?{urlencode(pairs)}" if pairs else ""
    return f"{scheme}://{host}{unquote(parts.path) or '/'}{query}"


def default_file_name(url: str, content_type: str | None) -> str:
    """Readable, collision-free file name for the body of ``url``."""
    path = unquote(urlsplit(url).path).lower()
    stem = re.sub(r"[^a-z0-9]+", "-", path).strip("-")[:40].strip("-") or "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}{_EXTENSIONS.get(content_type or '', '.txt')}"


def scratch_file_name(url: str, content_type: str | None) -> str:
    """File name for the body of ``url`` in a scratch directory (``fetch --save-dir``).

    Host, path and a digest of the whole URL, so that pages of different
    sites sort apart and two URLs never share a file. Only lower-case
    letters, digits, ``-`` and the dot of the extension occur, whatever the
    URL contains: the name can never leave the directory it is used in.
    """
    host = (urlsplit(url).hostname or "").lower()
    prefix = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:40].strip("-") or "host"
    return f"{prefix}-{default_file_name(url, content_type)}"


def is_source_id(value: str) -> bool:
    """Whether ``value`` has the form of a source id (``seminars.models.SourceId``).

    A source id names a directory below tests/fixtures. Checking it before
    a path is built from it is what keeps ``../elsewhere`` and ``a/b`` out.
    """
    return (
        len(value) <= _MAX_SOURCE_ID_LENGTH and re.fullmatch(SOURCE_ID_PATTERN, value) is not None
    )


class FixtureStore:
    """The recorded fixtures of one source (one directory below tests/fixtures/).

    Build it with ``FixtureStore.of(fixtures_dir, source_id)`` whenever the
    id comes from outside (a command line, a configuration): that checks
    the id before it becomes part of a path. The constructor takes the
    directory itself and only looks at its last component.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.source_id = directory.name
        if not is_source_id(self.source_id):
            raise FixtureError(f"{directory}: directory name is not a valid source id")
        self.manifest = self._load()

    @classmethod
    def of(cls, fixtures_dir: Path, source_id: str) -> Self:
        """The store of ``source_id`` in ``fixtures_dir``; ``FixtureError`` for anything else."""
        if not is_source_id(source_id):
            raise FixtureError(f"{source_id[:80]!a} is not a valid source id")
        return cls(fixtures_dir / source_id)

    def _load(self) -> FixtureManifest:
        path = self.directory / MANIFEST_NAME
        if not path.is_file():
            return FixtureManifest(source_id=self.source_id)
        try:
            return FixtureManifest.model_validate_json(read_text(path))
        except (OSError, ValidationError) as error:
            raise FixtureError(f"{path}: invalid fixture manifest: {error}") from error

    def find(self, url: str) -> tuple[str, FixtureEntry] | None:
        """The recorded URL and entry that ``url`` matches, if any."""
        entry = self.manifest.entries.get(url)
        if entry is not None:
            return url, entry
        for recorded, candidate in self.manifest.entries.items():
            volatile = candidate.volatile_params
            if fixture_key(url, volatile) == fixture_key(recorded, volatile):
                return recorded, candidate
        return None

    def read(self, url: str) -> FetchedDocument | None:
        """The recorded response for ``url`` as a document, or ``None``."""
        found = self.find(url)
        if found is None:
            return None
        _, entry = found
        return FetchedDocument(
            url=url,
            final_url=entry.final_url or url,
            status=entry.status,
            content_type=entry.content_type,
            text=read_text(self.directory / entry.file),
            fetched_at=entry.fetched_at,
        )

    def add(
        self,
        document: FetchedDocument,
        *,
        name: str | None = None,
        volatile_params: Sequence[str] = (),
    ) -> FixtureEntry:
        """Store ``document`` as the fixture of ``document.url`` and update the manifest.

        Recording a URL again replaces its entry and body. Binary content is
        refused, and so is a ``name`` that is not a fixture file name
        (``is_fixture_file_name``). Without ``name`` the previous file name
        of the URL is kept, or a new one is derived from the URL. The body
        is written exactly as ``document.text``, which is already in
        canonical form (no byte order mark, LF line endings, final newline;
        see ``FetchedDocument``).
        """
        if is_binary_media_type(document.content_type) or "\x00" in document.text:
            raise FixtureError(f"refusing to store binary content of {ascii_safe(document.url)}")
        found = self.find(document.url)
        previous = found[1] if found is not None else None
        file_name = name or (previous.file if previous is not None else None)
        file_name = file_name or default_file_name(document.url, document.content_type)
        entries = dict(self.manifest.entries)
        if found is not None:
            del entries[found[0]]
        if file_name in _RESERVED_NAMES or file_name.startswith(_NOTE_PREFIX):
            raise FixtureError(f"file name {file_name!r} is reserved")
        if any(entry.file == file_name for entry in entries.values()):
            raise FixtureError(f"file name {file_name!r} is already used by another URL")
        try:
            entry = FixtureEntry(
                file=file_name,
                status=document.status,
                content_type=document.content_type,
                fetched_at=document.fetched_at,
                final_url=document.final_url if document.final_url != document.url else None,
                volatile_params=list(volatile_params),
            )
        except ValidationError as error:
            raise FixtureError(f"cannot record {ascii_safe(document.url)}: {error}") from error
        write_text_atomic(self.directory / file_name, document.text)
        if previous is not None and previous.file != file_name:
            (self.directory / previous.file).unlink(missing_ok=True)
        self.manifest = self.manifest.evolve(entries={**entries, document.url: entry})
        write_text_atomic(self.directory / MANIFEST_NAME, dump_json(self.manifest))
        return entry

    def problems(self) -> list[str]:
        """Everything that is wrong with this directory; empty when it is consistent."""
        found: list[str] = []
        if self.manifest.source_id != self.source_id:
            found.append(f"manifest source_id is {self.manifest.source_id!r}")
        keys: dict[str, str] = {}
        for url, entry in self.manifest.entries.items():
            key = fixture_key(url, entry.volatile_params)
            if key in keys:
                found.append(
                    f"{ascii_safe(url)} and {ascii_safe(keys[key])} match the same requests"
                )
            keys[key] = url
            found.extend(self._body_problems(entry.file))
        expected = {entry.file for entry in self.manifest.entries.values()} | _RESERVED_NAMES
        *others, last = FIXTURE_EXTENSIONS
        only = f"only {', '.join(others)} and {last} files belong here"
        for path in sorted(self.directory.iterdir()) if self.directory.is_dir() else []:
            if path.suffix not in FIXTURE_EXTENSIONS:
                found.append(f"{path.name} is not a fixture file: {only}")
            elif path.name not in expected and not path.name.startswith(_NOTE_PREFIX):
                found.append(f"{path.name} is not listed in the manifest")
        return [f"{self.source_id}: {problem}" for problem in found]

    def _body_problems(self, file_name: str) -> list[str]:
        path = self.directory / file_name
        if not path.is_file():
            return [f"{file_name} is missing"]
        data = path.read_bytes()
        problems = []
        if len(data) > MAX_FIXTURE_BYTES:
            problems.append(f"{file_name} is larger than {MAX_FIXTURE_BYTES} bytes; trim it")
        if b"\x00" in data:
            problems.append(f"{file_name} is binary")
        return problems


def fixture_sources(fixtures_dir: Path) -> list[str]:
    """Source ids that have a fixture directory with a manifest, sorted."""
    if not fixtures_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in fixtures_dir.iterdir()
        if (path / MANIFEST_NAME).is_file() and is_source_id(path.name)
    )


def reference_time(fixtures_dir: Path) -> datetime | None:
    """The latest ``fetched_at`` over all fixtures: the "now" of an offline run.

    Offline runs must not use the real clock, because recorded events would
    drift out of the time window. Pin the run clock to this value instead.
    """
    moments = [
        entry.fetched_at
        for source_id in fixture_sources(fixtures_dir)
        for entry in FixtureStore.of(fixtures_dir, source_id).manifest.entries.values()
    ]
    return max(moments, default=None)


class OfflineHttpClient:
    """``HttpClient`` that serves recorded fixtures and never opens a connection.

    Lookups go through the given stores in order. Status handling matches the
    live client: a recorded error status raises ``HttpStatusError`` unless the
    caller allows it. ``requests`` lists every URL that was asked for.
    """

    def __init__(self, stores: Sequence[FixtureStore]) -> None:
        self._stores = tuple(stores)
        self.requests: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        split_url(url)
        self.requests.append(url)
        for store in self._stores:
            document = store.read(url)
            if document is not None:
                break
        else:
            searched = ", ".join(store.source_id for store in self._stores) or "no source"
            raise FixtureNotFoundError(
                url,
                f"offline mode: no fixture recorded in [{searched}]; record one with "
                "'uv run python -m seminars.fixtures record --source-id ID URL'",
            )
        if not 200 <= document.status < 300 and document.status not in allow_statuses:
            raise HttpStatusError(url, document.status)
        return document


def offline_client(*source_ids: str, fixtures_dir: Path | None = None) -> OfflineHttpClient:
    """Offline client over the fixtures of ``source_ids`` (default: every source).

    ``fixtures_dir`` defaults to tests/fixtures of the current project. Pass
    exactly one source id per adapter run so that a source can only see its
    own recordings. Raises ``FixtureError`` for anything that is not a
    source id; a path is never accepted in its place.
    """
    directory = fixtures_dir or ProjectPaths(find_project_root()).fixtures_dir
    names = source_ids or tuple(fixture_sources(directory))
    return OfflineHttpClient([FixtureStore.of(directory, name) for name in names])


def record(
    http: HttpClient,
    store: FixtureStore,
    url: str,
    *,
    name: str | None = None,
    volatile_params: Sequence[str] = (),
    allow_statuses: Collection[int] = (),
) -> FixtureEntry:
    """Fetch ``url`` through ``http`` and store the response in ``store``."""
    document = http.get(url, allow_statuses=allow_statuses)
    return store.add(document, name=name, volatile_params=volatile_params)


def check_tree(fixtures_dir: Path, source_ids: Sequence[str] = ()) -> list[str]:
    """Problems of the given (default: all) fixture directories."""
    problems: list[str] = []
    for source_id in source_ids or fixture_sources(fixtures_dir):
        try:
            problems.extend(FixtureStore.of(fixtures_dir, source_id).problems())
        except FixtureError as error:
            problems.append(f"{ascii_safe(source_id)}: {error}")
    return problems


def _source_id(value: str) -> str:
    """Command line type of ``--source-id``: a source id and nothing that could be a path."""
    if not is_source_id(value):
        raise argparse.ArgumentTypeError(
            f"{value[:80]!a} is not a valid source id (lower-case letters, digits, single hyphens)"
        )
    return value


def _name(value: str) -> str:
    """Command line type of ``--name``: a fixture file name, which has a data extension."""
    try:
        return _fixture_file_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m seminars.fixtures", description="Record and check HTTP fixtures."
    )
    parser.add_argument("--root", type=Path, default=None, help="project root (default: detect)")
    commands = parser.add_subparsers(dest="command", required=True)
    rec = commands.add_parser("record", help="fetch URLs politely and store them as fixtures")
    rec.add_argument(
        "--source-id",
        required=True,
        type=_source_id,
        help="source id, names the fixture directory",
    )
    rec.add_argument(
        "url",
        nargs="+",
        metavar="URL",
        help="absolute http(s) URL to record; several are fetched one after another",
    )
    rec.add_argument(
        "--name",
        default=None,
        type=_name,
        help="file name for the body (default: derived); one URL only; "
        f"must end in {', '.join(FIXTURE_EXTENSIONS)}",
    )
    rec.add_argument(
        "--volatile-param",
        action="append",
        default=[],
        metavar="NAME",
        help="query parameter whose value changes between runs (dates); repeatable",
    )
    rec.add_argument(
        "--allow-status",
        action="append",
        default=[],
        type=int,
        metavar="CODE",
        help="also record a response with this non-2xx status; repeatable",
    )
    fet = commands.add_parser(
        "fetch", help="fetch URLs politely and show what came back; nothing is recorded"
    )
    fet.add_argument(
        "url",
        nargs="+",
        metavar="URL",
        help="absolute http(s) URL to look at; several are fetched one after another",
    )
    fet.add_argument(
        "--save-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="also write each body to a file in DIR (relative to the project root), "
        "for example .scratch/TOPIC; inside the project only .scratch is accepted",
    )
    fet.add_argument(
        "--allow-status",
        action="append",
        default=[],
        type=int,
        metavar="CODE",
        help="do not treat a response with this non-2xx status as a failure; repeatable",
    )
    chk = commands.add_parser("check", help="verify manifests and body files")
    chk.add_argument(
        "--source-id", action="append", default=[], type=_source_id, help="limit to this source"
    )
    return parser


def _say(line: str, file: TextIO | None = None) -> None:
    """Print one line of output that may contain text of a site, a URL or a file name.

    Whatever is not printable ASCII is escaped, so nothing a site sent can
    act on the terminal and no line can pretend to be two.
    """
    print(ascii_printable(line), file=file)


def _record_one(args: argparse.Namespace, http: HttpClient, store: FixtureStore, url: str) -> bool:
    """Record ``url`` and report the outcome on the console; False when it failed."""
    try:
        entry = record(
            http,
            store,
            url,
            name=args.name,
            volatile_params=args.volatile_param,
            allow_statuses=args.allow_status,
        )
    except (HttpError, FixtureError) as error:
        _say(f"error: {error}", file=sys.stderr)
        return False
    except OSError as error:
        _say(f"error: cannot store {url}: {error.strerror or error}", file=sys.stderr)
        return False
    size = (store.directory / entry.file).stat().st_size
    print(f"recorded {args.source_id}/{entry.file} (HTTP {entry.status}, {size} bytes)")
    if size > MAX_FIXTURE_BYTES:
        print(
            f"warning: larger than {MAX_FIXTURE_BYTES} bytes; trim the file to what the tests "
            "need and note the trim in a README next to it",
            file=sys.stderr,
        )
    return True


def _live_client(paths: ProjectPaths, resolver: Resolver | None) -> PoliteClient:
    """The polite client of the project; says on stderr which cache directory it uses.

    The directory decides who this process keeps the crawl delay with, so
    it is shown once, before the first request. A client goes on with a
    warning when the directory cannot be used; a command that a person or
    an agent runs by hand must not, because nothing would then keep it
    from using a host at the same time as another call. It raises
    ``FixtureError`` instead, before anything is fetched.
    """
    from seminars.http import PoliteClient  # seminars.http imports this module

    settings = load_settings(paths.settings_file)
    cache_dir = paths.http_cache_dir()
    print(f"HTTP cache directory: {cache_dir}", file=sys.stderr)
    problem = lock_problem(cache_dir)
    if problem is not None:
        raise FixtureError(
            f"the HTTP cache directory cannot be used ({problem}), and without it nothing "
            "keeps this call from using a host at the same time as another one; make it "
            f"writable or name another directory in {ENV_HTTP_CACHE_DIR}"
        )
    return PoliteClient(settings.http, cache_dir=cache_dir, resolver=resolver)


def _record_command(
    args: argparse.Namespace,
    paths: ProjectPaths,
    http: HttpClient | None,
    resolver: Resolver | None,
) -> int:
    """Record every URL in order through one client; a failed URL does not stop the others."""
    live = None
    try:
        store = FixtureStore.of(paths.fixtures_dir, args.source_id)
        if http is None:
            http = live = _live_client(paths, resolver)
        outcomes = [_record_one(args, http, store, url) for url in args.url]
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if live is not None:
            live.close()
    return 0 if all(outcomes) else 1


def _fetch_one(
    http: HttpClient, url: str, allow_statuses: Collection[int], save_dir: Path | None
) -> bool:
    """Fetch ``url``, print what came back and save the body if asked; False when it failed."""
    try:
        document = http.get(url, allow_statuses=allow_statuses)
    except HttpError as error:
        _say(f"error: {error}", file=sys.stderr)
        return False
    _say(f"fetched {url}")
    _say(f"  status: {document.status}")
    _say(f"  final URL: {document.final_url}")
    _say(f"  content type: {document.content_type or 'unknown'}")
    _say(f"  bytes: {len(document.text.encode('utf-8'))}")
    if save_dir is None:
        return True
    path = save_dir / scratch_file_name(url, document.content_type)
    try:
        write_text_atomic(path, document.text)
    except OSError as error:
        print(f"error: cannot save {path}: {error.strerror or error}", file=sys.stderr)
        return False
    print(f"  saved: {path}")
    return True


def _scratch_dir(paths: ProjectPaths, directory: Path | None) -> Path | None:
    """The absolute directory ``fetch --save-dir`` writes to; ``FixtureError`` for a forbidden one.

    A relative directory counts from the project root. What is saved is
    whatever a site sent, unchecked, so inside the project it may only go
    below ``.scratch``, which git ignores: never among the committed files,
    the published data, the configuration or the recorded fixtures (that
    tree only holds what ``record`` wrote and what the manifests list).
    Symbolic links are followed before the place is judged. A directory
    outside the project is the caller's business.
    """
    if directory is None:
        return None
    root = paths.root.resolve()
    target = (root / directory.expanduser()).resolve()
    if target.is_relative_to(paths.fixtures_dir.resolve()):
        raise FixtureError(
            "--save-dir: tests/fixtures is for recorded fixtures only; "
            "use a scratch directory such as .scratch/TOPIC"
        )
    if target.is_relative_to(root) and not target.is_relative_to(root / paths.scratch_dir.name):
        raise FixtureError(
            "--save-dir: inside the project only .scratch may be written to "
            "(it is ignored by git); use .scratch/TOPIC"
        )
    return target


def _fetch_command(
    args: argparse.Namespace,
    paths: ProjectPaths,
    http: HttpClient | None,
    resolver: Resolver | None,
    save_dir: Path | None,
) -> int:
    """Fetch every URL in order through one client; a failed URL does not stop the others."""
    live = None
    try:
        if http is None:
            http = live = _live_client(paths, resolver)
        outcomes = [_fetch_one(http, url, args.allow_status, save_dir) for url in args.url]
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if live is not None:
            live.close()
    return 0 if all(outcomes) else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    http: HttpClient | None = None,
    resolver: Resolver | None = None,
) -> int:
    """Command line entry point.

    For tests: ``http`` replaces the live client, and ``resolver`` replaces
    the name resolution of the live client the command builds itself.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = ProjectPaths(args.root or find_project_root())
    if args.command == "record":
        if args.name is not None and len(args.url) > 1:
            parser.error("--name names a single file; record one URL at a time with it")
        return _record_command(args, paths, http, resolver)
    if args.command == "fetch":
        try:
            save_dir = _scratch_dir(paths, args.save_dir)
        except FixtureError as error:
            parser.error(str(error))
        return _fetch_command(args, paths, http, resolver, save_dir)
    problems = check_tree(paths.fixtures_dir, args.source_id)
    for problem in problems:
        _say(problem)
    print(f"{len(problems)} problem(s) found" if problems else "fixtures OK")
    return 1 if problems else 0


if __name__ == "__main__":
    from seminars.fixtures import main as _main  # use the importable module, not __main__

    raise SystemExit(_main())
