"""The error taxonomy — ``docs/ARCHITECTURE.md`` §5, origin R12.

Every failure this tool reports carries a stable machine code so an agent can
tell "your cookies died, ask the human" from "ESPN is down, retry later"
without parsing English. **Adding, renaming, or removing a code is an API
change**, not a refactor.

This module lives in ``core/`` rather than with the output layer on purpose:
the domain models raise :class:`SchemaDriftError` while validating a payload,
and ``output/`` depends on ``core/``, not the other way round. Putting the
taxonomy downstream would make the models unlandable.

Two rules the classes below encode:

* **Unclassifiable means unavailable, never throttled** (R12). ESPN's throttle
  signal is unconfirmed, so a failure we cannot positively classify maps to
  :class:`ProviderUnavailableError` with bounded retry. Guessing
  ``RATE_LIMITED`` would teach an agent to back off from an outage forever.
* **An error payload is not a data channel.** ``details`` is for field *names*,
  paths, and status codes — never provider bytes, response bodies, or anything
  that could carry an ``espn_s2`` value or a SWID GUID (CLAUDE.md rule 5).

Nothing here imports anything beyond the standard library, and nothing here is
allowed to import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

__all__ = [
    "ERROR_TYPES",
    "AuthExpiredError",
    "AuthMissingError",
    "ErrorCode",
    "FantasySportsError",
    "LeagueNotFoundError",
    "ProviderUnavailableError",
    "RateLimitedError",
    "SchemaDriftError",
    "error_type_for",
]


class ErrorCode(StrEnum):
    """The stable machine codes. This set is the API."""

    AUTH_MISSING = "AUTH_MISSING"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    LEAGUE_NOT_FOUND = "LEAGUE_NOT_FOUND"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"


class FantasySportsError(Exception):
    """Base class for every failure this tool reports with a machine code.

    Subclasses set :attr:`code`, :attr:`retryable`, and :attr:`agent_action`.
    ``output/`` renders :meth:`to_dict` to stderr as JSON with a nonzero exit;
    the exit-status mapping itself belongs to the output layer, not here.
    """

    code: ClassVar[ErrorCode]
    retryable: ClassVar[bool] = False
    agent_action: ClassVar[str]

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        # Copied, not aliased: a caller must not be able to mutate a rendered
        # payload after the fact, and we must not retain a reference into a
        # provider response.
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """The stable error payload. ``details`` is omitted when empty."""
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "agent_action": self.agent_action,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class AuthMissingError(FantasySportsError):
    """No credentials are configured for this provider."""

    code = ErrorCode.AUTH_MISSING
    retryable = False
    agent_action = "Ask the human to run `fantasy-sports auth login`."


class AuthExpiredError(FantasySportsError):
    """Credentials exist but the provider rejected them."""

    code = ErrorCode.AUTH_EXPIRED
    retryable = False
    agent_action = "Ask the human to re-extract their ESPN cookies."


class LeagueNotFoundError(FantasySportsError):
    """The league id is wrong, or these credentials cannot see it."""

    code = ErrorCode.LEAGUE_NOT_FOUND
    retryable = False
    agent_action = "Ask the human to confirm the league id and their access."


class ProviderUnavailableError(FantasySportsError):
    """The provider is down, timed out, or failed in a way we cannot classify.

    This is the honest landing place for an unclassifiable failure (R12) —
    bounded retry is safe, and claiming ``RATE_LIMITED`` would not be.
    """

    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = True
    agent_action = "Retry with bounded exponential backoff."


class RateLimitedError(FantasySportsError):
    """The provider explicitly throttled us.

    Only raise this on a positive throttle signal — never as a guess.
    """

    code = ErrorCode.RATE_LIMITED
    retryable = True
    agent_action = "Retry after `details.retry_after` seconds."

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after
        if retry_after is not None:
            self.details["retry_after"] = retry_after


class SchemaDriftError(FantasySportsError):
    """A response no longer has the shape we know how to read.

    This is the trigger for the canary's issue-filing and the client-side
    health check, so it must fire on *genuine* shape changes only. An absent
    optional value — a transaction with no processed date, a provider that
    does not publish slot eligibility — is ordinary data, not drift. A false
    alarm on a routine payload is the fastest way to make the signal
    ignorable.

    ``path`` records *where* the shape broke, by field name. Never put a
    provider value in it.
    """

    code = ErrorCode.SCHEMA_DRIFT
    retryable = False
    agent_action = "Stop and file an issue; the provider's response shape changed."

    def __init__(
        self,
        message: str,
        *,
        path: str | Sequence[str] | None = None,
        provider: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.path: tuple[str, ...] = (path,) if isinstance(path, str) else tuple(path or ())
        self.provider = provider
        if self.path:
            self.details["path"] = list(self.path)
        if provider is not None:
            self.details["provider"] = provider


ERROR_TYPES: tuple[type[FantasySportsError], ...] = (
    AuthMissingError,
    AuthExpiredError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)

_BY_CODE: dict[str, type[FantasySportsError]] = {cls.code.value: cls for cls in ERROR_TYPES}


def error_type_for(code: ErrorCode | str) -> type[FantasySportsError]:
    """Look up the exception class for a taxonomy code.

    Raises ``KeyError`` for anything outside the taxonomy — an unknown code is
    a programming error here, not a provider failure to be reported.
    """
    return _BY_CODE[str(code)]
