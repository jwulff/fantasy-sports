"""The command-line surface behaves for a human and for an agent."""

from __future__ import annotations

import subprocess
import sys

import pytest

from fantasy_sports import __version__
from fantasy_sports.cli.fastpath import handle, render_help


def _run_entry_point(args_literal: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real console-script target in a fresh interpreter."""
    code = (
        "from fantasy_sports.cli.app import main_entry; "
        f"raise SystemExit(main_entry({args_literal}))"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]):
    assert handle(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_short_version_flag_matches(capsys: pytest.CaptureFixture[str]):
    assert handle(["-V"]) == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_help_paths_print_usage_and_options(argv: list[str], capsys: pytest.CaptureFixture[str]):
    assert handle(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("Usage: fantasy-sports")
    assert "--league" in out and "--output" in out


def test_a_real_command_is_not_a_fast_path():
    """Anything that is not version or top-level help falls through to typer."""
    assert handle(["standings"]) is None
    assert handle(["auth", "--help"]) is None


def test_help_lists_exactly_what_is_registered():
    """The two surfaces are generated from one registry, so they cannot drift."""
    from fantasy_sports.commands import REGISTRY, groups, top_level

    out = render_help()
    for spec in top_level():
        assert spec.name in out
    for group in groups():
        assert group in out
    if not REGISTRY:
        assert "none registered yet" in out


def test_entry_point_is_installed_and_fast_path_works():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fantasy_sports.cli.app import main_entry; "
            "raise SystemExit(main_entry(['--version']))",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == __version__
    assert "Traceback" not in proc.stderr


def test_unknown_command_exits_nonzero_without_a_traceback():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fantasy_sports.cli.app import main_entry; "
            "raise SystemExit(main_entry(['no-such-command']))",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr


def test_typer_app_builds_from_the_registry():
    from fantasy_sports.cli.app import build_app
    from fantasy_sports.commands import groups

    app = build_app()
    assert {g.name for g in app.registered_groups} == set(groups())
