"""Every read command, against a real private ESPN league. Never run in CI.

    FANTASY_SPORTS_ESPN_LEAGUE=<id> uv run pytest -m live

This is the U8 verification step: the offline suite proves the commands read
the shapes they were *told* about, and only a real league proves ESPN still
sends them. Credentials come from the ordinary chain — environment first, then
the Keychain — so nothing here holds or prints a cookie.

Two things are deliberately absent. **No assertion names a team, a player, or
a manager**: those change week to week, and a test that fails when someone
renames their team is a test that gets muted. And **no payload is written
anywhere**: a real private league's payload carries other people's SWIDs, so it
must never become a fixture without going through the scrub hooks in
``tests/conftest.py``.

``enable_socket`` is load-bearing — see ``test_espn_live.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
from fantasy_sports.cli.app import build_app

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]

LEAGUE_ENV = "FANTASY_SPORTS_ESPN_LEAGUE"
SEASON_ENV = "FANTASY_SPORTS_ESPN_SEASON"
PROFILE = "live"


@pytest.fixture(scope="module")
def target() -> tuple[str, int]:
    league_id = os.environ.get(LEAGUE_ENV)
    if not league_id or not resolve_credentials(ESPN_CREDENTIALS).complete:
        pytest.skip(f"needs ESPN cookies and {LEAGUE_ENV}")
    return league_id, int(os.environ.get(SEASON_ENV, "2026"))


@pytest.fixture
def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: tuple[str, int]
) -> Iterator[CliRunner]:
    """A CLI pointed at a throwaway config and a throwaway cache.

    ``HOME`` is left alone on purpose: the macOS Keychain lives under it, and
    moving it would break the credential link this test exists to exercise.
    """
    league_id, season = target
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = tmp_path / "config" / "fantasy-sports"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'default = "{PROFILE}"\n\n'
        f"[leagues.{PROFILE}]\n"
        'provider = "espn"\n'
        f'league_id = "{league_id}"\n'
        f"season = {season}\n",
        encoding="utf-8",
    )
    yield CliRunner()


def _run(cli: CliRunner, args: list[str]) -> Any:
    result = cli.invoke(build_app(), args)
    assert result.exit_code == 0, f"{args} -> {result.exit_code}: {result.stderr}"
    return result


def _envelope(cli: CliRunner, args: list[str]) -> dict[str, Any]:
    payload = json.loads(_run(cli, [*args, "--output", "json"]).stdout)
    assert payload["schema"] == "fantasy-sports/v1"
    assert payload["provider"] == "espn"
    assert payload["error"] is None
    assert payload["sources"], f"{args} reported no upstream fetch"
    assert payload["data_age_seconds"] is not None
    return payload


def test_every_read_command_answers_a_real_private_league(cli: CliRunner, target):
    """The whole v0.1 surface, in one pass, against a league ESPN really serves."""
    league_id, season = target

    info = _envelope(cli, ["league", "info"])
    assert isinstance(info["data"], dict)
    assert info["league_id"] == league_id
    assert info["season"] == season
    assert info["data"]["team_count"] > 0
    assert info["data"]["roster_slots"], "rosterSettings.lineupSlotCounts disappeared"
    week = info["data"]["current_week"]

    teams = _envelope(cli, ["teams"])
    assert isinstance(teams["data"], list)
    assert len(teams["data"]) == info["data"]["team_count"]
    first = teams["data"][0]["provider_id"]

    standings = _envelope(cli, ["standings"])
    assert [team["standing"] for team in standings["data"]] == list(
        range(1, len(teams["data"]) + 1)
    )

    roster = _envelope(cli, ["roster", "--team", first])
    assert isinstance(roster["data"], list)
    assert any(slot["is_starter"] for slot in roster["data"])
    assert all(slot["player"]["eligible_slots"] for slot in roster["data"])

    matchups = _envelope(cli, ["matchups", "--week", str(week)])
    assert isinstance(matchups["data"], list)
    assert all(item["scoring_period_id"] == week for item in matchups["data"])

    moves = _envelope(cli, ["transactions", "--limit", "5"])
    assert isinstance(moves["data"], list)
    assert len(moves["data"]) <= 5

    agents = _envelope(cli, ["free-agents", "--limit", "5"])
    assert isinstance(agents["data"], list)
    assert len(agents["data"]) <= 5

    settings = _envelope(cli, ["raw", "--view", "mSettings"])
    assert isinstance(settings["data"], dict)
    assert settings["data"]["mSettings"]["complete"] is True
    assert str(settings["data"]["mSettings"]["payload"]["id"]) == league_id


def test_roster_accepts_a_team_name_as_well_as_an_id(cli: CliRunner):
    teams = _envelope(cli, ["teams"])["data"]
    by_id = _envelope(cli, ["roster", "--team", teams[0]["provider_id"]])["data"]
    by_name = _envelope(cli, ["roster", "--team", teams[0]["name"]])["data"]
    assert [slot["provider_id"] for slot in by_id] == [slot["provider_id"] for slot in by_name]


def test_free_agents_accept_a_position_filter(cli: CliRunner):
    payload = _envelope(cli, ["free-agents", "--pos", "WR", "--limit", "5"])
    assert payload["data"], "a live league in season has unrostered receivers"
    assert all(
        "WR" in agent["player"]["eligible_slots"] or agent["player"]["position"] == "WR"
        for agent in payload["data"]
    )


def test_a_repeated_raw_view_reports_one_source_per_view(cli: CliRunner):
    payload = _envelope(cli, ["raw", "--view", "mSettings", "--view", "mTeam"])
    assert list(payload["data"]) == ["mSettings", "mTeam"]
    assert [source["name"] for source in payload["sources"]] == ["mSettings", "mTeam"]


def test_an_unfiltered_player_pool_is_labelled_incomplete_live(cli: CliRunner):
    """ESPN really does answer 200 with a partial set. This is that, for real."""
    entry = _envelope(cli, ["raw", "--view", "kona_player_info"])["data"]["kona_player_info"]
    assert entry["complete"] is False
    assert entry["warning"]


def test_a_cache_hit_matches_the_live_fetch_apart_from_its_age(cli: CliRunner):
    from fantasy_sports.commands.context import CACHE_VARIANT_KEYS

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in CACHE_VARIANT_KEYS}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    cold = _envelope(cli, ["standings"])
    warm = _envelope(cli, ["standings"])
    assert any(source["cached"] for source in warm["sources"])
    assert strip(cold) == strip(warm)


def test_every_command_also_renders_a_table(cli: CliRunner, target):
    """The human surface. A table that raises is a broken command, not a nit."""
    league_id, _ = target
    first = _envelope(cli, ["teams"])["data"][0]["provider_id"]
    for args in (
        ["league", "info"],
        ["teams"],
        ["standings"],
        ["roster", "--team", first],
        ["matchups"],
        ["transactions", "--limit", "5"],
        ["free-agents", "--limit", "5"],
        ["raw", "--view", "mSettings"],
        ["auth", "status"],
    ):
        rendered = _run(cli, [*args, "--output", "table"]).stdout
        assert rendered.strip(), f"{args} rendered an empty table"
        with pytest.raises(json.JSONDecodeError):
            json.loads(rendered)


def test_auth_status_reports_a_complete_pair_without_printing_it(cli: CliRunner):
    payload = json.loads(_run(cli, ["auth", "status", "--output", "json"]).stdout)
    data = payload["data"]
    assert data["complete"] is True
    assert {row["name"] for row in data["credentials"]} == {"espn_s2", "swid"}
    assert data["staleness_threshold"]["verified"] is False

    # The one assertion that matters most in this file: no credential value can
    # appear in output a user might paste into an issue.
    revealed = resolve_credentials(ESPN_CREDENTIALS).as_mapping()
    rendered = json.dumps(payload)
    for value in revealed.values():
        assert value not in rendered


def test_an_unknown_league_fails_without_touching_espn(cli: CliRunner):
    result = cli.invoke(build_app(), ["standings", "--league", "no-such-profile"])
    assert result.exit_code == 5
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "LEAGUE_NOT_FOUND"
