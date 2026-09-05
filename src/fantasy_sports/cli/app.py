"""The typer projection over the command registry (ADR-0003).

Nothing in this module holds business logic. It parses argv, resolves the
registered handler, and hands rendering to the output layer. Import it only
when a real command runs — :mod:`fantasy_sports.cli.fastpath` answers
``--version`` and ``--help`` without it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fantasy_sports.cli.fastpath import (
    main_entry as main_entry,  # re-exported console-script target
)


def build_app() -> Any:
    """Construct the typer app from the registry. Imports typer."""
    import typer

    from fantasy_sports import __version__
    from fantasy_sports.commands import groups, in_group, top_level

    app = typer.Typer(
        name="fantasy-sports",
        help=(
            "Agent-native CLI for fantasy sports leagues. Every payload is a versioned "
            "envelope; every failure is a machine-readable code on stderr."
        ),
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        add_completion=False,
    )

    @app.callback()
    def _root() -> None:  # pragma: no cover - typer wiring
        """fantasy-sports."""

    group_apps: dict[str, Any] = {}
    for name in groups():
        group_app = typer.Typer(help=f"{name} commands.", no_args_is_help=True)
        group_apps[name] = group_app
        app.add_typer(group_app, name=name)

    for spec in [*top_level(), *(s for g in groups() for s in in_group(g))]:
        target = group_apps[spec.group] if spec.group else app
        target.command(spec.name, help=spec.summary)(spec.resolve())

    app.info.help = f"{app.info.help}\n\nVersion {__version__}."
    return app


def run(argv: Sequence[str]) -> int:
    """Execute ``argv`` through typer, returning a process exit code.

    Only typer's public exception surface is used. ``typer`` 0.27 vendors click
    as the private ``typer._click`` and no longer declares it as a dependency,
    so ``import click`` raises ``ModuleNotFoundError`` in a normal install —
    see ``docs/memory/typer-vendors-click.md``.
    """
    import sys

    import typer

    app = build_app()
    try:
        result = app(args=list(argv), standalone_mode=False)
    except typer.Abort:
        return 130
    except typer.TyperException as exc:
        # Usage errors render themselves; anything else at least reaches stderr.
        show = getattr(exc, "show", None)
        if callable(show):
            show()
        else:
            print(str(exc), file=sys.stderr)
        return int(getattr(exc, "exit_code", 1))
    # Outside standalone mode, click *returns* `typer.Exit`'s code instead of
    # raising it, so an int result is the exit status the command asked for.
    return result if isinstance(result, int) else 0
