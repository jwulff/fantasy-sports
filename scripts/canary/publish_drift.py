#!/usr/bin/env python3
"""Job 2 of the two-job canary workflow -- the write half (jwulff/fantasy-sports#64).

    uv run python scripts/canary/publish_drift.py --report-json <path>

``scripts/canary/run.py`` (job 1, ``permissions: contents: read``) classifies
a live payload and, given ``--report-json``, writes its :class:`CheckReport`
as JSON. ``.github/workflows/canary.yml`` wires this script (job 2,
``permissions: contents: write, issues: write``) to run only when that
report's classification was ``SCHEMA_DRIFT`` -- so this is the *only* code
path in the whole workflow that ever files/updates a GitHub issue or writes
``health.json``, and the only job that ever holds the token scope to do it.

Splitting the write path into its own job -- rather than giving job 1
elevated permissions and having it call
:mod:`scripts.canary.issue_filer`/:mod:`scripts.canary.health_manifest`
directly -- is the AC5 requirement made structurally true instead of merely
documented: job 1's ``GITHUB_TOKEN`` cannot file an issue or push a commit no
matter what this repo's Python does, because GitHub Actions enforces the
`permissions:` block at the token level, not at the call site.

Refuses (no-ops, exit 0) for any classification other than ``SCHEMA_DRIFT``
even though the workflow already gates on it -- belt and suspenders for
jwulff/fantasy-sports#64's AC4: a wiring mistake in the workflow must not be
the only thing standing between ``BUILD_ERROR``/``CANARY_INFRA`` noise and a
filed issue.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from scripts.canary import issue_filer  # noqa: E402 - see sys.path setup above
from scripts.canary.health_manifest import publish_drift  # noqa: E402
from scripts.canary.run import BOOTSTRAP_VIEWS, CANARY_LEAGUE, CANARY_SEASON  # noqa: E402
from scripts.canary.shapes import Classification, report_from_dict  # noqa: E402

DEFAULT_HEALTH_JSON_PATH = REPO_ROOT / "health.json"

#: The endpoint recorded on a filed issue and in health.json's known_issues
#: entry: the one combined view request run.py actually made (see its own
#: BOOTSTRAP_VIEWS docstring for why it's one request, not five).
BOOTSTRAP_ENDPOINT = "+".join(BOOTSTRAP_VIEWS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-json",
        type=Path,
        required=True,
        help="the CheckReport JSON run.py wrote via its own --report-json",
    )
    parser.add_argument(
        "--health-json",
        type=Path,
        default=DEFAULT_HEALTH_JSON_PATH,
        help="where to write the ADR-0005 §11.2 public manifest (default: repo root)",
    )
    parser.add_argument(
        "--repo",
        default=issue_filer.DEFAULT_REPO,
        help="owner/repo to file the auto-error issue against",
    )
    args = parser.parse_args(argv)

    report = report_from_dict(json.loads(args.report_json.read_text(encoding="utf-8")))
    if report.classification is not Classification.SCHEMA_DRIFT:
        print(
            f"Classification is {report.classification.value}, not SCHEMA_DRIFT; "
            "nothing to file or publish."
        )
        return 0

    outcome = issue_filer.file_drift_issue(
        report, repo=args.repo, league=CANARY_LEAGUE, season=CANARY_SEASON
    )

    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    publish_drift(
        report,
        outcome,
        path=args.health_json,
        provider="espn",
        checked_at=checked_at,
        endpoint=BOOTSTRAP_ENDPOINT,
    )

    verb = "Filed" if outcome.created else "Updated"
    print(f"{verb} issue #{outcome.issue_number}: {outcome.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
