#!/usr/bin/env python
"""Build ``tests/cassettes/espn/synthetic_2026.yaml``, a hand-authored fixture.

    uv run python scripts/build_synthetic_cassette.py

Three things the public canary league (``1234``, ``2018``) cannot record, and
this fixture supplies. All three are library refusals, not our limitations:

* **Free agents.** ``espn-api`` raises ``Cant use free agents before 2019`` and
  league 1234 exists *only* for 2018, so no request is ever made.
* **The activity feed.** Same refusal, plus ESPN stopped serving
  ``kona_league_communication`` for historical seasons (issue #546, open since
  2024). Without it there is no way to record the second of ESPN's two
  transaction surfaces, and the reconciliation between them is the whole point
  of ``fetch_transactions``.
* **A playoff week whose matchup period spans two scoring periods.** The canary
  is 1:1 for every week, so the ``scoringPeriodId``/``matchupPeriodId`` split —
  the one thing an in-season ESPN-only test never exercises — would go
  untested against it.

Being synthetic is a real limitation and is recorded in ``docs/testing.md``: a
hand-built payload proves this adapter reads the shape it was told about, not
that ESPN still sends that shape. The canary recording is what covers the
latter, and the ``live``-marked tests are what catch drift.

Two deliberate choices in the data:

* **Owner ids are brace-wrapped but are not GUIDs** (``{OWNER-ALPHA}``). A real
  SWID here would be rewritten to a single ``{SWID-REDACTED}`` placeholder by
  the scrub hook that guards recorded fixtures, which would collapse every
  member to one identity and quietly destroy the ``teams[].owners`` →
  ``members[].id`` join this fixture exists to exercise. Non-GUID ids survive
  the scrubber and the repo-wide credential scan intact, and they are the shape
  the stable-pseudonym work (jwulff/fantasy-sports#38) produces anyway.
* **Every timestamp is a round epoch-millisecond value**, so a test can assert
  the exact UTC instant a kickoff renders as and catch a naive, host-local
  datetime leaking through from ``espn-api``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CASSETTE = REPO / "tests" / "cassettes" / "espn" / "synthetic_2026.yaml"

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
SEASON = 2026
LEAGUE = 99
WEEK = 2

LEAGUE_URL = f"{BASE}/seasons/{SEASON}/segments/0/leagues/{LEAGUE}"
SEASON_URL = f"{BASE}/seasons/{SEASON}"

#: 2026-09-13T17:00:00Z and 2026-09-13T20:15:00Z, to the millisecond.
KICKOFF_EARLY = 1789318800000
KICKOFF_LATE = 1789330500000
#: 2026-09-09T16:00:00Z — the trade the activity feed carries and
#: ``mTransactions2`` does not.
TRADE_AT = 1788969600000
#: 2026-09-10T19:30:00Z — the waiver claim only mTransactions2 carries.
WAIVER_AT = 1789068600000

#: ESPN's ``filterSlotIds`` for a wide receiver. ``espn-api``'s ``POSITION_MAP``
#: maps ``WR`` to 4, and the adapter passes it straight through.
WIDE_RECEIVER_SLOTS = [4]

# --- the `x-fantasy-filter` headers `espn-api` sends for each read ---------- #
#
# Copied from `espn_api/football/league.py` and `espn_api/requests/espn_requests.py`
# rather than guessed: `tests/conftest.py` matches on this header, so a value
# that differs from what the library builds is a fixture the adapter cannot
# match. The matcher compares canonicalised JSON, so key and list order here do
# not have to reproduce the library's `json.dumps` byte for byte — which is
# just as well, since the library builds `filterType.value` from a Python set
# and its order changes on every interpreter run.

PLAYERS_FILTER = {"filterActive": {"value": True}}

TRANSACTIONS_FILTER = {
    "transactions": {
        "filterType": {
            "value": [
                "FREEAGENT",
                "WAIVER",
                "WAIVER_ERROR",
                "TRADE_ACCEPT",
                "TRADE_UPHOLD",
                "ROSTER",
                "DRAFT",
            ]
        }
    }
}

ACTIVITY_FILTER = {
    "topics": {
        "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
        "limit": 25,
        "limitPerMessageSet": {"value": 25},
        "offset": 0,
        "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
        "sortFor": {"sortPriority": 2, "sortAsc": False},
        "filterIncludeMessageTypeIds": {"value": [178, 180, 179, 239, 181, 244]},
    }
}


def free_agent_filter(slot_ids: list[int]) -> dict[str, Any]:
    """``kona_player_info``'s filter, for one position selection."""
    return {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": slot_ids},
            "limit": 50,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
        }
    }


def _player(
    player_id: int,
    name: str,
    *,
    position_id: int,
    eligible: list[int],
    pro_team: int,
    points: float,
    projected: float,
) -> dict[str, Any]:
    """One ``player`` object, in the shape ESPN sends and ``espn-api`` reads."""
    first, _, last = name.partition(" ")
    return {
        "active": True,
        "defaultPositionId": position_id,
        "droppable": True,
        "eligibleSlots": eligible,
        "firstName": first,
        "fullName": name,
        "id": player_id,
        "injured": False,
        "injuryStatus": "ACTIVE",
        "lastName": last,
        "ownership": {"percentOwned": 61.5, "percentStarted": 40.25},
        "proTeamId": pro_team,
        "stats": [
            {
                "appliedAverage": points,
                "appliedStats": {"42": 100.0},
                "appliedTotal": points,
                "externalId": str(SEASON),
                "id": f"0{SEASON}",
                "proTeamId": pro_team,
                "scoringPeriodId": WEEK,
                "seasonId": SEASON,
                "statSourceId": 0,
                "statSplitTypeId": 1,
                "stats": {"42": 100.0},
            },
            {
                "appliedAverage": projected,
                "appliedStats": {"42": 110.0},
                "appliedTotal": projected,
                "externalId": str(SEASON),
                "id": f"1{SEASON}",
                "proTeamId": pro_team,
                "scoringPeriodId": WEEK,
                "seasonId": SEASON,
                "statSourceId": 1,
                "statSplitTypeId": 1,
                "stats": {"42": 110.0},
            },
        ],
        "universeId": 1,
    }


#: (id, name, defaultPositionId, eligibleSlots, proTeamId, points, projected)
ROSTERED = [
    (2001, "Ada Lovelace", 0, [0, 20, 21], 12, 21.4, 19.8, 0),
    (2002, "Grace Hopper", 2, [2, 3, 23, 20, 21], 12, 14.2, 12.0, 2),
    (2003, "Katherine Johnson", 4, [3, 4, 5, 23, 20, 21], 26, 9.9, 11.5, 20),
    (2011, "Alan Turing", 0, [0, 20, 21], 26, 18.1, 17.2, 0),
    (2012, "Margaret Hamilton", 2, [2, 3, 23, 20, 21], 12, 11.0, 13.4, 2),
]
FREE_AGENTS = [
    (3001, "Barbara Liskov", 4, [3, 4, 5, 23, 20, 21], 26, 0.0, 8.4),
    (3002, "Radia Perlman", 6, [5, 6, 23, 20, 21], 12, 0.0, 6.1),
]


def _roster_entry(spec: tuple[Any, ...]) -> dict[str, Any]:
    player_id, name, position_id, eligible, pro_team, points, projected, slot = spec
    return {
        "acquisitionDate": WAIVER_AT,
        "acquisitionType": "DRAFT",
        "injuryStatus": "ACTIVE",
        "lineupSlotId": slot,
        "pendingTransactionIds": None,
        "playerId": player_id,
        "playerPoolEntry": {
            "appliedStatTotal": points,
            "id": player_id,
            "keeperValue": 0,
            "keeperValueFuture": 0,
            "lineupLocked": False,
            "onTeamId": 1,
            "player": _player(
                player_id,
                name,
                position_id=position_id,
                eligible=eligible,
                pro_team=pro_team,
                points=points,
                projected=projected,
            ),
            "ratings": {},
            "rosterLocked": False,
            "status": "ONTEAM",
            "tradeLocked": False,
        },
        "status": "NORMAL",
    }


def _team(team_id: int, name: str, owner: str, roster: list[Any]) -> dict[str, Any]:
    return {
        "abbrev": name[:4].upper(),
        "currentProjectedRank": team_id,
        "divisionId": 0,
        "draftDayProjectedRank": team_id,
        "id": team_id,
        "isActive": True,
        "name": name,
        "owners": [owner],
        "playoffSeed": team_id,
        "points": 210.5,
        "pointsAdjusted": 0,
        "pointsDelta": 0,
        "primaryOwner": owner,
        "rankCalculatedFinal": 0,
        "record": {
            "overall": {
                "wins": 2 if team_id == 1 else 0,
                "losses": 0 if team_id == 1 else 2,
                "ties": 0,
                "pointsFor": 210.5 if team_id == 1 else 180.25,
                "pointsAgainst": 180.25 if team_id == 1 else 210.5,
                "streakLength": 2,
                "streakType": "WIN" if team_id == 1 else "LOSS",
            }
        },
        "roster": {"entries": [_roster_entry(spec) for spec in roster]},
        "transactionCounter": {"acquisitions": 1, "drops": 1, "trades": 1, "moveToIR": 0},
        "waiverRank": team_id,
    }


def _schedule() -> list[dict[str, Any]]:
    """Two regular weeks plus a playoff round that spans scoring periods 13-14."""
    return [
        {
            "away": {"teamId": 2, "totalPoints": 88.5, "gamesPlayed": 1},
            "home": {"teamId": 1, "totalPoints": 105.25, "gamesPlayed": 1},
            "id": 1,
            "matchupPeriodId": 1,
            "winner": "HOME",
        },
        {
            "away": {"teamId": 2, "totalPoints": 91.75, "gamesPlayed": 1},
            "home": {"teamId": 1, "totalPoints": 105.25, "gamesPlayed": 1},
            "id": 2,
            "matchupPeriodId": 2,
            "winner": "HOME",
        },
        {
            "away": {"teamId": 2, "totalPoints": 150.0, "gamesPlayed": 1},
            "home": {"teamId": 1, "totalPoints": 149.5, "gamesPlayed": 1},
            "id": 13,
            "matchupPeriodId": 13,
            "playoffTierType": "WINNERS_BRACKET",
            "winner": "AWAY",
        },
    ]


def _bootstrap() -> dict[str, Any]:
    return {
        "draftDetail": {"drafted": True, "inProgress": False},
        "gameId": 1,
        "id": LEAGUE,
        # Two members with display names and *distinct* ids: the join this
        # fixture exists to exercise is `teams[].owners` -> `members[].id`.
        "members": [
            {
                "id": "{OWNER-ALPHA}",
                "displayName": "alpha",
                "firstName": "Ann",
                "lastName": "Alpha",
            },
            {
                "id": "{OWNER-BRAVO}",
                "displayName": "bravo",
                "firstName": "Bo",
                "lastName": "Bravo",
            },
        ],
        "schedule": _schedule(),
        "scoringPeriodId": WEEK,
        "seasonId": SEASON,
        "segmentId": 0,
        "settings": {
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
            "draftSettings": {"keeperCount": 0},
            "name": "Synthetic Test League",
            "rosterSettings": {
                # QB 1, RB 2, WR 2, TE 1, FLEX 1, D/ST 1, K 1, BE 5
                "lineupSlotCounts": {
                    "0": 1,
                    "2": 2,
                    "4": 2,
                    "6": 1,
                    "16": 1,
                    "17": 1,
                    "20": 5,
                    "23": 1,
                }
            },
            "scheduleSettings": {
                "divisions": [],
                "matchupPeriodCount": 14,
                # 13 spans two scoring periods: the playoff split.
                "matchupPeriods": {str(n): [n] for n in range(1, 13)} | {"13": [13, 14]},
                "playoffMatchupPeriodLength": 2,
                "playoffSeedingRule": "TOTAL_POINTS_SCORED",
                "playoffTeamCount": 2,
            },
            "scoringSettings": {
                "matchupTieRule": "NONE",
                "playoffMatchupTieRule": "NONE",
                "scoringItems": [],
                "scoringType": "H2H_POINTS",
            },
            "size": 2,
            "tradeSettings": {"vetoVotesRequired": 0},
        },
        "status": {
            "currentMatchupPeriod": WEEK,
            "finalScoringPeriod": 14,
            "firstScoringPeriod": 1,
            "isActive": True,
            "latestScoringPeriod": WEEK,
            "previousSeasons": [],
            "standingsUpdateDate": WAIVER_AT,
        },
        "teams": [
            _team(1, "Team Alpha", "{OWNER-ALPHA}", ROSTERED[:3]),
            _team(2, "Team Bravo", "{OWNER-BRAVO}", ROSTERED[3:]),
        ],
    }


def _players_wl() -> list[dict[str, Any]]:
    everyone = ROSTERED + [spec + (None,) for spec in FREE_AGENTS]
    return [{"id": spec[0], "fullName": spec[1]} for spec in everyone]


def _pro_schedule() -> dict[str, Any]:
    """``proTeamSchedules_wl``: the only honest source of a kickoff instant.

    ``espn-api`` turns these epoch-millisecond values into naive, host-local
    datetimes. The adapter re-derives them from this payload instead, so these
    two constants are what a timezone test asserts against.
    """
    return {
        "settings": {
            "proTeams": [
                {"id": 0, "abbrev": "FA", "proGamesByScoringPeriod": {}},
                {
                    "id": 12,
                    "abbrev": "KC",
                    "proGamesByScoringPeriod": {
                        str(WEEK): [
                            {
                                "awayProTeamId": 12,
                                "date": KICKOFF_EARLY,
                                "homeProTeamId": 26,
                                "id": 4001,
                            }
                        ]
                    },
                },
                {
                    "id": 26,
                    "abbrev": "SEA",
                    "proGamesByScoringPeriod": {
                        str(WEEK): [
                            {
                                "awayProTeamId": 12,
                                "date": KICKOFF_LATE,
                                "homeProTeamId": 26,
                                "id": 4002,
                            }
                        ]
                    },
                },
            ]
        }
    }


def _draft() -> dict[str, Any]:
    return {
        "draftDetail": {
            "drafted": True,
            "inProgress": False,
            "picks": [
                {
                    "bidAmount": 0,
                    "keeper": False,
                    "nominatingTeamId": 0,
                    "playerId": 2001,
                    "roundId": 1,
                    "roundPickNumber": 1,
                    "teamId": 1,
                }
            ],
        }
    }


def _transactions() -> dict[str, Any]:
    """``mTransactions2``: a waiver claim. Deliberately **no trade**.

    The trade lives only in the activity feed below, which is what makes the
    reconciliation testable: an adapter reading one surface returns one row
    here, and an adapter reading both returns two.
    """
    return {
        "id": LEAGUE,
        "seasonId": SEASON,
        "scoringPeriodId": WEEK,
        "transactions": [
            {
                "bidAmount": 17,
                "executionType": "EXECUTE",
                "id": "waiver-0001",
                "isPending": False,
                "items": [
                    {"fromTeamId": 0, "playerId": 3001, "toTeamId": 1, "type": "ADD"},
                    {"fromTeamId": 1, "playerId": 2003, "toTeamId": 0, "type": "DROP"},
                ],
                "memberId": "{OWNER-ALPHA}",
                "processDate": WAIVER_AT,
                "scoringPeriodId": WEEK,
                "status": "EXECUTED",
                "teamId": 1,
                "type": "WAIVER",
            },
            {
                "bidAmount": 0,
                "executionType": "EXECUTE",
                "id": "proposal-0002",
                "isPending": True,
                "items": [{"fromTeamId": 1, "playerId": 2002, "toTeamId": 2, "type": "ADD"}],
                "proposedDate": WAIVER_AT,
                "scoringPeriodId": WEEK,
                "status": "PENDING",
                "teamId": 2,
                # No normalized equivalent: a proposal is not a roster move.
                # It must not appear in `fetch_transactions` output.
                "type": "TRADE_PROPOSAL",
            },
        ],
    }


def _activity() -> dict[str, Any]:
    """``kona_league_communication``: ESPN's *other* transaction vocabulary.

    Message type 244 is a trade, and ``espn-api`` synthesises **two** rows from
    it — ``TRADE_SENT`` from the losing team and ``TRADE_RECEIVED`` by the
    gaining one. Neither string appears anywhere in ``mTransactions2``.
    """
    return {
        "topics": [
            {
                "author": "{OWNER-ALPHA}",
                "date": TRADE_AT,
                "id": "topic-0001",
                "messages": [
                    {
                        "author": "{OWNER-ALPHA}",
                        "date": TRADE_AT,
                        "from": 1,
                        "id": "message-0001",
                        "messageTypeId": 244,
                        "targetId": 2002,
                        "to": 2,
                    }
                ],
                "type": "ACTIVITY_TRANSACTIONS",
            }
        ]
    }


def _free_agents(slot_ids: list[int] | None = None) -> dict[str, Any]:
    """The free-agent pool ESPN would return for one ``filterSlotIds`` value.

    Filtered here, exactly as ESPN filters it server-side, so the fixture holds
    two *different* payloads behind one URL. That is what makes the
    ``x-fantasy-filter`` matcher testable against the real corpus rather than
    only against a synthetic pair: without the matcher, the position-filtered
    read replays the unfiltered response and still passes.
    """
    wanted = [spec for spec in FREE_AGENTS if not slot_ids or not set(spec[3]).isdisjoint(slot_ids)]
    return {
        "players": [
            {
                "draftAuctionValue": 0,
                "id": spec[0],
                "keeperValue": 0,
                "lineupLocked": False,
                "onTeamId": 0,
                "player": _player(
                    spec[0],
                    spec[1],
                    position_id=spec[2],
                    eligible=spec[3],
                    pro_team=spec[4],
                    points=spec[5],
                    projected=spec[6],
                ),
                "ratings": {},
                "rosterLocked": False,
                "status": "FREEAGENT",
                "tradeLocked": False,
            }
            for spec in wanted
        ]
    }


def _positional_ratings() -> dict[str, Any]:
    return {
        "positionAgainstOpponent": {
            "positionalRatings": {
                str(position): {
                    "average": 12.0,
                    "ratingsByOpponent": {
                        "12": {"average": 11.0, "rank": 4},
                        "26": {"average": 13.0, "rank": 9},
                    },
                }
                for position in (0, 2, 4, 6)
            }
        }
    }


def _interaction(uri: str, payload: Any, fantasy_filter: Any = None) -> dict[str, Any]:
    """One recorded exchange.

    ``fantasy_filter`` is the ``x-fantasy-filter`` header ``espn-api`` sends for
    this read. It is part of the request identity — ``tests/conftest.py``
    matches on it — so a hand-authored interaction that omits one ESPN would
    have received is a fixture the adapter can never match. The values here are
    the ones ``espn-api`` actually builds; see ``docs/testing.md`` §2.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    headers: dict[str, list[str]] = {}
    if fantasy_filter is not None:
        headers["x-fantasy-filter"] = [json.dumps(fantasy_filter)]
    return {
        "request": {
            "body": None,
            "headers": headers,
            "method": "GET",
            "uri": uri,
        },
        "response": {
            "body": {"string": body},
            "headers": {"Content-Type": ["application/json;charset=utf-8"]},
            "status": {"code": 200, "message": "OK"},
        },
    }


def main() -> int:
    import yaml

    interactions = [
        _interaction(
            f"{LEAGUE_URL}?view=mTeam&view=mRoster&view=mMatchup&view=mSettings&view=mStandings",
            _bootstrap(),
        ),
        _interaction(
            f"{BASE}/seasons/{SEASON}/players?view=players_wl", _players_wl(), PLAYERS_FILTER
        ),
        _interaction(f"{SEASON_URL}?view=proTeamSchedules_wl", _pro_schedule()),
        _interaction(f"{LEAGUE_URL}?view=mDraftDetail", _draft()),
        _interaction(f"{LEAGUE_URL}?view=mMatchupScore", _bootstrap()),
        _interaction(
            f"{LEAGUE_URL}?scoringPeriodId={WEEK}&view=mTransactions2",
            _transactions(),
            TRANSACTIONS_FILTER,
        ),
        _interaction(
            f"{LEAGUE_URL}/communication/?view=kona_league_communication",
            _activity(),
            ACTIVITY_FILTER,
        ),
        # Two interactions, one URL. They differ only in `filterSlotIds`, and
        # they are the corpus-level proof that the `x-fantasy-filter` matcher
        # works: without it the second read replays the first response.
        _interaction(
            f"{LEAGUE_URL}?scoringPeriodId={WEEK}&view=kona_player_info",
            _free_agents(),
            free_agent_filter([]),
        ),
        _interaction(
            f"{LEAGUE_URL}?scoringPeriodId={WEEK}&view=kona_player_info",
            _free_agents(WIDE_RECEIVER_SLOTS),
            free_agent_filter(WIDE_RECEIVER_SLOTS),
        ),
        _interaction(
            f"{LEAGUE_URL}?scoringPeriodId={WEEK}&view=mPositionalRatings", _positional_ratings()
        ),
    ]
    CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    CASSETTE.write_text(
        yaml.safe_dump({"interactions": interactions, "version": 1}, sort_keys=False)
    )
    print(f"wrote {CASSETTE.relative_to(REPO)} ({CASSETTE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
