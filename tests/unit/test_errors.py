"""The error taxonomy (``docs/ARCHITECTURE.md`` §5), enforced.

Adding, renaming, or removing a code here is an API change: agents branch on
these strings to tell "ask the human" from "retry later". The tests below pin
the code set, the retry semantics, and the fact that no error payload is built
out of provider bytes.
"""

from __future__ import annotations

import json
import traceback

import pytest

from fantasy_sports.core.errors import (
    ERROR_TYPES,
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    ErrorCode,
    FantasySportsError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    error_type_for,
)
from fantasy_sports.core.redaction import REDACTED, forget_secrets, remember_secret

# The taxonomy exactly as ARCHITECTURE.md §5 documents it.
DOCUMENTED_CODES = {
    "AUTH_MISSING",
    "AUTH_EXPIRED",
    "LEAGUE_NOT_FOUND",
    "CONFIG_INVALID",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "SCHEMA_DRIFT",
}

# A sentinel that cannot occur by accident anywhere in rendered output.
SECRET = "AEBqp7SENTINELs2cookievalue0123456789abcdefXYZ%2Fzz"


@pytest.fixture(autouse=True)
def _forget_secrets():
    """The scrub set is process-wide; keep it from leaking between tests."""
    forget_secrets()
    yield
    forget_secrets()


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
    assert ConfigInvalidError("broken toml").retryable is False
    assert SchemaDriftError("shape changed").retryable is False
    assert ProviderUnavailableError("espn 503").retryable is True
    assert RateLimitedError("slow down").retryable is True


def test_a_broken_config_is_not_a_missing_league():
    """The reason `CONFIG_INVALID` was added (decision on #6).

    `LEAGUE_NOT_FOUND` tells an agent to retry with a different `--league`.
    That cannot work when the file itself will not parse, so the two causes
    must be distinguishable from the payload alone.
    """
    broken = ConfigInvalidError("config.toml is not valid TOML")
    absent = LeagueNotFoundError("no league named 'keeper'")

    assert broken.code is ErrorCode.CONFIG_INVALID
    assert broken.code is not ErrorCode.LEAGUE_NOT_FOUND
    assert broken.to_dict()["code"] != absent.to_dict()["code"]
    assert broken.to_dict()["agent_action"] != absent.to_dict()["agent_action"]
    # Not retryable, and distinguishable from both availability causes.
    assert broken.retryable is False
    assert broken.code not in {ErrorCode.PROVIDER_UNAVAILABLE, ErrorCode.RATE_LIMITED}


def test_config_invalid_defaults_to_kind_config_and_records_it_in_details():
    """jwulff/fantasy-sports#48 (ADR-0004 amended by ADR-0009).

    `CONFIG_INVALID` covers two causes now — a config file that will not
    parse, and a CLI argument only the provider can validate — rather than an
    eighth taxonomy code for the second. `kind` is the discriminator, and it
    defaults to `"config"` because every pre-existing raise site (a broken
    `config.toml`, an unknown provider name, a malformed credentials file) is
    that cause and none of them pass `kind` explicitly.
    """
    default = ConfigInvalidError("config.toml is not valid TOML")
    assert default.kind == "config"
    assert default.to_dict()["details"] == {"kind": "config"}

    argument = ConfigInvalidError("bad --pos", kind="argument")
    assert argument.kind == "argument"
    assert argument.to_dict()["details"] == {"kind": "argument"}

    # The class-level instruction no longer names a config file specifically —
    # it has to be honest for both causes now.
    assert "config file" not in default.agent_action.lower()
    assert default.agent_action == argument.agent_action
    # Exit status and retry semantics are identical either way; `kind` is
    # metadata for a consumer that wants it, not a second taxonomy surface.
    assert default.retryable is argument.retryable is False
    assert default.code is argument.code is ErrorCode.CONFIG_INVALID


# --- scrubbing: the guarantee that travelled with deleting `AuthError` ------


def test_a_non_auth_error_scrubs_a_credential_a_caller_interpolated():
    """The constraint that moved when `auth.chain.AuthError` was deleted (#35).

    `AuthError` scrubbed its message at construction. If that had not moved
    down to `FantasySportsError`, every *other* error type would silently have
    lost the protection and #34's redaction guarantee would be worth less than
    it reads.

    This is deliberately built from a **non-auth** error. An auth-shaped test
    would still pass if the scrubbing had stayed behind in `auth/`, which is
    exactly the regression it has to catch. The realistic leak is the one in
    `docs/memory/credential-leak-channels.md`: a caller three layers away
    formatting a request URL that carries a cookie.
    """
    remember_secret(SECRET)
    exc = ProviderUnavailableError(
        f"GET https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET} failed",
        details={"url": f"https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET}"},
    )

    assert SECRET not in str(exc)
    assert SECRET not in repr(exc)
    assert SECRET not in "".join(str(arg) for arg in exc.args)
    assert SECRET not in exc.message
    assert SECRET not in json.dumps(exc.to_dict())
    assert SECRET not in "".join(traceback.format_exception(exc))
    assert REDACTED in exc.message


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_every_error_type_scrubs_not_just_the_auth_ones(cls):
    """Scrubbing is a property of the base class, so it holds for all of them."""
    remember_secret(SECRET)
    assert SECRET not in json.dumps(cls(f"leaked {SECRET}").to_dict())


def test_an_unregistered_value_is_left_alone():
    """Control: proves the tests above measure the scrub set, not a coincidence.

    Without this, a `redact()` that blanked every message would pass every
    assertion above while destroying the error text.
    """
    exc = ProviderUnavailableError(f"GET https://espn.com/x?espn_s2={SECRET} failed")
    assert SECRET in exc.message, "nothing was registered; the value must survive"


def test_a_detail_added_after_construction_is_also_scrubbed():
    """`SchemaDriftError` writes `path`/`provider` after `super().__init__`."""
    remember_secret(SECRET)
    exc = SchemaDriftError("shape changed", path=[f"Team.{SECRET}"], provider=SECRET)
    assert SECRET not in json.dumps(exc.to_dict())


def test_scrubbing_reaches_into_nested_details():
    """`details` is caller-assembled, so a credential can hide below the top level."""
    remember_secret(SECRET)
    exc = ProviderUnavailableError(
        "espn 503",
        details={
            "request": {"url": f"https://espn.com/x?espn_s2={SECRET}"},
            "tried": (f"env:{SECRET}", "keychain"),
            "attempts": 3,
        },
    )
    payload = exc.to_dict()
    assert SECRET not in json.dumps(payload)
    # Shape and non-string scalars survive; only the credential is replaced.
    assert payload["details"]["attempts"] == 3
    assert payload["details"]["tried"][1] == "keychain"
    assert REDACTED in payload["details"]["request"]["url"]


def test_rate_limited_carries_retry_after():
    exc = RateLimitedError("slow down", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.to_dict()["details"]["retry_after"] == 30.0


def test_rate_limited_without_a_retry_after_reports_null_details():
    assert RateLimitedError("slow down").to_dict()["details"] is None


def test_schema_drift_records_the_offending_path():
    exc = SchemaDriftError("missing teams", path="mTeams.teams", provider="espn")
    details = exc.to_dict()["details"]
    assert details["path"] == ["mTeams.teams"]
    assert details["provider"] == "espn"


def test_schema_drift_accepts_several_paths():
    exc = SchemaDriftError("missing fields", path=["Team.name", "Team.wins"])
    assert exc.to_dict()["details"]["path"] == ["Team.name", "Team.wins"]


def test_schema_drift_without_a_path_reports_null_details():
    assert SchemaDriftError("shape changed").to_dict()["details"] is None


def test_the_error_payload_keys_are_always_present():
    """ADR-0004 as amended by U5 (#6): every key, every time, `None` when empty.

    A key that disappears when it is empty is a key some consumers will never
    read for, which is exactly what promoting `remediation` out of `details`
    was meant to prevent.
    """
    expected = ["code", "message", "retryable", "agent_action", "remediation", "details"]
    for error_cls in ERROR_TYPES:
        assert list(error_cls("bare").to_dict()) == expected, error_cls.__name__


def test_remediation_is_a_first_class_key_and_is_scrubbed():
    exc = ProviderUnavailableError("espn 503", remediation="Retry in a minute.")
    assert exc.remediation == "Retry in a minute."
    assert exc.to_dict()["remediation"] == "Retry in a minute."
    assert exc.details == {}, "it is not a detail"

    remember_secret(SECRET)
    leaky = ProviderUnavailableError("espn 503", remediation=f"Rotate {SECRET} in DevTools.")
    assert SECRET not in leaky.to_dict()["remediation"]
    assert REDACTED in leaky.to_dict()["remediation"]


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
