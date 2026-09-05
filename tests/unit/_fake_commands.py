"""Handlers used by the CLI execution tests.

They live in their own module because the registry resolves handlers by dotted
path, which is the behaviour under test.

Every handler here has the shape a real command has: **it returns an envelope
and never prints**, or it raises. Rendering belongs to ``cli/app.py``, which is
what the tests around these are checking. The two that raise ``typer``
exceptions are the exception — they exist to prove that the projection lets
typer's own control flow through untouched rather than classifying it as a
provider failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fantasy_sports.output.envelope import Envelope


def _envelope(marker: str) -> Envelope:
    return Envelope.success(
        provider="espn",
        data=[{"ran": marker}],
        league_id="123456",
        season=2026,
        generated_at=datetime(2026, 8, 26, 18, 4, 11, tzinfo=UTC),
    )


def ok() -> Envelope:
    return _envelope("ok-ran")


def in_a_group() -> Envelope:
    return _envelope("group-ran")


def explicit_exit() -> Any:
    import typer

    raise typer.Exit(code=3)


def aborted() -> Any:
    import typer

    raise typer.Abort()


def raises_bare_typer_exception() -> Any:
    import typer

    raise typer.TyperException("something went wrong upstream")


def emit_envelope() -> Envelope:
    """The fixed success envelope `test_output.py` asserts the exact shape of."""
    return Envelope.success(
        provider="espn",
        data=[{"name": "Team Chaos", "wins": 8}],
        league_id="123456",
        season=2026,
        generated_at=datetime(2026, 8, 26, 18, 4, 11, tzinfo=UTC),
    )


def raises_auth_expired() -> Any:
    """A taxonomy failure: JSON on stderr, empty stdout, nonzero exit."""
    from fantasy_sports.core.errors import AuthExpiredError

    raise AuthExpiredError(
        "ESPN rejected the stored cookies.",
        remediation="Re-extract the cookies and run `auth login`.",
    )


def returns_the_wrong_shape() -> Envelope:
    """A success envelope whose ``data`` is neither a list nor a mapping."""
    return Envelope.success(provider="espn", data="not a collection")
