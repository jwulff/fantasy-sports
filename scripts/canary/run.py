#!/usr/bin/env python3
"""The live ESPN canary — ADR-0005 server side, detection only.

    uv run python scripts/canary/run.py            # respects the cadence gate
    uv run python scripts/canary/run.py --force     # always runs (workflow_dispatch)

## Scope: what this does and does not do

jwulff/fantasy-sports#11's acceptance criteria originally included auto-filing
a GitHub issue on drift and publishing/updating a `health.json` manifest in
the repo. The 2026-08-28 review comment on #11 reclassified that:
**detection is core scope; the public-facing health manifest and auto-filed
issues stay deferred**, split into a follow-up issue. This script is the
detection half only. On drift it makes the failure loud and unambiguous —
a red, clearly-labelled scheduled workflow run — which is what stops
`SCHEMA_DRIFT` firing client-side "with nothing behind it." It does not open
or update any GitHub issue, and it does not write anything back to the repo.

`shape_manifest.json` next to this file is **not** the ADR-0005 §11.2 public
`health.json` — that one carries `latest_version`/`known_issues` for end
users and stays unbuilt until the follow-up issue. This one is an internal
structural fingerprint of the canary league's response, used only to diff
run-to-run drift. See `scripts/canary/README.md`.

## Classification

Three-way, and the *why* is `docs/research/03-espn-api-surface.md` §4.2: a
live-league smoke test can go red for reasons that have nothing to do with
ESPN — the espn-api project's own canary went red for 12 consecutive days
from a transitive `idna` release breaking *import*, not an ESPN change.

1. **BUILD_ERROR** — an exception before any HTTP request was attempted:
   an import failure, a dependency-resolution break. `uv sync --locked` in
   the workflow (and here) is the primary defense: it pins as tightly as the
   runtime and refuses to silently re-resolve a newer `idna`. This is what
   the 2026-08 incident would have looked like if it happened to us.
2. **CANARY_INFRA** — a request was attempted and failed before a parseable
   JSON payload came back: connection error, 401/404/429, or anything our
   own error taxonomy maps to something other than success. Not a shape
   problem — ESPN (or the network) is unreachable, not wrong-shaped.
3. **SCHEMA_DRIFT** — a real payload came back and `scripts/canary/shapes.py`
   found something wrong with it. This is the only classification that
   means "ESPN changed something."

Only #3 is actual evidence of drift. #1 and #2 are still failures worth a
human's attention (this workflow run goes red either way — GitHub's own
failure notification is the signal), but the run's summary makes which one
happened impossible to miss.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.canary.shapes import CheckReport

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = Path(__file__).resolve().parent / "shape_manifest.json"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

CANARY_LEAGUE = "1234"
CANARY_SEASON = 2018

#: The exact combined `view=` list `espn-api`'s own `get_league()` sends
#: (`espn_api/requests/espn_requests.py`), reused verbatim rather than
#: invented — docs/research/03-espn-api-surface.md §7.7: "two views combined
#: != two views called separately." This is the same request
#: `EspnProvider._bootstrap_payload` reads for `fetch_league`/`fetch_teams`/
#: `fetch_standings`/`fetch_roster`, so it is the one call that matters most.
BOOTSTRAP_VIEWS = ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]

#: NFL fantasy runs roughly September through the first week or two of
#: January. A heuristic, not an ESPN-published calendar — see
#: scripts/canary/README.md for why a fixed range was chosen over trying to
#: derive it from ESPN's own `status` payload (which would require a
#: successful request just to decide whether to run one).
IN_SEASON_MONTHS = frozenset({9, 10, 11, 12})
OFFSEASON_WEEKLY_WEEKDAY = 0  # Monday


def in_season(today: date) -> bool:
    return today.month in IN_SEASON_MONTHS or (today.month == 1 and today.day <= 13)


def should_run_today(today: date) -> bool:
    """ADR-0005: daily in season, weekly in the offseason."""
    return in_season(today) or today.weekday() == OFFSEASON_WEEKLY_WEEKDAY


def _write_summary(text: str) -> None:
    """Append to the GitHub Actions job summary, if running in a workflow."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _emit(report_summary: str) -> None:
    print(report_summary)
    _write_summary(report_summary)


def _build_error_report(exc: BaseException) -> CheckReport:
    from scripts.canary.shapes import CheckReport, Classification

    return CheckReport(
        classification=Classification.BUILD_ERROR,
        detail=(
            f"The canary's own environment failed before any ESPN request was "
            f"attempted ({type(exc).__name__}: {exc}). This is NOT evidence of "
            "ESPN drift — see docs/research/03-espn-api-surface.md §4.2, the "
            "idna/espn-api incident this classification exists to catch."
        ),
    )


def _infra_report(exc: BaseException) -> CheckReport:
    from scripts.canary.shapes import CheckReport, Classification

    return CheckReport(
        classification=Classification.CANARY_INFRA,
        detail=(
            f"ESPN or the network was unreachable, or rejected the request "
            f"({type(exc).__name__}: {exc}). This is not a shape-assertion "
            "failure — no payload was ever obtained to inspect."
        ),
    )


def run(*, force: bool) -> int:
    today = datetime.now(UTC).date()
    if not force and not should_run_today(today):
        message = (
            f"Offseason and not this week's scheduled day ({today.isoformat()}, "
            f"{today.strftime('%A')}); skipping. Pass --force to run anyway."
        )
        _emit(f"## Canary result: SKIPPED\n{message}")
        return 0

    try:
        import espn_api  # noqa: F401 - proves the canary's own build is sound

        from fantasy_sports.core.errors import FantasySportsError
        from fantasy_sports.providers.espn import EspnProvider
    except BaseException as exc:  # noqa: BLE001 - this *is* the classification
        _emit(_build_error_report(exc).render_summary())
        return 1

    try:
        provider = EspnProvider()
        payload = provider.fetch_raw(CANARY_LEAGUE, CANARY_SEASON, view=BOOTSTRAP_VIEWS)
    except FantasySportsError as exc:
        _emit(_infra_report(exc).render_summary())
        return 1
    except Exception as exc:  # noqa: BLE001 - unclassified is infra, never drift (R12)
        _emit(_infra_report(exc).render_summary())
        return 1

    from espn_api.football.constant import POSITION_MAP, PRO_TEAM_MAP

    from scripts.canary.shapes import classify

    manifest = json.loads(MANIFEST_PATH.read_text())
    report = classify(
        payload,
        manifest_signature=manifest["signature"],
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    _emit(report.render_summary())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the season cadence gate (used for workflow_dispatch)",
    )
    args = parser.parse_args(argv)
    return run(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
