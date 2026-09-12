"""Offline tests for ``scripts/canary/run.py``'s ``--report-json`` output
(jwulff/fantasy-sports#64).

The two-job canary workflow hands the detection job's findings to the
publish job as a file rather than a re-fetch; this proves ``run()``/``main()``
actually write it, in the shape ``report_from_dict`` reads back, for every
branch that produces a :class:`CheckReport` -- and that a caller who never
passes ``--report-json`` (every existing invocation) sees no change at all.

Never mocks ``gh`` or touches the network: :mod:`fantasy_sports.providers.espn`
is monkeypatched with an in-memory fake ESPN payload, the same "detect
without a live call" approach ``tests/unit/test_canary_shapes.py`` already
uses for the pure classification logic.
"""

from __future__ import annotations

import json

import fantasy_sports.providers.espn as espn_module
from scripts.canary import run as run_module
from scripts.canary.shapes import Classification, report_from_dict


def _player(*, player_id: int) -> dict:
    return {
        "playerPoolEntry": {
            "player": {
                "id": player_id,
                "fullName": f"Player {player_id}",
                "defaultPositionId": 0,
                "eligibleSlots": [0],
                "proTeamId": 1,
            }
        }
    }


def _team(*, team_id: int) -> dict:
    return {
        "id": team_id,
        "record": {
            "overall": {"wins": 1, "losses": 1, "ties": 0, "pointsFor": 1.0, "pointsAgainst": 1.0}
        },
        "roster": {"entries": [_player(player_id=100 + team_id)]},
    }


def _conforming_payload() -> dict:
    return {
        "status": {"currentMatchupPeriod": 1, "finalScoringPeriod": 17},
        "settings": {
            "scoringSettings": {"scoringType": "H2H_POINTS"},
            "rosterSettings": {"lineupSlotCounts": {"0": 1}},
        },
        "teams": [_team(team_id=1), _team(team_id=2)],
    }


class FakeProvider:
    """Stands in for ``EspnProvider``: no network, no credentials."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch_raw(self, league: str, season: int, *, view) -> dict:
        return _conforming_payload()


def _write_manifest(path, payload: dict) -> None:
    from scripts.canary.shapes import extract_signature

    path.write_text(
        json.dumps({"signature": extract_signature(payload)}),
        encoding="utf-8",
    )


def test_ok_run_writes_a_report_json_the_publish_job_can_read_back(monkeypatch, tmp_path):
    monkeypatch.setattr(espn_module, "EspnProvider", FakeProvider)
    manifest_path = tmp_path / "shape_manifest.json"
    _write_manifest(manifest_path, _conforming_payload())
    monkeypatch.setattr(run_module, "MANIFEST_PATH", manifest_path)

    report_path = tmp_path / "report.json"
    exit_code = run_module.main(["--force", "--report-json", str(report_path)])

    assert exit_code == 0
    restored = report_from_dict(json.loads(report_path.read_text(encoding="utf-8")))
    assert restored.classification is Classification.OK


def test_schema_drift_run_writes_the_missing_paths(monkeypatch, tmp_path):
    payload = _conforming_payload()
    manifest_path = tmp_path / "shape_manifest.json"
    _write_manifest(manifest_path, payload)  # manifest matches the *conforming* shape
    del payload["status"]["finalScoringPeriod"]  # now drift it

    class DriftedProvider(FakeProvider):
        def fetch_raw(self, league, season, *, view):
            return payload

    monkeypatch.setattr(espn_module, "EspnProvider", DriftedProvider)
    monkeypatch.setattr(run_module, "MANIFEST_PATH", manifest_path)

    report_path = tmp_path / "report.json"
    exit_code = run_module.main(["--force", "--report-json", str(report_path)])

    assert exit_code == 1
    restored = report_from_dict(json.loads(report_path.read_text(encoding="utf-8")))
    assert restored.classification is Classification.SCHEMA_DRIFT
    assert "status.finalScoringPeriod" in restored.missing_paths


def test_without_report_json_nothing_extra_is_written(monkeypatch, tmp_path):
    """Backward compatibility: every existing invocation of run.py omits the
    flag and must see identical behavior to before this feature existed."""
    monkeypatch.setattr(espn_module, "EspnProvider", FakeProvider)
    manifest_path = tmp_path / "shape_manifest.json"
    _write_manifest(manifest_path, _conforming_payload())
    monkeypatch.setattr(run_module, "MANIFEST_PATH", manifest_path)

    exit_code = run_module.main(["--force"])

    assert exit_code == 0
    assert list(tmp_path.iterdir()) == [manifest_path]


def test_a_build_error_still_writes_a_report_when_asked(tmp_path):
    """``_write_report_json`` is exercised on the BUILD_ERROR branch too --
    covered directly here rather than by actually breaking an import, since
    provoking a real import failure from a test would be its own hazard."""
    from scripts.canary.run import _build_error_report, _write_report_json

    report = _build_error_report(RuntimeError("boom"))
    path = tmp_path / "report.json"
    _write_report_json(report, path)
    restored = report_from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored.classification is Classification.BUILD_ERROR


def test_write_report_json_is_a_no_op_when_path_is_none(tmp_path):
    from scripts.canary.run import _build_error_report, _write_report_json

    _write_report_json(_build_error_report(RuntimeError("boom")), None)
    assert list(tmp_path.iterdir()) == []
