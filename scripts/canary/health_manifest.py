"""Publishes/updates the ADR-0005 §11.2 public health manifest on confirmed drift.

jwulff/fantasy-sports#64 -- the other half of #11's originally-deferred scope
(see :mod:`scripts.canary.issue_filer` for the issue-filing half). Only ever
invoked for a confirmed :data:`~scripts.canary.shapes.Classification.SCHEMA_DRIFT`
report; see :func:`apply_drift`'s guard and ``scripts/canary/publish_drift.py``,
the job-2 entry point that is the only thing that calls this module.

**Distinct from two other files that sound similar:**

* ``scripts/canary/shape_manifest.json`` -- #11's private structural
  fingerprint, diffed run-to-run to *detect* drift. This module never reads
  or writes it.
* :mod:`fantasy_sports.health.manifest` -- the *client-side* parser (#10)
  that turns the very JSON document this module writes into a
  :class:`~fantasy_sports.health.manifest.HealthManifest`. This module is
  its write-side counterpart, one repo boundary over: nothing here is
  shipped in the wheel (``scripts/`` is dev/CI-only), and nothing there does
  any writing.

## What is deliberately not handled: recovery to OK

This module is wired to run only on ``SCHEMA_DRIFT`` (mirroring the issue
filer). A later canary run that classifies ``OK`` never calls this module at
all, so a provider marked ``"degraded"`` stays that way until a human edits
``health.json`` by hand.

That is a considered decision, not an oversight -- see
``changes/0087-canary-drift-issue-and-health-json.md`` for the full
reasoning. In short: flipping status back to ``"healthy"`` automatically
would require either (a) deciding a fixed shape is a *permanent* fix without
knowing whether a release has actually shipped it, or (b) tying health.json
state to auto-closing the GitHub issue, which #64's acceptance criteria
never asked for and which risks the two falling out of sync in the other
direction (issue open, manifest says healthy). Recovery stays a human action
today; a follow-up issue can revisit it once there is real signal about how
often it matters in practice.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fantasy_sports.core.errors import ErrorCode
from scripts.canary.issue_filer import IssueOutcome
from scripts.canary.shapes import CheckReport, Classification

__all__ = [
    "HEALTH_SCHEMA",
    "MAX_KNOWN_ISSUES_PER_PROVIDER",
    "apply_drift",
    "load_health_manifest",
    "publish_drift",
    "write_health_manifest",
]

HEALTH_SCHEMA = "fantasy-sports-health/v1"

#: The retention cap :func:`apply_drift` enforces on one provider's
#: ``known_issues`` list. Recovery to ``OK`` never prunes an entry (see the
#: module docstring), so this is the only thing standing between one
#: provider and an unboundedly growing list of every distinct drift ever
#: observed. Five is arbitrary but generous -- ADR-0005's whole premise is
#: that a genuine, *distinct* schema break is rare; five simultaneously open
#: ones would itself be a signal something else is wrong.
MAX_KNOWN_ISSUES_PER_PROVIDER = 5


def _skeleton() -> dict[str, Any]:
    return {
        "schema": HEALTH_SCHEMA,
        "latest_version": None,
        "min_supported_version": None,
        "yanked_versions": [],
        "providers": {},
        "updated_at": None,
    }


def load_health_manifest(path: Path) -> dict[str, Any]:
    """Read the committed ``health.json``, or a fresh skeleton if absent or malformed.

    A missing file is the normal state before the first drift ever occurs
    (this repo does not seed a placeholder -- ARCHITECTURE §11.3's client
    side already treats "no manifest" and "manifest says nothing wrong" the
    same way, fail-open). A malformed one -- however it got that way -- must
    not crash the canary either: the next confirmed drift simply starts a
    fresh manifest rather than blocking on repairing the old one by hand.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    return raw if isinstance(raw, dict) else _skeleton()


def write_health_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Pretty-printed, trailing newline -- matches every other committed JSON
    fixture in this repo (``scripts/canary/shape_manifest.json``)."""
    path.write_text(json.dumps(dict(manifest), indent=2) + "\n", encoding="utf-8")


def _known_issue_entry(
    report: CheckReport, outcome: IssueOutcome, *, endpoint: str
) -> dict[str, Any]:
    """One ``known_issues[]`` entry (ARCHITECTURE §11.2's shape).

    ``code`` is :data:`fantasy_sports.core.errors.ErrorCode.SCHEMA_DRIFT`'s
    own value, not :class:`~scripts.canary.shapes.Classification`'s
    (lowercase, this canary's private vocabulary) -- the client side matches
    ``known_issues[].code`` against ``FantasySportsError.code.value``
    (``fantasy_sports.health.manifest.ProviderHealth.issues_for``), so this
    is the one place the two taxonomies must speak the same word.

    ``affects``/``fixed_in`` stay ``None``: the canary has no knowledge of
    PyPI releases or which version, if any, has actually shipped a fix --
    guessing here would be worse than leaving it for a human (or a future
    release process) to fill in when it is actually known, matching how
    ``min_supported_version``/``yanked_versions`` ship unused per ADR-0005's
    own "Consequences" section.
    """
    return {
        "code": ErrorCode.SCHEMA_DRIFT.value,
        "endpoint": endpoint,
        "affects": None,
        "fixed_in": None,
        "issue": outcome.issue_number,
        "url": outcome.url,
        "summary": report.detail or "ESPN's response shape changed.",
    }


def apply_drift(
    manifest: Mapping[str, Any],
    report: CheckReport,
    outcome: IssueOutcome,
    *,
    provider: str,
    checked_at: str,
    endpoint: str,
) -> dict[str, Any]:
    """Pure merge: fold one confirmed-drift run into an existing manifest dict.

    Upserts the ``known_issues`` entry keyed by GitHub issue number -- the
    same identity :func:`~scripts.canary.issue_filer.file_drift_issue`'s
    dedup already ties to this signature, so a recurring run against the
    same open issue *replaces* its entry (fresh ``checked_at``/``summary``)
    instead of appending a duplicate, while a genuinely distinct drift (a
    different open issue) is added alongside it.

    **Ordering: the current run's entry always leads the list.** Every entry
    this canary writes carries the same ``code`` (``SCHEMA_DRIFT`` -- see
    :func:`_known_issue_entry`), so
    ``fantasy_sports.health.manifest.ProviderHealth.issues_for`` cannot
    distinguish them by code, and
    ``fantasy_sports.health.client.build_health_block`` picks ``matches[0]``
    -- whichever entry happens to be *first* -- to surface to a failing
    command. Appending would let the oldest (possibly long-since-irrelevant)
    drift keep winning that pick forever; inserting at index 0 means the
    manifest always points a user at what the canary just observed, not
    whatever was filed first. (Caught in review on #64 -- Codex, PR #91.)

    **Retention: capped at :data:`MAX_KNOWN_ISSUES_PER_PROVIDER`.** Recovery
    to ``OK`` is deliberately not handled by this module (see the module
    docstring), so nothing ever removes a stale entry on its own; without a
    cap, every genuinely distinct drift ever seen would accumulate here
    forever. The oldest entries beyond the cap are dropped -- "oldest" here
    means *not recently reconfirmed*, since the leading position is always
    the just-observed drift and eviction happens from the tail.

    Never touches ``latest_version``/``min_supported_version``/
    ``yanked_versions`` or any other provider's entry -- those belong to the
    release process and to that provider's own canary, respectively, neither
    of which this call is about.

    :raises ValueError: if ``report`` is not a confirmed ``SCHEMA_DRIFT`` --
        this module must never be reachable from a ``BUILD_ERROR``/
        ``CANARY_INFRA``/``OK`` run (jwulff/fantasy-sports#64 AC4).
    """
    if report.classification is not Classification.SCHEMA_DRIFT:
        raise ValueError(
            f"apply_drift called with classification {report.classification.value!r}; "
            "only SCHEMA_DRIFT may update health.json"
        )

    result: dict[str, Any] = {**_skeleton(), **manifest}
    providers = dict(result.get("providers") or {})
    provider_entry = dict(providers.get(provider) or {})
    known_issues = [
        dict(issue) for issue in provider_entry.get("known_issues") or [] if isinstance(issue, dict)
    ]

    new_entry = _known_issue_entry(report, outcome, endpoint=endpoint)
    known_issues = [issue for issue in known_issues if issue.get("issue") != outcome.issue_number]
    known_issues.insert(0, new_entry)
    known_issues = known_issues[:MAX_KNOWN_ISSUES_PER_PROVIDER]

    provider_entry.update(
        {"status": "degraded", "checked_at": checked_at, "known_issues": known_issues}
    )
    providers[provider] = provider_entry
    result["providers"] = providers
    result["updated_at"] = checked_at
    return result


def publish_drift(
    report: CheckReport,
    outcome: IssueOutcome,
    *,
    path: Path,
    provider: str,
    checked_at: str,
    endpoint: str,
) -> dict[str, Any]:
    """Load, merge, and write ``health.json`` in one call -- what
    ``scripts/canary/publish_drift.py`` (job 2) actually calls."""
    existing = load_health_manifest(path)
    updated = apply_drift(
        existing, report, outcome, provider=provider, checked_at=checked_at, endpoint=endpoint
    )
    write_health_manifest(path, updated)
    return updated
