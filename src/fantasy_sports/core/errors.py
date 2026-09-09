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
* **``remediation`` is a first-class payload key, not a detail.** It is the part
  a caller acts on, and a key that rides in a general-purpose bag is a key some
  consumers will never read. ``agent_action`` is the class-level instruction
  ("ask the human to re-auth"); ``remediation`` is the *instance's* concrete
  next step, naming the environment variable or the file that would actually fix
  this failure. Promoted from ``details`` in the U5 output layer, on the
  decision recorded on jwulff/fantasy-sports#6; ADR-0004 amended to match.
* **The base class scrubs, so no raise site has to remember to.** Every
  message and every ``details`` value passes through
  :func:`~fantasy_sports.core.redaction.redact` at construction. The leak this
  defends against is written by a caller — ``raise ProviderUnavailableError(f"GET {url}")``
  where ``url`` carries ``?espn_s2=…`` — and raise sites are where scrubbing
  gets forgotten (``docs/memory/credential-leak-channels.md``).

Nothing here imports anything beyond the standard library, and nothing here is
allowed to import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from fantasy_sports.core.redaction import redact, scrub

__all__ = [
    "ERROR_TYPES",
    "AuthExpiredError",
    "AuthMissingError",
    "ConfigInvalidError",
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
    CONFIG_INVALID = "CONFIG_INVALID"
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

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # Scrub at construction, not at render. Once the message is stored
        # redacted there is no path — args, str(), repr(), traceback, payload —
        # that can put the value back. This is the guarantee that used to live
        # on `auth.chain.AuthError`; it belongs here so that every error type
        # has it rather than only the auth ones.
        message = redact(message)
        super().__init__(message)
        self.message = message
        self.remediation = redact(remediation) if remediation else None
        # Copied, not aliased: a caller must not be able to mutate a rendered
        # payload after the fact, and we must not retain a reference into a
        # provider response.
        self.details: dict[str, Any] = {
            key: scrub(value) for key, value in dict(details or {}).items()
        }

    def _record_detail(self, key: str, value: Any) -> None:
        """Add one detail post-``super().__init__``, scrubbed like the rest.

        Subclasses that derive a detail from their own arguments go through
        here so nothing reaches :meth:`to_dict` unscrubbed.
        """
        self.details[key] = scrub(value)

    def to_dict(self) -> dict[str, Any]:
        """The stable error payload. Every key is always present.

        ``remediation`` and ``details`` are ``None`` rather than absent when
        empty. A key a consumer *may* have to look for is a key some consumers
        will not look for, and the whole point of the taxonomy is that a
        failure tells its caller what to do next.
        """
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "agent_action": self.agent_action,
            "remediation": self.remediation,
            "details": dict(self.details) if self.details else None,
        }


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


class ConfigInvalidError(FantasySportsError):
    """``config.toml`` will not parse, or an argument only the provider can
    validate came back invalid.

    Distinct from :class:`LeagueNotFoundError` on purpose, and the distinction
    is the whole reason the code was added (decision on
    jwulff/fantasy-sports#6). ``LEAGUE_NOT_FOUND`` tells an agent to retry with
    a different ``--league``, which cannot possibly work when the file itself
    will not parse. This is user-fixable and not retryable: a human changes
    what they gave us, and nothing the agent does on its own changes the
    outcome.

    **Two causes, one code (decided on jwulff/fantasy-sports#48, ADR-0004
    amended by ADR-0009).** A config file that will not parse and a CLI
    argument ESPN itself rejects — a ``--pos`` it does not recognise, a
    ``--filter`` that is not JSON, a ``raw`` with no ``--view`` — read
    identically to an agent: not retryable, a human must change the input
    named in the message. Adding a second code for the second cause would
    duplicate that instruction under a new name. ``kind`` records which one
    this instance is, for a consumer that wants to log or branch on the
    distinction without needing a second exit status to do it; it never
    changes ``retryable`` or the exit code.
    """

    code = ErrorCode.CONFIG_INVALID
    retryable = False
    agent_action = (
        "Ask the human to fix the input named in the message — a config value or a "
        "command argument. Retrying unchanged cannot work."
    )

    def __init__(
        self,
        message: str,
        *,
        kind: str = "config",
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.kind = kind
        self._record_detail("kind", kind)


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
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.retry_after = retry_after
        if retry_after is not None:
            self._record_detail("retry_after", retry_after)


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
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.path: tuple[str, ...] = (path,) if isinstance(path, str) else tuple(path or ())
        self.provider = provider
        if self.path:
            self._record_detail("path", list(self.path))
        if provider is not None:
            self._record_detail("provider", provider)


ERROR_TYPES: tuple[type[FantasySportsError], ...] = (
    AuthMissingError,
    AuthExpiredError,
    LeagueNotFoundError,
    ConfigInvalidError,
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
