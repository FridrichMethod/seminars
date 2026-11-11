"""Recorded HTTP fixtures: the store, the offline client and the recorder.

Layout (see tests/fixtures/README.md for the full convention)::

    tests/fixtures/<source_id>/
        manifest.json     maps every recorded request URL to its body file
        <file>            one recorded response body per URL, text only
        README.md         optional notes, e.g. what was trimmed and why

``FixtureStore`` reads and writes one such directory. ``OfflineHttpClient``
is an ``HttpClient`` that serves recorded bodies by URL and raises
``FixtureNotFoundError`` for anything that was not recorded; it backs
``seminars run --offline`` and every adapter test. The recorder fetches
through the polite client, so robots.txt and Crawl-delay are honoured while
researching sources too::

    uv run python -m seminars.fixtures record --source-id ID URL [--name FILE]
    uv run python -m seminars.fixtures check [--source-id ID]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from pydantic import AfterValidator, StringConstraints, ValidationError

from seminars.config import ProjectPaths, find_project_root, load_settings
from seminars.httpbase import (
    HttpClient,
    HttpError,
    HttpStatusError,
    ascii_safe,
    is_binary_media_type,
    split_url,
)
from seminars.models import SOURCE_ID_PATTERN, FetchedDocument, Model, dump_json
from seminars.models.base import SortedStrList, SourceId, Timestamp, sorted_dict
from seminars.textio import read_text, write_text_atomic

MANIFEST_NAME: Final = "manifest.json"
MAX_FIXTURE_BYTES: Final = 500_000
"""Bodies larger than this must be trimmed by hand (and the trim noted in a README)."""

_RESERVED_NAMES: Final = frozenset({MANIFEST_NAME})
_BOM: Final = chr(0xFEFF)
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

FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]


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

    Scheme and host are lower-cased, the path is percent-decoded, the
    fragment is dropped and the query parameters are sorted, so that the
    order and the encoding of parameters do not matter. Parameters named in
    ``volatile_params`` match any value (their value becomes ``*``).
    """
    parts = urlsplit(url)
    volatile = set(volatile_params)
    pairs = sorted(
        (name, "*" if name in volatile else value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    query = f"?{urlencode(pairs)}" if pairs else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{unquote(parts.path) or '/'}{query}"


def normalize_body(text: str) -> str:
    """Bring a body into the form fixtures are stored in.

    A leading byte order mark is removed, CRLF and CR become LF and the text
    ends with a newline. Parsers must therefore accept LF-only input, which
    matters for ICS files (the standard asks for CRLF).
    """
    cleaned = text.removeprefix(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned if cleaned.endswith("\n") else cleaned + "\n"


def default_file_name(url: str, content_type: str | None) -> str:
    """Readable, collision-free file name for the body of ``url``."""
    path = unquote(urlsplit(url).path).lower()
    stem = re.sub(r"[^a-z0-9]+", "-", path).strip("-")[:40].strip("-") or "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}{_EXTENSIONS.get(content_type or '', '.txt')}"


class FixtureStore:
    """The recorded fixtures of one source (one directory below tests/fixtures/)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.source_id = directory.name
        if re.fullmatch(SOURCE_ID_PATTERN, self.source_id) is None:
            raise FixtureError(f"{directory}: directory name is not a valid source id")
        self.manifest = self._load()

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
        refused. Without ``name`` the previous file name of the URL is kept,
        or a new one is derived from the URL.
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
        write_text_atomic(self.directory / file_name, normalize_body(document.text))
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
        for path in sorted(self.directory.iterdir()) if self.directory.is_dir() else []:
            if path.name not in expected and not path.name.startswith(_NOTE_PREFIX):
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
        if (path / MANIFEST_NAME).is_file() and re.fullmatch(SOURCE_ID_PATTERN, path.name)
    )


def reference_time(fixtures_dir: Path) -> datetime | None:
    """The latest ``fetched_at`` over all fixtures: the "now" of an offline run.

    Offline runs must not use the real clock, because recorded events would
    drift out of the time window. Pin the run clock to this value instead.
    """
    moments = [
        entry.fetched_at
        for source_id in fixture_sources(fixtures_dir)
        for entry in FixtureStore(fixtures_dir / source_id).manifest.entries.values()
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
    own recordings.
    """
    directory = fixtures_dir or ProjectPaths(find_project_root()).fixtures_dir
    names = source_ids or tuple(fixture_sources(directory))
    return OfflineHttpClient([FixtureStore(directory / name) for name in names])


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
            problems.extend(FixtureStore(fixtures_dir / source_id).problems())
        except FixtureError as error:
            problems.append(f"{source_id}: {error}")
    return problems


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m seminars.fixtures", description="Record and check HTTP fixtures."
    )
    parser.add_argument("--root", type=Path, default=None, help="project root (default: detect)")
    commands = parser.add_subparsers(dest="command", required=True)
    rec = commands.add_parser("record", help="fetch one URL politely and store it as a fixture")
    rec.add_argument("--source-id", required=True, help="source id, names the fixture directory")
    rec.add_argument("url", help="absolute http(s) URL to record")
    rec.add_argument("--name", default=None, help="file name for the body (default: derived)")
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
    chk = commands.add_parser("check", help="verify manifests and body files")
    chk.add_argument("--source-id", action="append", default=[], help="limit to this source")
    return parser


def _record_command(args: argparse.Namespace, paths: ProjectPaths, http: HttpClient | None) -> int:
    live = None
    try:
        store = FixtureStore(paths.fixtures_dir / args.source_id)
        if http is None:
            from seminars.http import PoliteClient

            settings = load_settings(paths.settings_file)
            http = live = PoliteClient(settings.http, cache_dir=paths.http_cache_dir)
        entry = record(
            http,
            store,
            args.url,
            name=args.name,
            volatile_params=args.volatile_param,
            allow_statuses=args.allow_status,
        )
    except (HttpError, FixtureError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if live is not None:
            live.close()
    size = (store.directory / entry.file).stat().st_size
    print(f"recorded {args.source_id}/{entry.file} (HTTP {entry.status}, {size} bytes)")
    if size > MAX_FIXTURE_BYTES:
        print(
            f"warning: larger than {MAX_FIXTURE_BYTES} bytes; trim the file to what the tests "
            "need and note the trim in a README next to it",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None, *, http: HttpClient | None = None) -> int:
    """Command line entry point; ``http`` replaces the live client (for tests)."""
    args = _build_parser().parse_args(argv)
    paths = ProjectPaths(args.root or find_project_root())
    if args.command == "record":
        return _record_command(args, paths, http)
    problems = check_tree(paths.fixtures_dir, args.source_id)
    for problem in problems:
        print(problem)
    print(f"{len(problems)} problem(s) found" if problems else "fixtures OK")
    return 1 if problems else 0


if __name__ == "__main__":
    from seminars.fixtures import main as _main  # use the importable module, not __main__

    raise SystemExit(_main())
