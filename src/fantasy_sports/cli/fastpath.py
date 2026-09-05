"""The zero-dependency front door.

``--version`` and top-level ``--help`` are answered here, from the command
registry, without importing typer. That is not a micro-optimisation: importing
typer costs ~44 ms on its own, and ADR-0008 budgets the whole cold start at
50 ms, so an eager import cannot meet the budget on any machine. It is also the
lazy-import rule in ``CLAUDE.md`` applied to its most-used path — ``--help``
must not pay for an HTTP stack it will never use.

Help text is generated from :data:`fantasy_sports.commands.REGISTRY`, the same
source the typer app is built from, so the two surfaces cannot drift. A test
asserts they list the same commands.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_VERSION_FLAGS = frozenset({"--version", "-V"})
_HELP_FLAGS = frozenset({"--help", "-h"})

USAGE = "Usage: fantasy-sports [OPTIONS] COMMAND [ARGS]..."

_META_OPTIONS: tuple[tuple[str, str], ...] = (
    ("-V, --version", "Show the version and exit."),
    ("-h, --help", "Show this message and exit."),
)
"""The two flags this module answers itself. Everything else comes from the
registry's :data:`~fantasy_sports.commands.GLOBAL_PARAMS`, so the option list a
user reads here and the options typer actually accepts cannot drift."""


def global_options() -> tuple[tuple[str, str], ...]:
    """Every option accepted before or after a command, plus the two meta flags.

    Global options are accepted on **both** sides of the command name, which is
    why they are listed here rather than only in each command's own help.
    """
    from fantasy_sports.commands import GLOBAL_PARAMS

    return (*((p.display, p.help) for p in GLOBAL_PARAMS), *_META_OPTIONS)


def _group_summary(group: str) -> str:
    """One line naming a group's subcommands, so ``--help`` lists them all."""
    from fantasy_sports.commands import in_group

    names = [spec.name for spec in in_group(group)]
    label = "subcommand" if len(names) == 1 else "subcommands"
    return f"{len(names)} {label}: " + ", ".join(names)


def render_help() -> str:
    """Build top-level help from the registry, importing nothing expensive."""
    from fantasy_sports.commands import REGISTRY, groups, top_level

    lines = [
        USAGE,
        "",
        "  Agent-native CLI for fantasy sports leagues. Every payload is a versioned",
        "  envelope; every failure is a machine-readable code on stderr.",
        "",
        "Options (accepted before or after the command):",
    ]
    options = global_options()
    width = max(len(flag) for flag, _ in options)
    lines += [f"  {flag.ljust(width)}  {blurb}" for flag, blurb in options]

    if not REGISTRY:
        lines += ["", "Commands:", "  (none registered)"]
        return "\n".join(lines)

    entries = [(s.name, s.summary) for s in top_level()]
    entries += [(g, _group_summary(g)) for g in groups()]
    width = max(len(name) for name, _ in entries)
    lines += ["", "Commands:"]
    lines += [f"  {name.ljust(width)}  {summary}" for name, summary in sorted(entries)]
    return "\n".join(lines)


def handle(argv: Sequence[str]) -> int | None:
    """Answer ``argv`` if it is a fast path; otherwise return ``None``.

    Only *top-level* invocations qualify. ``fantasy-sports auth --help`` falls
    through to typer, which owns per-command help.
    """
    from fantasy_sports import __version__

    args = [a for a in argv if a != "--"]
    if not args:
        print(render_help())
        return 0
    if args[0] in _VERSION_FLAGS:
        print(__version__)
        return 0
    if args[0] in _HELP_FLAGS:
        print(render_help())
        return 0
    return None


def main_entry(argv: Sequence[str] | None = None) -> int:
    """Console-script target. Fast paths answer here; everything else needs typer."""
    args = list(sys.argv[1:] if argv is None else argv)
    code = handle(args)
    if code is not None:
        return code
    from fantasy_sports.cli.app import run

    return run(args)
