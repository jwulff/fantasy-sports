"""Offline tests for ``scripts/canary/publish_drift.py`` (jwulff/fantasy-sports#64).

This is job 2 of the two-job canary workflow -- the one holding ``contents:
write``/``issues: write`` -- and it is wired by ``.github/workflows/canary.yml``
to run only when job 1 (``contents: read``) classified ``SCHEMA_DRIFT``. This
suite proves the belt-and-suspenders case AC4 asks for directly: even if the
workflow wiring were ever wrong, a non-drift report reaching this script
still does nothing to GitHub or to ``health.json``.
"""

from __future__ import annotations

import json

import pytest

from scripts.canary import publish_drift
from scripts.canary.shapes import CheckReport, Classification


def _write_report(path, report: CheckReport) -> None:
    path.write_text(json.dumps(report.to_dict()), encoding="utf-8")


class FakeGh:
    def __init__(self, *responses: str) -> None:
        self.calls: list[list[str]] = []
        self._responses = list(responses)

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        return self._responses.pop(0) if self._responses else ""


@pytest.fixture(autouse=True)
def _fake_gh(monkeypatch):
    """Every test in this file gets a fresh fake ``gh`` wired into the
    default runner used by ``main()`` -- ``main`` itself takes no ``runner``
    parameter (a CLI entry point shouldn't need one), so the injection point
    is the module-level default the underlying calls resolve to."""
    fake = FakeGh("[]", "https://github.com/jwulff/fantasy-sports/issues/7\n")
    monkeypatch.setattr("scripts.canary.issue_filer._default_gh_runner", fake)
    return fake


def test_main_does_nothing_for_a_build_error_report(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    health_path = tmp_path / "health.json"
    _write_report(
        report_path, CheckReport(classification=Classification.BUILD_ERROR, detail="boom")
    )

    exit_code = publish_drift.main(
        ["--report-json", str(report_path), "--health-json", str(health_path)]
    )

    assert exit_code == 0
    assert not health_path.exists()


def test_main_does_nothing_for_a_canary_infra_report(tmp_path):
    report_path = tmp_path / "report.json"
    health_path = tmp_path / "health.json"
    _write_report(
        report_path, CheckReport(classification=Classification.CANARY_INFRA, detail="down")
    )

    exit_code = publish_drift.main(
        ["--report-json", str(report_path), "--health-json", str(health_path)]
    )

    assert exit_code == 0
    assert not health_path.exists()


def test_main_does_nothing_for_an_ok_report(tmp_path):
    report_path = tmp_path / "report.json"
    health_path = tmp_path / "health.json"
    _write_report(report_path, CheckReport(classification=Classification.OK, detail="fine"))

    exit_code = publish_drift.main(
        ["--report-json", str(report_path), "--health-json", str(health_path)]
    )

    assert exit_code == 0
    assert not health_path.exists()


def test_main_files_an_issue_and_writes_health_json_on_schema_drift(tmp_path, _fake_gh):
    report_path = tmp_path / "report.json"
    health_path = tmp_path / "health.json"
    _write_report(
        report_path,
        CheckReport(
            classification=Classification.SCHEMA_DRIFT,
            missing_paths=["status.finalScoringPeriod"],
            detail="drift!",
        ),
    )

    exit_code = publish_drift.main(
        ["--report-json", str(report_path), "--health-json", str(health_path)]
    )

    assert exit_code == 0
    assert health_path.exists()
    manifest = json.loads(health_path.read_text(encoding="utf-8"))
    assert manifest["providers"]["espn"]["status"] == "degraded"
    assert manifest["providers"]["espn"]["known_issues"][0]["issue"] == 7

    list_call, create_call = _fake_gh.calls
    assert list_call[:2] == ["issue", "list"]
    assert create_call[:2] == ["issue", "create"]


def test_main_uses_the_repo_flag(tmp_path, _fake_gh):
    report_path = tmp_path / "report.json"
    health_path = tmp_path / "health.json"
    _write_report(
        report_path,
        CheckReport(classification=Classification.SCHEMA_DRIFT, missing_paths=["a.b"], detail="d"),
    )

    publish_drift.main(
        [
            "--report-json",
            str(report_path),
            "--health-json",
            str(health_path),
            "--repo",
            "someone/fork",
        ]
    )

    list_call = _fake_gh.calls[0]
    assert "someone/fork" in list_call
