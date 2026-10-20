"""The ``experts`` commands against real FantasyPros. Never run in CI.

    uv run pytest -m live tests/live/test_experts_live.py

One smoke test, three requests (the players table, one position's consensus
ranking, one player's news) out of a premium key's 500 a day. It proves the
shapes the trimmed recording says FantasyPros sends are still the shapes it
sends, which only a real call can. The key comes from the ordinary chain, so
nothing here holds or prints it, and the cache is a throwaway so the three
requests really are sent.

No assertion names a player or a ranking, because those change every day.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fantasy_sports.auth.chain import FANTASYPROS_CREDENTIALS, resolve_credentials
from fantasy_sports.cli.app import build_app

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _reach_the_network() -> None:
    """Undo ``--disable-socket`` for this module. See ``test_espn_live.py``."""
    from pytest_socket import enable_socket

    enable_socket()


def _run(args: list[str]) -> dict[str, Any]:
    result = CliRunner().invoke(build_app(), [*args, "--output", "json"])
    assert result.exit_code == 0, f"{args} -> {result.exit_code}: {result.stderr}"
    return json.loads(result.stdout)


def test_rankings_then_one_players_news_against_real_fantasypros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if not resolve_credentials(FANTASYPROS_CREDENTIALS).complete:
        pytest.skip("needs a FantasyPros API key (FANTASY_SPORTS_FANTASYPROS_API_KEY)")
    # HOME stays put: the macOS Keychain lives under it.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    ranked = _run(["experts", "rankings", "--pos", "RB"])
    assert ranked["provider"] == "fantasypros" and ranked["error"] is None
    assert [s["name"] for s in ranked["sources"]] == [
        "fantasypros:players",
        "fantasypros:consensus-rankings",
    ]
    rows = ranked["data"]
    assert len(rows) > 10, "a premium key returns the whole position, not the free tier's ten"
    top = rows[0]
    assert top["rank_ecr"] == 1 and top["position"] == "RB"
    assert isinstance(top["rank_ave"], float) and isinstance(top["fp_id"], int)
    assert isinstance(top["espn_id"], str), "the top-ranked player joins onto ESPN"

    wire = _run(["experts", "news", "--player", top["espn_id"], "--limit", "3"])
    assert wire["sources"][-1]["name"] == "fantasypros:news"
    for item in wire["data"]:
        assert item["player"]["fp_id"] == top["fp_id"]
        assert item["player"]["espn_id"] == top["espn_id"]
        assert item["created_at"].endswith("Z")
