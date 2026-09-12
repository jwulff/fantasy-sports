#!/usr/bin/env python3
"""The live ESPN canary — ADR-0005 server side, the detection half.

    uv run python scripts/canary/run.py            # respects the cadence gate
    uv run python scripts/canary/run.py --force     # always runs (workflow_dispatch)
    uv run python scripts/canary/run.py --report-json canary_report.json  # for publish_drift.py

## Scope: what this does and does not do

jwulff/fantasy-sports#11's acceptance criteria originally included auto-filing
a GitHub issue on drift and publishing/updating a `health.json` manifest in
the repo. The 2026-08-28 review comment on #11 deferred that into a follow-up
issue (#64, now built as `scripts/canary/publish_drift.py`) so #11 could ship
detection alone first. This script is still the detection half only — it
does not itself open or update any GitHub issue, and it does not write
anything back to the repo. On drift it makes the failure loud and
unambiguous — a red, clearly-labelled scheduled workflow run, unchanged from
#11 — and, given `--report-json`, hands its structured findings to
`publish_drift.py` (a second, separately-permissioned CI job; see
`.github/workflows/canary.yml`) rather than filing anything itself.

`shape_manifest.json` next to this file is **not** the ADR-0005 §11.2 public
`health.json` — that one carries `latest_version`/`known_issues` for end
users, and is written by `scripts/canary/health_manifest.py` on confirmed
drift. This one is an internal structural fingerprint of the canary league's
response, used only to diff run-to-run drift. See `scripts/canary/README.md`.

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


def _write_report_json(report: CheckReport, path: Path | None) -> None:
    """Write ``report.to_dict()`` to ``--report-json``, if given.

    This is the hand-off jwulff/fantasy-sports#64's two-job workflow needs:
    job 1 (this script, ``permissions: contents: read``) writes the
    structured findings here; job 2 (``scripts/canary/publish_drift.py``,
    ``permissions: contents: write, issues: write``) reads them back with
    :func:`scripts.canary.shapes.report_from_dict` rather than re-fetching
    ESPN or re-parsing :meth:`CheckReport.render_summary`'s prose. A no-op
    when ``path`` is ``None`` -- every existing caller of ``run.py`` that
    never passes ``--report-json`` keeps working unchanged.
    """
    if path is None:
        return
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")


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


def run(*, force: bool, report_path: Path | None = None) -> int:
    today = datetime.now(UTC).date()
    if not force and not should_run_today(today):
        message = (
            f"Offseason and not this week's scheduled day ({today.isoformat()}, "
            f"{today.strftime('%A')}); skipping. Pass --force to run anyway."
        )
        _emit(f"## Canary result: SKIPPED\n{message}")
        # No CheckReport exists for a skip -- nothing for job 2 to act on, and
        # its workflow `if:` gate only ever matches "schema_drift" anyway.
        return 0

    try:
        import espn_api  # noqa: F401 - proves the canary's own build is sound

        from fantasy_sports.core.errors import FantasySportsError
        from fantasy_sports.providers.espn import EspnProvider
    except BaseException as exc:  # noqa: BLE001 - this *is* the classification
        report = _build_error_report(exc)
        _emit(report.render_summary())
        _write_report_json(report, report_path)
        return 1

    try:
        provider = EspnProvider()
        payload = provider.fetch_raw(CANARY_LEAGUE, CANARY_SEASON, view=BOOTSTRAP_VIEWS)
    except FantasySportsError as exc:
        report = _infra_report(exc)
        _emit(report.render_summary())
        _write_report_json(report, report_path)
        return 1
    except Exception as exc:  # noqa: BLE001 - unclassified is infra, never drift (R12)
        report = _infra_report(exc)
        _emit(report.render_summary())
        _write_report_json(report, report_path)
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
    _write_report_json(report, report_path)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the season cadence gate (used for workflow_dispatch)",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=None,
        help=(
            "write the CheckReport as JSON to this path -- the hand-off "
            "scripts/canary/publish_drift.py (job 2) reads (jwulff/fantasy-sports#64)"
        ),
    )
    args = parser.parse_args(argv)
    return run(force=args.force, report_path=args.report_json)


if __name__ == "__main__":
    sys.exit(main())
