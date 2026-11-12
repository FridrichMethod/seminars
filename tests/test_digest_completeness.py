"""Tests for the digest's word on sources that were not read in full.

A source whose last run delivered events but is known to have missed some
(pages that could not be fetched, a listing far smaller than usual) is not
healthy: it is listed with the sources that did not deliver, with the
reason. A source whose last run took its smaller listing as the usual size
is healthy again, and the digest says what happened to its size.
"""

from __future__ import annotations

import dataclasses

from factories import make_source
from seminars.config import AppConfig
from seminars.models import HealthFile
from test_digest import NEVER, both, ran, render, rows, section, with_sources, zh

HEALTH = "Source health"


class TestSourcesNotReadInFull:
    def test_a_source_that_skipped_pages_is_listed_with_the_reason(
        self, app_config: AppConfig
    ) -> None:
        config = with_sources(app_config, "sites-ok", "sites-short")
        health = ran(HealthFile(), "sites-ok", "ok", 12, accepted=12)
        health = ran(health, "sites-short", "ok", 3, accepted=3, skipped_pages=2)
        summary, table, _ = section(render(config, health=health), HEALTH)
        assert summary.split("\n")[0] == "1 of 2 sources are healthy."
        reason = "<br>".join(
            [
                "2 page(s) could not be fetched",
                zh("digest.health.skipped", count=2),
            ]
        )
        success = "2026-11-10"
        assert rows(table) == [
            ["Source sites-short", both("digest.health.status_partial"), "3", success, reason]
        ]

    def test_a_listing_far_smaller_than_usual_gives_both_sizes(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-short")
        health = ran(HealthFile(), "sites-short", "ok", 4, accepted=5, shrank_from=20)
        _, table, _ = section(render(config, health=health), HEALTH)
        ((_, status, events, _, reason),) = rows(table)
        assert (status, events) == (both("digest.health.status_partial"), "4")
        english, chinese = reason.split("<br>")
        assert english == "5 usable item(s) where it usually has 20"
        assert chinese == zh("digest.health.shrank", listed=5, usual=20)

    def test_both_reasons_are_given_in_each_language(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-short")
        health = ran(
            HealthFile(), "sites-short", "ok", 4, accepted=5, shrank_from=20, skipped_pages=1
        )
        _, table, _ = section(render(config, health=health), HEALTH)
        ((*_, reason),) = rows(table)
        assert reason.split("<br>")[0] == (
            "1 page(s) could not be fetched; 5 usable item(s) where it usually has 20"
        )

    def test_an_empty_run_keeps_its_own_label(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-empty")
        health = ran(HealthFile(), "sites-empty", "empty", 0, accepted=2, skipped_pages=1)
        _, table, _ = section(render(config, health=health), HEALTH)
        ((_, status, _, success, reason),) = rows(table)
        assert (status, success) == (both("digest.health.status_empty"), NEVER)
        assert reason.split("<br>")[0] == "1 page(s) could not be fetched"

    def test_a_new_usual_size_is_named_below_the_summary(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-ok", "sites-resized")
        health = ran(HealthFile(), "sites-ok", "ok", 12, accepted=12)
        health = ran(health, "sites-resized", "ok", 5, accepted=5, resized_from=20)
        blocks = section(render(config, health=health), HEALTH)
        assert blocks[0].split("\n")[0] == "All 2 sources are healthy."
        assert blocks[1] == "\n".join(
            [
                "Source sites-resized: the listing has been far smaller than usual for so long "
                "that its new size is now taken as the usual one (5 instead of 20).",
                zh("digest.health.resized", source="Source sites-resized", listed=5, usual=20),
            ]
        )
        assert blocks[2].startswith("Field tagging used rules only")

    def test_a_disabled_source_is_not_named(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-ok")
        off = make_source(id="sites-off", name="Source sites-off", enabled=False)
        config = dataclasses.replace(config, sources=(*config.sources, off))
        health = ran(HealthFile(), "sites-ok", "ok", 12, accepted=12)
        health = ran(health, "sites-off", "ok", 5, accepted=5, resized_from=20)
        health = ran(health, "sites-off", "ok", 5, accepted=5, shrank_from=20)
        blocks = section(render(config, health=health), HEALTH)
        assert blocks[0].split("\n")[0] == "All 1 sources are healthy."
        assert len(blocks) == 2
