"""``box-scores``: both lineups for a week, player by player.

The fixture interaction these tests replay is derived from
``tests/football/unit/data/league_boxscore_2018.json`` in
`cwendt94/espn-api <https://github.com/cwendt94/espn-api>`_, **MIT licensed,
Copyright (c) 2019 Christian Wendt**. It is a real ESPN response, trimmed to
one matchup and one scoring period. ``docs/testing.md`` §3 records the
derivation and the attribution requirement.

It could not be recorded: ``espn-api`` refuses box scores before 2019 and the
canary league exists only for 2018.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _harness import RecordedEspn, install_espn, isolate_home  # noqa: E402

from fantasy_sports.commands import REGISTRY, DataShape  # noqa: E402
from fantasy_sports.commands.box_scores import box_scores  # noqa: E402
from fantasy_sports.core.errors import ErrorCode, NotAvailableError  # noqa: E402
from fantasy_sports.core.models import BoxScore, LineupEntry  # noqa: E402
from fantasy_sports.providers.espn import BENCH_SLOTS, _member_name  # noqa: E402

WEEK = 1


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> RecordedEspn:
    return install_espn(monkeypatch)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)


def _first(espn: RecordedEspn) -> dict:
    return box_scores(week=WEEK).data[0]


def test_box_scores_is_registered_as_a_collection():
    spec = REGISTRY["box-scores"]
    assert spec.shape is DataShape.COLLECTION
    assert spec.handler == "fantasy_sports.commands.box_scores:box_scores"


def test_a_box_score_carries_both_lineups(espn: RecordedEspn):
    found = _first(espn)
    assert found["team_a_provider_id"] and found["team_b_provider_id"]
    assert len(found["team_a_lineup"]) == 15


def test_a_lineup_entry_carries_what_a_recap_is_written_from(espn: RecordedEspn):
    entry = _first(espn)["team_a_lineup"][0]
    assert set(entry) >= {
        "slot",
        "player_name",
        "position",
        "pro_opponent",
        "projected_points",
        "actual_points",
        "started",
    }
    assert entry["player_name"]
    assert isinstance(entry["projected_points"], float)


def test_bench_slots_are_not_started_and_starters_are(espn: RecordedEspn):
    """The starter/bench split is the axis every weekly award turns on."""
    lineup = _first(espn)["team_a_lineup"]
    assert any(e["started"] for e in lineup), "no starters found"
    assert any(not e["started"] for e in lineup), "no bench found"
    for entry in lineup:
        assert entry["started"] is (entry["slot"] not in BENCH_SLOTS)


def test_bench_points_are_arithmetic_over_the_bench_only():
    """Not a projection and not an optimal-lineup claim — just addition."""
    started = LineupEntry(
        provider="espn",
        provider_id="1",
        raw={},
        slot="RB",
        player_name="Started",
        actual_points=9.0,
        started=True,
    )
    benched = LineupEntry(
        provider="espn",
        provider_id="2",
        raw={},
        slot="BE",
        player_name="Benched",
        actual_points=21.5,
        started=False,
    )
    box = BoxScore(
        provider="espn",
        provider_id="1-0",
        raw={},
        week=WEEK,
        team_a_provider_id="1",
        team_a_score=9.0,
        team_a_lineup=(started, benched),
        team_b_provider_id="2",
        team_b_score=100.0,
        team_b_lineup=(),
    )
    assert box.team_a_bench_points == 21.5
    assert box.team_b_bench_points == 0.0


def test_an_injured_reserve_slot_counts_as_bench_for_scoring():
    """IR is not a bench slot for roster legality, but it scores like one."""
    assert "IR" in BENCH_SLOTS


def test_a_season_without_box_scores_is_refused_by_name_not_returned_empty(
    monkeypatch: pytest.MonkeyPatch, espn: RecordedEspn
):
    """ "No box score exists" and "nobody scored" are different answers, and a
    consumer cannot tell them apart from an empty list."""
    from fantasy_sports.providers import espn as adapter

    class _Refuses:
        def box_scores(self, week: int):
            raise Exception("Cant use box score before 2019")

        settings = type("S", (), {"matchup_periods": {}})()
        current_week = WEEK

    monkeypatch.setattr(adapter.EspnProvider, "_read", lambda self, *a, **k: _ctx(_Refuses()))
    with pytest.raises(NotAvailableError) as err:
        adapter.EspnProvider().fetch_box_scores("99", 2018, WEEK)
    assert err.value.code == ErrorCode.NOT_AVAILABLE
    assert err.value.retryable is False
    assert "2018" in err.value.details["season"]
    assert "2019" in err.value.remediation


def _ctx(value):
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        yield value

    return _cm()


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        (
            {"firstName": "Marta", "lastName": "O'Brien", "displayName": "ESPNFAN4690433888"},
            "Marta O'Brien",
        ),
        ({"firstName": "John", "lastName": "", "displayName": "johnwulff"}, "John"),
        ({"displayName": "cwhick"}, "cwhick"),
        ({}, ""),
    ],
)
def test_a_real_name_beats_an_account_handle(member: dict, expected: str):
    """Half the display names in a real league name nobody. A paper that
    prints "ESPNFAN4690433888 lost to johnwulff" is not publishable."""
    assert _member_name(member) == expected


# --------------------------------------------------------------------------- #
# jwulff/fantasy-sports#72: the opponent must not depend on positional ratings
# --------------------------------------------------------------------------- #

#: Week-1 NFL games for the pro teams the fixture's lineups are on, in the
#: shape of ``proTeamSchedules_wl``. ``18`` NO, ``29`` CAR, ``3`` CHI, ``1`` ATL,
#: ``12`` KC, ``5`` CLE (``espn_api.football.constant.PRO_TEAM_MAP``).
WEEK_ONE_GAMES = [(18, 29, 4101), (3, 1, 4102), (12, 5, 4103)]


def _schedule_with_week_one() -> dict:
    teams = [{"id": 0, "abbrev": "FA", "proGamesByScoringPeriod": {}}]
    for away, home, game_id in WEEK_ONE_GAMES:
        game = {"awayProTeamId": away, "homeProTeamId": home, "date": 1789318800000, "id": game_id}
        for team in (away, home):
            teams.append({"id": team, "proGamesByScoringPeriod": {str(WEEK): [game]}})
    return {"settings": {"proTeams": teams}}


#: What ESPN served ``view=mPositionalRatings`` for league 467763 on
#: 2026-09-09, before any week-1 game had been played: league status and no
#: ``positionAgainstOpponent`` key at all. ``espn-api`` only names an
#: opponent when the player's position appears under that key, so every
#: player came back with none. Trimmed to the keys that matter.
RATINGS_NOT_YET_PUBLISHED = {
    "draftDetail": {"drafted": True, "inProgress": False},
    "gameId": 1,
    "id": 99,
    "scoringPeriodId": WEEK,
    "seasonId": 2026,
    "segmentId": 0,
    "status": {"currentMatchupPeriod": 1, "isActive": True, "latestScoringPeriod": 1},
}


def _by_name(lineup: list[dict]) -> dict[str, dict]:
    return {entry["player_name"]: entry for entry in lineup}


def test_the_opponent_comes_from_the_schedule_not_the_positional_ratings(
    monkeypatch: pytest.MonkeyPatch,
):
    """Two of three leagues lost every ``pro_opponent`` on opening night (#72).

    The schedule says who a team plays; the ratings say how good the matchup
    is. ``espn-api`` gates the first on the second, and ESPN does not publish
    the ratings for a league until it feels like it. The adapter reads the
    opponent from ``proTeamSchedules_wl`` directly.
    """
    install_espn(
        monkeypatch,
        overrides={
            "proTeamSchedules_wl": _schedule_with_week_one(),
            "mPositionalRatings": RATINGS_NOT_YET_PUBLISHED,
        },
    )
    lineup = _by_name(box_scores(week=WEEK).data[0]["team_a_lineup"])
    assert lineup["Michael Thomas"]["pro_team"] == "NO"
    assert lineup["Michael Thomas"]["pro_opponent"] == "CAR"
    assert lineup["Christian McCaffrey"]["pro_team"] == "CAR"
    assert lineup["Christian McCaffrey"]["pro_opponent"] == "NO"
    assert lineup["Patrick Mahomes"]["pro_opponent"] == "CLE"


def test_a_bye_week_is_no_opponent_but_still_a_team(monkeypatch: pytest.MonkeyPatch):
    """When the value is genuinely unavailable the consumer can still place the
    player by his own club; ``"None"`` is ``espn-api``'s sentinel, not a team."""
    install_espn(monkeypatch, overrides={"mPositionalRatings": RATINGS_NOT_YET_PUBLISHED})
    lineup = _by_name(box_scores(week=WEEK).data[0]["team_a_lineup"])
    assert lineup["Michael Thomas"]["pro_team"] == "NO"
    assert lineup["Michael Thomas"]["pro_opponent"] is None
    assert all(entry["pro_opponent"] != "None" for entry in lineup.values())
