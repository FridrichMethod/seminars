"""Shared building blocks for all models: base class, vocabularies and field types."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints

# --- closed vocabularies -------------------------------------------------------------------

FieldId = Literal["math", "cs", "ai-ml", "chem", "bio", "physics", "med"]
Institution = Literal["stanford", "slac", "arc", "biohub", "other"]
EventType = Literal[
    "seminar", "colloquium", "lecture", "conference", "workshop", "defense", "other"
]
EventStatus = Literal["confirmed", "tentative", "cancelled"]
FieldTagMethod = Literal["source", "rules", "llm"]
ExtractionMethod = Literal["api", "feed", "jsonld", "html", "llm"]

FIELD_IDS: Final[tuple[str, ...]] = ("math", "cs", "ai-ml", "chem", "bio", "physics", "med")
"""Canonical field ids in canonical order."""

FEED_IDS: Final[tuple[str, ...]] = ("all", "picks", *FIELD_IDS)
"""Ids of the nine ICS feeds; feed ``<id>`` is published as ``feeds/<id>.ics``."""

UID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SOURCE_ID_PATTERN: Final = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

CREDENTIAL_QUERY_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "auth",
        "key",
        "passcode",
        "password",
        "pwd",
        "secret",
        "sig",
        "signature",
        "tk",
        "token",
    }
)
"""Query parameter names that mark a URL as carrying a credential."""


# --- base class ----------------------------------------------------------------------------


class Model(BaseModel):
    """Base class of every model: immutable, and unknown keys are an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def evolve(self, **changes: Any) -> Self:
        """Return a validated copy with ``changes`` applied.

        Models are frozen, so this is the way to "modify" one. Unlike
        ``model_copy(update=...)`` the result is validated again.
        """
        data: dict[str, Any] = {name: getattr(self, name) for name in type(self).model_fields}
        data.update(self.model_extra or {})
        data.update(changes)
        return type(self).model_validate(data)


# --- validators and annotated field types --------------------------------------------------


def _drop_microseconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def canonical_fields(values: Iterable[str]) -> list[str]:
    """Validate field ids; return them without duplicates in canonical order."""
    given = set(values)
    unknown = sorted(given - set(FIELD_IDS))
    if unknown:
        allowed = ", ".join(FIELD_IDS)
        raise ValueError(f"unknown field id(s): {', '.join(unknown)} (allowed: {allowed})")
    return [field for field in FIELD_IDS if field in given]


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def sorted_dict[V](value: dict[str, V]) -> dict[str, V]:
    """Return ``value`` with its keys in sorted order (for deterministic dumps)."""
    return dict(sorted(value.items()))


def check_timezone(name: str) -> str:
    """Validate an IANA time zone name such as ``America/Los_Angeles``."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown IANA time zone: {name!r}") from error
    return name


def check_http_url(value: str) -> str:
    """Validate an absolute http(s) URL and return it unchanged (no normalization)."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an absolute http(s) URL: {value!r}")
    if any(char.isspace() for char in value):
        raise ValueError(f"URL must not contain whitespace: {value!r}")
    return value


def url_has_credential(url: object) -> bool:
    """Return True when a URL carries a credential and must not be published.

    A URL counts as carrying a credential when it has a user or password part,
    or when its query or fragment has a parameter named like a secret
    (``pwd``, ``passcode``, ``tk``, ``token``, ...; see CREDENTIAL_QUERY_PARAMS).
    Normalization drops such stream URLs and only keeps ``is_online=True``.
    """
    parts = urlsplit(str(url))
    if parts.username or parts.password:
        return True
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs += parse_qsl(parts.fragment, keep_blank_values=True)
    return any(name.lower() in CREDENTIAL_QUERY_PARAMS for name, _ in pairs)


def source_key(source_id: str, native_id: str) -> str:
    """Build an ``Event.source_ids`` entry: ``<source id>:<source-native id>``."""
    return f"{source_id}:{native_id}"


def split_source_key(key: str) -> tuple[str, str]:
    """Inverse of ``source_key``: ``(source id, native id)``.

    Source ids never contain a colon, so the first colon is the separator.
    """
    source_id, separator, native_id = key.partition(":")
    if not separator or not source_id:
        raise ValueError(f"not a source key: {key!r}")
    return source_id, native_id


Timestamp = Annotated[AwareDatetime, AfterValidator(_drop_microseconds)]
"""Timezone-aware datetime truncated to whole seconds."""

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Uid = Annotated[str, StringConstraints(pattern=UID_PATTERN)]
SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID_PATTERN, max_length=64)]
FieldList = Annotated[list[str], AfterValidator(canonical_fields)]
"""List of canonical field ids: validated, de-duplicated, in canonical order."""

SortedStrList = Annotated[list[str], AfterValidator(_sorted_unique)]
"""List of strings kept sorted and without duplicates."""

TimezoneName = Annotated[str, AfterValidator(check_timezone)]
HttpUrlStr = Annotated[str, AfterValidator(check_http_url)]
"""Absolute http(s) URL kept exactly as written."""
