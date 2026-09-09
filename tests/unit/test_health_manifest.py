"""Parsing ``health.json`` (ADR-0005 §11.2), defensively.

A client on an old release may have to read a manifest written by a newer
canary. Every field here is optional except the document shape itself, so the
tests below are mostly about what survives being *absent* or *wrong-shaped*
rather than what a well-formed document produces.
"""

from __future__ import annotations

import pytest

from fantasy_sports.health.manifest import (
    HealthManifest,
    KnownIssue,
    ManifestError,
    ProviderHealth,
    parse_manifest,
)

FULL_MANIFEST = {
    "schema": "fantasy-sports-health/v1",
    "latest_version": "0.1.4",
    "min_supported_version": "0.1.2",
    "yanked_versions": ["0.1.3"],
    "providers": {
        "espn": {
            "status": "degraded",
            "checked_at": "2026-09-03T06:00:00Z",
            "known_issues": [
                {
                    "code": "SCHEMA_DRIFT",
                    "endpoint": "mRoster",
                    "affects": "<0.1.4",
                    "fixed_in": "0.1.4",
                    "issue": 42,
                    "url": "https://github.com/jwulff/fantasy-sports/issues/42",
                    "summary": "ESPN changed mRoster player-entry shape on 2026-09-03",
                }
            ],
        }
    },
    "updated_at": "2026-09-03T06:00:00Z",
}


def test_a_well_formed_manifest_parses_in_full():
    manifest = parse_manifest(FULL_MANIFEST)
    assert manifest == HealthManifest(
        schema="fantasy-sports-health/v1",
        latest_version="0.1.4",
        min_supported_version="0.1.2",
        yanked_versions=("0.1.3",),
        providers={
            "espn": ProviderHealth(
                name="espn",
                status="degraded",
                checked_at="2026-09-03T06:00:00Z",
                known_issues=(
                    KnownIssue(
                        code="SCHEMA_DRIFT",
                        endpoint="mRoster",
                        affects="<0.1.4",
                        fixed_in="0.1.4",
                        issue=42,
                        url="https://github.com/jwulff/fantasy-sports/issues/42",
                        summary="ESPN changed mRoster player-entry shape on 2026-09-03",
                    ),
                ),
            )
        },
        updated_at="2026-09-03T06:00:00Z",
    )


def test_an_empty_document_parses_to_all_defaults():
    manifest = parse_manifest({})
    assert manifest == HealthManifest()
    assert manifest.providers == {}
    assert manifest.yanked_versions == ()


def test_something_that_is_not_an_object_is_refused():
    with pytest.raises(ManifestError):
        parse_manifest(["not", "an", "object"])
    with pytest.raises(ManifestError):
        parse_manifest("also not an object")
    with pytest.raises(ManifestError):
        parse_manifest(None)


@pytest.mark.parametrize(
    "raw",
    [
        {"providers": "not a mapping"},
        {"providers": {"espn": "not a mapping"}},
        {"providers": {"": {"status": "healthy"}}},
        {"providers": {123: {"status": "healthy"}}},
    ],
)
def test_a_malformed_providers_block_is_dropped_not_raised(raw):
    manifest = parse_manifest(raw)
    assert manifest.providers == {}


def test_a_malformed_known_issue_entry_is_dropped_the_rest_survive():
    raw = {
        "providers": {
            "espn": {
                "status": "healthy",
                "known_issues": ["not a mapping", {"summary": "a real one", "issue": 7}],
            }
        }
    }
    manifest = parse_manifest(raw)
    issues = manifest.providers["espn"].known_issues
    assert len(issues) == 1
    assert issues[0].summary == "a real one"
    assert issues[0].issue == 7


def test_yanked_versions_that_is_not_a_list_becomes_empty_not_raised():
    assert parse_manifest({"yanked_versions": "0.1.3"}).yanked_versions == ()
    assert parse_manifest({"yanked_versions": {"a": 1}}).yanked_versions == ()
    assert parse_manifest({"yanked_versions": [1, "0.1.3", None]}).yanked_versions == ("0.1.3",)


def test_a_bool_issue_number_is_not_treated_as_an_int():
    """``bool`` is an ``int`` in Python; an issue number of ``True`` is nonsense."""
    raw = {"providers": {"espn": {"known_issues": [{"issue": True, "summary": "x"}]}}}
    issue = parse_manifest(raw).providers["espn"].known_issues[0]
    assert issue.issue is None


def test_provider_returns_none_for_an_unconfigured_or_null_name():
    manifest = parse_manifest(FULL_MANIFEST)
    assert manifest.provider(None) is None
    assert manifest.provider("sleeper") is None
    assert manifest.provider("espn") is not None


def test_issues_for_matches_by_code_only():
    provider = parse_manifest(FULL_MANIFEST).providers["espn"]
    assert len(provider.issues_for("SCHEMA_DRIFT")) == 1
    assert provider.issues_for("PROVIDER_UNAVAILABLE") == ()


def test_a_known_issue_with_no_code_never_matches_any_lookup():
    provider = ProviderHealth(
        name="espn", known_issues=(KnownIssue(code=None, summary="no code at all"),)
    )
    assert provider.issues_for("SCHEMA_DRIFT") == ()
    assert provider.issues_for("") == ()


def test_to_health_payload_carries_exactly_the_four_documented_keys():
    issue = KnownIssue(
        code="SCHEMA_DRIFT",
        endpoint="mRoster",
        affects="<0.1.4",
        fixed_in="0.1.4",
        issue=42,
        url="https://github.com/jwulff/fantasy-sports/issues/42",
        summary="ESPN changed mRoster player-entry shape",
    )
    assert issue.to_health_payload() == {
        "issue": 42,
        "url": "https://github.com/jwulff/fantasy-sports/issues/42",
        "fixed_in": "0.1.4",
        "summary": "ESPN changed mRoster player-entry shape",
    }
