#!/usr/bin/env python
"""Build ``tests/cassettes/fantasypros/experts_2026.yaml`` from real captures.

The ``experts`` commands' fixture (jwulff/fantasy-sports#129). Like the client's
(``build_fantasypros_cassette.py``) it is a **trimmed recording**: every body
started as a real premium-tier response captured 2026-10-20 with John's key, and
this script cuts each list to a few rows, rewrites ``count``, scrubs the result
through the cassette hooks, scans the finished file, and deletes it if anything
credential-shaped survived. It is a second file rather than more interactions in
the client's, because the commands need a *current* week (7), a closed week (5),
the rest-of-season ranking, and a players table that actually contains the
players the lists name, none of which the client's recording was cut to hold.

The raw captures are not committed (the players table alone is 4 MB)::

    uv run python scripts/build_experts_cassette.py --captures /path/to/captures

The directory holds one file per request, named for it: ``players.json``,
``consensus_week.json``, ``consensus_closed.json``, ``consensus_ros.json``,
``projections_week.json``, ``projections_ros.json``, ``projections_player.json``,
``injuries.json``, ``injuries_team.json``, ``injuries_player.json``,
``news.json``, ``news_injury.json`` and ``news_player.json``. The requests are
exactly what ``experts rankings|projections|injuries|news`` sends for the
arguments the tests pass, which is why the URIs below can be matched.

What the trimmed recording deliberately contains, because a test reads it:

* a rest-of-season ranking row whose player the players table lists **without**
  an ``espn_id``, and a week's ranking row for a player the table does not list
  at all (both join to ``null``);
* four players named ``Josh A...`` and two exact ``Josh Johnson`` rows, for the
  unique-prefix and the ambiguous-name paths of ``--player``;
* injuries with a probability, with none, undated, on IR, and OUT.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from conftest import scan_file  # noqa: E402

from fantasy_sports.sources.fantasypros import BASE_URL  # noqa: E402
from scripts.build_fantasypros_cassette import _interaction, _rows  # noqa: E402

CASSETTE = REPO / "tests" / "cassettes" / "fantasypros" / "experts_2026.yaml"
SEASON = 2026
WEEK = 7
CLOSED_WEEK = 5
ROWS = 8
BIJAN = 23133
AJ_BROWN = 18218
NO_ESPN_ID = 28109
"""J'Mari Taylor: in the players table, with no ``espn_id``."""


def _players_of(payload: dict[str, Any], key: str, id_key: str) -> list[int]:
    return [int(row[id_key]) for row in payload[key] if row.get(id_key) is not None]


def build(captures: Path) -> list[dict[str, Any]]:
    def load(name: str) -> dict[str, Any]:
        with (captures / f"{name}.json").open(encoding="utf-8") as handle:
            return json.load(handle)

    players = load("players")
    full = {int(p["player_id"]): p for p in players["players"]}

    consensus = load("consensus_week")
    closed = load("consensus_closed")
    ros = load("consensus_ros")
    proj_week = load("projections_week")
    proj_ros = load("projections_ros")
    proj_player = load("projections_player")
    injuries = load("injuries")
    injuries_team = load("injuries_team")
    injuries_player = load("injuries_player")
    news = load("news")
    news_injury = load("news_injury")
    news_player = load("news_player")

    # --- rankings: the head, plus the rows that join to null --------------- #
    # Every RB in the current week's list has an ESPN id, so the row the table
    # lists *without* one comes from the rest-of-season list; the row the table
    # does not list at all comes from the week's.
    ranked = consensus["players"]
    unlisted = ranked[ROWS]
    week_rows = [*ranked[: ROWS - 1], unlisted]
    no_espn = next(r for r in ros["players"] if int(r["player_id"]) == NO_ESPN_ID)
    ros_rows = [*ros["players"][: ROWS - 1], no_espn]

    # --- injuries: one of each kind a test reads --------------------------- #
    def first(pred: Any) -> dict[str, Any]:
        return next(r for r in injuries["injuries"] if pred(r))

    chosen_injuries = [
        first(lambda r: r["status"] == "Questionable" and r["probability_of_playing"]),
        first(lambda r: r["status"] == "OUT"),
        first(lambda r: r["status"] == "IR" and r["ir_weeks"] and r["injury_update_date"]),
        first(lambda r: r["status"] == "PUP"),
        first(lambda r: r["status"] == "" and r["practice_1"] and r["position_id"] in {"WR", "RB"}),
        first(lambda r: r["injury_update_date"] is None and r["position_id"] in {"WR", "RB", "TE"}),
        first(lambda r: r["player_id"] == AJ_BROWN),
    ]
    chosen_injuries = list({r["player_id"]: r for r in chosen_injuries}.values())

    trimmed = {
        "consensus_week": _rows(consensus, "players", week_rows),
        "consensus_closed": _rows(closed, "players", closed["players"][:ROWS]),
        "consensus_ros": _rows(ros, "players", ros_rows),
        "projections_week": _rows(proj_week, "players", proj_week["players"][:ROWS]),
        "projections_ros": _rows(proj_ros, "players", proj_ros["players"][:ROWS]),
        "projections_player": proj_player,
        "injuries": _rows(injuries, "injuries", chosen_injuries),
        "injuries_team": _rows(injuries_team, "injuries", injuries_team["injuries"][:4]),
        "injuries_player": injuries_player,
        "news": _rows(news, "items", news["items"][:6]),
        "news_injury": news_injury,
        "news_player": _rows(news_player, "items", news_player["items"][:3]),
    }

    # --- the players table: everyone named above, plus the name fixtures --- #
    wanted: set[int] = set()
    for key in ("consensus_week", "consensus_closed", "consensus_ros"):
        wanted.update(_players_of(trimmed[key], "players", "player_id"))
    for key in ("projections_week", "projections_ros", "projections_player"):
        wanted.update(_players_of(trimmed[key], "players", "fpid"))
    for key in ("injuries", "injuries_team", "injuries_player"):
        wanted.update(_players_of(trimmed[key], "injuries", "player_id"))
    for key in ("news", "news_injury", "news_player"):
        wanted.update(_players_of(trimmed[key], "items", "player_id"))
    wanted.add(BIJAN)
    wanted.discard(int(unlisted["player_id"]))
    names = [
        p
        for p in players["players"]
        if p["player_name"].startswith("Josh A") or p["player_name"] == "Josh Johnson"
    ]
    wanted.update(int(p["player_id"]) for p in names)
    table = [full[i] for i in sorted(wanted) if i in full]
    # The table is not exhaustive: an id the lists name but it lacks must stay
    # absent, and every other id the lists name must be present.
    present = {int(p["player_id"]) for p in table}
    assert int(unlisted["player_id"]) not in present
    assert NO_ESPN_ID in present and not full[NO_ESPN_ID].get("espn_id")

    nfl = f"{BASE_URL}/nfl"
    season = f"{nfl}/{SEASON}"
    return [
        _interaction(f"{nfl}/players?external_ids=espn", _rows(players, "players", table)),
        _interaction(
            f"{season}/consensus-rankings?week={WEEK}&position=RB&scoring=PPR",
            trimmed["consensus_week"],
        ),
        _interaction(
            f"{season}/consensus-rankings?week={CLOSED_WEEK}&position=RB&scoring=PPR",
            trimmed["consensus_closed"],
        ),
        _interaction(
            f"{season}/consensus-rankings?type=ros&position=RB&scoring=PPR",
            trimmed["consensus_ros"],
        ),
        _interaction(f"{season}/projections?week={WEEK}&position=RB", trimmed["projections_week"]),
        _interaction(f"{season}/projections?position=RB&ros=true", trimmed["projections_ros"]),
        _interaction(
            f"{season}/projections?week={WEEK}&players={BIJAN}", trimmed["projections_player"]
        ),
        _interaction(
            f"{nfl}/injuries?year={SEASON}&week={WEEK}&include_probabilities=true",
            trimmed["injuries"],
        ),
        _interaction(
            f"{nfl}/injuries?year={SEASON}&week={WEEK}&team_id=NE&include_probabilities=true",
            trimmed["injuries_team"],
        ),
        _interaction(
            f"{nfl}/injuries?year={SEASON}&week={WEEK}&player_ids={AJ_BROWN}"
            "&include_probabilities=true",
            trimmed["injuries_player"],
        ),
        _interaction(f"{nfl}/news?limit=25", trimmed["news"]),
        _interaction(f"{nfl}/news?limit=5&category=injury", trimmed["news_injury"]),
        _interaction(f"{nfl}/news?limit=5&fpid={BIJAN}", trimmed["news_player"]),
    ]


def main(argv: list[str] | None = None) -> int:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--captures", type=Path, required=True, help="directory of raw captures")
    args = parser.parse_args(argv)

    interactions = build(args.captures)
    CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    CASSETTE.write_text(
        yaml.safe_dump({"interactions": interactions, "version": 1}, sort_keys=False),
        encoding="utf-8",
    )
    findings = scan_file(CASSETTE)
    if findings:
        CASSETTE.unlink()
        print("refusing to keep the cassette; credential-shaped text survived:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print(f"wrote {CASSETTE.relative_to(REPO)} ({CASSETTE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
