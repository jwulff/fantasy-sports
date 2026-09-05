"""The import budget (ADR-0008), enforced.

``--help`` and ``--version`` must not pay for an HTTP stack, a CLI framework, or
a keyring. These assertions are the reason ``cli/fastpath.py`` exists.
"""

from __future__ import annotations

import subprocess
import sys

EXPENSIVE = ("espn_api", "requests", "keyring", "typer", "click", "rich")


def _modules_after(code: str) -> set[str]:
    roots = "{m.split('.')[0] for m in sys.modules}"
    probe = f"{code}\nimport sys, json; print(json.dumps(sorted({roots})))"
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    import json

    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_importing_the_package_is_cheap():
    assert not (_modules_after("import fantasy_sports") & set(EXPENSIVE))


def test_importing_the_entry_point_is_cheap():
    assert not (_modules_after("import fantasy_sports.cli.fastpath") & set(EXPENSIVE))


def test_registry_is_typer_free():
    """ADR-0003: commands are plain functions; the CLI is a projection."""
    assert not (_modules_after("import fantasy_sports.commands") & set(EXPENSIVE))


def test_resolving_every_command_stays_free_of_the_cli_and_the_provider():
    """ADR-0003: a command is a plain function, importable without a CLI.

    Resolving all ten handlers must not drag in typer, rich, `espn_api`,
    `requests`, or `keyring`. That is the executable form of "the MCP server is
    a projection": whatever imports a command gets the command, and nothing
    else.
    """
    code = (
        "from fantasy_sports.commands import REGISTRY\n"
        "[spec.resolve() for spec in REGISTRY.values()]"
    )
    assert not (_modules_after(code) & set(EXPENSIVE))


def test_rendering_help_stays_cheap():
    code = "from fantasy_sports.cli.fastpath import render_help; render_help()"
    assert not (_modules_after(code) & set(EXPENSIVE))


def test_version_flag_does_not_import_typer():
    code = "from fantasy_sports.cli.fastpath import handle; handle(['--version'])"
    assert not (_modules_after(code) & set(EXPENSIVE))
