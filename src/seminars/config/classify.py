"""Schemas of config/taxonomy.yaml and config/interests.yaml."""

from __future__ import annotations

from typing import Annotated, Any, Self

from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from seminars.models import FIELD_IDS, FieldId, Model, RuleExclusionReason
from seminars.models.base import FieldList, NonEmptyStr, SourceId


def _list_to_weights(value: Any) -> Any:
    if isinstance(value, list):
        return dict.fromkeys(value, 1.0)
    return value


def _check_terms(value: dict[str, float]) -> dict[str, float]:
    for term, weight in value.items():
        if not term.strip():
            raise ValueError("terms must not be blank")
        if weight <= 0:
            raise ValueError(f"weight of {term!r} must be positive")
    return value


WeightedTerms = Annotated[
    dict[str, float], BeforeValidator(_list_to_weights), AfterValidator(_check_terms)
]
"""Mapping ``term -> positive weight``; a plain list means weight 1.0 for every term."""


class FieldRules(Model):
    """Rules that assign one field (config/taxonomy.yaml, ``fields.<id>``).

    * ``keywords``: terms looked for in title, series and abstract. Matching
      is case-insensitive on word boundaries. A field is assigned when the
      weights of its matched terms add up to at least ``min_score``, so weak
      terms can be given weights below 1.0.
    * ``series``: series names (case-insensitive) that imply the field.
    * ``exclude_keywords``: terms that veto the field when they match.
    """

    keywords: WeightedTerms = {}
    series: list[str] = []
    exclude_keywords: list[str] = []
    min_score: Annotated[float, Field(gt=0)] = 1.0


def _all_fields(value: dict[str, FieldRules]) -> dict[str, FieldRules]:
    return {field: value.get(field, FieldRules()) for field in FIELD_IDS}


class Taxonomy(Model):
    """Content of config/taxonomy.yaml: field rules and filter phrases::

        fields:
          chem:
            keywords: {catalysis: 1.0, synthesis: 0.5}
            series: [Physical Chemistry Seminar]
        source_defaults:
          sites-chemistry: [chem]
        departments:
          Chemical Engineering: [chem]
        exclusions:
          office_hours: [office hours, drop-in hours]
        out_of_scope_categories: [Stanford Center for Continuing Medical Education]

    * ``fields``: rules per field id; ids that are left out get empty rules,
      unknown ids are an error. The mapping always holds all seven fields in
      canonical order.
    * ``source_defaults``: source id -> fields, in addition to the
      ``default_fields`` of the source entry itself.
    * ``departments``: department or category label (matched
      case-insensitively against ``EventHints.categories``, never against
      ``EventHints.tags``) -> fields.
    * ``exclusions``: phrases per rule-based exclusion reason for the filter.
    * ``out_of_scope_categories``: category labels (matched case-insensitively
      against ``EventHints.categories``, like ``departments``) whose events are
      out of scope whatever their other categories, keywords or source fields
      say. Such an event gets no fields and is excluded with reason
      ``out_of_scope``, without an LLM call. For units whose events are never
      academic talks although they share a school with ones that are (the
      continuing medical education courses of the School of Medicine).
    """

    fields: Annotated[
        dict[FieldId, FieldRules], AfterValidator(_all_fields), Field(validate_default=True)
    ] = {}
    source_defaults: dict[SourceId, FieldList] = {}
    departments: dict[str, FieldList] = {}
    exclusions: dict[RuleExclusionReason, list[str]] = {}
    out_of_scope_categories: list[NonEmptyStr] = []


class PicksMultipliers(Model):
    """Where a keyword match counts most: title > series > abstract."""

    title: float = 3.0
    series: float = 2.0
    abstract: float = 1.0

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if not self.title > self.series > self.abstract > 0:
            raise ValueError("multipliers must satisfy title > series > abstract > 0")
        return self


class Interests(Model):
    """Content of config/interests.yaml: the rule-based picks.

    score = sum over matched keywords of ``weight * multiplier`` (the
    multiplier of the best place the keyword appears in)
    + ``series_bonus`` if the series is on ``series_allowlist``
    + ``speaker_bonus`` if a speaker is on ``speaker_watchlist``
    - the penalty of every matched ``exclusions`` term.

    An event is a pick when ``score >= threshold``. ``llm_rerank`` is a
    reserved switch for a future LLM re-ranking step and defaults to off.
    """

    threshold: float = 3.0
    multipliers: PicksMultipliers = PicksMultipliers()
    keywords: WeightedTerms = {}
    series_allowlist: list[str] = []
    series_bonus: Annotated[float, Field(ge=0)] = 2.0
    speaker_watchlist: list[str] = []
    speaker_bonus: Annotated[float, Field(ge=0)] = 3.0
    exclusions: WeightedTerms = {}
    llm_rerank: bool = False
