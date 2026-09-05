"""Providers that fail in specific, taxonomy-shaped ways.

The command layer's job on a failure is to *not get in the way*: a code the
adapter chose must reach stderr with its details intact, and nothing may reach
stdout. Proving that needs a provider that fails on demand, which no cassette
can supply — a recording of a 401 records the absence of a credential, which is
indistinguishable from a recording where the scrub ran
(``tests/unit/test_espn_provider.py``).

These also stand in for "some other provider", which is the only way to check
that :data:`fantasy_sports.commands.context.PROVIDERS` is a real indirection
rather than an ESPN import with a dict around it.
"""

from __future__ import annotations

from typing import Any

from fantasy_sports.auth.chain import ESPN_CREDENTIALS
from fantasy_sports.core.errors import AuthExpiredError, SchemaDriftError


class _Base:
    """Enough of the Provider Protocol for a command to call one method."""

    name = "fake"

    def __init__(self, credentials: Any = None, **_: Any) -> None:
        self.credentials = credentials
        self.last_fetch = None

    def credential_specs(self) -> list[Any]:
        return list(ESPN_CREDENTIALS)

    def _fail(self) -> Any:
        raise NotImplementedError

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class AuthExpiredProvider(_Base):
    """Every read reports that the stored credentials were rejected."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise AuthExpiredError(
            "ESPN rejected the stored cookies.",
            remediation="Re-extract the cookies and run `fantasy-sports auth login`.",
        )

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class DriftingProvider(_Base):
    """Every read reports a shape change, carrying the offending field path."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise SchemaDriftError(
            "ESPN's mTeam response no longer carries the field we read.",
            path=["teams", "record", "overall", "wins"],
            provider="espn",
            details={"view": "mTeam"},
        )

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class CrashingProvider(_Base):
    """Every read raises something the taxonomy has never heard of."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("a bug nobody classified")

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail
