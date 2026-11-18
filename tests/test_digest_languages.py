"""The weekly digest says in both languages what the pipeline writes in its own words.

The picks reasons are lines built by the picks scorer from the interests, and the
field names are our labels: neither is scraped content, so each table cell that holds
them has an English line and a Chinese line, and a heading names a field so that its
two languages can be told apart. The terms of the configuration stay as they are.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from factories import make_event
from seminars.config import AppConfig
from seminars.i18n import BILINGUAL_SEPARATOR, LOCALES, load_locales
from seminars.models import FIELD_IDS, HealthFile
from seminars.outputs.digest import render_digest

WEEK = date(2026, 11, 16)  # a Monday; make_event starts on the Tuesday after it
LINES = [
    '"protein design" in title (+3)',
    'series "Bioengineering Seminar" (+3)',
    'speaker "Jane Doe" (+2)',
    '"cryo-EM" in abstract (+1.5)',
    'penalty "undergraduate" (-2)',
]
QUOTED = re.compile(r'"[^"]*"')
LATIN = re.compile(r"[A-Za-z]")


def body(config: AppConfig, **overrides: object) -> str:
    event = make_event(picks_score=7.5, fields=["bio", "ai-ml"], **overrides)
    return render_digest(
        [event], HealthFile(), config=config, week_start=WEEK, previous_issue_at=None
    ).body


def picks_row(config: AppConfig, **overrides: object) -> list[str]:
    """The cells of the one row of the picks table."""
    text = body(config, **overrides)
    table = text.split("## Picks")[1].split("\n\n")[1]
    (line,) = table.split("\n")[2:]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]


class TestPicksReasons:
    def test_the_english_line_reads_like_the_calendar_feeds(self, app_config: AppConfig) -> None:
        reasons = picks_row(app_config, picks_reasons=LINES)[6]
        english, chinese = reasons.split("<br>")
        assert english == "; ".join(LINES)
        assert chinese != english

    def test_the_chinese_line_keeps_only_the_configured_terms_in_english(
        self, app_config: AppConfig
    ) -> None:
        chinese = picks_row(app_config, picks_reasons=LINES)[6].split("<br>")[1]
        locale = load_locales(app_config.paths.locales_dir)["zh-CN"]
        place = locale.t("digest.reasons.places.title")
        first = locale.t("digest.reasons.keyword", term="protein design", place=place, points="+3")
        assert chinese.startswith(first + "; ")
        assert len(chinese.split("; ")) == len(LINES)
        assert LATIN.search(QUOTED.sub("", chinese)) is None
        assert "(-2)" not in chinese
        assert "-2" in chinese

    def test_a_line_of_no_known_form_is_given_once(self, app_config: AppConfig) -> None:
        assert picks_row(app_config, picks_reasons=["listed by hand"])[6] == "listed by hand"
        mixed = picks_row(app_config, picks_reasons=[LINES[1], "listed by hand"])[6]
        english, chinese = mixed.split("<br>")
        assert english == f"{LINES[1]}; listed by hand"
        assert chinese.endswith("; listed by hand")

    def test_without_reasons_the_cell_is_empty(self, app_config: AppConfig) -> None:
        assert picks_row(app_config, picks_reasons=[])[6] == "-"


class TestFieldNames:
    def test_the_field_cell_is_in_both_languages(self, app_config: AppConfig) -> None:
        locales = load_locales(app_config.paths.locales_dir)
        cell = picks_row(app_config, picks_reasons=LINES)[5]
        assert cell == "<br>".join(
            ", ".join(locales[lang].t(f"fields.{field}") for field in ("ai-ml", "bio"))
            for lang in LOCALES
        )

    @pytest.mark.parametrize("lang", LOCALES)
    def test_no_field_name_holds_the_separator_of_the_two_languages(
        self, app_config: AppConfig, lang: str
    ) -> None:
        locale = load_locales(app_config.paths.locales_dir)[lang]
        names = [locale.t(f"fields.{field}") for field in FIELD_IDS]
        assert [name for name in names if BILINGUAL_SEPARATOR in name] == []

    def test_every_heading_splits_into_its_two_languages(self, app_config: AppConfig) -> None:
        text = body(app_config, picks_reasons=LINES)
        headings = [line for line in text.split("\n") if line.startswith("#")]
        assert len(headings) == 5 + len(FIELD_IDS)  # title, picks, fields, meetings, new, health
        for heading in headings:
            english, chinese = heading.split(" ", 1)[1].split(BILINGUAL_SEPARATOR)
            assert english.isascii(), heading
            assert not chinese.isascii(), heading
