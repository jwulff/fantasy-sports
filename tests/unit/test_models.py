"""The normalized domain model (ADR-0002 as amended, origin R2/R3).

Every assertion here is a promise made to an adapter author. The two that bite
hardest in practice: a missing key is schema drift and never a bare
``KeyError``, and an *absent* optional value — a transaction with no processed
date — is ordinary provider data, not drift.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from fantasy_sports.core.errors import SchemaDriftError
from fantasy_sports.core.models import (
    NORMALIZED_MODELS,
    TRANSACTION_TYPES,
    CredentialSpec,
    FreeAgent,
    League,
    Matchup,
    Player,
    ProviderObject,
    RosterSlot,
    Team,
    Transaction,
)

# --- fixtures: payloads shaped the way an adapter will hand them over --------

ESPN_LEAGUE_RAW = {
    "id": 123456,
    "seasonId": 2026,
    "scoringPeriodId": 3,
    "status": {"currentMatchupPeriod": 3, "latestScoringPeriod": 3},
    "settings": {"name": "Dynasty Warriors", "size": 12},
}

LEAGUE_PAYLOAD = {
    "provider": "espn",
    "provider_id": "123456",
    "name": "Dynasty Warriors",
    "season": 2026,
    "sport": "nfl",
    "team_count": 12,
    "current_week": 3,
    "raw": ESPN_LEAGUE_RAW,
}

TEAM_PAYLOAD = {
    "provider": "espn",
    "provider_id": "7",
    "name": "Regression to the Mean",
    "owner_names": ["John Wulff", "Courtney Wulff"],
    "wins": 2,
    "losses": 1,
    "ties": 0,
    "points_for": 331.4,
    "points_against": 298.2,
    "raw": {"id": 7, "owners": ["{GUID-A}", "{GUID-B}"]},
    "standing": 4,
}

PLAYER_PAYLOAD = {
    "provider": "espn",
    "provider_id": "3139477",
    "name": "Patrick Mahomes",
    "position": "QB",
    "raw": {"playerId": 3139477, "eligibleSlots": [0, 7, 20, 21]},
    "eligible_slots": ["QB", "OP", "BE", "IR"],
    "status": "ACTIVE",
    "injury_status": None,
    "pro_team": "KC",
    "opponent": "DEN",
    "projected_points": 21.7,
    "kickoff": datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
}

ROSTER_SLOT_PAYLOAD = {
    "provider": "espn",
    "provider_id": "3139477",
    "player": PLAYER_PAYLOAD,
    "slot": "QB",
    "is_starter": True,
    "raw": {"lineupSlotId": 0, "playerPoolEntry": {"id": 3139477}},
    "is_locked": False,
}

MATCHUP_PAYLOAD = {
    "provider": "espn",
    "provider_id": "2026-mp3-1",
    "week": 3,
    "team_a_provider_id": "7",
    "team_a_score": 118.4,
    "team_b_provider_id": "2",
    "team_b_score": 101.9,
    "is_playoff": False,
    "raw": {"matchupPeriodId": 3, "scoringPeriodId": 3, "home": {"teamId": 7}},
    "scoring_period_id": 3,
    "matchup_period_id": 3,
}

TRANSACTION_PAYLOAD = {
    "provider": "espn",
    "provider_id": "txn-991",
    "type": "waiver_claim",
    "raw": {"type": "WAIVER", "status": "EXECUTED", "proposedDate": 1758326400000},
    "team_provider_id": "7",
    "players_in": ["4262921"],
    "players_out": ["3116406"],
    "faab_spent": 17,
    "timestamp": datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
}

FREE_AGENT_PAYLOAD = {
    "provider": "espn",
    "provider_id": "4262921",
    "player": PLAYER_PAYLOAD,
    "raw": {"id": 4262921, "ownership": {"percentOwned": 41.2}},
    "percent_owned": 41.2,
}

PAYLOADS = {
    League: LEAGUE_PAYLOAD,
    Team: TEAM_PAYLOAD,
    Player: PLAYER_PAYLOAD,
    RosterSlot: ROSTER_SLOT_PAYLOAD,
    Matchup: MATCHUP_PAYLOAD,
    Transaction: TRANSACTION_PAYLOAD,
    FreeAgent: FREE_AGENT_PAYLOAD,
}


def _ids(cls):
    return cls.__name__


# --- the promises every normalized object makes -----------------------------


def test_the_model_set_is_the_one_the_plan_names():
    assert set(NORMALIZED_MODELS) == set(PAYLOADS)


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_every_model_carries_provider_provider_id_and_raw(cls):
    names = {f.name for f in dataclasses.fields(cls)}
    assert {"provider", "provider_id", "raw"} <= names


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_every_model_constructs_from_a_fixture_dict(cls):
    model = cls.from_payload(PAYLOADS[cls])
    assert isinstance(model, cls)
    assert isinstance(model, ProviderObject)
    assert model.provider == "espn"
    assert model.provider_id == PAYLOADS[cls]["provider_id"]


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_raw_round_trips_unmodified(cls):
    payload = dict(PAYLOADS[cls])
    original = {"probe": ["untouched", {"nested": 1}]}
    payload["raw"] = original
    before = repr(original)
    model = cls.from_payload(payload)
    assert model.raw == original
    assert repr(original) == before, "constructing a model mutated the raw payload"


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_every_model_is_frozen(cls):
    model = cls.from_payload(PAYLOADS[cls])
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.provider = "sleeper"


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_unknown_keys_are_ignored_because_upstream_additions_are_not_drift(cls):
    payload = dict(PAYLOADS[cls]) | {"someFieldEspnAddedLastTuesday": 42}
    assert cls.from_payload(payload) == cls.from_payload(PAYLOADS[cls])


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_a_missing_required_key_raises_schema_drift_not_a_key_error(cls):
    required = [
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    ]
    assert required, f"{cls.__name__} has no required fields to test"
    for name in required:
        payload = {k: v for k, v in PAYLOADS[cls].items() if k != name}
        with pytest.raises(SchemaDriftError) as caught:
            cls.from_payload(payload)
        assert not isinstance(caught.value, KeyError)
        assert f"{cls.__name__}.{name}" in caught.value.to_dict()["details"]["path"]


def test_schema_drift_from_a_model_names_the_provider_when_the_payload_has_one():
    payload = {k: v for k, v in TEAM_PAYLOAD.items() if k != "name"}
    with pytest.raises(SchemaDriftError) as caught:
        Team.from_payload(payload)
    assert caught.value.to_dict()["details"]["provider"] == "espn"


def test_schema_drift_reports_field_names_only_and_never_provider_values():
    """Rule 5: an error payload must not become a channel for credential bytes."""
    payload = {k: v for k, v in TEAM_PAYLOAD.items() if k != "name"}
    payload["raw"] = {"owners": ["{SECRET-SWID-GUID}"]}
    with pytest.raises(SchemaDriftError) as caught:
        Team.from_payload(payload)
    assert "SECRET-SWID-GUID" not in repr(caught.value.to_dict())


def test_from_payload_rejects_a_non_mapping_payload_as_drift():
    with pytest.raises(SchemaDriftError):
        League.from_payload(["not", "a", "mapping"])


# --- model-specific promises ------------------------------------------------


def test_a_team_with_two_owners_preserves_both_names():
    team = Team.from_payload(TEAM_PAYLOAD)
    assert team.owner_names == ("John Wulff", "Courtney Wulff")


def test_owner_names_are_an_immutable_sequence_even_when_handed_a_list():
    team = Team(
        provider="espn",
        provider_id="7",
        name="Regression to the Mean",
        owner_names=["Solo Owner"],
        wins=0,
        losses=0,
        ties=0,
        points_for=0.0,
        points_against=0.0,
        raw={},
    )
    assert team.owner_names == ("Solo Owner",)
    assert isinstance(team.owner_names, tuple)


def test_standing_is_optional_because_not_every_provider_ranks_the_same_way():
    payload = {k: v for k, v in TEAM_PAYLOAD.items() if k != "standing"}
    assert Team.from_payload(payload).standing is None


def test_a_player_carries_the_r3_lineup_decision_context():
    """R3: eligibility, status, opponent, projection — enough to pick a lineup."""
    player = Player.from_payload(PLAYER_PAYLOAD)
    assert player.eligible_slots == ("QB", "OP", "BE", "IR")
    assert player.status == "ACTIVE"
    assert player.opponent == "DEN"
    assert player.projected_points == 21.7
    assert player.kickoff == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def test_slot_eligibility_defaults_to_empty_for_a_provider_that_does_not_publish_it():
    """Sleeper computes eligibility client-side; absence is not drift."""
    payload = {k: v for k, v in PLAYER_PAYLOAD.items() if k != "eligible_slots"}
    assert Player.from_payload(payload).eligible_slots == ()


def test_a_roster_slot_carries_occupancy_and_lock_state():
    slot = RosterSlot.from_payload(ROSTER_SLOT_PAYLOAD)
    assert slot.slot == "QB"
    assert slot.is_starter is True
    assert slot.is_locked is False
    assert isinstance(slot.player, Player)
    assert slot.player.name == "Patrick Mahomes"
    assert slot.player_provider_id == "3139477"


def test_a_roster_slot_accepts_an_already_built_player():
    player = Player.from_payload(PLAYER_PAYLOAD)
    payload = dict(ROSTER_SLOT_PAYLOAD) | {"player": player}
    assert RosterSlot.from_payload(payload).player is player


def test_a_nested_player_missing_a_key_reports_the_players_path():
    broken = {k: v for k, v in PLAYER_PAYLOAD.items() if k != "position"}
    payload = dict(ROSTER_SLOT_PAYLOAD) | {"player": broken}
    with pytest.raises(SchemaDriftError) as caught:
        RosterSlot.from_payload(payload)
    assert "Player.position" in caught.value.to_dict()["details"]["path"]


def test_lock_state_is_optional_because_not_every_provider_exposes_it():
    payload = {k: v for k, v in ROSTER_SLOT_PAYLOAD.items() if k != "is_locked"}
    assert RosterSlot.from_payload(payload).is_locked is None


def test_a_matchup_is_symmetric_not_home_and_away():
    names = {f.name for f in dataclasses.fields(Matchup)}
    assert {"team_a_provider_id", "team_b_provider_id"} <= names
    assert not {n for n in names if "home" in n or "away" in n}


def test_a_matchup_surfaces_both_period_identifiers():
    """ESPN's scoringPeriodId/matchupPeriodId split is invisible until playoffs."""
    matchup = Matchup.from_payload(MATCHUP_PAYLOAD)
    assert matchup.scoring_period_id == 3
    assert matchup.matchup_period_id == 3
    assert matchup.raw["scoringPeriodId"] == 3
    assert matchup.raw["matchupPeriodId"] == 3


def test_a_playoff_matchup_may_span_two_scoring_periods():
    payload = dict(MATCHUP_PAYLOAD) | {
        "week": 15,
        "is_playoff": True,
        "scoring_period_id": 16,
        "matchup_period_id": 15,
        "raw": {"matchupPeriodId": 15, "scoringPeriodId": 16},
    }
    matchup = Matchup.from_payload(payload)
    assert matchup.week == 15
    assert matchup.scoring_period_id != matchup.matchup_period_id


def test_a_league_can_carry_its_roster_slot_configuration():
    """R3a: a legal target lineup must be constructible from normalized output."""
    payload = dict(LEAGUE_PAYLOAD) | {"roster_slots": {"QB": 1, "RB": 2, "RB/WR/TE": 1, "BE": 7}}
    assert League.from_payload(payload).roster_slots["RB/WR/TE"] == 1


def test_roster_slot_configuration_defaults_to_empty():
    assert League.from_payload(LEAGUE_PAYLOAD).roster_slots == {}


@pytest.mark.parametrize("kind", TRANSACTION_TYPES)
def test_every_narrowed_transaction_type_is_accepted(kind):
    payload = dict(TRANSACTION_PAYLOAD) | {"type": kind}
    assert Transaction.from_payload(payload).type == kind


def test_a_transaction_type_outside_the_narrowing_is_drift():
    payload = dict(TRANSACTION_PAYLOAD) | {"type": "TRADE_VETO"}
    with pytest.raises(SchemaDriftError) as caught:
        Transaction.from_payload(payload)
    assert "Transaction.type" in caught.value.to_dict()["details"]["path"]


def test_a_transaction_with_no_processed_or_proposed_date_is_not_drift():
    """An absent date is ordinary ESPN data. A false drift alarm on routine
    payloads is the fastest way to make the drift signal ignorable."""
    payload = {k: v for k, v in TRANSACTION_PAYLOAD.items() if k != "timestamp"}
    payload["raw"] = {"type": "WAIVER", "status": "EXECUTED"}
    txn = Transaction.from_payload(payload)
    assert txn.timestamp is None
    assert txn.type == "waiver_claim"


def test_a_transaction_moves_players_in_both_directions():
    txn = Transaction.from_payload(TRANSACTION_PAYLOAD)
    assert txn.players_in == ("4262921",)
    assert txn.players_out == ("3116406",)
    assert txn.faab_spent == 17


def test_a_drop_has_no_incoming_players_and_that_is_not_drift():
    payload = {
        "provider": "espn",
        "provider_id": "txn-992",
        "type": "drop",
        "raw": {"type": "ROSTER"},
    }
    txn = Transaction.from_payload(payload)
    assert txn.players_in == ()
    assert txn.players_out == ()
    assert txn.faab_spent is None


def test_a_free_agent_carries_ownership_when_the_provider_reports_it():
    agent = FreeAgent.from_payload(FREE_AGENT_PAYLOAD)
    assert agent.percent_owned == 41.2
    assert agent.player.name == "Patrick Mahomes"


def test_a_free_agent_without_ownership_is_not_drift():
    """Sleeper does not track per-player ownership the way ESPN and Yahoo do."""
    payload = {k: v for k, v in FREE_AGENT_PAYLOAD.items() if k != "percent_owned"}
    assert FreeAgent.from_payload(payload).percent_owned is None


# --- serialization ----------------------------------------------------------


def test_to_dict_expands_nested_models_and_sequences():
    payload = RosterSlot.from_payload(ROSTER_SLOT_PAYLOAD).to_dict()
    assert payload["player"]["name"] == "Patrick Mahomes"
    assert payload["player"]["eligible_slots"] == ["QB", "OP", "BE", "IR"]
    assert isinstance(payload["player"]["eligible_slots"], list)
    assert payload["raw"] == ROSTER_SLOT_PAYLOAD["raw"]


@pytest.mark.parametrize("cls", NORMALIZED_MODELS, ids=_ids)
def test_to_dict_keeps_every_field(cls):
    payload = cls.from_payload(PAYLOADS[cls]).to_dict()
    assert set(payload) == {f.name for f in dataclasses.fields(cls)}


# --- credential specs -------------------------------------------------------


def test_a_credential_spec_describes_what_a_provider_needs():
    spec = CredentialSpec(
        name="espn_s2",
        label="ESPN espn_s2 cookie",
        staleness="Silent expiry; no documented lifetime. Report age, do not predict.",
    )
    assert spec.secret is True
    assert spec.required is True
    assert "age" in spec.staleness


def test_a_credential_spec_is_frozen():
    spec = CredentialSpec(name="espn_s2", label="ESPN espn_s2 cookie")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "SWID"


def test_a_credential_spec_can_describe_a_non_secret_requirement():
    spec = CredentialSpec(name="league_id", label="League id", secret=False, required=False)
    assert spec.secret is False
    assert spec.required is False
    assert spec.staleness is None


def test_credential_spec_is_not_a_normalized_provider_object():
    """It describes a provider, not something a provider returned."""
    assert CredentialSpec not in NORMALIZED_MODELS


# --- layering ---------------------------------------------------------------

EXPENSIVE = ("espn_api", "requests", "keyring", "typer", "click", "rich")


@pytest.mark.parametrize(
    "module",
    ["fantasy_sports.core", "fantasy_sports.core.models", "fantasy_sports.providers.base"],
)
def test_the_domain_layer_costs_nothing_to_import(module):
    """CLAUDE.md rule 2 and ADR-0008: ``core/`` and ``providers/`` are stdlib only."""
    probe = (
        f"import {module}\n"
        "import sys, json; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert not (loaded & set(EXPENSIVE))
