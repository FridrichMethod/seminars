"""The one test that talks to the real network: the live client against a real site.

Everything else in the suite runs the client over mock transports, a fake
socket layer or the loopback interface. Here its own connections meet the
real resolver, the certificates of the real certificate store and a real
server. The test is marked ``live`` and is not part of a normal run; run
it on purpose::

    uv run pytest -m live tests/test_live_http.py

It sends two polite requests to one host (its robots.txt as the rule book,
then the same file as the page), with the User-Agent and the settings of
this project, and keeps its cache in a temporary directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from seminars.config import load_settings
from seminars.http import PoliteClient

REPO_ROOT = Path(__file__).resolve().parents[1]
ROBOTS = "https://events.stanford.edu/robots.txt"


@pytest.mark.live
def test_the_live_client_fetches_a_real_page_over_its_own_connections(tmp_path: Path) -> None:
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    with PoliteClient(settings.http, cache_dir=tmp_path / "http") as client:
        document = client.get(ROBOTS)
    assert document.status == 200
    assert document.text.strip()
