"""Offline tests for ``scripts/canary/health_manifest.py`` (jwulff/fantasy-sports#64).

``docs/ARCHITECTURE.md`` §11.2 / ADR-0005 define ``health.json``'s shape;
``fantasy_sports.health.manifest.parse_manifest`` (#10) is the client-side
reader. This suite proves the write side produces something that reader
already understands -- every assertion about ``known_issues[].code`` is
checked against ``fantasy_sports.core.errors.ErrorCode``, not a value typed
twice.
"""

from __future__ import annotations

import json

from fantasy_sports.core.errors import ErrorCode
from fantasy_sports.health.manifest import parse_manifest
from scripts.canary.health_manifest import (
    HEALTH_SCHEMA,
    apply_drift,
    load_health_manifest,
    publish_drift,
    write_health_manifest,
)
from scripts.canary.issue_filer import IssueOutcome
from scripts.canary.shapes import CheckReport, Classification

CHECKED_AT = "2026-09-12T13:00:00Z"
ENDPOINT = "mTeam+mRoster+mMatchup+mSettings+mStandings"


def _drift_report(
    detail: str = "ESPN's response shape no longer matches what it expects.",
) -> CheckReport:
    return CheckReport(
        classification=Classification.SCHEMA_DRIFT,
        missing_paths=["status.finalScoringPeriod"],
        detail=detail,
    )


def _outcome(*, issue_number: int = 42, created: bool = True) -> IssueOutcome:
    return IssueOutcome(
        signature="abc123def456",
        issue_number=issue_number,
        created=created,
        url=f"https://github.com/jwulff/fantasy-sports/issues/{issue_number}",
    )


# --------------------------------------------------------------------------- #
# load_health_manifest
# --------------------------------------------------------------------------- #


def test_load_returns_an_empty_skeleton_when_the_file_does_not_exist(tmp_path):
    manifest = load_health_manifest(tmp_path / "health.json")
    assert manifest["schema"] == HEALTH_SCHEMA
    assert manifest["providers"] == {}


def test_load_tolerates_a_malformed_file(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("not json at all", encoding="utf-8")
    manifest = load_health_manifest(path)
    assert manifest["schema"] == HEALTH_SCHEMA


def test_load_reads_back_what_was_written(tmp_path):
    path = tmp_path / "health.json"
    write_health_manifest(path, {"schema": HEALTH_SCHEMA, "providers": {}})
    assert load_health_manifest(path) == {"schema": HEALTH_SCHEMA, "providers": {}}


# --------------------------------------------------------------------------- #
# apply_drift -- the pure merge
# --------------------------------------------------------------------------- #


def test_apply_drift_on_an_empty_manifest_marks_the_provider_degraded():
    manifest = apply_drift(
        {},
        _drift_report(),
        _outcome(),
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    espn = manifest["providers"]["espn"]
    assert espn["status"] == "degraded"
    assert espn["checked_at"] == CHECKED_AT
    assert len(espn["known_issues"]) == 1
    issue = espn["known_issues"][0]
    assert issue["code"] == ErrorCode.SCHEMA_DRIFT.value
    assert issue["issue"] == 42
    assert issue["url"] == "https://github.com/jwulff/fantasy-sports/issues/42"
    assert issue["endpoint"] == ENDPOINT


def test_apply_drift_refuses_a_non_drift_report():
    import pytest

    with pytest.raises(ValueError):
        apply_drift(
            {},
            CheckReport(classification=Classification.CANARY_INFRA, detail="down"),
            _outcome(),
            provider="espn",
            checked_at=CHECKED_AT,
            endpoint=ENDPOINT,
        )


def test_apply_drift_preserves_release_management_fields_it_does_not_own():
    existing = {
        "schema": HEALTH_SCHEMA,
        "latest_version": "0.1.4",
        "min_supported_version": "0.1.2",
        "yanked_versions": ["0.1.3"],
        "providers": {},
    }
    manifest = apply_drift(
        existing,
        _drift_report(),
        _outcome(),
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    assert manifest["latest_version"] == "0.1.4"
    assert manifest["min_supported_version"] == "0.1.2"
    assert manifest["yanked_versions"] == ["0.1.3"]


def test_apply_drift_never_touches_a_different_providers_entry():
    existing = {
        "providers": {
            "yahoo": {"status": "healthy", "checked_at": "2026-01-01T00:00:00Z", "known_issues": []}
        }
    }
    manifest = apply_drift(
        existing,
        _drift_report(),
        _outcome(),
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    assert manifest["providers"]["yahoo"] == existing["providers"]["yahoo"]
    assert manifest["providers"]["espn"]["status"] == "degraded"


def test_apply_drift_replaces_the_entry_for_the_same_issue_rather_than_duplicating():
    """A recurring run against the same open issue updates that one entry
    (checked_at, summary) instead of growing the list forever."""
    manifest = apply_drift(
        {},
        _drift_report("first wording"),
        _outcome(issue_number=42),
        provider="espn",
        checked_at="2026-09-10T00:00:00Z",
        endpoint=ENDPOINT,
    )
    manifest = apply_drift(
        manifest,
        _drift_report("second wording"),
        _outcome(issue_number=42, created=False),
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    known_issues = manifest["providers"]["espn"]["known_issues"]
    assert len(known_issues) == 1
    assert known_issues[0]["summary"] == "second wording"
    assert manifest["providers"]["espn"]["checked_at"] == CHECKED_AT


def test_apply_drift_appends_a_second_entry_for_a_distinct_issue():
    """Two genuinely different drifts open two different issues; both should
    be visible in known_issues at once."""
    manifest = apply_drift(
        {},
        _drift_report(),
        _outcome(issue_number=42),
        provider="espn",
        checked_at="2026-09-10T00:00:00Z",
        endpoint=ENDPOINT,
    )
    manifest = apply_drift(
        manifest,
        CheckReport(
            classification=Classification.SCHEMA_DRIFT,
            missing_paths=["settings.scoringSettings"],
            detail="a second, distinct drift",
        ),
        _outcome(issue_number=99),
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    known_issues = manifest["providers"]["espn"]["known_issues"]
    assert {issue["issue"] for issue in known_issues} == {42, 99}


def test_apply_drift_sets_the_top_level_updated_at():
    manifest = apply_drift(
        {}, _drift_report(), _outcome(), provider="espn", checked_at=CHECKED_AT, endpoint=ENDPOINT
    )
    assert manifest["updated_at"] == CHECKED_AT


def test_apply_drift_never_populates_affects_or_fixed_in_it_cannot_know():
    """The canary has no release/PyPI knowledge -- see the changes file for
    why these stay null until a human (or the release process) fills them in
    when the fix actually ships."""
    manifest = apply_drift(
        {}, _drift_report(), _outcome(), provider="espn", checked_at=CHECKED_AT, endpoint=ENDPOINT
    )
    issue = manifest["providers"]["espn"]["known_issues"][0]
    assert issue["affects"] is None
    assert issue["fixed_in"] is None


# --------------------------------------------------------------------------- #
# write_health_manifest / publish_drift -- the I/O wrapper
# --------------------------------------------------------------------------- #


def test_write_health_manifest_is_pretty_printed_with_a_trailing_newline(tmp_path):
    path = tmp_path / "health.json"
    write_health_manifest(path, {"schema": HEALTH_SCHEMA})
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n  " in text  # indented, not a single-line dump


def test_publish_drift_writes_a_file_the_client_side_parser_accepts(tmp_path):
    path = tmp_path / "health.json"
    publish_drift(
        _drift_report(),
        _outcome(),
        path=path,
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    parsed = parse_manifest(raw)
    espn = parsed.provider("espn")
    assert espn is not None
    assert espn.status == "degraded"
    assert espn.issues_for(ErrorCode.SCHEMA_DRIFT.value)


def test_publish_drift_reads_the_existing_file_before_merging(tmp_path):
    path = tmp_path / "health.json"
    write_health_manifest(
        path,
        {
            "schema": HEALTH_SCHEMA,
            "latest_version": "0.1.4",
            "providers": {},
        },
    )
    publish_drift(
        _drift_report(),
        _outcome(),
        path=path,
        provider="espn",
        checked_at=CHECKED_AT,
        endpoint=ENDPOINT,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["latest_version"] == "0.1.4"
    assert raw["providers"]["espn"]["status"] == "degraded"
