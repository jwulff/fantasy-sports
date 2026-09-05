#!/usr/bin/env python
"""Re-record an ESPN cassette, and refuse to leave a dirty one on disk.

    uv run python scripts/record_espn_cassettes.py                  # the canary
    uv run python scripts/record_espn_cassettes.py --credentialed   # + Keychain cookies
    uv run python scripts/record_espn_cassettes.py --league 12345 --season 2026

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

## The two guards

**Nothing is trusted until it has been read back off disk.** After the cassette
is written, :func:`verify` re-reads the bytes, runs the repo-wide credential
scan against them, and additionally greps for the *literal* credential values
this run actually used, in every form they could have been serialised in. A
scrubber asserted through its own return value proves nothing about the file.
If either check fails the cassette is deleted and the script exits non-zero.

**A private league is not committable.** Only the public canary may be written
into ``tests/cassettes/espn/``. Every other league lands in
``tests/cassettes/private/``, which is gitignored: those payloads carry other
people's team names, display names and pseudonymised member ids, and the scrub
covers credentials, not the rest of a league's roster. See ``docs/testing.md``
§6.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CANARY_LEAGUE = "1234"
CANARY_SEASON = 2018
CANARY_CASSETTE = "espn/canary_2018.yaml"

#: The only recording that may be committed. Everything else is somebody's
#: private league, and a private league is PII whether or not it is scrubbed.
COMMITTABLE = {(CANARY_LEAGUE, CANARY_SEASON): CANARY_CASSETTE}

#: Gitignored. `tests/cassettes/private/` never reaches a commit.
PRIVATE_DIR = "private"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=CANARY_LEAGUE, help="ESPN league id")
    parser.add_argument("--season", type=int, default=CANARY_SEASON, help="season year")
    parser.add_argument(
        "--credentialed",
        action="store_true",
        help="send the ESPN cookies from the auth chain (Keychain first). Required "
        "for a private league; harmless against the canary, and the cheapest "
        "end-to-end proof that the scrub covers a real credential.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="cassette path relative to tests/cassettes/ (default: derived from the league)",
    )
    return parser.parse_args(argv)


def cassette_name(league: str, season: int, out: str | None) -> str:
    """Where this recording is allowed to land."""
    if out is not None:
        return out
    return COMMITTABLE.get((league, season), f"{PRIVATE_DIR}/espn_{league}_{season}.yaml")


def credential_variants(values: list[str]) -> set[bytes]:
    """Every serialisation a credential could survive as, as raw bytes.

    A SWID travels brace-wrapped in a cookie, bare in a JSON body, and
    percent-encoded in a URL path (``%7B...%7D``) — that last form matches
    neither the scan patterns nor the scrubber, which is the trap recorded in
    ``docs/memory/espn-401-tells-you-nothing.md``. Checking the literal value we
    actually sent is the one check that does not depend on a pattern being
    right.
    """
    variants: set[bytes] = set()
    for value in values:
        if not value:
            continue
        for form in (value, value.strip("{}"), quote(value, safe=""), quote(value.strip("{}"))):
            variants.add(form.encode("utf-8"))
            variants.add(form.upper().encode("utf-8"))
            variants.add(form.lower().encode("utf-8"))
    return variants


def verify(target: Path, secrets: list[str]) -> list[str]:
    """Re-read the written cassette and report every reason it is not clean.

    Never echoes a matched value: this output lands in a terminal and, if the
    procedure is ever automated, in a CI log.
    """
    from conftest import format_findings, scan_file  # noqa: PLC0415 - path set up above

    problems: list[str] = []
    findings = scan_file(target)
    if findings:
        problems.append("credential pattern in the recording:\n" + format_findings(findings))

    raw = target.read_bytes()
    for index, variant in enumerate(sorted(credential_variants(secrets))):
        if variant in raw:
            problems.append(
                f"a credential this run actually sent survived into {target.name} "
                f"(variant #{index}); the value is not printed"
            )
            break
    return problems


def resolve_cookies() -> tuple[object, list[str]]:
    """The ESPN credentials, from the auth chain, plus their revealed values.

    The revealed values are held for exactly one purpose — grepping the
    finished file for them in :func:`verify` — and are never printed, logged,
    or written anywhere. Keychain is the first link of the chain, so on a
    developer machine with ``fantasy-sports auth login`` already run this needs
    no environment at all.
    """
    from fantasy_sports.auth.chain import (  # noqa: PLC0415 - path set up above
        ESPN_CREDENTIALS,
        resolve_credentials,
    )

    resolved = resolve_credentials(ESPN_CREDENTIALS)
    if not resolved.complete:
        print(
            f"warning: no value for {', '.join(resolved.missing)}; recording without them",
            file=sys.stderr,
        )
    secrets = [found.reveal() for name in resolved.resolved if (found := resolved.get(name))]
    return resolved, secrets


def record(provider: object, league: str, season: int) -> None:
    """The read set the adapter actually makes, driven once against ESPN."""
    print(f"league        {provider.fetch_league(league, season).name!r}")
    teams = provider.fetch_teams(league, season)
    print(f"teams         {len(teams)}")
    print(f"standings     {len(provider.fetch_standings(league, season))}")
    print(f"matchups w1   {len(provider.fetch_matchups(league, season, 1))}")
    print(f"transactions  {len(provider.fetch_transactions(league, season))}")
    # `since` sweeps every scoring period, which is what puts real transaction
    # rows in the cassette: the canary's *current* period is week 18 of a
    # finished season and has none.
    print(f"transactions* {len(provider.fetch_transactions(league, season, since=EPOCH))} swept")
    print(f"roster        {len(provider.fetch_roster(league, season, teams[0].provider_id))}")
    print(f"roster w1     {len(provider.fetch_roster(league, season, teams[0].provider_id, 1))}")
    print(f"raw mSettings {sorted(provider.fetch_raw(league, season, view='mSettings'))}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from conftest import CASSETTE_LIBRARY_DIR, build_vcr  # noqa: PLC0415 - path set up above

    from fantasy_sports.providers.espn import EspnProvider

    name = cassette_name(args.league, args.season, args.out)
    target = CASSETTE_LIBRARY_DIR / name
    if (args.league, args.season) not in COMMITTABLE and not name.startswith(f"{PRIVATE_DIR}/"):
        print(
            f"refusing to write league {args.league} to {name}: only the public canary "
            f"is committable. Use --out {PRIVATE_DIR}/... (gitignored). See docs/testing.md §6.",
            file=sys.stderr,
        )
        return 2

    credentials: object | None = None
    secrets: list[str] = []
    if args.credentialed:
        credentials, secrets = resolve_cookies()
        print(f"credentials   {len(secrets)} resolved from the auth chain (values not shown)")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with build_vcr().use_cassette(name, record_mode="all", allow_playback_repeats=True):
        record(EspnProvider(credentials), args.league, args.season)

    problems = verify(target, secrets)
    if problems:
        target.unlink(missing_ok=True)
        print("\nREFUSED — the recording was deleted:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"\nwrote {target.relative_to(REPO)} ({target.stat().st_size:,} bytes)")
    print("verified  scan clean, and no credential this run sent is present in the bytes")
    if (args.league, args.season) not in COMMITTABLE:
        print("NOTE      this is a private-league recording; it is gitignored. Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
