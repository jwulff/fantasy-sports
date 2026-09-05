"""Live smoke tests for the ESPN adapter. Excluded from CI; never run by default.

    uv run pytest -m live

Two groups, because they answer different questions and need different things.

**The canary group needs no credentials.** It reads ESPN's public test league —
``1234``, ``2018`` — and is the thing that tells us the recorded cassette has
gone stale. When it goes red and the unit tests stay green, ESPN changed
something; that is the whole point of the canary (ADR-0005). It is valid for
league, team, standings, draft, settings, matchup and transaction reads **only**:
``espn-api`` refuses box scores and free agents before 2019, and league 1234
exists only for 2018 (ARCHITECTURE §14 item 6, as amended).

**The private-league group needs real cookies** and is skipped without them. It
covers what no public league can: a private league that answers 200 with
credentials, and the failure classification when they are absent. Set
``FANTASY_SPORTS_ESPN_LEAGUE`` to a league the configured account is in.

Nothing here asserts on a person's name, a team name, or a roster: those change
week to week and an assertion on them is a test that fails for the wrong reason.
The assertions are structural.
"""

from __future__ import annotations

import os

import pytest

from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
from fantasy_sports.core.errors import AuthMissingError, LeagueNotFoundError
from fantasy_sports.providers.espn import EspnProvider

pytestmark = pytest.mark.live

CANARY_LEAGUE = "1234"
CANARY_SEASON = 2018


@pytest.fixture(scope="module")
def canary() -> EspnProvider:
    return EspnProvider()


@pytest.fixture(scope="module")
def private() -> tuple[EspnProvider, str, int]:
    credentials = resolve_credentials(ESPN_CREDENTIALS)
    league_id = os.environ.get("FANTASY_SPORTS_ESPN_LEAGUE")
    if not credentials.complete or not league_id:
        pytest.skip("needs ESPN cookies and FANTASY_SPORTS_ESPN_LEAGUE")
    season = int(os.environ.get("FANTASY_SPORTS_ESPN_SEASON", "2026"))
    return EspnProvider(credentials), league_id, season


def test_the_canary_league_still_answers_every_read_it_can(canary: EspnProvider):
    """If this goes red while the unit tests stay green, ESPN moved."""
    league = canary.fetch_league(CANARY_LEAGUE, CANARY_SEASON)
    assert league.team_count > 0
    assert league.roster_slots, "rosterSettings.lineupSlotCounts disappeared"

    teams = canary.fetch_teams(CANARY_LEAGUE, CANARY_SEASON)
    assert len(teams) == league.team_count

    standings = canary.fetch_standings(CANARY_LEAGUE, CANARY_SEASON)
    assert [team.standing for team in standings] == list(range(1, len(teams) + 1))

    roster = canary.fetch_roster(CANARY_LEAGUE, CANARY_SEASON, teams[0].provider_id)
    assert roster and any(slot.is_starter for slot in roster)

    matchups = canary.fetch_matchups(CANARY_LEAGUE, CANARY_SEASON, 1)
    assert matchups and all(item.scoring_period_id == 1 for item in matchups)

    assert canary.fetch_raw(CANARY_LEAGUE, CANARY_SEASON, view="mSettings")["id"] == 1234


def test_the_canary_still_cannot_produce_a_box_score(canary: EspnProvider):
    """Pinned so the fixture policy in ``docs/testing.md`` stays honest.

    ``espn-api`` refuses box scores before 2019 and league 1234 exists only for
    2018. If this ever stops raising, the synthetic box-score fixture #29 needs
    can be replaced with a recording, and this test is where that shows up.
    """
    from espn_api.football import League

    with pytest.raises(Exception, match="box score before 2019"):
        League(league_id=int(CANARY_LEAGUE), year=CANARY_SEASON).box_scores(1)


def test_a_private_league_reads_with_credentials(private):
    provider, league_id, season = private
    league = provider.fetch_league(league_id, season)
    assert league.provider_id == str(league_id)
    assert league.team_count > 0
    assert provider.fetch_teams(league_id, season)


def test_a_private_league_without_credentials_never_reports_expiry(private):
    """The classification the whole 401 design turns on.

    ESPN's 401 body is identical for no cookies, a bad ``espn_s2``, and a league
    the account was never in, so an unauthenticated read of a private league
    must report ``AUTH_MISSING`` — a fact about us — and a *credentialed* refusal
    must never claim the cookies expired.
    """
    _, league_id, season = private
    anonymous = EspnProvider()
    with pytest.raises((AuthMissingError, LeagueNotFoundError)) as err:
        anonymous.fetch_league(league_id, season)
    assert err.value.code.value != "AUTH_EXPIRED"
    reason = err.value.details.get("espn_reason")
    if reason is not None:
        # Record any *new* value in docs/memory/espn-401-tells-you-nothing.md.
        assert isinstance(reason, str)
