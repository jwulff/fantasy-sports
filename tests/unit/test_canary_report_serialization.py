"""Round-tripping ``CheckReport`` through JSON (jwulff/fantasy-sports#64).

The two-job canary workflow needs the detection job (``contents: read``) to
hand its findings to the publish job (``contents: write``, ``issues: write``)
without either job re-fetching ESPN or the publish job needing the write
scope to *detect* anything. A JSON file is the hand-off; this suite proves
the encode/decode round-trips every field :meth:`CheckReport.render_summary`
already renders, not just the classification.
"""

from __future__ import annotations

from scripts.canary.shapes import (
    CheckReport,
    Classification,
    EnumGap,
    SignatureDiff,
    report_from_dict,
)


def test_round_trips_a_bare_ok_report():
    report = CheckReport(classification=Classification.OK, detail="all good")
    assert report_from_dict(report.to_dict()) == report


def test_round_trips_missing_paths_and_enum_gaps():
    report = CheckReport(
        classification=Classification.SCHEMA_DRIFT,
        missing_paths=["status.finalScoringPeriod"],
        enum_gaps=[EnumGap(field="defaultPositionId", value=99, context="Some Player")],
        detail="ESPN's response shape no longer matches what providers/espn.py expects.",
    )
    restored = report_from_dict(report.to_dict())
    assert restored == report


def test_round_trips_a_signature_diff():
    report = CheckReport(
        classification=Classification.SCHEMA_DRIFT,
        signature_diff=SignatureDiff(
            added={"status": ["newField"]}, removed={"status": ["finalScoringPeriod"]}
        ),
        detail="drift",
    )
    restored = report_from_dict(report.to_dict())
    assert restored == report


def test_round_trips_a_report_with_no_signature_diff():
    report = CheckReport(classification=Classification.CANARY_INFRA, detail="no payload")
    restored = report_from_dict(report.to_dict())
    assert restored.signature_diff is None


def test_to_dict_is_json_serializable():
    import json

    report = CheckReport(
        classification=Classification.SCHEMA_DRIFT,
        missing_paths=["a.b"],
        enum_gaps=[EnumGap(field="x", value=1, context="y")],
        signature_diff=SignatureDiff(added={}, removed={"a": ["b"]}),
        detail="d",
    )
    # Must not raise -- this is exactly what run.py writes to --report-json.
    encoded = json.dumps(report.to_dict())
    restored = report_from_dict(json.loads(encoded))
    assert restored == report
