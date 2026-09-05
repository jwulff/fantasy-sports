"""The typer projection over the command registry (ADR-0003).

Nothing in this module holds business logic. It parses argv, resolves the
registered handler, and hands rendering to the output layer. Import it only
when a real command runs — :mod:`fantasy_sports.cli.fastpath` answers
``--version`` and ``--help`` without it.

**Callbacks are generated from the registry's declared parameters, never from a
handler's signature.** Introspecting a handler would mean importing it, and
building the app imports every registered command — which would put a provider
and an HTTP stack on a path that may only be printing a usage error. So
:class:`~fantasy_sports.commands.Param` is the single description both surfaces
read, a callback is assembled from it with :func:`inspect.Signature`, and
``spec.resolve()`` runs only once a command is actually chosen. A test asserts
each spec's declared parameters match its handler's real signature, which is
what keeps the two from drifting apart.

**Global options are accepted on both sides of the command name.**
``fantasy-sports --league dynasty standings`` is what ARCHITECTURE §7
documents; ``fantasy-sports standings --league dynasty`` is what everyone
actually types. The root callback stashes its values on the click context and
:func:`_merge_globals` prefers whatever was given after the command name.

**A handler returns an envelope; it never prints.** That is the whole of what
makes the future MCP server a projection: the same call returns the same object
there, with no stdout in the picture.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from fantasy_sports.cli.fastpath import (
    main_entry as main_entry,  # re-exported console-script target
)
from fantasy_sports.commands import GLOBAL_PARAMS, CommandSpec, Param, groups, in_group, top_level

__all__ = ["build_app", "main_entry", "run"]

_CONTEXT_ARG = "ctx"


def build_app() -> Any:
    """Construct the typer app from the registry. Imports typer."""
    import typer

    from fantasy_sports import __version__

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
    app.callback()(_root_callback())

    group_apps: dict[str, Any] = {}
    for name in groups():
        group_app = typer.Typer(help=f"{name} commands.", no_args_is_help=True)
        group_apps[name] = group_app
        app.add_typer(group_app, name=name)

    for spec in [*top_level(), *(s for g in groups() for s in in_group(g))]:
        target = group_apps[spec.group] if spec.group else app
        target.command(spec.name, help=spec.summary)(_command_callback(spec))

    app.info.help = f"{app.info.help}\n\nVersion {__version__}."
    return app


# --------------------------------------------------------------------------- #
# Callback assembly
# --------------------------------------------------------------------------- #


def _root_callback() -> Any:
    """The root callback: records global options and holds no logic at all."""

    def root(**values: Any) -> None:
        ctx = values.pop(_CONTEXT_ARG)
        ctx.obj = values

    root.__doc__ = "fantasy-sports."
    return _with_signature(root, GLOBAL_PARAMS, name="root")


def _command_callback(spec: CommandSpec) -> Any:
    """One command's typer callback: parse, delegate, exit with the status."""
    import typer

    def callback(**values: Any) -> None:
        ctx = values.pop(_CONTEXT_ARG)
        raise typer.Exit(_dispatch(spec, ctx, values))

    callback.__doc__ = spec.summary
    return _with_signature(callback, spec.cli_params, name=spec.name)


def _with_signature(func: Any, params: Sequence[Param], *, name: str) -> Any:
    """Give ``func`` the signature typer needs to build click options from.

    ``typer`` reads ``inspect.signature`` for defaults and ``get_type_hints``
    for annotations, so both are set. Real type objects are used rather than
    strings, so nothing has to be evaluated in this module's namespace.
    """
    import typer

    parameters = [
        inspect.Parameter(
            _CONTEXT_ARG, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context
        )
    ]
    annotations: dict[str, Any] = {_CONTEXT_ARG: typer.Context}
    for param in params:
        parameters.append(
            inspect.Parameter(
                param.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=typer.Option(
                    ... if param.required else param.default,
                    *param.cli_flags,
                    help=param.help,
                ),
                annotation=param.annotation,
            )
        )
        annotations[param.name] = param.annotation

    func.__signature__ = inspect.Signature(parameters)
    func.__annotations__ = annotations
    func.__name__ = name.replace("-", "_")
    return func


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def _dispatch(spec: CommandSpec, ctx: Any, values: Mapping[str, Any]) -> int:
    """Run one command and emit its envelope. Returns the process exit status."""
    import typer

    from fantasy_sports.output import emit, emit_failure, resolve_format

    merged = _merge_globals(spec, ctx, values)
    fmt = merged.pop("output", None)
    try:
        resolve_format(fmt)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--output") from exc

    try:
        envelope = spec.resolve()(**merged)
    except (typer.Exit, typer.Abort, typer.TyperException):
        raise
    except Exception as exc:  # noqa: BLE001 - classified by the taxonomy, never a traceback
        return emit_failure(exc, provider=_provider_hint(spec))
    return emit(envelope, fmt=fmt)


def _merge_globals(spec: CommandSpec, ctx: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Combine root-level and command-level options; after the command wins.

    A value that is still its declared default was not typed after the command
    name, so the root-level one — if there is one — is what the user meant.
    """
    root = getattr(ctx, "obj", None) or {}
    defaults = {param.name: param.default for param in spec.cli_params}
    merged = dict(values)
    for name, value in root.items():
        # `auth status` declares none of the read options, so a root-level
        # `--league` simply has nowhere to go on it. Skipping is right: the
        # alternative is calling a handler with an argument it never declared.
        if name in merged and merged[name] == defaults[name]:
            merged[name] = value
    return merged


def _provider_hint(spec: CommandSpec) -> str | None:
    """The provider to name on a failure, when one can be known without a read.

    Only the ``auth`` commands can answer this before resolving a league, and
    they are single-provider in v0.1. Everything else leaves it null rather
    than guessing: a ``CONFIG_INVALID`` failure happens before a provider is
    chosen, which is exactly why the envelope allows a null one.
    """
    if spec.group == "auth":
        from fantasy_sports.commands.auth import PROVIDER

        return PROVIDER
    return None


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
