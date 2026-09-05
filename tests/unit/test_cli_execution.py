"""End-to-end behaviour of the typer projection and its exit codes.

Exercises the paths an agent depends on: a command runs, an explicit exit
propagates its code, an abort is distinguishable, and a usage error is a
message plus a nonzero status rather than a traceback.
"""

from __future__ import annotations

import pytest

from fantasy_sports.cli.app import build_app, run
from fantasy_sports.cli.fastpath import main_entry, render_help
from fantasy_sports.commands import REGISTRY, CommandSpec, register

HANDLERS = "_fake_commands"


@pytest.fixture
def populated_registry():
    saved = dict(REGISTRY)
    REGISTRY.clear()
    register(CommandSpec(name="ok", summary="Print ok.", handler=f"{HANDLERS}:ok"))
    register(CommandSpec(name="boom", summary="Exit 3.", handler=f"{HANDLERS}:explicit_exit"))
    register(CommandSpec(name="stop", summary="Abort.", handler=f"{HANDLERS}:aborted"))
    register(
        CommandSpec(
            name="status", summary="Grouped.", handler=f"{HANDLERS}:in_a_group", group="auth"
        )
    )
    register(
        CommandSpec(name="bare", summary="Bare.", handler=f"{HANDLERS}:raises_bare_typer_exception")
    )
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(saved)


def test_app_exposes_top_level_commands_and_groups(populated_registry):
    app = build_app()
    assert {c.name for c in app.registered_commands} == {"ok", "boom", "stop", "bare"}
    assert {g.name for g in app.registered_groups} == {"auth"}


def test_a_registered_command_runs(populated_registry, capsys: pytest.CaptureFixture[str]):
    assert run(["ok"]) == 0
    assert "ok-ran" in capsys.readouterr().out


def test_a_grouped_command_runs(populated_registry, capsys: pytest.CaptureFixture[str]):
    assert run(["auth", "status"]) == 0
    assert "group-ran" in capsys.readouterr().out


def test_explicit_exit_code_propagates(populated_registry):
    assert run(["boom"]) == 3


def test_abort_is_distinguishable_from_a_usage_error(populated_registry):
    assert run(["stop"]) == 130


def test_usage_error_is_a_message_and_a_nonzero_status(
    populated_registry, capsys: pytest.CaptureFixture[str]
):
    code = run(["no-such-command"])
    assert code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.strip()


def test_main_entry_falls_through_to_the_app(
    populated_registry, capsys: pytest.CaptureFixture[str]
):
    assert main_entry(["ok"]) == 0
    assert "ok-ran" in capsys.readouterr().out


def test_help_lists_registered_commands_and_groups(populated_registry):
    out = render_help()
    assert "ok" in out and "boom" in out
    assert "auth" in out and "status" in out
    assert "none registered yet" not in out


def test_an_exception_without_its_own_renderer_still_reaches_stderr(
    populated_registry, capsys: pytest.CaptureFixture[str]
):
    """A `TyperException` that is not a usage error must not vanish silently."""
    assert run(["bare"]) == 1
    err = capsys.readouterr().err
    assert "something went wrong upstream" in err
    assert "Traceback" not in err
