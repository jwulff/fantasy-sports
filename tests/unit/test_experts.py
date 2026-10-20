"""The ``experts`` command group (jwulff/fantasy-sports#129), offline.

Every FantasyPros read replays from ``tests/cassettes/fantasypros/experts_2026.yaml``
through ``requests.get``, the same seam the client uses in production, so the
tests drive the real client, the real cache and the real credential gate. The
cassette is a trimmed premium recording (``docs/testing.md`` §10); what it
cannot hold -- another scoring, a hostile name, a reordered list -- is injected
by overriding one endpoint's body with a edited copy of the recorded one.

The row field lists below are **the contract** with ``jwulff/league-gazette``
(issue #129 and its 2026-10-20 comment). A test that fails because a name here
changed is a breaking change being made on purpose or by accident.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _harness import FAKE_FP_KEY, RecordedFantasyPros, isolate_home

from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands import experts as experts_commands
from fantasy_sports.core.errors import (
    AuthMissingError,
    ConfigInvalidError,
    ProviderUnavailableError,
    SchemaDriftError,
)
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output import render
from fantasy_sports.sources import fantasypros
from fantasy_sports.sources.fantasypros import Pacer

CASSETTE = "fantasypros/experts_2026.yaml"
WEEK = 7
BIJAN = 23133
BIJAN_ESPN = "4430807"
AJ_BROWN = 18218

RANKING_FIELDS = [
    "fp_id", "name", "pro_team", "position", "rank_ecr", "rank_min", "rank_max", "rank_ave",
    "rank_std", "tier", "ecr_delta", "opponent", "kickoff", "bye_week", "owned_espn_pct",
    "espn_id", "start_sit_grade", "pos_rank", "r2p_pts", "game_status",
]  # fmt: skip
PROJECTION_FIELDS = [
    "fp_id", "name", "pro_team", "position", "espn_id", "points_std", "points_half", "points_ppr",
    "stats",
]  # fmt: skip
INJURY_FIELDS = [
    "fp_id", "name", "pro_team", "position", "espn_id", "status", "status_short", "injury_type",
    "probability_of_playing", "practice", "ir_weeks", "updated_at",
]  # fmt: skip
NEWS_FIELDS = [
    "id", "created_at", "title", "summary", "impact", "player", "team", "categories", "url",
]  # fmt: skip

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private XDG tree, the fake key in the environment, and no pacing sleeps."""
    home = isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("FANTASY_SPORTS_FANTASYPROS_API_KEY", FAKE_FP_KEY)
    monkeypatch.setattr(
        fantasypros, "_SHARED_PACER", Pacer(monotonic=lambda: 0.0, sleep=lambda _: None)
    )
    yield home
    forget_secrets()


def serve(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any] | None = None
) -> RecordedFantasyPros:
    """Route ``requests.get`` at the experts cassette, with optional overrides."""
    import requests

    http = RecordedFantasyPros(CASSETTE, overrides)
    monkeypatch.setattr(requests, "get", http)
    return http


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> RecordedFantasyPros:
    return serve(monkeypatch)


def body(http: RecordedFantasyPros, suffix: str, **match: str) -> dict[str, Any]:
    """The recorded body of the request whose path ends ``suffix`` and has these query pairs."""
    for (path, pairs), text in http.bodies.items():
        if path.endswith(suffix) and all(dict(pairs).get(k) == v for k, v in match.items()):
            return json.loads(text)
    raise AssertionError(f"no recorded {suffix} {match}")


def call(command: str, **kwargs: Any) -> Any:
    return REGISTRY[f"experts {command}"].resolve()(**kwargs)


def data(command: str, **kwargs: Any) -> list[dict[str, Any]]:
    return call(command, **kwargs).to_dict()["data"]


def sent(http: RecordedFantasyPros, endpoint: str) -> dict[str, Any]:
    """The params of the one request to ``endpoint`` that was sent."""
    (match,) = [c for c in http.calls if c["url"].endswith(f"/{endpoint}")]
    return match["params"]


# --------------------------------------------------------------------------- #
# The registry: the group exists, is a list, and takes no league
# --------------------------------------------------------------------------- #


def test_the_four_commands_are_registered_in_one_group_as_collections():
    names = {s.invocation for s in REGISTRY.values() if s.group == "experts"}
    assert names == {
        "experts rankings",
        "experts projections",
        "experts injuries",
        "experts news",
    }
    for name in names:
        assert REGISTRY[name].shape is DataShape.COLLECTION
        assert REGISTRY[name].takes_league is False


def test_the_fast_path_help_lists_the_group():
    from fantasy_sports.cli.fastpath import render_help

    help_text = render_help()
    (line,) = [ln for ln in help_text.splitlines() if ln.strip().startswith("experts")]
    for name in ("rankings", "projections", "injuries", "news"):
        assert name in line


def test_week_and_ros_are_declared_where_the_contract_says():
    declared = {
        s.name: {p.name for p in s.handler_params}
        for s in REGISTRY.values()
        if s.group == "experts"
    }
    assert {"week", "ros"} <= declared["rankings"] and "scoring" in declared["rankings"]
    assert {"week", "ros", "player"} <= declared["projections"]
    assert {"week", "player", "pos", "team"} <= declared["injuries"]
    assert "week" not in declared["news"] and "ros" not in declared["injuries"]
    assert next(p for p in REGISTRY["experts rankings"].params if p.name == "pos").required


# --------------------------------------------------------------------------- #
# rankings
# --------------------------------------------------------------------------- #


def test_rankings_reads_the_players_table_for_the_week_and_the_join(http: RecordedFantasyPros):
    envelope = call("rankings", pos="RB")
    payload = envelope.to_dict()

    assert payload["provider"] == "fantasypros"
    assert payload["league_id"] is None and payload["season"] == 2026
    assert isinstance(payload["data"], list)
    assert [s["name"] for s in payload["sources"]] == [
        "fantasypros:players",
        "fantasypros:consensus-rankings",
    ]
    assert all(s["cached"] is False for s in payload["sources"])
    assert http.paths() == ["nfl/players", "nfl/2026/consensus-rankings"]
    # The default week is the one FantasyPros reports on the players table.
    assert sent(http, "consensus-rankings") == {"week": WEEK, "position": "RB", "scoring": "PPR"}


def test_a_ranking_row_has_exactly_the_contract_fields_with_the_contract_types(
    http: RecordedFantasyPros,
):
    rows = data("rankings", pos="RB")
    assert rows
    for row in rows:
        assert list(row) == RANKING_FIELDS
        assert isinstance(row["fp_id"], int) and isinstance(row["rank_ecr"], int)
        assert isinstance(row["rank_min"], int) and isinstance(row["rank_max"], int)
        assert isinstance(row["rank_ave"], float) and isinstance(row["rank_std"], float)
        assert row["ecr_delta"] is None or isinstance(row["ecr_delta"], int)
        assert isinstance(row["bye_week"], int)
        assert isinstance(row["owned_espn_pct"], float)
        assert row["kickoff"] is None or UTC_Z.match(row["kickoff"])
        assert row["tier"] is None  # FantasyPros sends no tier for these lists
        assert row["espn_id"] is None or isinstance(row["espn_id"], str)
    top = rows[0]
    assert (top["name"], top["position"], top["rank_ecr"]) == ("Jahmyr Gibbs", "RB", 1)
    assert top["pos_rank"] == "RB1" and top["start_sit_grade"]
    assert isinstance(top["r2p_pts"], float)
    assert top["opponent"] and top["game_status"] == "scheduled"


def test_numbers_the_wire_sends_as_text_come_out_as_numbers(monkeypatch: pytest.MonkeyPatch):
    served = serve(monkeypatch)
    wire = body(served, "consensus-rankings", week="7")
    wire["players"][0].update(player_ecr_delta="-3", rank_ave="1.50", rank_std="", r2p_pts="23.0")
    wire["players"][0].update(player_game_kickoff_ts=0, tier="2")
    serve(monkeypatch, {"consensus-rankings": (200, wire)})
    first = data("rankings", pos="RB")[0]
    assert (first["ecr_delta"], first["rank_ave"], first["r2p_pts"]) == (-3, 1.5, 23.0)
    assert first["rank_std"] is None and first["kickoff"] is None
    assert first["tier"] == "2"  # passed through when FantasyPros sends one


def test_rankings_are_ordered_by_rank_ecr_whatever_order_the_wire_used(
    monkeypatch: pytest.MonkeyPatch,
):
    recorded = serve(monkeypatch)
    shuffled = body(recorded, "consensus-rankings", week="7")
    shuffled["players"].reverse()
    shuffled["players"][0]["rank_ecr"] = None  # a missing rank sorts last, never first
    serve(monkeypatch, {"consensus-rankings": (200, shuffled)})
    ranks = [r["rank_ecr"] for r in data("rankings", pos="RB")]
    assert ranks[-1] is None
    assert ranks[:-1] == sorted(ranks[:-1])


def test_espn_id_is_a_string_joined_from_the_players_table_and_null_without_a_join(
    http: RecordedFantasyPros,
):
    rows = {r["fp_id"]: r for r in data("rankings", pos="RB")}
    assert rows[BIJAN]["espn_id"] == BIJAN_ESPN
    # The last row names a player the cached table does not list.
    unlisted = [r for r in rows.values() if r["espn_id"] is None]
    assert len(unlisted) == 1


def test_a_closed_week_is_served_not_refused(http: RecordedFantasyPros):
    rows = data("rankings", pos="RB", week=5)
    assert sent(http, "consensus-rankings")["week"] == 5
    assert rows and {r["game_status"] for r in rows} == {"closed"}
    assert all(r["start_sit_grade"] and r["r2p_pts"] is not None for r in rows)


def test_ros_sends_the_ros_type_and_no_week_and_joins_a_player_without_an_espn_id(
    http: RecordedFantasyPros,
):
    rows = data("rankings", pos="RB", ros=True)
    assert sent(http, "consensus-rankings") == {"type": "ros", "position": "RB", "scoring": "PPR"}
    assert all(r["kickoff"] is None for r in rows)
    taylor = next(r for r in rows if r["name"] == "J'Mari Taylor")
    assert taylor["espn_id"] is None


def test_ros_and_week_together_are_a_bad_argument_and_cost_nothing(http: RecordedFantasyPros):
    with pytest.raises(ConfigInvalidError) as caught:
        call("rankings", pos="RB", ros=True, week=7)
    assert caught.value.details["kind"] == "argument"
    assert http.calls == []


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [({"pos": "QUARTERBACK"}, "pos"), ({"pos": "RB", "scoring": "full"}, "scoring")],
)
def test_a_bad_position_or_scoring_is_refused_before_any_request(
    http: RecordedFantasyPros, kwargs: dict[str, str], named: str
):
    with pytest.raises(ConfigInvalidError) as caught:
        call("rankings", **kwargs)
    assert caught.value.details["kind"] == "argument" and named in caught.value.details
    assert http.calls == []


def test_espns_spelling_of_the_defense_is_accepted(http: RecordedFantasyPros):
    with pytest.raises(ProviderUnavailableError):  # the cassette holds only RB; this is DST
        call("rankings", pos="D/ST")
    assert [c["params"]["position"] for c in http.calls if "consensus" in c["url"]] == ["DST"]


@pytest.mark.parametrize("scoring", ["STD", "half", "PPR"])
def test_scoring_is_passed_through(monkeypatch: pytest.MonkeyPatch, scoring: str):
    served = serve(monkeypatch)
    canned = body(served, "consensus-rankings", week="7")
    served = serve(monkeypatch, {"consensus-rankings": (200, canned)})
    call("rankings", pos="RB", scoring=scoring)
    assert sent(served, "consensus-rankings")["scoring"] == scoring.upper()


def test_a_missing_current_week_asks_for_one_instead_of_guessing(monkeypatch: pytest.MonkeyPatch):
    served = serve(monkeypatch)
    table = body(served, "players")
    del table["week"]
    serve(monkeypatch, {"players": (200, table)})
    with pytest.raises(ConfigInvalidError) as caught:
        call("rankings", pos="RB")
    assert caught.value.details["kind"] == "argument"
    assert "--week" in (caught.value.remediation or "")


# --------------------------------------------------------------------------- #
# scoring from the league
# --------------------------------------------------------------------------- #


def _settings(points: float | None) -> dict[str, Any]:
    items = [{"statId": 42, "points": 0.1}]
    if points is not None:
        items.append({"statId": 53, "points": points})
    return {"settings": {"scoringSettings": {"scoringItems": items}}}


@pytest.mark.parametrize(
    ("points", "expected"),
    [(1.0, "PPR"), (0.5, "HALF"), (0.0, "STD"), (None, "STD"), (2.0, "STD"), (0.25, "STD")],
)
def test_the_receptions_setting_picks_the_scoring(points: float | None, expected: str):
    assert experts_commands.scoring_from_settings(_settings(points)) == expected


@pytest.mark.parametrize("raw", [{}, {"settings": {}}, {"settings": {"scoringSettings": {}}}])
def test_league_settings_without_scoring_items_are_schema_drift_not_standard(raw: dict[str, Any]):
    with pytest.raises(SchemaDriftError):
        experts_commands.scoring_from_settings(raw)


@pytest.fixture
def league_with(monkeypatch: pytest.MonkeyPatch):
    """A league whose receptions setting is chosen per test, without touching ESPN."""
    from fantasy_sports.commands import context
    from fantasy_sports.output.envelope import DataSource, utc_now

    def install(points: float | None) -> SimpleNamespace:
        fetched = SimpleNamespace(raw=_settings(points))
        provider = SimpleNamespace(
            name="espn",
            fetch_league=lambda *_: fetched,
            last_fetch=SimpleNamespace(sources=(DataSource("mSettings", utc_now(), cached=False),)),
        )
        ctx = SimpleNamespace(league_id="99", season=2026, target=("99", 2026), provider=provider)
        monkeypatch.setattr(context, "open_read", lambda *a, **k: ctx)
        return ctx

    return install


@pytest.mark.parametrize(("points", "expected"), [(1.0, "PPR"), (0.5, "HALF"), (0.0, "STD")])
def test_rankings_use_the_leagues_scoring_and_name_espn_as_a_source(
    monkeypatch: pytest.MonkeyPatch, league_with, points: float, expected: str
):
    served = serve(monkeypatch)
    canned = body(served, "consensus-rankings", week="7")
    served = serve(monkeypatch, {"consensus-rankings": (200, canned)})
    league_with(points)

    envelope = call("rankings", pos="RB", league="synthetic").to_dict()

    assert sent(served, "consensus-rankings")["scoring"] == expected
    assert envelope["league_id"] == "99"
    assert [s["name"] for s in envelope["sources"]] == [
        "mSettings",
        "fantasypros:players",
        "fantasypros:consensus-rankings",
    ]


def test_an_explicit_scoring_beats_the_leagues_and_skips_the_espn_read(
    monkeypatch: pytest.MonkeyPatch, league_with
):
    served = serve(monkeypatch)
    canned = body(served, "consensus-rankings", week="7")
    served = serve(monkeypatch, {"consensus-rankings": (200, canned)})
    league_with(1.0)
    envelope = call("rankings", pos="RB", scoring="STD", league="synthetic").to_dict()
    assert sent(served, "consensus-rankings")["scoring"] == "STD"
    assert not any(s["name"] == "mSettings" for s in envelope["sources"])


def test_a_league_that_does_not_exist_fails_before_the_players_table_is_fetched(
    http: RecordedFantasyPros,
):
    from fantasy_sports.core.errors import LeagueNotFoundError

    with pytest.raises(LeagueNotFoundError):
        call("rankings", pos="RB", league="no-such-league")
    assert http.calls == []


# --------------------------------------------------------------------------- #
# projections
# --------------------------------------------------------------------------- #


def test_a_projection_row_has_exactly_the_contract_fields(http: RecordedFantasyPros):
    rows = data("projections", pos="RB")
    assert sent(http, "projections") == {"week": WEEK, "position": "RB"}
    assert rows
    for row in rows:
        assert list(row) == PROJECTION_FIELDS
        for field in ("points_std", "points_half", "points_ppr"):
            assert isinstance(row[field], float)
        assert row["stats"]["points_ppr"] == row["points_ppr"]
        assert row["stats"]["points"] == row["points_std"]
    assert rows[0]["espn_id"] is not None


def test_projections_are_ordered_by_ppr_by_default(http: RecordedFantasyPros):
    ppr = [r["points_ppr"] for r in data("projections", pos="RB")]
    assert ppr == sorted(ppr, reverse=True)


@pytest.mark.parametrize(
    ("points", "order"),
    [(1.0, [2, 0, 1]), (0.5, [1, 2, 0]), (0.0, [0, 1, 2])],
    ids=["ppr", "half", "std"],
)
def test_projections_are_ordered_by_the_leagues_scoring(
    monkeypatch: pytest.MonkeyPatch, league_with, points: float, order: list[int]
):
    served = serve(monkeypatch)
    canned = body(served, "projections", week="7", position="RB")
    canned["players"] = canned["players"][:3]
    # Three rows whose standard, half-PPR and PPR totals each rank them differently.
    for row, totals in zip(
        canned["players"], [(30, 10, 20), (20, 30, 10), (10, 20, 30)], strict=True
    ):
        row["stats"].update(
            zip(("points", "points_half", "points_ppr"), map(float, totals), strict=True)
        )
    names = [row["name"] for row in canned["players"]]
    serve(monkeypatch, {"projections": (200, canned)})
    league_with(points)
    rows = data("projections", pos="RB", league="synthetic")
    assert [r["name"] for r in rows] == [names[i] for i in order]


def test_ros_projections_send_ros_and_no_week(http: RecordedFantasyPros):
    rows = data("projections", pos="RB", ros=True)
    assert sent(http, "projections") == {"position": "RB", "ros": "true"}
    assert rows and all(r["points_ppr"] > 100 for r in rows)  # a season's worth, not a week's


def test_projections_need_a_position_or_a_player(http: RecordedFantasyPros):
    with pytest.raises(ConfigInvalidError) as caught:
        call("projections")
    assert caught.value.details["kind"] == "argument"
    assert http.calls == []


@pytest.mark.parametrize("token", [str(BIJAN), BIJAN_ESPN, "Bijan Robinson", "bijan", "  BIJAN r "])
def test_player_takes_a_fantasypros_id_an_espn_id_or_a_name(http: RecordedFantasyPros, token: str):
    rows = data("projections", player=token)
    assert [r["fp_id"] for r in rows] == [BIJAN]
    assert sent(http, "projections") == {"week": WEEK, "players": str(BIJAN)}


def test_player_narrowed_by_position_still_resolves(http: RecordedFantasyPros):
    rows = data("projections", pos="RB", player="Bijan")
    assert [r["fp_id"] for r in rows] == [BIJAN]


@pytest.mark.parametrize(
    ("token", "expect"),
    [
        ("Josh A", ["Josh Adams", "Josh Ali", "Josh Allen", "Josh Avery"]),
        ("Josh Johnson", ["Josh Johnson", "Josh Johnson"]),
    ],
)
def test_an_ambiguous_player_is_config_invalid_listing_the_candidates(
    http: RecordedFantasyPros, token: str, expect: list[str]
):
    with pytest.raises(ConfigInvalidError) as caught:
        call("projections", player=token)
    error = caught.value
    assert error.details["kind"] == "argument"
    assert [c["name"] for c in error.details["candidates"]] == expect
    for candidate in error.details["candidates"]:
        assert set(candidate) == {"fp_id", "name", "position", "pro_team", "espn_id"}
        assert candidate["name"] in str(error)
    assert error.details["total"] == len(expect)
    assert not any(c["url"].endswith("/projections") for c in http.calls)


def test_an_exact_name_wins_over_longer_names_it_is_a_prefix_of(http: RecordedFantasyPros):
    # "Josh Al" is ambiguous (Ali, Allen); the exact "Josh Allen" is not.
    with pytest.raises(ConfigInvalidError):
        call("projections", player="Josh Al")
    served = http
    with pytest.raises(ProviderUnavailableError):  # resolved; the cassette has no row for him
        call("projections", player="Josh Allen")
    assert sent(served, "projections")["players"] == "17298"


def test_a_position_narrows_an_ambiguous_name(http: RecordedFantasyPros):
    # "Josh A" fits four players; only one of them is a wide receiver.
    with pytest.raises(ProviderUnavailableError):  # resolved; the cassette has no row for him
        call("projections", pos="WR", player="Josh A")
    assert sent(http, "projections") == {"week": WEEK, "players": "25218"}
    with pytest.raises(ConfigInvalidError) as caught:
        call("projections", pos="TE", player="Josh A")
    assert caught.value.details["candidates"] == []


@pytest.mark.parametrize("token", ["999999999", "Nobody Atall", "   "])
def test_an_unknown_or_empty_player_is_a_bad_argument(http: RecordedFantasyPros, token: str):
    with pytest.raises(ConfigInvalidError) as caught:
        call("news", player=token)
    assert caught.value.details["kind"] == "argument"
    assert not any(c["url"].endswith("/news") for c in http.calls)


def test_a_negative_number_is_a_team_defense_espn_id(http: RecordedFantasyPros):
    # The cassette's players table carries San Francisco's defense as espn_id
    # "-16025"; the snapshot in jwulff/league-gazette#132 passes those ids as-is.
    with pytest.raises(ProviderUnavailableError):  # resolved; the cassette has no news row for it
        call("news", player="-16025")


def test_a_number_that_is_one_players_fantasypros_id_and_anothers_espn_id_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
):
    served = serve(monkeypatch)
    table = body(served, "players")
    first, second = table["players"][0], table["players"][1]
    second["espn_id"] = str(first["player_id"])  # collide: espn == the other's fp id
    serve(monkeypatch, {"players": (200, table)})
    with pytest.raises(ConfigInvalidError) as caught:
        call("news", player=str(first["player_id"]))
    assert len(caught.value.details["candidates"]) == 2


# --------------------------------------------------------------------------- #
# injuries
# --------------------------------------------------------------------------- #


def test_an_injury_row_has_exactly_the_contract_fields_with_the_contract_types(
    http: RecordedFantasyPros,
):
    rows = data("injuries")
    assert sent(http, "injuries") == {"year": 2026, "week": WEEK, "include_probabilities": "true"}
    assert rows
    for row in rows:
        assert list(row) == INJURY_FIELDS
        # The API sends a string; the contract is a float or null.
        assert row["probability_of_playing"] is None or isinstance(
            row["probability_of_playing"], float
        )
        assert len(row["practice"]) == 3
        assert isinstance(row["ir_weeks"], list)
        assert row["updated_at"] is None or UTC_Z.match(row["updated_at"])
        assert row["status"] != "" and row["status_short"] != ""  # empty is null, not ""
    assert any(isinstance(r["probability_of_playing"], float) for r in rows)
    assert any(r["probability_of_playing"] is None for r in rows)
    assert any(r["status"] == "IR" and r["ir_weeks"] for r in rows)


def test_injuries_are_most_recently_updated_first_and_undated_last(http: RecordedFantasyPros):
    stamps = [r["updated_at"] for r in data("injuries")]
    dated = [s for s in stamps if s is not None]
    assert dated == sorted(dated, reverse=True)
    assert stamps[: len(dated)] == dated and all(s is None for s in stamps[len(dated) :])


def test_an_injury_type_falls_back_to_the_practice_reports(monkeypatch: pytest.MonkeyPatch):
    served = serve(monkeypatch)
    injuries = body(served, "injuries", year="2026", include_probabilities="true")
    injuries["injuries"] = [
        {**injuries["injuries"][0], "injury_type": "", "practice_report_injury_type": "ankle"}
    ]
    serve(monkeypatch, {"injuries": (200, injuries)})
    assert data("injuries")[0]["injury_type"] == "ankle"


def test_injuries_take_a_week_a_team_and_a_player(http: RecordedFantasyPros):
    rows = data("injuries", team="ne", week=7)
    assert sent(http, "injuries")["team_id"] == "NE"
    assert rows and {r["pro_team"] for r in rows} == {"NE"}


def test_injuries_for_one_player_by_espn_id_or_name(http: RecordedFantasyPros):
    by_name = data("injuries", player="A.J. Brown")
    assert [r["fp_id"] for r in by_name] == [AJ_BROWN]
    assert sent(http, "injuries")["player_ids"] == str(AJ_BROWN)
    assert by_name[0]["probability_of_playing"] == pytest.approx(0.97772)
    assert by_name[0]["practice"] == ["Limit", None, None]


def test_injuries_pos_filters_to_that_position(http: RecordedFantasyPros):
    rows = data("injuries", pos="wr")
    assert rows and {r["position"] for r in rows} == {"WR"}


def test_injuries_refuse_a_position_they_cannot_filter_on(http: RecordedFantasyPros):
    with pytest.raises(ConfigInvalidError):
        call("injuries", pos="FLX")
    assert http.calls == []


# --------------------------------------------------------------------------- #
# news
# --------------------------------------------------------------------------- #


def test_a_news_row_has_exactly_the_contract_fields(http: RecordedFantasyPros):
    rows = data("news")
    assert sent(http, "news") == {"limit": 25}
    assert rows
    for row in rows:
        assert list(row) == NEWS_FIELDS
        assert isinstance(row["id"], int)
        assert UTC_Z.match(row["created_at"])
        assert isinstance(row["categories"], list) and row["categories"]
        assert row["url"].startswith("https://www.fantasypros.com/")
        assert row["title"] and row["summary"]
        assert set(row["player"]) == {"fp_id", "name", "espn_id"}
        assert row["player"]["name"]


def test_news_is_newest_first_and_created_is_utc_with_z(http: RecordedFantasyPros):
    rows = data("news")
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)
    assert stamps[0].endswith("Z") and stamps[0].startswith("2026-10-20T")


def test_news_for_one_player_passes_fpid_and_limit(http: RecordedFantasyPros):
    rows = data("news", player=BIJAN_ESPN, limit=5)
    assert sent(http, "news") == {"limit": 5, "fpid": BIJAN}
    assert {r["player"]["fp_id"] for r in rows} == {BIJAN}
    assert rows[0]["player"] == {"fp_id": BIJAN, "name": "Bijan Robinson", "espn_id": BIJAN_ESPN}


def test_news_category_is_passed_through(http: RecordedFantasyPros):
    rows = data("news", category="injury", limit=5)
    assert sent(http, "news") == {"limit": 5, "category": "injury"}
    assert all("Injury" in r["categories"] for r in rows)


@pytest.mark.parametrize("limit", [0, -3, True])
def test_a_bad_limit_is_refused_before_any_request(http: RecordedFantasyPros, limit: Any):
    with pytest.raises(ConfigInvalidError):
        call("news", limit=limit)
    assert http.calls == []


def test_news_text_is_labelled_untrusted_by_path(http: RecordedFantasyPros):
    envelope = call("news").to_dict()
    rows = envelope["data"]
    assert envelope["untrusted"]["[0].title"] == rows[0]["title"]
    assert envelope["untrusted"]["[0].summary"] == rows[0]["summary"]
    assert envelope["untrusted"]["[0].impact"] == rows[0]["impact"]
    assert set(envelope["untrusted"]) <= {
        f"[{i}].{f}" for i in range(len(rows)) for f in ("title", "summary", "impact")
    }


def test_a_news_item_with_no_player_has_a_null_player(monkeypatch: pytest.MonkeyPatch):
    served = serve(monkeypatch)
    wire = body(served, "news", limit="25")
    wire["items"][0]["player_id"] = None
    serve(monkeypatch, {"news": (200, wire)})
    assert any(r["player"] is None for r in data("news"))


# --------------------------------------------------------------------------- #
# Provenance, cache, credentials -- true of all four
# --------------------------------------------------------------------------- #

ALL_CALLS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("rankings", {"pos": "RB"}, "consensus-rankings"),
    ("projections", {"pos": "RB"}, "projections"),
    ("injuries", {}, "injuries"),
    ("news", {}, "news"),
)


@pytest.mark.parametrize(("command", "kwargs", "endpoint"), ALL_CALLS)
def test_every_envelope_names_fantasypros_by_endpoint_and_carries_age(
    http: RecordedFantasyPros, command: str, kwargs: dict[str, Any], endpoint: str
):
    payload = call(command, **kwargs).to_dict()
    assert isinstance(payload["data"], list)
    assert payload["error"] is None and payload["raw_omitted"] is False
    names = [s["name"] for s in payload["sources"]]
    assert names == ["fantasypros:players", f"fantasypros:{endpoint}"]
    for source in payload["sources"]:
        assert UTC_Z.match(source["fetched_at"]) and isinstance(source["age_seconds"], int)
        assert source["cached"] is False
    assert payload["data_as_of"] is not None


@pytest.mark.parametrize(("command", "kwargs", "endpoint"), ALL_CALLS)
def test_a_second_call_is_served_from_the_cache_and_says_so(
    http: RecordedFantasyPros, command: str, kwargs: dict[str, Any], endpoint: str
):
    first = call(command, **kwargs).to_dict()
    made = len(http.calls)
    second = call(command, **kwargs).to_dict()
    assert len(http.calls) == made, "a cache hit costs no request against the daily budget"
    assert all(s["cached"] for s in second["sources"])
    assert second["data"] == first["data"]


def test_fresh_refetches_and_no_cache_neither_reads_nor_writes(http: RecordedFantasyPros):
    call("news")
    base = len(http.calls)
    call("news", fresh=True)
    assert len(http.calls) == base + 2
    call("news", no_cache=True)
    assert len(http.calls) == base + 4
    call("news")  # the bypass wrote nothing new, but the fresh entry is still good
    assert len(http.calls) == base + 4


def test_the_key_is_sent_only_as_a_header_and_never_appears_in_an_envelope(
    http: RecordedFantasyPros,
):
    rendered = call("news").to_dict()
    assert FAKE_FP_KEY not in json.dumps(rendered)
    assert {c["headers"]["x-api-key"] for c in http.calls} == {FAKE_FP_KEY}
    assert all(FAKE_FP_KEY not in json.dumps(c["params"]) for c in http.calls)


def test_with_no_key_and_a_cold_cache_the_failure_is_auth_missing(
    monkeypatch: pytest.MonkeyPatch, http: RecordedFantasyPros
):
    from fantasy_sports.auth.chain import FANTASYPROS_CREDENTIALS, resolve_credentials

    monkeypatch.delenv("FANTASY_SPORTS_FANTASYPROS_API_KEY")
    absent = resolve_credentials(
        FANTASYPROS_CREDENTIALS, environ={}, keychain_reader=lambda s: None, config={}
    )
    monkeypatch.setattr(fantasypros, "resolve_credentials", lambda *_a, **_k: absent)
    with pytest.raises(AuthMissingError):
        call("news")
    assert http.calls == []


def test_a_cassette_miss_is_loud_not_a_silent_network_call(monkeypatch: pytest.MonkeyPatch):
    serve(monkeypatch)
    with pytest.raises(ProviderUnavailableError) as caught:
        call("rankings", pos="RB", week=3)
    assert caught.value.details["cause"] == "AssertionError"


def test_the_season_flag_sets_the_season_in_the_request_and_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
):
    served = serve(monkeypatch)
    with pytest.raises(ProviderUnavailableError):
        call("rankings", pos="RB", week=3, season=2025)
    assert any("/2025/consensus-rankings" in c["url"] for c in served.calls)


# --------------------------------------------------------------------------- #
# Rendering: table columns, CSV, and the CLI projection
# --------------------------------------------------------------------------- #


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    from fantasy_sports.cli.app import run

    code = run(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    ("argv", "shown", "hidden"),
    [
        (
            ["experts", "rankings", "--pos", "RB"],
            ["rank_ecr", "name", "pos_rank", "rank_ave", "owned_espn_pct", "Jahmyr Gibbs"],
            ["rank_min", "rank_std", "start_sit_grade", "game_status"],
        ),
        (
            ["experts", "projections", "--pos", "RB"],
            ["points_ppr", "points_half", "points_std", "Jahmyr Gibbs"],
            ["stats", "rush_att"],
        ),
        (
            ["experts", "injuries"],
            ["status_short", "injury_type", "probability_of_playing", "practice", "A.J. Brown"],
            ["ir_weeks", "updated_at"],
        ),
        (
            ["experts", "news", "--limit", "25"],
            ["created_at", "player.name", "team", "title"],
            ["impact", "categories", "summary"],
        ),
    ],
    ids=["rankings", "projections", "injuries", "news"],
)
def test_output_table_shows_a_sane_column_set(
    http: RecordedFantasyPros,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    shown: list[str],
    hidden: list[str],
):
    code, out, err = _run([*argv, "--output", "table"], capsys)
    assert code == 0 and err == ""
    header = out.splitlines()[0]
    assert header.startswith("fantasypros") and "season 2026" in header
    for text in shown:
        assert text in out, text
    for text in hidden:
        assert text not in out, text
    assert "fantasypros:players" in out.splitlines()[-1]


def test_a_table_cell_with_markup_renders_literally(monkeypatch: pytest.MonkeyPatch, capsys):
    served = serve(monkeypatch)
    wire = body(served, "news", limit="25")
    wire["items"][0]["title"] = "[bold red]HACKED[/bold red] and [/oops]"
    serve(monkeypatch, {"news": (200, wire)})
    code, out, _ = _run(["experts", "news", "--output", "table"], capsys)
    assert code == 0 and "HACKED" in out and "[bold red]" in out


def test_json_and_csv_always_carry_every_field_whatever_the_table_shows(
    http: RecordedFantasyPros, capsys: pytest.CaptureFixture[str]
):
    code, out, _ = _run(["experts", "rankings", "--pos", "RB", "--output", "json"], capsys)
    assert code == 0
    assert list(json.loads(out)["data"][0]) == RANKING_FIELDS
    code, out, _ = _run(["experts", "rankings", "--pos", "RB", "--output", "csv"], capsys)
    assert code == 0 and out.splitlines()[0].startswith("fp_id,name,pro_team")
    assert "start_sit_grade" in out.splitlines()[0]


def test_the_cli_maps_a_bad_argument_to_exit_6_with_the_candidates_on_stderr(
    http: RecordedFantasyPros, capsys: pytest.CaptureFixture[str]
):
    code, out, err = _run(["experts", "projections", "--player", "Josh A"], capsys)
    assert code == 6 and out == ""
    error = json.loads(err)["error"]
    assert error["code"] == "CONFIG_INVALID" and error["details"]["kind"] == "argument"
    assert len(error["details"]["candidates"]) == 4


def test_the_cli_requires_pos_for_rankings_as_a_usage_error(
    http: RecordedFantasyPros, capsys: pytest.CaptureFixture[str]
):
    code, _, err = _run(["experts", "rankings"], capsys)
    assert code == 2 and "--pos" in err
    assert http.calls == []


def test_the_cli_accepts_the_global_options_before_the_command(
    http: RecordedFantasyPros, capsys: pytest.CaptureFixture[str]
):
    code, out, _ = _run(["--fresh", "experts", "news", "--output", "json"], capsys)
    assert code == 0 and json.loads(out)["sources"][0]["cached"] is False


def test_render_with_columns_only_narrows_the_table():
    from fantasy_sports.output.envelope import Envelope

    envelope = Envelope.success(
        provider="fantasypros",
        data=[{"a": 1, "b": {"c": "deep"}, "d": ["x", None, 3], "e": "hidden"}],
    )
    table = render(envelope, "table", columns=["a", "b.c", "d", "missing"])
    assert "deep" in table and "x, -, 3" in table and "hidden" not in table
    assert "hidden" in render(envelope, "table")
    assert "hidden" in render(envelope, "json", columns=["a"])
    assert "hidden" in render(envelope, "csv", columns=["a"])


def test_projections_ros_and_week_together_are_a_bad_argument_and_cost_nothing(
    http: RecordedFantasyPros,
):
    with pytest.raises(ConfigInvalidError) as caught:
        call("projections", pos="RB", ros=True, week=7)
    assert caught.value.details["kind"] == "argument"
    assert http.calls == []


def test_a_timestamp_in_a_shape_nobody_documented_is_null_not_passed_through(
    monkeypatch: pytest.MonkeyPatch,
):
    served = serve(monkeypatch)
    injuries = body(served, "injuries", year="2026", include_probabilities="true")
    injuries["injuries"][0]["injury_update_date"] = "last Tuesday"
    serve(monkeypatch, {"injuries": (200, injuries)})
    assert any(r["updated_at"] is None for r in data("injuries"))


def test_a_column_path_through_a_non_mapping_is_an_empty_cell():
    from fantasy_sports.output.envelope import Envelope

    envelope = Envelope.success(provider="fantasypros", data=[{"a": "text"}])
    assert "text" in render(envelope, "table", columns=["a", "a.b"])


def test_flx_narrows_a_name_to_the_flex_positions(http: RecordedFantasyPros):
    # "Josh A" fits an RB, a WR, a QB and a DT; a flex slot can hold only the first two.
    with pytest.raises(ConfigInvalidError) as caught:
        call("projections", pos="FLX", player="Josh A")
    assert [c["position"] for c in caught.value.details["candidates"]] == ["RB", "WR"]
