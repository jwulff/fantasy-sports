"""Auto-files (and dedupes) a GitHub issue for a confirmed ``SCHEMA_DRIFT`` run.

ADR-0005 server side, jwulff/fantasy-sports#64 -- the half of #11 the
2026-08-28 review comment deferred. Only ever called for
:data:`~scripts.canary.shapes.Classification.SCHEMA_DRIFT` (:func:`run.py`
never invokes this for ``BUILD_ERROR``/``CANARY_INFRA``, and
:func:`file_drift_issue` refuses any other classification defensively). See
``scripts/canary/README.md`` and ``.github/workflows/canary.yml`` for how the
two-job workflow keeps the write scope this module needs off the detection
job.

## Dedup strategy

``docs/research/01-telemetry-auto-issues.md`` §4 analyzed this exact
"don't file a hundred issues for one outage" problem for the *client-side*
telemetry proposal and it applies unchanged here: fingerprint the drift from
its own content (never a timestamp), then search GitHub for an open
``auto-error`` issue that already carries that fingerprint before creating
one -- reusing the user's own ``gh`` rather than a project-held token
(§4 point 4). A hit gets a comment instead of a second issue.

Nothing here talks to the network directly; every function that shells out
to ``gh`` takes a ``runner`` (default: a real ``gh`` subprocess call) so
tests can inject a fake and never risk a live call — ``pytest-socket``
wouldn't even catch a real ``gh``, since it exec's a subprocess rather than
opening a socket itself.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from fantasy_sports.core.errors import ErrorCode
from scripts.canary.shapes import CheckReport, Classification

__all__ = [
    "AUTO_ERROR_LABEL",
    "DEFAULT_REPO",
    "KNOWN_ISSUE_CODE",
    "GhRunner",
    "IssueOutcome",
    "drift_signature",
    "file_drift_issue",
    "find_open_issue",
    "render_comment_body",
    "render_issue_body",
    "render_issue_title",
]

DEFAULT_REPO = "jwulff/fantasy-sports"

#: CLAUDE.md: "`auto-error` is reserved for issues filed automatically by the
#: canary or client telemetry."
AUTO_ERROR_LABEL = "auto-error"

#: The vocabulary ``health.json``'s ``known_issues[].code`` and the client's
#: error taxonomy (``fantasy_sports.core.errors.ErrorCode``) share --
#: deliberately *not* :class:`~scripts.canary.shapes.Classification`'s own
#: lowercase value, which is this canary's private vocabulary. Imported from
#: the client taxonomy rather than hardcoded so the two can never drift apart
#: silently.
KNOWN_ISSUE_CODE = ErrorCode.SCHEMA_DRIFT.value

#: What every ``runner=`` parameter below accepts: the ``gh`` argv (without
#: the leading ``"gh"`` itself) in, stdout out. Tests inject a fake; nothing
#: in this module's own logic ever calls the real one.
GhRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class IssueOutcome:
    """What happened when :func:`file_drift_issue` ran, for the health.json publish step."""

    signature: str
    issue_number: int
    created: bool
    url: str


# --------------------------------------------------------------------------- #
# The fingerprint -- content, never timestamps
# --------------------------------------------------------------------------- #


def drift_signature(report: CheckReport) -> str:
    """A stable fingerprint of *what* drifted, independent of *when* or *why worded how*.

    Adapted from the fingerprint formula
    ``docs/research/01-telemetry-auto-issues.md`` §4.2 designed for client
    telemetry (``sha256(f"{error_code}:{provider}:{endpoint}:{shape_summary}")``):
    this canary only ever hits one provider (``espn``) via one combined
    endpoint (the bootstrap view), so the only real variable is the shape
    diff itself.

    Two things are deliberately excluded from ``shape_summary`` so a
    fingerprint means "the same underlying break", not "the same run":

    * :attr:`CheckReport.detail` — prose, can be reworded without minting a
      new issue for the same drift.
    * :class:`~scripts.canary.shapes.EnumGap.context` — *which player*
      happened to carry an unmapped id. Roster membership turns over weekly;
      the id with no mapping is the signal, not the name it was seen on this
      particular run.

    Added signature keys are excluded too (mirroring
    ``shapes.classify``'s own rule that additions alone never fail a run) --
    only ``missing_paths``, the ``(field, value)`` pairs from ``enum_gaps``,
    and ``signature_diff.removed`` are load-bearing.
    """
    shape_summary = {
        "missing_paths": sorted(report.missing_paths),
        "enum_gaps": sorted({(gap.field, gap.value) for gap in report.enum_gaps}),
        "removed_keys": {
            path: sorted(keys)
            for path, keys in sorted(
                (report.signature_diff.removed if report.signature_diff else {}).items()
            )
        },
    }
    blob = f"{KNOWN_ISSUE_CODE}:espn:bootstrap:{json.dumps(shape_summary, sort_keys=True)}"
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_issue_title(signature: str) -> str:
    return f"ESPN response shape drift ({signature})"


def render_issue_body(report: CheckReport, *, signature: str, league: str, season: int) -> str:
    """The body for a freshly filed issue. Carries the fingerprint marker
    :func:`find_open_issue` searches for on every later run."""
    lines = [
        "🤖 Auto-filed by the ESPN canary (`scripts/canary/run.py`) on confirmed "
        "`SCHEMA_DRIFT` — jwulff/fantasy-sports#64.",
        "",
        report.render_summary(),
        "",
        "---",
        f"- League/season checked: `{league}`/`{season}`",
        f"- Signature: `{signature}`",
        "",
        f"<!-- canary-signature:{signature} -->",
    ]
    return "\n".join(lines)


def render_comment_body(report: CheckReport, *, signature: str, today: date | None = None) -> str:
    """Posted to an already-open issue instead of filing a second one."""
    when = (today or date.today()).isoformat()
    lines = [
        f"🤖 The canary saw the same drift again on {when} (signature `{signature}`).",
        "",
        report.render_summary(),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# ``gh`` plumbing
# --------------------------------------------------------------------------- #


def _default_gh_runner(args: Sequence[str]) -> str:
    """The real ``gh`` invocation. Never used by a test -- see the module docstring."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return result.stdout


def find_open_issue(
    signature: str,
    *,
    repo: str,
    runner: GhRunner | None = None,
) -> int | None:
    """The open ``auto-error`` issue already carrying this signature, if any.

    Mirrors ``docs/research/01-telemetry-auto-issues.md`` §4 point 4's
    server-side dedup: ``gh issue list --search "<signature> in:body"``,
    scoped to the ``auto-error`` label and open state, invoked as the
    workflow's own ``gh`` rather than a project-held HTTP token.

    ``runner`` defaults to ``None`` and resolves to :func:`_default_gh_runner`
    *inside* the function body rather than as the parameter's own default --
    a default value is bound once at import time, which would make a test's
    ``monkeypatch.setattr(issue_filer, "_default_gh_runner", fake)`` silently
    no-op for every caller that didn't pass ``runner=`` explicitly (exactly
    the case ``scripts/canary/publish_drift.py`` is in).
    """
    runner = runner or _default_gh_runner
    output = runner(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            AUTO_ERROR_LABEL,
            "--state",
            "open",
            "--search",
            f"{signature} in:body",
            "--json",
            "number",
        ]
    )
    try:
        rows = json.loads(output)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    numbers = [
        row["number"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]
    return min(numbers) if numbers else None


def _parse_issue_number(create_output: str) -> int:
    """``gh issue create`` prints the new issue's URL to stdout on success."""
    url = create_output.strip().splitlines()[-1] if create_output.strip() else ""
    trailing = url.rstrip("/").rsplit("/", 1)[-1]
    if not trailing.isdigit():
        raise RuntimeError(
            f"could not parse an issue number from `gh issue create` output: {create_output!r}"
        )
    return int(trailing)


def file_drift_issue(
    report: CheckReport,
    *,
    repo: str = DEFAULT_REPO,
    league: str,
    season: int,
    runner: GhRunner | None = None,
) -> IssueOutcome:
    """File a new ``auto-error`` issue for this drift, or update the existing one.

    :raises ValueError: if ``report`` is not a confirmed ``SCHEMA_DRIFT`` --
        the whole reason #11 built the three-way classification first is so
        ``BUILD_ERROR``/``CANARY_INFRA`` noise never reaches this function.

    See :func:`find_open_issue` for why ``runner`` resolves to
    :func:`_default_gh_runner` here rather than as the parameter default.
    """
    if report.classification is not Classification.SCHEMA_DRIFT:
        raise ValueError(
            f"file_drift_issue called with classification {report.classification.value!r}; "
            "only SCHEMA_DRIFT may file or update an issue"
        )

    runner = runner or _default_gh_runner
    signature = drift_signature(report)
    existing = find_open_issue(signature, repo=repo, runner=runner)
    if existing is not None:
        runner(
            [
                "issue",
                "comment",
                str(existing),
                "--repo",
                repo,
                "--body",
                render_comment_body(report, signature=signature),
            ]
        )
        return IssueOutcome(
            signature=signature,
            issue_number=existing,
            created=False,
            url=f"https://github.com/{repo}/issues/{existing}",
        )

    output = runner(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            render_issue_title(signature),
            "--body",
            render_issue_body(report, signature=signature, league=league, season=season),
            "--label",
            AUTO_ERROR_LABEL,
        ]
    )
    number = _parse_issue_number(output)
    return IssueOutcome(signature=signature, issue_number=number, created=True, url=output.strip())
