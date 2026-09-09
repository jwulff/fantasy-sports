"""Offline tests for ``scripts/canary/shapes.py`` (jwulff/fantasy-sports#11).

Every fixture below is hand-built, not a live recording. It models the
documented shape from ``docs/research/03-espn-api-surface.md`` §4.4 — proving
ESPN still sends that shape is the live canary's job (``scripts/canary/run.py``,
excluded from CI, see ``tests/live/``), not this suite's. What this suite
proves is that the pure diff/classification logic reacts correctly to a shape
that matches, and to each way a shape can fail to match.
"""

from __future__ import annotations

import copy

from scripts.canary.shapes import (
    Classification,
    classify,
    diff_signature,
    enum_coverage_gaps,
    extract_signature,
    missing_required_paths,
    resolve_path,
)

POSITION_MAP = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 23: "FLEX"}
PRO_TEAM_MAP = {0: "None", 1: "ATL", 2: "BUF"}


def _player(*, player_id: int, name: str, position: int = 4, pro_team: int = 1) -> dict:
    return {
        "playerPoolEntry": {
            "player": {
                "id": player_id,
                "fullName": name,
                "defaultPositionId": position,
                "eligibleSlots": [position, 23],
                "proTeamId": pro_team,
            }
        }
    }


def _team(*, team_id: int) -> dict:
    return {
        "id": team_id,
        "record": {
            "overall": {
                "wins": 4,
                "losses": 3,
                "ties": 0,
                "pointsFor": 512.5,
                "pointsAgainst": 480.2,
            }
        },
        "roster": {
            "entries": [
                _player(player_id=100 + team_id, name=f"Player {team_id}"),
            ]
        },
    }


def valid_payload() -> dict:
    """A minimal payload with every §4.4 required path present and every
    enum value mapped — the shape the committed manifest should match."""
    return {
        "status": {"currentMatchupPeriod": 1, "finalScoringPeriod": 17},
        "settings": {
            "scoringSettings": {"scoringType": "H2H_POINTS"},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2}},
        },
        "teams": [_team(team_id=1), _team(team_id=2)],
    }


# --------------------------------------------------------------------------- #
# resolve_path
# --------------------------------------------------------------------------- #


def test_resolve_path_walks_a_plain_dotted_path():
    assert resolve_path(valid_payload(), "status.currentMatchupPeriod") == [1]


def test_resolve_path_fans_out_across_a_list():
    values = resolve_path(valid_payload(), "teams[].record.overall.wins")
    assert values == [4, 4]


def test_resolve_path_returns_empty_when_any_segment_is_missing():
    payload = valid_payload()
    del payload["settings"]["rosterSettings"]
    assert resolve_path(payload, "settings.rosterSettings.lineupSlotCounts") == []


def test_resolve_path_does_not_fan_out_into_a_string():
    # A field that happens to be a string must not be iterated character by
    # character just because a later segment used `[]`.
    payload = {"teams": "not-a-list"}
    assert resolve_path(payload, "teams[].id") == []


# --------------------------------------------------------------------------- #
# missing_required_paths
# --------------------------------------------------------------------------- #


def test_a_conforming_payload_has_no_missing_required_paths():
    assert missing_required_paths(valid_payload()) == []


def test_a_removed_top_level_field_is_reported_missing():
    payload = valid_payload()
    del payload["status"]["finalScoringPeriod"]
    assert "status.finalScoringPeriod" in missing_required_paths(payload)


def test_a_removed_nested_player_field_is_reported_missing():
    payload = valid_payload()
    del payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]["fullName"]
    # Team 2 still has it, but the path is reported missing only when *every*
    # branch is missing it -- assert the field genuinely vanishes everywhere
    # first, matching what real drift looks like (ESPN doesn't drop a field
    # for one team and not another).
    del payload["teams"][1]["roster"]["entries"][0]["playerPoolEntry"]["player"]["fullName"]
    assert "teams[].roster.entries[].playerPoolEntry.player.fullName" in missing_required_paths(
        payload
    )


# --------------------------------------------------------------------------- #
# enum_coverage_gaps
# --------------------------------------------------------------------------- #


def test_a_fully_mapped_payload_has_no_enum_gaps():
    gaps = enum_coverage_gaps(valid_payload(), position_map=POSITION_MAP, pro_team_map=PRO_TEAM_MAP)
    assert gaps == []


def test_an_unmapped_position_id_is_a_gap():
    """This is the #662 case: a new enum value that degrades silently
    (``POSITION_MAP.get(x, '')``) rather than raising, so only an explicit
    assertion like this one catches it."""
    payload = valid_payload()
    player = payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]
    player["defaultPositionId"] = 99
    gaps = enum_coverage_gaps(payload, position_map=POSITION_MAP, pro_team_map=PRO_TEAM_MAP)
    assert any(gap.field == "defaultPositionId" and gap.value == 99 for gap in gaps)


def test_an_unmapped_eligible_slot_is_a_gap():
    payload = valid_payload()
    player = payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]
    player["eligibleSlots"] = [4, 999]
    gaps = enum_coverage_gaps(payload, position_map=POSITION_MAP, pro_team_map=PRO_TEAM_MAP)
    assert any(gap.field == "eligibleSlots" and gap.value == 999 for gap in gaps)


def test_an_unmapped_pro_team_id_is_a_gap():
    payload = valid_payload()
    player = payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]
    player["proTeamId"] = 12345
    gaps = enum_coverage_gaps(payload, position_map=POSITION_MAP, pro_team_map=PRO_TEAM_MAP)
    assert any(gap.field == "proTeamId" and gap.value == 12345 for gap in gaps)


# --------------------------------------------------------------------------- #
# extract_signature / diff_signature
# --------------------------------------------------------------------------- #


def test_extract_signature_is_a_sorted_key_fingerprint():
    signature = extract_signature(valid_payload())
    assert signature["status"] == ["currentMatchupPeriod", "finalScoringPeriod"]
    assert signature["teams[].record.overall"] == [
        "losses",
        "pointsAgainst",
        "pointsFor",
        "ties",
        "wins",
    ]


def test_identical_signatures_diff_to_empty():
    signature = extract_signature(valid_payload())
    diff = diff_signature(signature, copy.deepcopy(signature))
    assert diff.is_empty
    assert not diff.has_removals
    assert not diff.has_additions


def test_a_removed_key_shows_up_as_removed_not_added():
    expected = extract_signature(valid_payload())
    payload = valid_payload()
    del payload["status"]["finalScoringPeriod"]
    observed = extract_signature(payload)
    diff = diff_signature(expected, observed)
    assert diff.has_removals
    assert diff.removed["status"] == ["finalScoringPeriod"]
    assert not diff.has_additions


def test_a_new_key_shows_up_as_added_not_removed():
    expected = extract_signature(valid_payload())
    payload = valid_payload()
    payload["status"]["newField"] = "surprise"
    observed = extract_signature(payload)
    diff = diff_signature(expected, observed)
    assert diff.has_additions
    assert diff.added["status"] == ["newField"]
    assert not diff.has_removals


# --------------------------------------------------------------------------- #
# classify — the integration of all three checks
# --------------------------------------------------------------------------- #


def test_classify_reports_ok_for_a_conforming_payload_against_its_own_manifest():
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    assert report.classification is Classification.OK
    assert report.ok


def test_classify_reports_schema_drift_when_a_required_path_disappears():
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    del payload["settings"]["rosterSettings"]["lineupSlotCounts"]
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    assert report.classification is Classification.SCHEMA_DRIFT
    assert "settings.rosterSettings.lineupSlotCounts" in report.missing_paths


def test_classify_reports_schema_drift_on_an_unmapped_enum_value_even_though_nothing_crashed():
    """The additive case: the payload still parses, every required path is
    present, but a new id has no mapping -- exactly the #662 shape."""
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    player = payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"]
    player["defaultPositionId"] = 4321
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    assert report.classification is Classification.SCHEMA_DRIFT
    assert report.enum_gaps and report.enum_gaps[0].value == 4321


def test_classify_reports_schema_drift_when_a_manifest_key_disappears():
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    del payload["status"]["finalScoringPeriod"]
    # finalScoringPeriod is also in REQUIRED_PATHS, so pick a signature-only
    # regression to prove the manifest diff triggers drift on its own: add a
    # key to the manifest baseline that the live payload doesn't have.
    manifest_signature = dict(manifest_signature)
    manifest_signature["status"] = [*manifest_signature.get("status", []), "wasHereBefore"]
    payload = valid_payload()
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    assert report.classification is Classification.SCHEMA_DRIFT
    assert report.signature_diff is not None
    assert "wasHereBefore" in report.signature_diff.removed["status"]


def test_classify_does_not_fail_on_a_purely_additive_manifest_diff():
    """A brand-new key nobody has added to the manifest yet must not fail the
    canary on its own -- only removals and the two explicit checks do."""
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    payload["status"]["newField"] = "surprise"
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    assert report.classification is Classification.OK
    assert report.signature_diff is not None
    assert report.signature_diff.has_additions


def test_classify_with_no_payload_is_infra_not_drift():
    report = classify(
        None, manifest_signature={}, position_map=POSITION_MAP, pro_team_map=PRO_TEAM_MAP
    )
    assert report.classification is Classification.CANARY_INFRA


# --------------------------------------------------------------------------- #
# CheckReport.render_summary — must not explode, must mention the classification
# --------------------------------------------------------------------------- #


def test_render_summary_names_the_classification():
    payload = valid_payload()
    manifest_signature = extract_signature(payload)
    del payload["status"]["finalScoringPeriod"]
    report = classify(
        payload,
        manifest_signature=manifest_signature,
        position_map=POSITION_MAP,
        pro_team_map=PRO_TEAM_MAP,
    )
    summary = report.render_summary()
    assert "SCHEMA_DRIFT" in summary
    assert "status.finalScoringPeriod" in summary
