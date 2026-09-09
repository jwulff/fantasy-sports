"""The typer projection, end to end, through ``CliRunner``.

``tests/unit/test_commands.py`` proves the handlers are right. This file proves
the *projection* is right: that typing a command reaches the registered
function, that the envelope on stdout is the one an agent will parse, that a
failure is JSON on stderr with an empty stdout, and that a bad argument is a
usage error rather than either of those.

Nothing here re-checks a payload's contents. A test that asserts on team names
through the CLI is a slower copy of a unit test, and it goes red for reasons
that have nothing to do with the CLI.

The plan calls this file ``tests/integration/test_cli.py``. It is not named
that because ``tests/unit/test_cli.py`` already exists, and pytest's default
import mode requires test-module basenames to be unique across directories
that have no ``__init__.py`` — the collision is a collection error, not a
skipped test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import LEAGUE_ID, SEASON, RecordedEspn, install_espn, isolate_home
from typer.testing import CliRunner

from fantasy_sports.cli.app import build_app
from fantasy_sports.cli.fastpath import render_help
from fantasy_sports.commands import GLOBAL_PARAMS, REGISTRY
from fantasy_sports.core.errors import ErrorCode
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output import EXIT_CODES
from fantasy_sports.output.errors import EXIT_USAGE

ENVELOPE_KEYS = [
    "schema",
    "provider",
    "league_id",
    "season",
    "generated_at",
    "data_as_of",
    "data_age_seconds",
    "sources",
    "untrusted",
    "raw_omitted",
    "data",
    "error",
]
"""Key order is part of the contract (ADR-0004). Asserted as a list, not a set."""

#: One invocation per registered read command, in the form a user types.
INVOCATIONS: tuple[tuple[str, list[str]], ...] = (
    ("league info", ["league", "info"]),
    ("teams", ["teams"]),
    ("standings", ["standings"]),
    ("roster", ["roster", "--team", "1"]),
    ("matchups", ["matchups", "--week", "2"]),
    ("transactions", ["transactions", "--limit", "5"]),
    ("free-agents", ["free-agents", "--limit", "2"]),
    ("raw", ["raw", "--view", "mDraftDetail"]),
)


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> RecordedEspn:
    return install_espn(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def cli() -> Any:
    return CliRunner()


#: A rich-rendered help panel is not a string you can search. Rich splits a
#: single option name across escape runs — `--league` arrives as
#: `ESC[1;36m-ESC[0mESC[1;36m-leagueESC[0m` — and wraps it at the terminal
#: width. Both depend on the environment, so a bare `"--league" in output`
#: passes on a laptop (no colour, wide terminal) and fails in CI, for reasons
#: that have nothing to do with the CLI.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Belt to the ANSI-stripping braces: ask rich for no colour and a wide
#: terminal so the raw output is closer to readable in a failure message.
_RUNNER_ENV = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb", "FORCE_COLOR": ""}


def _invoke(cli: Any, args: list[str]) -> Any:
    return cli.invoke(build_app(), args, env=_RUNNER_ENV)


def _searchable(output: str) -> str:
    """Help or usage output with colour removed and wrapping collapsed."""
    return re.sub(r"\s+", "", _ANSI.sub("", output))


def _raw_key_paths(value: Any, path: str = "$") -> list[str]:
    """Every JSON path under ``value`` whose key is literally ``raw``."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            if key == "raw":
                found.append(here)
            found.extend(_raw_key_paths(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_raw_key_paths(item, f"{path}[{index}]"))
    return found


# --------------------------------------------------------------------------- #
# The envelope an agent parses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("command", "argv"), INVOCATIONS, ids=[name for name, _ in INVOCATIONS])
def test_each_command_emits_the_exact_json_envelope(
    cli: Any, espn: RecordedEspn, command: str, argv: list[str]
):
    result = _invoke(cli, argv)
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert list(payload) == ENVELOPE_KEYS
    assert payload["schema"] == "fantasy-sports/v1"
    assert payload["provider"] == "espn"
    assert payload["league_id"] == LEAGUE_ID
    assert payload["season"] == SEASON
    assert payload["error"] is None
    # Contents (which commands label which paths) are a unit-test concern --
    # tests/unit/test_commands.py::test_teams_labels_each_teams_name_and_owners_as_untrusted
    # and friends -- this file only proves the key survives the CLI projection.
    assert isinstance(payload["untrusted"], dict)
    assert payload["data"] is not None


def test_output_table_is_a_table_and_output_csv_is_rows(cli: Any, espn: RecordedEspn):
    table = _invoke(cli, ["teams", "--output", "table"])
    assert table.exit_code == 0
    assert "league 99" in table.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(table.stdout)

    rows = _invoke(cli, ["teams", "--output", "csv"])
    assert rows.exit_code == 0
    assert rows.stdout.splitlines()[0].startswith("provider,")


def test_an_unknown_output_format_is_a_usage_error_not_a_taxonomy_failure(
    cli: Any, espn: RecordedEspn
):
    """Bad arguments are the CLI's own failure mode, and exit 2 is reserved for it."""
    result = _invoke(cli, ["teams", "--output", "yaml"])
    assert result.exit_code == EXIT_USAGE
    assert "yaml" in _searchable(result.output)
    assert espn.calls == [], "a bad format must be caught before the provider runs"


# --------------------------------------------------------------------------- #
# Global options, on both sides of the command name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["--league", "synthetic", "teams"],
        ["teams", "--league", "synthetic"],
        ["-l", "synthetic", "teams"],
    ],
    ids=["before", "after", "short"],
)
def test_league_is_accepted_before_or_after_the_command(
    cli: Any, espn: RecordedEspn, argv: list[str]
):
    """ARCHITECTURE §7 documents the first form; everyone types the second."""
    result = _invoke(cli, argv)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["league_id"] == LEAGUE_ID


def test_a_value_after_the_command_wins_over_one_before_it(cli: Any, espn: RecordedEspn):
    result = _invoke(cli, ["--season", "1999", "teams", "--season", str(SEASON)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["season"] == SEASON


@pytest.mark.parametrize("flag", ["--fresh", "--no-cache"])
def test_the_cache_flags_are_accepted_by_every_read_command(
    cli: Any, espn: RecordedEspn, flag: str
):
    for _, argv in INVOCATIONS:
        result = _invoke(cli, [*argv, flag])
        assert result.exit_code == 0, f"{argv} {flag}: {result.output}"


def test_a_repeated_view_reaches_the_provider_as_several_requests(cli: Any, espn: RecordedEspn):
    result = _invoke(cli, ["raw", "--view", "mDraftDetail", "--view", "mMatchupScore"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert list(data) == ["mDraftDetail", "mMatchupScore"]
    assert [source["name"] for source in json.loads(result.stdout)["sources"]] == [
        "mDraftDetail",
        "mMatchupScore",
    ]


# --------------------------------------------------------------------------- #
# `--no-raw` (jwulff/fantasy-sports#52)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [["--no-raw", "roster", "--team", "1"], ["roster", "--team", "1", "--no-raw"]],
    ids=["before", "after"],
)
def test_no_raw_is_accepted_before_or_after_the_command(
    cli: Any, espn: RecordedEspn, argv: list[str]
):
    result = _invoke(cli, argv)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["raw_omitted"] is True
    assert _raw_key_paths(payload["data"]) == []


def test_no_raw_strips_a_roster_but_keeps_the_fields_that_matter(cli: Any, espn: RecordedEspn):
    """jwulff/fantasy-sports#52's acceptance criterion, field by field."""
    result = _invoke(cli, ["roster", "--team", "1", "--no-raw"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["raw_omitted"] is True
    assert _raw_key_paths(payload["data"]) == []

    slot = payload["data"][0]
    assert slot["slot"]
    player = slot["player"]
    assert player["provider_id"]
    assert player["name"]
    assert player["position"]
    assert player["pro_team"]
    assert player["projected_points"] is not None


def test_no_raw_makes_a_roster_read_much_smaller(cli: Any, espn: RecordedEspn):
    """The issue's size claim, measured against the synthetic fixture.

    The issue's own numbers are two orders of magnitude on a real 15-player
    ESPN roster (~530 KB, normalized fields under 1%). This fixture has three
    players and a fixed envelope wrapper (schema/sources/etc.) that `--no-raw`
    does not touch, so that wrapper is a much larger share of a small payload
    — the bar here is set to what this fixture can honestly demonstrate, well
    below the issue's real-world ratio, not at it.
    """
    full = _invoke(cli, ["roster", "--team", "1"])
    stripped = _invoke(cli, ["roster", "--team", "1", "--no-raw"])
    assert full.exit_code == 0
    assert stripped.exit_code == 0
    assert len(stripped.stdout) * 5 < len(full.stdout), (len(stripped.stdout), len(full.stdout))


def test_no_raw_strips_a_box_scores_response_including_nested_lineups(cli: Any, espn: RecordedEspn):
    """A box score's own `raw` is separate from each lineup entry's `raw`."""
    result = _invoke(cli, ["box-scores", "--week", "1", "--no-raw"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["raw_omitted"] is True
    assert _raw_key_paths(payload["data"]) == []
    assert payload["data"][0]["team_a_lineup"][0]["player_name"]


def test_no_raw_leaves_the_visible_table_rows_unaffected(cli: Any, espn: RecordedEspn):
    """The table already strips `raw` unconditionally (ADR-0004); the rows a
    human reads do not change either way.

    The one real difference is the omission notice: without ``--no-raw`` the
    table drops `raw` on its own account and says so; with it, `raw` is
    already gone by the time the table renders, so there is nothing left for
    the table to report dropping. That is correct, not a bug — asserted here
    so it cannot regress silently.

    Both calls pass ``--no-cache``: a second live invocation in this process
    would otherwise be a cache hit for some of `teams`' sources and not
    others, which changes the ``sources:`` footer for reasons that have
    nothing to do with ``--no-raw``.
    """
    without_flag = _invoke(cli, ["teams", "--output", "table", "--no-cache"])
    with_flag = _invoke(cli, ["teams", "--output", "table", "--no-cache", "--no-raw"])
    assert without_flag.exit_code == 0
    assert with_flag.exit_code == 0
    assert "`raw` omitted" in without_flag.stdout
    assert "`raw` omitted" not in with_flag.stdout

    def _rows(output: str) -> list[str]:
        return [line for line in output.splitlines() if "Team" in line]

    assert _rows(without_flag.stdout) == _rows(with_flag.stdout)
    assert _rows(without_flag.stdout), "the fixture must actually produce rows to compare"


def test_raw_command_ignores_no_raw(cli: Any, espn: RecordedEspn):
    """`raw` is passthrough by definition (ARCHITECTURE §5); the flag is a no-op on it."""
    without_flag = _invoke(cli, ["raw", "--view", "mDraftDetail"])
    with_flag = _invoke(cli, ["raw", "--view", "mDraftDetail", "--no-raw"])
    assert without_flag.exit_code == 0
    assert with_flag.exit_code == 0
    assert json.loads(with_flag.stdout)["data"] == json.loads(without_flag.stdout)["data"]
    assert json.loads(with_flag.stdout)["raw_omitted"] is False


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


def test_a_taxonomy_failure_is_json_on_stderr_with_an_empty_stdout(
    cli: Any, monkeypatch: pytest.MonkeyPatch
):
    """An agent piping stdout into a parser must never receive half a payload."""
    monkeypatch.setattr(
        "fantasy_sports.commands.context.PROVIDERS",
        {"nonesuch": "_fake_provider:AuthExpiredProvider"},
    )
    result = _invoke(cli, ["teams", "--league", "broken"])

    assert result.exit_code == EXIT_CODES[ErrorCode.AUTH_EXPIRED]
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert list(payload) == ENVELOPE_KEYS
    assert payload["data"] is None
    assert payload["error"]["code"] == "AUTH_EXPIRED"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["agent_action"]
    assert "Traceback" not in result.stderr


def test_schema_drift_propagates_its_path_instead_of_being_swallowed(
    cli: Any, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "fantasy_sports.commands.context.PROVIDERS",
        {"nonesuch": "_fake_provider:DriftingProvider"},
    )
    result = _invoke(cli, ["standings", "--league", "broken"])

    assert result.exit_code == EXIT_CODES[ErrorCode.SCHEMA_DRIFT]
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "SCHEMA_DRIFT"
    assert error["details"]["path"] == ["teams", "record", "overall", "wins"]


def test_an_unclassified_crash_becomes_availability_never_a_traceback(
    cli: Any, monkeypatch: pytest.MonkeyPatch
):
    """R12: what we cannot classify is retryable, and never `RATE_LIMITED`."""
    monkeypatch.setattr(
        "fantasy_sports.commands.context.PROVIDERS",
        {"nonesuch": "_fake_provider:CrashingProvider"},
    )
    result = _invoke(cli, ["teams", "--league", "broken"])

    assert result.exit_code == EXIT_CODES[ErrorCode.PROVIDER_UNAVAILABLE]
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert "Traceback" not in result.stderr


def test_an_unknown_league_exits_with_its_own_status(cli: Any, espn: RecordedEspn):
    result = _invoke(cli, ["standings", "--league", "nope"])
    assert result.exit_code == EXIT_CODES[ErrorCode.LEAGUE_NOT_FOUND]
    assert result.stdout == ""
    assert espn.calls == []


def test_a_missing_required_option_is_a_usage_error(cli: Any, espn: RecordedEspn):
    result = _invoke(cli, ["roster"])
    assert result.exit_code == EXIT_USAGE
    assert "--team" in _searchable(result.output)


# --------------------------------------------------------------------------- #
# Help, which is the only documentation an agent gets
# --------------------------------------------------------------------------- #


def test_the_two_help_surfaces_list_the_same_commands(cli: Any):
    """One registry, two renderings. A command becomes visible by registration."""
    fast = render_help()
    typed = _searchable(_invoke(cli, ["--help"]).output)
    for spec in REGISTRY.values():
        surface = (
            typed
            if spec.group is None
            else _searchable(_invoke(cli, [spec.group, "--help"]).output)
        )
        assert spec.name in surface, f"{spec.invocation} is missing from typer's help"
        assert spec.name in fast or spec.group in fast


def test_every_declared_option_appears_in_its_command_help(cli: Any):
    for spec in REGISTRY.values():
        flat = _searchable(_invoke(cli, [*spec.path, "--help"]).output)
        for param in spec.cli_params:
            assert param.cli_flags[0] in flat, f"{spec.invocation}: {param.name} undocumented"


def test_the_root_help_documents_every_global_option(cli: Any):
    flat = _searchable(_invoke(cli, ["--help"]).output)
    for param in GLOBAL_PARAMS:
        assert param.cli_flags[0] in flat


def test_a_failure_before_a_league_is_resolved_still_names_the_provider(
    cli, monkeypatch: pytest.MonkeyPatch
):
    """`auth` is single-provider in v0.1, so its failures can say which one.

    A read command deliberately cannot: the provider is a property of the
    league profile, and a `CONFIG_INVALID` failure happens before one is
    chosen. The envelope allows a null provider for exactly that case.
    """
    monkeypatch.setattr(
        "fantasy_sports.auth.staleness.build_auth_status",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("state file is unreadable")),
    )
    result = _invoke(cli, ["auth", "status"])
    payload = json.loads(result.stderr)
    assert payload["provider"] == "espn"
    assert payload["league_id"] is None

    unresolved = _invoke(cli, ["standings", "--league", "nope"])
    assert json.loads(unresolved.stderr)["provider"] is None


def test_a_root_option_a_command_does_not_declare_is_simply_ignored(cli, espn):
    """`auth status` reads no league, so `--league` before it has nowhere to go."""
    result = _invoke(cli, ["--league", "synthetic", "auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["league_id"] is None
