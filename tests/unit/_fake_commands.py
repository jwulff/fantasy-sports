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
