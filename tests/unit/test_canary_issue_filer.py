"""Offline tests for ``scripts/canary/issue_filer.py`` (jwulff/fantasy-sports#64).

Everything here injects a fake ``runner`` in place of the real ``gh`` CLI
subprocess call. Never exercises the default runner, network, or a real
GitHub token -- ``docs/research/01-telemetry-auto-issues.md`` §4's
server-side dedup strategy (search-before-file, via ``gh``) is what this
suite proves, not ``gh`` itself.
"""

from __future__ import annotations

import pytest

from fantasy_sports.core.errors import ErrorCode
from scripts.canary.issue_filer import (
    AUTO_ERROR_LABEL,
    IssueOutcome,
    drift_signature,
    file_drift_issue,
    find_open_issue,
    render_issue_body,
)
from scripts.canary.shapes import CheckReport, Classification, EnumGap, SignatureDiff


def _drift_report(**overrides) -> CheckReport:
    defaults = {
        "classification": Classification.SCHEMA_DRIFT,
        "missing_paths": ["status.finalScoringPeriod"],
        "detail": "ESPN's response shape no longer matches what providers/espn.py expects.",
    }
    defaults.update(overrides)
    return CheckReport(**defaults)


class FakeGh:
    """Records every ``gh`` invocation and answers with a scripted queue."""

    def __init__(self, *responses: str) -> None:
        self.calls: list[list[str]] = []
        self._responses = list(responses)

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        return self._responses.pop(0) if self._responses else ""


# --------------------------------------------------------------------------- #
# drift_signature
# --------------------------------------------------------------------------- #


def test_signature_is_stable_for_identical_reports():
    assert drift_signature(_drift_report()) == drift_signature(_drift_report())


def test_signature_changes_when_the_missing_paths_differ():
    a = drift_signature(_drift_report(missing_paths=["status.finalScoringPeriod"]))
    b = drift_signature(_drift_report(missing_paths=["settings.scoringSettings"]))
    assert a != b


def test_signature_is_independent_of_detail_prose():
    """The prose (``detail``) can be reworded without minting a new issue for
    the same underlying drift -- only the shape diff is signal."""
    a = drift_signature(_drift_report(detail="first wording"))
    b = drift_signature(_drift_report(detail="a totally different sentence"))
    assert a == b


def test_signature_ignores_which_player_happened_to_carry_an_enum_gap():
    """Roster membership changes weekly; the *id* that has no mapping is the
    signal, not which player's name it was seen on this particular run."""
    a = drift_signature(
        _drift_report(
            missing_paths=[],
            enum_gaps=[EnumGap(field="defaultPositionId", value=99, context="Player A")],
        )
    )
    b = drift_signature(
        _drift_report(
            missing_paths=[],
            enum_gaps=[EnumGap(field="defaultPositionId", value=99, context="Player B")],
        )
    )
    assert a == b


def test_signature_changes_when_the_enum_gap_value_differs():
    a = drift_signature(
        _drift_report(
            missing_paths=[],
            enum_gaps=[EnumGap(field="defaultPositionId", value=99, context="Player A")],
        )
    )
    b = drift_signature(
        _drift_report(
            missing_paths=[],
            enum_gaps=[EnumGap(field="defaultPositionId", value=100, context="Player A")],
        )
    )
    assert a != b


def test_signature_reflects_removed_signature_keys():
    a = drift_signature(
        _drift_report(
            missing_paths=[],
            signature_diff=SignatureDiff(added={}, removed={"status": ["finalScoringPeriod"]}),
        )
    )
    b = drift_signature(
        _drift_report(
            missing_paths=[],
            signature_diff=SignatureDiff(added={}, removed={"status": ["somethingElse"]}),
        )
    )
    assert a != b


def test_signature_ignores_purely_additive_signature_diffs():
    """Added keys never fail the run (see ``shapes.classify``); they should
    not be able to mint a new signature on their own either."""
    a = drift_signature(
        _drift_report(
            missing_paths=[], signature_diff=SignatureDiff(added={"status": ["x"]}, removed={})
        )
    )
    b = drift_signature(
        _drift_report(
            missing_paths=[], signature_diff=SignatureDiff(added={"status": ["y"]}, removed={})
        )
    )
    assert a == b


def test_signature_is_a_short_hex_string():
    signature = drift_signature(_drift_report())
    assert len(signature) == 12
    int(signature, 16)  # does not raise


# --------------------------------------------------------------------------- #
# render_issue_body
# --------------------------------------------------------------------------- #


def test_issue_body_carries_a_machine_readable_signature_marker():
    report = _drift_report()
    signature = drift_signature(report)
    body = render_issue_body(report, signature=signature, league="1234", season=2018)
    assert f"<!-- canary-signature:{signature} -->" in body
    assert "status.finalScoringPeriod" in body
    assert "1234" in body and "2018" in str(body)


def test_issue_body_never_calls_the_report_classification_anything_but_schema_drift_worthy():
    report = _drift_report()
    signature = drift_signature(report)
    body = render_issue_body(report, signature=signature, league="1234", season=2018)
    assert "🤖" in body


# --------------------------------------------------------------------------- #
# find_open_issue
# --------------------------------------------------------------------------- #


def test_find_open_issue_returns_none_when_gh_finds_nothing():
    gh = FakeGh("[]")
    assert find_open_issue("abc123def456", repo="jwulff/fantasy-sports", runner=gh) is None
    assert gh.calls[0][:2] == ["issue", "list"]
    assert "--repo" in gh.calls[0] and "jwulff/fantasy-sports" in gh.calls[0]
    assert AUTO_ERROR_LABEL in gh.calls[0]


def test_find_open_issue_returns_the_issue_number_on_a_hit():
    gh = FakeGh('[{"number": 42}]')
    assert find_open_issue("abc123def456", repo="jwulff/fantasy-sports", runner=gh) == 42


def test_find_open_issue_tolerates_malformed_gh_output():
    gh = FakeGh("not json")
    assert find_open_issue("abc123def456", repo="jwulff/fantasy-sports", runner=gh) is None


def test_find_open_issue_picks_the_lowest_number_on_multiple_hits():
    gh = FakeGh('[{"number": 99}, {"number": 7}]')
    assert find_open_issue("abc123def456", repo="jwulff/fantasy-sports", runner=gh) == 7


# --------------------------------------------------------------------------- #
# file_drift_issue -- the orchestration
# --------------------------------------------------------------------------- #


def test_file_drift_issue_creates_a_new_issue_when_none_is_open():
    gh = FakeGh("[]", "https://github.com/jwulff/fantasy-sports/issues/123\n")
    report = _drift_report()
    outcome = file_drift_issue(
        report, repo="jwulff/fantasy-sports", league="1234", season=2018, runner=gh
    )
    assert outcome == IssueOutcome(
        signature=drift_signature(report),
        issue_number=123,
        created=True,
        url="https://github.com/jwulff/fantasy-sports/issues/123",
    )
    list_call, create_call = gh.calls
    assert list_call[:2] == ["issue", "list"]
    assert create_call[:2] == ["issue", "create"]
    assert AUTO_ERROR_LABEL in create_call


def test_file_drift_issue_comments_on_an_existing_open_issue_instead_of_filing_again():
    gh = FakeGh('[{"number": 55}]', "")
    report = _drift_report()
    outcome = file_drift_issue(
        report, repo="jwulff/fantasy-sports", league="1234", season=2018, runner=gh
    )
    assert outcome.created is False
    assert outcome.issue_number == 55
    list_call, comment_call = gh.calls
    assert comment_call[:2] == ["issue", "comment"]
    assert "55" in comment_call


def test_file_drift_issue_refuses_a_non_drift_report():
    report = CheckReport(classification=Classification.BUILD_ERROR, detail="boom")
    gh = FakeGh()
    with pytest.raises(ValueError):
        file_drift_issue(
            report, repo="jwulff/fantasy-sports", league="1234", season=2018, runner=gh
        )
    assert gh.calls == []


def test_the_known_issue_code_matches_the_client_side_error_taxonomy():
    """``fantasy_sports.health.client.ProviderHealth.issues_for`` matches a
    known issue's ``code`` against ``FantasySportsError.code.value`` -- the
    canary's own :class:`Classification` enum uses a different (lowercase)
    vocabulary, so this is the one place the two must be reconciled."""
    from scripts.canary import issue_filer

    assert issue_filer.KNOWN_ISSUE_CODE == ErrorCode.SCHEMA_DRIFT.value == "SCHEMA_DRIFT"
