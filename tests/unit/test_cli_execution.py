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
    register(
        CommandSpec(name="ok", summary="Print ok.", handler=f"{HANDLERS}:ok", takes_league=False)
    )
    register(
        CommandSpec(
            name="boom", summary="Exit 3.", handler=f"{HANDLERS}:explicit_exit", takes_league=False
        )
    )
    register(
        CommandSpec(
            name="stop", summary="Abort.", handler=f"{HANDLERS}:aborted", takes_league=False
        )
    )
    register(
        CommandSpec(
            name="status",
            summary="Grouped.",
            handler=f"{HANDLERS}:in_a_group",
            group="auth",
            takes_league=False,
        )
    )
    register(
        CommandSpec(
            name="bare",
            summary="Bare.",
            handler=f"{HANDLERS}:raises_bare_typer_exception",
            takes_league=False,
        )
    )
    register(
        CommandSpec(
            name="notice",
            summary="Posts a notice.",
            handler=f"{HANDLERS}:posts_a_notice",
            takes_league=False,
        )
    )
    register(
        CommandSpec(
            name="noticefail",
            summary="Posts a notice, then fails.",
            handler=f"{HANDLERS}:posts_a_notice_then_fails",
            takes_league=False,
        )
    )
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(saved)


def test_app_exposes_top_level_commands_and_groups(populated_registry):
    app = build_app()
    assert {c.name for c in app.registered_commands} == {
        "ok",
        "boom",
        "stop",
        "bare",
        "notice",
        "noticefail",
    }
    assert {g.name for g in app.registered_groups} == {"auth"}


def test_a_registered_command_runs(populated_registry, capsys: pytest.CaptureFixture[str]):
    assert run(["ok"]) == 0
    assert '"ran": "ok-ran"' in capsys.readouterr().out


def test_a_grouped_command_runs(populated_registry, capsys: pytest.CaptureFixture[str]):
    assert run(["auth", "status"]) == 0
    assert '"ran": "group-ran"' in capsys.readouterr().out


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
    assert '"ran": "ok-ran"' in capsys.readouterr().out


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


def test_a_notice_a_provider_posted_reaches_any_commands_envelope_and_stderr(
    populated_registry, capsys: pytest.CaptureFixture[str]
):
    """Commands that build their own envelope (experts --league, doctor --live) report it too."""
    import json

    assert run(["notice"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["warnings"] == ["a provider repaired something"]
    assert "warning: a provider repaired something" in captured.err


def test_a_notice_from_a_failed_command_does_not_leak_into_the_next_one(
    populated_registry, capsys: pytest.CaptureFixture[str]
):
    import json

    assert run(["noticefail"]) != 0
    capsys.readouterr()
    assert run(["ok"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["warnings"] == []
    assert "warning:" not in captured.err


class TestProviderHintOnFailure:
    """A failure envelope names the provider when one is known without a read (#180).

    ``init`` and ``leagues discover`` both fail before a league is resolved —
    no credentials stored, or ``fan.api`` unreachable — so the envelope can name
    ESPN without guessing. Naming it is what lets the provider health check and
    its known-issue guidance apply to those failures; with ``provider: null``
    they are skipped and a human sees a bare error with no next step.
    """

    def test_hint_names_espn_for_init(self):
        from fantasy_sports.cli.app import _provider_hint
        from fantasy_sports.commands import REGISTRY

        assert _provider_hint(REGISTRY["init"]) == "espn"

    def test_hint_names_espn_for_leagues_discover(self):
        from fantasy_sports.cli.app import _provider_hint
        from fantasy_sports.commands import REGISTRY

        assert _provider_hint(REGISTRY["leagues discover"]) == "espn"

    def test_init_failure_envelope_names_espn(self):
        """``--season`` with ``--all-seasons`` is rejected before any read happens."""
        import json

        from typer.testing import CliRunner

        from fantasy_sports.cli.app import build_app

        result = CliRunner().invoke(build_app(), ["init", "--season", "2026", "--all-seasons"])
        assert result.stdout == ""
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "CONFIG_INVALID"
        assert payload["provider"] == "espn"

    def test_leagues_discover_failure_envelope_names_espn(self, monkeypatch):
        """No stored SWID: the failure belongs to ESPN, not to an unknown provider."""
        import json

        from typer.testing import CliRunner

        from fantasy_sports.auth import chain
        from fantasy_sports.cli.app import build_app
        from fantasy_sports.core.errors import AuthMissingError

        def no_credentials(*_args, **_kwargs):
            raise AuthMissingError("No credentials configured for: swid.")

        monkeypatch.setattr(chain, "require_credentials", no_credentials)

        result = CliRunner().invoke(build_app(), ["leagues", "discover"])
        assert result.stdout == ""
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "AUTH_MISSING"
        assert payload["provider"] == "espn"
