"""The read commands, offline.

Every test here calls a **handler** — the plain typed function in the registry —
rather than the CLI, because that is what ADR-0003 says the command *is*. The
typer projection over these same functions is covered in
``tests/integration/test_cli.py``; if a behaviour can be checked at this level
it is checked here, so a failure names the command rather than the parser.

Upstream is served by ``tests/cassettes/espn/synthetic_2026.yaml`` through a
stub on ``requests.get`` rather than through vcrpy. The reason is
``transactions``: the walk backward asks ``mTransactions2`` about scoring
periods the fixture does not record, and a cassette can only answer "miss". A
period ESPN has no transactions for is a **200 with an empty result**, not a
failure (``docs/memory/espn-api-is-a-shape-reader-not-a-client.md``), so the
stub synthesises exactly that and refuses everything else. It also counts
requests, which is how the upstream-call cap is asserted at all.

``pytest-socket`` is on, so a request the stub does not answer fails loudly
instead of quietly reaching ESPN.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    FAKE_S2,
    FAKE_SWID,
    LEAGUE_ID,
    SEASON,
    RecordedEspn,
    install_espn,
    isolate_home,
)

from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands import auth as auth_commands
from fantasy_sports.commands import free_agents as free_agents_command
from fantasy_sports.commands import league as league_commands
from fantasy_sports.commands import matchups as matchups_command
from fantasy_sports.commands import raw as raw_command
from fantasy_sports.commands import roster as roster_command
from fantasy_sports.commands import transactions as transactions_command
from fantasy_sports.commands.context import CACHE_VARIANT_KEYS, DataShapeError, open_read
from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    LeagueNotFoundError,
    SchemaDriftError,
)
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output import OutputFormat, render


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> RecordedEspn:
    """The stub transport, installed on ``requests.get``."""
    return install_espn(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private XDG tree, synthetic credentials, and a two-league config file."""
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


def _plain(envelope: Any) -> dict[str, Any]:
    return envelope.to_dict()


# --------------------------------------------------------------------------- #
# Every command answers, in both modes, with the declared `data` shape
# --------------------------------------------------------------------------- #


def _call(name: str, **kwargs: Any) -> Any:
    return REGISTRY[name].resolve()(**kwargs)


READS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("league info", {}),
    ("teams", {}),
    ("standings", {}),
    ("roster", {"team": "1"}),
    ("matchups", {}),
    ("transactions", {"limit": 5}),
    ("free-agents", {"limit": 2}),
    ("raw", {"view": ["mDraftDetail"]}),
)


@pytest.mark.parametrize(("command", "kwargs"), READS, ids=[name for name, _ in READS])
def test_every_read_command_answers_with_its_declared_data_shape(
    espn: RecordedEspn, command: str, kwargs: dict[str, Any]
):
    """The shape contract, per command, against real recorded payloads.

    ``data`` is a list for a collection and a mapping for a single object. This
    is the assertion that turns that from a convention into a contract: the
    first consumer (``jwulff/league-gazette``) parses this output and branches
    on it.
    """
    envelope = _call(command, **kwargs)
    payload = _plain(envelope)

    assert payload["schema"] == "fantasy-sports/v1"
    assert payload["provider"] == "espn"
    assert payload["league_id"] == LEAGUE_ID
    assert payload["season"] == SEASON
    assert payload["error"] is None
    assert payload["sources"], "every read reports the fetches that produced it"

    expected = REGISTRY[command].shape
    if expected is DataShape.COLLECTION:
        assert isinstance(payload["data"], list)
    else:
        assert isinstance(payload["data"], dict)


@pytest.mark.parametrize(("command", "kwargs"), READS, ids=[name for name, _ in READS])
@pytest.mark.parametrize("fmt", list(OutputFormat))
def test_every_read_command_renders_in_every_mode(
    espn: RecordedEspn, command: str, kwargs: dict[str, Any], fmt: OutputFormat
):
    """JSON, table, and CSV all render without reaching inside `data`."""
    rendered = render(_call(command, **kwargs), fmt)
    assert rendered.strip(), f"{command} rendered nothing as {fmt}"
    if fmt is OutputFormat.JSON:
        assert json.loads(rendered)["schema"] == "fantasy-sports/v1"
    if fmt is OutputFormat.TABLE:
        assert "league 99" in rendered


def test_a_command_whose_data_disagrees_with_its_shape_is_refused(espn: RecordedEspn):
    """The contract is enforced at construction, not only in this test file."""
    from fantasy_sports.commands.context import require_shape

    with pytest.raises(DataShapeError, match="must be a list"):
        require_shape({"not": "a list"}, command="teams")
    with pytest.raises(DataShapeError, match="must be a mapping"):
        require_shape([1, 2], command="league info")


# --------------------------------------------------------------------------- #
# The individual commands
# --------------------------------------------------------------------------- #


def test_league_info_carries_the_roster_slot_configuration(espn: RecordedEspn):
    data = league_commands.info().data
    assert data["name"] == "Synthetic Test League"
    assert data["team_count"] == 2
    assert data["current_week"] == 2
    # R3a: a legal target lineup must be constructible from normalized output.
    assert data["roster_slots"]["QB"] == 1
    assert data["roster_slots"]["BE"] == 5


def test_teams_lists_every_team_with_plural_owners(espn: RecordedEspn):
    data = league_commands.teams().data
    assert [team["provider_id"] for team in data] == ["1", "2"]
    # Real name over the account handle "alpha" (jwulff/fantasy-sports#29).
    assert data[0]["owner_names"] == ["Ann Alpha"]


def test_standings_are_ranked_and_not_merely_teams_reordered(espn: RecordedEspn):
    data = league_commands.standings().data
    assert [team["standing"] for team in data] == [1, 2]
    assert data[0]["wins"] > data[1]["wins"]


@pytest.mark.parametrize(
    "wanted",
    ["1", "Team Alpha", "team alpha", "Alpha", "alph"],
    ids=["id", "exact-name", "case-folded", "substring", "prefix-fragment"],
)
def test_roster_team_accepts_an_id_or_a_name(espn: RecordedEspn, wanted: str):
    data = roster_command.roster(team=wanted).data
    assert data, "Team Alpha has a roster"
    assert all(slot["provider"] == "espn" for slot in data)


def test_a_roster_slot_carries_what_a_lineup_decision_needs(espn: RecordedEspn):
    slot = roster_command.roster(team="1").data[0]
    assert slot["slot"]
    assert isinstance(slot["is_starter"], bool)
    assert slot["is_locked"] in (True, False)
    assert slot["player"]["eligible_slots"]


def test_an_ambiguous_team_name_is_refused_rather_than_guessed(espn: RecordedEspn):
    """Reading the wrong team's roster is the expensive way to be wrong."""
    with pytest.raises(LeagueNotFoundError, match="matches more than one team"):
        roster_command.roster(team="Team")


def test_an_unknown_team_names_the_ones_that_exist(espn: RecordedEspn):
    with pytest.raises(LeagueNotFoundError) as caught:
        roster_command.roster(team="Team Zulu")
    assert "1='Team Alpha'" in str(caught.value)


def test_matchups_defaults_to_the_current_week(espn: RecordedEspn):
    data = matchups_command.matchups().data
    assert [item["scoring_period_id"] for item in data] == [2]
    assert data[0]["matchup_period_id"] == 2
    assert data[0]["is_playoff"] is False


def test_matchups_on_the_second_scoring_period_of_a_playoff_round(espn: RecordedEspn):
    """KTD11. Scoring period 14 belongs to matchup period 13 in this league.

    ``espn-api``'s ``scoreboard()`` filters on ``matchupPeriodId``, so a command
    that passed ``--week`` straight through would return the wrong round or
    nothing. The resolution lives in the adapter — this asserts the command
    does not re-derive it, by asking for the period the two disagree on.
    """
    data = matchups_command.matchups(week=14).data
    assert len(data) == 1
    assert data[0]["scoring_period_id"] == 14
    assert data[0]["matchup_period_id"] == 13
    assert data[0]["is_playoff"] is True


def test_transactions_walk_backward_under_a_stated_cap(espn: RecordedEspn):
    """The walk is bounded, and every period it read reaches the envelope."""
    envelope = transactions_command.transactions(limit=50)
    periods = [view for view in espn.views() if view.startswith("mTransactions2")]

    # Current week is 2, so the walk can only reach periods 2 and 1 before the
    # season starts -- well inside the cap, and never below period 1.
    assert periods == ["mTransactions2@2", "mTransactions2@1"]
    assert len(periods) <= transactions_command.PERIOD_CAP

    names = [source["name"] for source in _plain(envelope)["sources"]]
    assert names.count("mTransactions2") == 2, "each period is its own source"


def test_transactions_report_the_oldest_contributing_age(espn: RecordedEspn):
    payload = _plain(transactions_command.transactions(limit=50))
    ages = [source["age_seconds"] for source in payload["sources"]]
    assert payload["data_age_seconds"] == max(ages)
    assert payload["data_as_of"] == payload["sources"][0]["fetched_at"]


def test_transactions_merge_both_espn_surfaces_without_duplicating_them(espn: RecordedEspn):
    """The activity feed is re-read per period; the same move must appear once."""
    data = transactions_command.transactions(limit=50).data
    identities = [
        (item["type"], item["team_provider_id"], tuple(item["players_in"]), item["timestamp"])
        for item in data
    ]
    assert len(identities) == len(set(identities))
    assert {item["type"] for item in data} == {"waiver_claim", "trade"}


def test_transactions_are_newest_first_and_bounded_by_limit(espn: RecordedEspn):
    data = transactions_command.transactions(limit=1).data
    assert len(data) == 1
    everything = transactions_command.transactions(limit=50).data
    stamps = [item["timestamp"] for item in everything if item["timestamp"]]
    assert stamps == sorted(stamps, reverse=True)


def test_free_agents_bound_the_result_and_the_upstream_page(espn: RecordedEspn):
    data = free_agents_command.free_agents(limit=1).data
    assert len(data) == 1
    assert data[0]["player"]["name"]
    assert data[0]["percent_owned"] is not None


def test_free_agents_pos_becomes_the_fantasy_filter_espn_actually_reads(espn: RecordedEspn):
    """ESPN scopes this by header, not by URL. A missing filter is a silent miss."""
    free_agents_command.free_agents(pos="WR", limit=2)
    filters = [
        call["headers"]["x-fantasy-filter"]
        for call in espn.calls
        if "x-fantasy-filter" in call["headers"]
    ]
    assert filters, "the position must travel as a header"
    assert any("filterSlotIds" in value for value in filters)


def test_a_position_espn_would_silently_ignore_is_refused_before_the_request(
    espn: RecordedEspn,
):
    """ESPN answers an unknown position with its *default* set and a 200."""
    with pytest.raises(ConfigInvalidError, match="not an ESPN position"):
        free_agents_command.free_agents(pos="QUARTERBACK")


def test_raw_returns_each_requested_view_unmodified(espn: RecordedEspn):
    envelope = raw_command.raw(view=["mMatchupScore", "mDraftDetail"])
    data = envelope.data
    assert list(data) == ["mMatchupScore", "mDraftDetail"]
    for name, entry in data.items():
        assert entry["view"] == name
        assert entry["complete"] is True
        assert entry["warning"] is None

    # Unmodified means unmodified: the payload is byte-for-byte what the stub
    # served, not a reshaped subset of it.
    assert data["mDraftDetail"]["payload"] == json.loads(espn.by_view["mDraftDetail"])

    names = [source["name"] for source in _plain(envelope)["sources"]]
    assert names == ["mMatchupScore", "mDraftDetail"]


def test_raw_repeats_are_collapsed_and_order_is_preserved(espn: RecordedEspn):
    data = raw_command.raw(view=["mDraftDetail", "mDraftDetail", "mMatchupScore"]).data
    assert list(data) == ["mDraftDetail", "mMatchupScore"]


def test_an_unfiltered_subset_view_is_labelled_and_never_presented_as_authoritative(
    espn: RecordedEspn,
):
    """ESPN answers 200 with its own default set, which looks like the whole one."""
    entry = raw_command.raw(view=["kona_player_info"]).data["kona_player_info"]
    assert entry["filtered"] is False
    assert entry["complete"] is False
    assert "partial result" in entry["warning"]
    assert entry["payload"], "the payload is still returned; it is labelled, not withheld"


def test_a_filter_makes_the_same_view_complete(espn: RecordedEspn):
    entry = raw_command.raw(view=["kona_player_info"], filter='{"players":{"limit":5}}').data[
        "kona_player_info"
    ]
    assert entry["filtered"] is True
    assert entry["complete"] is True
    assert entry["warning"] is None
    sent = [
        c["headers"]["x-fantasy-filter"] for c in espn.calls if "x-fantasy-filter" in c["headers"]
    ]
    assert sent == ['{"players": {"limit": 5}}']


def test_raw_refuses_a_filter_that_is_not_json(espn: RecordedEspn):
    """A malformed filter is ignored by ESPN, which answers its default subset."""
    with pytest.raises(ConfigInvalidError, match="must be JSON"):
        raw_command.raw(view=["mDraftDetail"], filter="players.limit=5")
    assert espn.calls == []


def test_raw_needs_at_least_one_view(espn: RecordedEspn):
    with pytest.raises(ConfigInvalidError, match="needs at least one --view"):
        raw_command.raw(view=[])
    assert espn.calls == []


# --------------------------------------------------------------------------- #
# The options every command honours
# --------------------------------------------------------------------------- #


def test_season_overrides_the_profile_without_editing_it(espn: RecordedEspn):
    """`--season 2026` on a 2026 profile is a no-op; the plumbing is what matters."""
    ctx = open_read("synthetic", 2026)
    assert ctx.season == 2026
    assert ctx.profile.name == "synthetic"


def test_league_selects_a_named_profile(espn: RecordedEspn):
    assert open_read("synthetic").league_id == LEAGUE_ID


def test_an_unknown_league_fails_before_any_network_call_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
):
    """The cheapest failure there is, and it must stay cheap."""
    import requests

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a network call was attempted for an unknown league")

    monkeypatch.setattr(requests, "get", refuse)
    with pytest.raises(LeagueNotFoundError, match="no league named 'nope'"):
        league_commands.teams(league="nope")


def test_an_unknown_provider_is_a_config_failure_not_a_missing_league(
    monkeypatch: pytest.MonkeyPatch,
):
    """No other `--league` value can fix a provider this build does not have."""
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(ConfigInvalidError, match="Unknown provider 'nonesuch'"):
        league_commands.teams(league="broken")


def test_absent_credentials_are_reported_before_a_request(monkeypatch: pytest.MonkeyPatch):
    import requests

    monkeypatch.delenv("FANTASY_SPORTS_ESPN_S2")
    monkeypatch.delenv("FANTASY_SPORTS_SWID")
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(AuthMissingError):
        league_commands.teams()


def test_no_cache_still_builds_a_store_so_bodies_are_still_scrubbed(espn: RecordedEspn):
    """A provider with no store hands the adapter *unscrubbed* bytes.

    `--no-cache` must not be the one flag that changes what gets parsed, so it
    is a cache *mode*, never a missing cache.
    """
    from fantasy_sports.cache.store import CacheMode

    ctx = open_read(no_cache=True)
    assert ctx.store is not None
    assert ctx.provider._cache is ctx.store
    assert ctx.provider._cache_mode is CacheMode.BYPASS


def test_fresh_refreshes_rather_than_bypasses(espn: RecordedEspn):
    from fantasy_sports.cache.store import CacheMode

    assert open_read(fresh=True).provider._cache_mode is CacheMode.FRESH
    assert open_read().provider._cache_mode is CacheMode.DEFAULT
    assert open_read(fresh=True, no_cache=True).provider._cache_mode is CacheMode.BYPASS


def test_no_cache_writes_nothing_to_the_store(espn: RecordedEspn):
    league_commands.teams(no_cache=True)
    store = open_read(no_cache=True).store
    connection = store._connect()
    assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# The cache is transparent to this layer
# --------------------------------------------------------------------------- #


def test_a_cache_hit_and_a_live_fetch_differ_only_in_the_named_keys(espn: RecordedEspn):
    """The proof that the cache decorator is invisible from up here.

    If this ever fails on a key outside :data:`CACHE_VARIANT_KEYS`, the cache
    stopped being a decorator and became part of the output contract.
    """
    cold = _plain(league_commands.teams())
    warm = _plain(league_commands.teams())

    assert any(source["cached"] for source in warm["sources"]), "the second read hit the store"
    assert _without_variants(cold) == _without_variants(warm)


def test_the_excluded_key_set_is_exactly_what_a_cache_may_move(espn: RecordedEspn):
    """Fixed, and asserted, so widening it is a deliberate act."""
    assert {
        "generated_at",
        "data_as_of",
        "data_age_seconds",
        "fetched_at",
        "age_seconds",
        "cached",
    } == CACHE_VARIANT_KEYS


def _without_variants(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_variants(item)
            for key, item in value.items()
            if key not in CACHE_VARIANT_KEYS
        }
    if isinstance(value, list):
        return [_without_variants(item) for item in value]
    return value


def test_a_composite_read_reports_every_contributing_request(espn: RecordedEspn):
    """Free agents fans out; one blended age would hide a stale projection."""
    payload = _plain(free_agents_command.free_agents(limit=2))
    names = [source["name"] for source in payload["sources"]]

    assert len(names) >= 3, names
    assert "kona_player_info" in names
    assert "players_wl" in names
    assert payload["data_age_seconds"] == max(s["age_seconds"] for s in payload["sources"])


# --------------------------------------------------------------------------- #
# Failures keep the code the adapter chose
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the `broken` profile at a provider that fails on demand."""
    monkeypatch.setattr(
        "fantasy_sports.commands.context.PROVIDERS",
        {"nonesuch": "_fake_provider:AuthExpiredProvider"},
    )


def test_a_provider_reporting_expiry_keeps_that_code(fake_provider: None):
    with pytest.raises(AuthExpiredError) as caught:
        league_commands.teams(league="broken")
    assert caught.value.code.value == "AUTH_EXPIRED"
    assert caught.value.retryable is False


def test_schema_drift_carries_the_offending_path_to_the_command_layer(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "fantasy_sports.commands.context.PROVIDERS",
        {"nonesuch": "_fake_provider:DriftingProvider"},
    )
    with pytest.raises(SchemaDriftError) as caught:
        league_commands.standings(league="broken")
    assert caught.value.to_dict()["details"]["path"] == [
        "teams",
        "record",
        "overall",
        "wins",
    ]


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


def test_auth_status_reports_presence_and_source_without_a_value(espn: RecordedEspn):
    data = auth_commands.status().data
    assert data["complete"] is True
    names = {row["name"]: row for row in data["credentials"]}
    assert names["espn_s2"]["source"] == "env"
    assert data["staleness_threshold"]["verified"] is False
    assert FAKE_S2 not in json.dumps(data)
    assert FAKE_SWID not in json.dumps(data)


def test_auth_status_never_contacts_the_provider(monkeypatch: pytest.MonkeyPatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("no request expected"))
    assert auth_commands.status().ok


def test_auth_login_stores_names_and_reports_what_it_repaired(monkeypatch: pytest.MonkeyPatch):
    """A SWID pasted without braces is repaired before the write, not rejected."""
    entered = iter([FAKE_S2, FAKE_SWID.strip("{}")])
    written: dict[str, str] = {}

    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(entered))
    monkeypatch.setattr("fantasy_sports.auth.chain.write_to_keychain", written.__setitem__)

    data = auth_commands.login().data
    assert data["stored"] == ["espn_s2", "swid"]
    assert data["repaired"] == ["swid"]
    assert written["swid"] == FAKE_SWID
    assert FAKE_S2 not in json.dumps(data)


def test_auth_login_writes_nothing_when_a_value_is_missing(monkeypatch: pytest.MonkeyPatch):
    """Half a cookie pair reaches ESPN unauthenticated; it must not be stored."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    monkeypatch.setattr(
        "fantasy_sports.auth.chain.write_to_keychain",
        lambda *a: pytest.fail("nothing should be written"),
    )
    with pytest.raises(AuthMissingError, match="Nothing entered"):
        auth_commands.login()


# --------------------------------------------------------------------------- #
# The boundary that makes the CLI a projection (ADR-0003, KTD7)
# --------------------------------------------------------------------------- #


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


COMMAND_MODULES = sorted(Path("src/fantasy_sports/commands").rglob("*.py"))


def test_the_command_package_is_not_empty():
    assert COMMAND_MODULES


@pytest.mark.parametrize("path", COMMAND_MODULES, ids=str)
def test_no_command_module_imports_a_presentation_dependency(path: Path):
    """The registry is a plain layer; typer and rich belong to `cli/` alone.

    An AST scan rather than a `sys.modules` check, because a lazy import inside
    a function is exactly what this rule forbids *here* — a command that
    imports typer only when it runs still ties the registry to the CLI, and the
    tie only shows up once an MCP server tries to call it.
    """
    roots = _imported_roots(path)
    for forbidden in ("typer", "click", "rich"):
        assert forbidden not in roots, f"{path} imports {forbidden}"


@pytest.mark.parametrize("path", COMMAND_MODULES, ids=str)
def test_no_command_module_names_a_provider_at_module_scope(path: Path):
    """A provider reaches a command through `PROVIDERS`, resolved at call time."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert "providers" not in name, f"{path} imports {name} at module scope"
            assert not name.startswith("espn_api"), f"{path} imports espn_api"


def test_a_credential_with_no_guidance_still_prompts(monkeypatch: pytest.MonkeyPatch):
    """`CredentialSpec.guidance` is optional; a provider may declare none."""
    from dataclasses import replace

    from fantasy_sports.auth.chain import ESPN_CREDENTIALS

    bare = tuple(replace(spec, guidance="") for spec in ESPN_CREDENTIALS)
    monkeypatch.setattr("fantasy_sports.auth.chain.ESPN_CREDENTIALS", bare)
    entered = iter([FAKE_S2, FAKE_SWID])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(entered))
    monkeypatch.setattr("fantasy_sports.auth.chain.write_to_keychain", lambda *a: None)

    data = auth_commands.login().data
    assert data["stored"] == ["espn_s2", "swid"]


def test_an_empty_team_is_refused_rather_than_matched_against_everything(espn: RecordedEspn):
    """An empty string is a substring of every name; it must not pick the first."""
    with pytest.raises(LeagueNotFoundError, match="empty --team"):
        roster_command.roster(team="   ")
