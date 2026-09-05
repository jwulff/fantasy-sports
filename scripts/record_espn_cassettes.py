#!/usr/bin/env python
"""Re-record the ESPN cassettes from the public canary league.

    uv run python scripts/record_espn_cassettes.py

Records ``tests/cassettes/espn/canary_2018.yaml`` by driving
:class:`~fantasy_sports.providers.espn.EspnProvider` against ESPN's public test
league — ``league_id=1234, year=2018``, the league ``espn-api``'s own
integration test has hit daily and unattended for years (ARCHITECTURE §14 item
6). No credentials are needed and none are sent.

**This is the only script in the repo that talks to ESPN.** Unit tests replay
the recording offline; ``pytest-socket`` makes a cassette miss fail loudly
rather than quietly calling ESPN.

Two things the recording deliberately does *not* cover, both because the canary
cannot produce them and both covered by hand-authored fixtures instead:

* **Free agents.** ``espn-api`` refuses ``free_agents()`` before 2019 and
  league 1234 exists only for 2018, so no request is ever made.
* **The activity feed.** Same refusal, plus ESPN stopped serving
  ``kona_league_communication`` for historical seasons entirely (issue #546).

Recording goes through ``tests/conftest.py``'s scrub hooks, which is not
optional: they run ``decode_compressed_response`` before the body scrubber, so
the SWID GUIDs ESPN echoes in ``members``/``owners`` are visible as text rather
than as a gzip stream that matches nothing.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CANARY_LEAGUE = "1234"
CANARY_SEASON = 2018
CASSETTE = "espn/canary_2018.yaml"


def main() -> int:
    import vcr
    from conftest import build_vcr_config  # noqa: PLC0415 - path is set up above

    from fantasy_sports.providers.espn import EspnProvider

    config = build_vcr_config()
    recorder = vcr.VCR(**config)
    target = Path(config["cassette_library_dir"]) / CASSETTE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    with recorder.use_cassette(CASSETTE, record_mode="all", allow_playback_repeats=True):
        provider = EspnProvider()
        league = provider.fetch_league(CANARY_LEAGUE, CANARY_SEASON)
        print(f"league        {league.name!r} teams={league.team_count} week={league.current_week}")
        print(f"teams         {len(provider.fetch_teams(CANARY_LEAGUE, CANARY_SEASON))}")
        print(f"standings     {len(provider.fetch_standings(CANARY_LEAGUE, CANARY_SEASON))}")
        print(f"matchups w1   {len(provider.fetch_matchups(CANARY_LEAGUE, CANARY_SEASON, 1))}")
        print(f"transactions  {len(provider.fetch_transactions(CANARY_LEAGUE, CANARY_SEASON))}")
        # `since` sweeps every scoring period, which is what puts real
        # transaction rows in the cassette: the canary's *current* period is
        # week 18 of a finished season and has none.
        swept = provider.fetch_transactions(CANARY_LEAGUE, CANARY_SEASON, since=EPOCH)
        print(f"transactions* {len(swept)} swept across every scoring period")
        first_team = provider.fetch_teams(CANARY_LEAGUE, CANARY_SEASON)[0]
        print(
            "roster        "
            f"{len(provider.fetch_roster(CANARY_LEAGUE, CANARY_SEASON, first_team.provider_id))}"
        )
        print(
            "roster w1     "
            f"{len(provider.fetch_roster(CANARY_LEAGUE, CANARY_SEASON, first_team.provider_id, 1))}"
        )
        raw = provider.fetch_raw(CANARY_LEAGUE, CANARY_SEASON, view="mSettings")
        print(f"raw mSettings {sorted(raw)}")

    size = target.stat().st_size
    print(f"\nwrote {target.relative_to(REPO)} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
