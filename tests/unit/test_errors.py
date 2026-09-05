"""The error taxonomy (``docs/ARCHITECTURE.md`` §5), enforced.

Adding, renaming, or removing a code here is an API change: agents branch on
these strings to tell "ask the human" from "retry later". The tests below pin
the code set, the retry semantics, and the fact that no error payload is built
out of provider bytes.
"""

from __future__ import annotations

import pytest

from fantasy_sports.core.errors import (
    ERROR_TYPES,
    AuthExpiredError,
    AuthMissingError,
    ErrorCode,
    FantasySportsError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    error_type_for,
)

# The taxonomy exactly as ARCHITECTURE.md §5 documents it.
DOCUMENTED_CODES = {
    "AUTH_MISSING",
    "AUTH_EXPIRED",
    "LEAGUE_NOT_FOUND",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "SCHEMA_DRIFT",
}


def test_the_taxonomy_is_exactly_the_documented_set():
    assert {code.value for code in ErrorCode} == DOCUMENTED_CODES


def test_every_code_has_exactly_one_exception_type():
    assert {cls.code for cls in ERROR_TYPES} == set(ErrorCode)
    assert len(ERROR_TYPES) == len(ErrorCode)


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_every_error_is_a_fantasy_sports_error_with_a_stable_shape(cls):
    exc = cls("something went wrong")
    assert isinstance(exc, FantasySportsError)
    assert str(exc) == "something went wrong"
    payload = exc.to_dict()
    assert payload["code"] == cls.code.value
    assert payload["message"] == "something went wrong"
    assert isinstance(payload["retryable"], bool)
    assert payload["agent_action"]


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_error_type_for_round_trips_every_code(cls):
    assert error_type_for(cls.code) is cls
    assert error_type_for(cls.code.value) is cls


def test_error_type_for_rejects_an_unknown_code():
    with pytest.raises(KeyError):
        error_type_for("NOT_A_CODE")


def test_credential_failures_are_not_retryable_and_availability_is():
    assert AuthMissingError("no creds").retryable is False
    assert AuthExpiredError("dead cookies").retryable is False
    assert LeagueNotFoundError("nope").retryable is False
    assert SchemaDriftError("shape changed").retryable is False
    assert ProviderUnavailableError("espn 503").retryable is True
    assert RateLimitedError("slow down").retryable is True


def test_rate_limited_carries_retry_after():
    exc = RateLimitedError("slow down", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.to_dict()["details"]["retry_after"] == 30.0


def test_rate_limited_without_a_retry_after_omits_it():
    assert "retry_after" not in RateLimitedError("slow down").to_dict().get("details", {})


def test_schema_drift_records_the_offending_path():
    exc = SchemaDriftError("missing teams", path="mTeams.teams", provider="espn")
    details = exc.to_dict()["details"]
    assert details["path"] == ["mTeams.teams"]
    assert details["provider"] == "espn"


def test_schema_drift_accepts_several_paths():
    exc = SchemaDriftError("missing fields", path=["Team.name", "Team.wins"])
    assert exc.to_dict()["details"]["path"] == ["Team.name", "Team.wins"]


def test_schema_drift_without_a_path_reports_no_details():
    assert "details" not in SchemaDriftError("shape changed").to_dict()


def test_details_are_copied_so_a_caller_cannot_mutate_the_payload_later():
    supplied = {"league_id": "123456"}
    exc = ProviderUnavailableError("espn 503", details=supplied)
    supplied["league_id"] = "mutated"
    assert exc.to_dict()["details"]["league_id"] == "123456"


def test_an_unclassifiable_failure_is_available_shaped_not_throttled():
    """R12: unclassifiable maps to availability with bounded retry, never throttling."""
    exc = ProviderUnavailableError("unclassifiable upstream failure")
    assert exc.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert exc.retryable is True
    assert exc.code is not ErrorCode.RATE_LIMITED
