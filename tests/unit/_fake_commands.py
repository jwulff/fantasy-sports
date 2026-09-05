"""Handlers used by the CLI execution tests.

They live in their own module because the registry resolves handlers by dotted
path, which is the behaviour under test.
"""

from __future__ import annotations


def ok() -> None:
    print("ok-ran")


def explicit_exit() -> None:
    import typer

    raise typer.Exit(code=3)


def aborted() -> None:
    import typer

    raise typer.Abort()


def in_a_group() -> None:
    print("group-ran")


def raises_bare_typer_exception() -> None:
    import typer

    raise typer.TyperException("something went wrong upstream")


def emit_envelope() -> None:
    """Emit a success envelope through the output layer and exit with its status.

    Used by `test_output.py` to prove the exact JSON shape through a real typer
    invocation. `cli/app.py` is wired to the output layer by
    jwulff/fantasy-sports#9, not here.
    """
    from datetime import UTC, datetime

    import typer

    from fantasy_sports.output import emit
    from fantasy_sports.output.envelope import Envelope

    raise typer.Exit(
        emit(
            Envelope.success(
                provider="espn",
                data=[{"name": "Team Chaos", "wins": 8}],
                league_id="123456",
                season=2026,
                generated_at=datetime(2026, 8, 26, 18, 4, 11, tzinfo=UTC),
            ),
            fmt="json",
        )
    )


def emit_failure() -> None:
    """Emit a taxonomy failure: JSON on stderr, empty stdout, nonzero exit."""
    import typer

    from fantasy_sports.core.errors import AuthExpiredError
    from fantasy_sports.output import emit
    from fantasy_sports.output.envelope import Envelope

    raise typer.Exit(
        emit(
            Envelope.failure(
                AuthExpiredError(
                    "ESPN rejected the stored cookies.",
                    remediation="Re-extract the cookies and run `auth login`.",
                ),
                provider="espn",
            )
        )
    )
