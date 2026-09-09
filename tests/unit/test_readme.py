"""README examples must resolve against the live command registry.

Every fenced ``bash``/``console`` block in ``README.md`` is scanned for lines
that invoke the CLI (``fantasy-sports ...``). For each one, this asserts the
command (and subcommand, for a grouped command like ``auth login``) exists in
:data:`fantasy_sports.commands.REGISTRY`, and every long option
(``--foo``) is one that command's :class:`CommandSpec.cli_params` actually
declares. This is what keeps the README from drifting the way the old one did
(#53: it advertised ``doctor`` and ``standings --league dynasty`` before
either existed).

Offline and read-only: this imports the registry (a plain dict of dataclasses,
never a provider or an HTTP stack) and reads ``README.md`` off disk. It never
imports ``typer``, never resolves a handler, and never runs a command.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from fantasy_sports.commands import REGISTRY, CommandSpec

README = Path(__file__).resolve().parents[2] / "README.md"

# Global flags typer accepts at the very front, ahead of any command name,
# that name no registered command at all.
_TOP_LEVEL_ONLY_FLAGS = {"--version", "-V", "--help", "-h"}

_FENCE_RE = re.compile(r"```(?:bash|console)\n(.*?)```", re.DOTALL)


def _command_lines() -> list[str]:
    """Every ``fantasy-sports ...`` line inside a fenced bash/console block."""
    text = README.read_text()
    lines = []
    for body in _FENCE_RE.findall(text):
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("$ "):
                line = line[2:].strip()
            if line.startswith("fantasy-sports"):
                lines.append(line)
    return lines


def _long_flags(spec: CommandSpec) -> set[str]:
    """Every ``--flag`` this command's CLI projection declares, plus ``--help``."""
    flags = {"--help"}
    for param in spec.cli_params:
        flags.update(flag for flag in param.cli_flags if flag.startswith("--"))
    return flags


def _resolve(rest: list[str], line: str) -> tuple[CommandSpec | None, int]:
    """Match ``rest`` (tokens after ``fantasy-sports``) against the registry.

    Returns the matched spec and how many leading tokens named it, or
    ``(None, 0)`` for a bare top-level flag like ``--version``.
    """
    if rest and rest[0] in _TOP_LEVEL_ONLY_FLAGS:
        return None, 1

    two_word = " ".join(rest[:2]) if len(rest) >= 2 else None
    if two_word in REGISTRY:
        return REGISTRY[two_word], 2
    if rest and rest[0] in REGISTRY:
        return REGISTRY[rest[0]], 1

    pytest.fail(
        f"{rest[0] if rest else '<empty>'!r} is not a registered command "
        f"(from README line: {line!r}); registered: {sorted(REGISTRY)}"
    )


COMMAND_LINES = _command_lines()


def test_readme_has_command_examples():
    """A regression guard: if this hits zero, the extraction regex broke, not
    that the README stopped giving examples."""
    assert len(COMMAND_LINES) >= 5


@pytest.mark.parametrize("line", COMMAND_LINES)
def test_readme_command_resolves_against_registry(line: str):
    tokens = shlex.split(line)
    assert tokens[0] == "fantasy-sports"
    rest = tokens[1:]
    assert rest, f"empty invocation: {line!r}"

    spec, consumed = _resolve(rest, line)
    if spec is None:
        # A bare top-level flag (--version/--help): nothing further to check.
        return

    allowed = _long_flags(spec)
    for token in rest[consumed:]:
        if not token.startswith("--"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in allowed, (
            f"{flag!r} is not declared for {spec.invocation!r} "
            f"(from README line: {line!r}); declared: {sorted(allowed)}"
        )
