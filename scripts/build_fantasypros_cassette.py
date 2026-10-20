#!/usr/bin/env python
"""Build ``tests/cassettes/fantasypros/premium_2026.yaml`` from real captures.

The FantasyPros fixture is a *trimmed recording*, not a hand-authored one
(``docs/testing.md`` §10). Each body started as a real premium-tier response
captured on 2026-10-16 with John's key; this script cuts every list down to a
handful of rows, rewrites ``count`` to match, scrubs the result through the
same hooks the cassette harness uses, scans the finished file, and refuses to
leave it on disk if anything credential-shaped survives.

The raw captures are **not** committed -- 4 MB of players alone -- so this
script takes the directory they live in::

    uv run python scripts/build_fantasypros_cassette.py --captures /path/to/fp-shapes

Every request URI here is exactly what ``sources/fantasypros.py`` sends for the
corresponding call, so a replay through real ``requests`` under
``build_vcr().use_cassette`` matches. The ``x-api-key`` header is present with
its value replaced, the way a recording that went through the scrub hooks
would look: a cassette with no key header at all is indistinguishable from one
recorded unauthenticated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from conftest import REDACTED, scan_file, scrub_text  # noqa: E402

from fantasy_sports.sources.fantasypros import (  # noqa: E402
    BASE_URL,
    DOCTOR_PROBE_PLAYER_ID,
)

CASSETTE = REPO / "tests" / "cassettes" / "fantasypros" / "premium_2026.yaml"
SEASON = 2026
WEEK = 6
ROWS = 5
"""How many rows each list keeps. Enough to exercise a parser, small enough to read."""


def _rows(payload: dict[str, Any], key: str, keep: list[Any]) -> dict[str, Any]:
    """``payload`` with ``payload[key]`` replaced by ``keep`` and ``count`` rewritten.

    ``count`` keeps its original type -- FantasyPros sends it as an int on
    some endpoints and a string on others, and a parser should see both.
    """
    trimmed = dict(payload)
    trimmed[key] = keep
    if "count" in trimmed:
        trimmed["count"] = str(len(keep)) if isinstance(trimmed["count"], str) else len(keep)
    return trimmed


def _players(raw: dict[str, Any]) -> dict[str, Any]:
    """A DST, the doctor probe's row, and the first skill players with an ``espn_id``."""
    players = raw["players"]
    dst = next(p for p in players if p["position_id"] == "DST" and p.get("espn_id"))
    probe = next(p for p in players if p["player_id"] == DOCTOR_PROBE_PLAYER_ID)
    skill = [
        p
        for p in players
        if p.get("espn_id") and p["position_id"] in {"QB", "RB", "WR", "TE"} and p is not probe
    ][: ROWS - 2]
    return _rows(raw, "players", [dst, probe, *skill])


def _probe(raw: dict[str, Any]) -> dict[str, Any]:
    """What ``?player=<fpid>`` answers: the same envelope, one row."""
    probe = next(p for p in raw["players"] if p["player_id"] == DOCTOR_PROBE_PLAYER_ID)
    return _rows(raw, "players", [probe])


def _rankings(raw: dict[str, Any]) -> dict[str, Any]:
    trimmed = _rows(raw, "players", raw["players"][:ROWS])
    # ``ecr_experts`` is the panel per scoring and position: hundreds of
    # expert rows that the shape test does not need more than two of.
    trimmed["ecr_experts"] = {
        scoring: {position: experts[:2] for position, experts in table.items()}
        for scoring, table in raw["ecr_experts"].items()
    }
    return trimmed


def _interaction(uri: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = scrub_text(json.dumps(payload, separators=(",", ":")))
    return {
        "request": {
            "body": None,
            "headers": {"Accept": ["application/json"], "x-api-key": [REDACTED]},
            "method": "GET",
            "uri": uri,
        },
        "response": {
            "body": {"string": body},
            "headers": {
                "Content-Type": ["application/json"],
                "cache-control": ["max-age=600"],
            },
            "status": {"code": 200, "message": "OK"},
        },
    }


def build(captures: Path) -> list[dict[str, Any]]:
    def load(name: str) -> dict[str, Any]:
        with (captures / name).open(encoding="utf-8") as handle:
            return json.load(handle)

    players = load("players.json")
    consensus_ppr = load("consensus_rb_ppr.json")
    consensus_std = load("consensus_rb.json")
    rankings = load("rankings.json")
    projections = load("projections_rb.json")
    projections_ros = load("projections_ros.json")
    injuries = load("injuries.json")
    news = load("news.json")
    experts = load("experts.json")
    points = load("points.json")

    nfl = f"{BASE_URL}/nfl"
    season = f"{nfl}/{SEASON}"
    return [
        _interaction(f"{nfl}/players?external_ids=espn", _players(players)),
        _interaction(
            f"{nfl}/players?external_ids=espn&player={DOCTOR_PROBE_PLAYER_ID}", _probe(players)
        ),
        _interaction(
            f"{season}/consensus-rankings?week={WEEK}&position=RB&scoring=PPR",
            _rows(consensus_ppr, "players", consensus_ppr["players"][:ROWS]),
        ),
        _interaction(
            f"{season}/consensus-rankings?week={WEEK}&position=RB&scoring=STD&experts=show",
            _rows(consensus_std, "players", consensus_std["players"][:ROWS]),
        ),
        _interaction(
            f"{season}/rankings?week={WEEK}&rankstats=true&range=true", _rankings(rankings)
        ),
        _interaction(
            f"{season}/projections?week={WEEK}&position=RB",
            _rows(projections, "players", projections["players"][:ROWS]),
        ),
        _interaction(
            f"{season}/projections?position=RB&ros=true",
            _rows(projections_ros, "players", projections_ros["players"][:ROWS]),
        ),
        _interaction(
            f"{nfl}/injuries?year={SEASON}&week={WEEK}&include_probabilities=true",
            _rows(injuries, "injuries", injuries["injuries"][:ROWS]),
        ),
        _interaction(
            f"{nfl}/news?limit=5&category=injury", _rows(news, "items", news["items"][:5])
        ),
        _interaction(
            f"{season}/rankings/experts", _rows(experts, "experts", experts["experts"][:ROWS])
        ),
        _interaction(
            f"{season}/player-points?position=RB&scoring=PPR&start=5&end=5",
            _rows(points, "players", points["players"][:ROWS]),
        ),
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
