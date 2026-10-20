"""The FantasyPros expert source (ADR-0011, jwulff/fantasy-sports#128).

Every endpoint the next issues consume replays from the trimmed premium
recording in ``tests/cassettes/fantasypros/premium_2026.yaml`` through the
client's ``http`` seam; the error paths are injected, because no recording of
a 403 or a free-tier answer could be trusted (``docs/testing.md`` §4, §10).

Three things here are security controls rather than behaviour checks: the key
is asserted absent from every error payload, from the SQLite row the cache
wrote, and from a cassette recorded through the real vcrpy hooks -- read back
off disk in each case, because the bytes are what leak.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _harness import FAKE_FP_KEY, RecordedFantasyPros, isolate_home

from fantasy_sports.auth.chain import (
    ALL_CREDENTIALS,
    FANTASYPROS_API_KEY,
    FANTASYPROS_CREDENTIALS,
    CredentialSet,
    CredentialSource,
    resolve_credentials,
)
from fantasy_sports.cache.store import CacheMode, CacheStore
from fantasy_sports.cache.tags import (
    FANTASYPROS_RESOURCES,
    SEASON_SCOPED_RESOURCES,
    TTL_SECONDS,
    Resource,
    TagScope,
    facet_tag,
    scope_of,
    season_tag,
)
from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.sources import fantasypros
from fantasy_sports.sources.fantasypros import (
    API_KEY_HEADER,
    BASE_URL,
    DEFAULT_TIMEOUT,
    DOCTOR_PROBE_PLAYER_ID,
    SOURCE,
    FantasyProsClient,
    Pacer,
    SourceResponse,
    default_season,
)

SEASON = 2026
WEEK = 6
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private XDG tree and the synthetic key in the environment.

    The environment is the first link of the chain, so John's **real** key in
    the Keychain -- stored under this exact service and account name -- is
    never reached by a test.
    """
    home = isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("FANTASY_SPORTS_FANTASYPROS_API_KEY", FAKE_FP_KEY)
    yield home
    forget_secrets()


@pytest.fixture
def http() -> RecordedFantasyPros:
    return RecordedFantasyPros()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CacheStore]:
    clock = {"now": 1_000_000.0}
    with CacheStore(tmp_path / "fp-cache.sqlite3", now=lambda: clock["now"]) as cache:
        cache.clock = clock  # type: ignore[attr-defined]
        yield cache


def _client(
    http: Any,
    store: CacheStore,
    *,
    mode: CacheMode = CacheMode.DEFAULT,
    credentials: CredentialSet | None = None,
    pacer: Pacer | None = None,
) -> FantasyProsClient:
    return FantasyProsClient(
        credentials,
        season=SEASON,
        store=store,
        mode=mode,
        http=http,
        pacer=pacer or Pacer(monotonic=lambda: 0.0, sleep=lambda _: None),
        now=lambda: NOW,
    )


def _rows(store: CacheStore) -> list[tuple[str, str]]:
    store.close()
    connection = sqlite3.connect(store.path)
    try:
        return connection.execute("SELECT url, body FROM entries").fetchall()
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Credential: the chain, the spec, the gate
# --------------------------------------------------------------------------- #


def test_the_spec_is_in_the_auth_chain_and_optional_to_the_tool():
    (spec,) = FANTASYPROS_CREDENTIALS
    assert spec.name == FANTASYPROS_API_KEY == "fantasypros_api_key"
    assert spec.env_vars == ("FANTASY_SPORTS_FANTASYPROS_API_KEY",)
    assert spec.required is False
    assert spec in ALL_CREDENTIALS


def test_the_key_resolves_env_first_then_keychain_then_config():
    env = {"FANTASY_SPORTS_FANTASYPROS_API_KEY": FAKE_FP_KEY}
    from_env = resolve_credentials(
        FANTASYPROS_CREDENTIALS, environ=env, keychain_reader=lambda s: None
    )
    assert from_env.get(FANTASYPROS_API_KEY).source is CredentialSource.ENV  # type: ignore[union-attr]

    from_keychain = resolve_credentials(
        FANTASYPROS_CREDENTIALS, environ={}, keychain_reader=lambda spec: FAKE_FP_KEY
    )
    assert from_keychain.get(FANTASYPROS_API_KEY).source is CredentialSource.KEYCHAIN  # type: ignore[union-attr]

    from_config = resolve_credentials(
        FANTASYPROS_CREDENTIALS,
        environ={},
        keychain_reader=lambda spec: None,
        config={"fantasypros_api_key": FAKE_FP_KEY},
    )
    assert from_config.get(FANTASYPROS_API_KEY).source is CredentialSource.CONFIG  # type: ignore[union-attr]
    assert from_config.reveal(FANTASYPROS_API_KEY) == FAKE_FP_KEY


def test_a_missing_key_is_auth_missing_naming_the_variable_and_nothing_is_sent(
    http: RecordedFantasyPros, store: CacheStore
):
    absent = resolve_credentials(
        FANTASYPROS_CREDENTIALS, environ={}, keychain_reader=lambda s: None, config={}
    )
    client = _client(http, store, credentials=absent)
    with pytest.raises(AuthMissingError) as caught:
        client.players()
    assert "FANTASY_SPORTS_FANTASYPROS_API_KEY" in (caught.value.remediation or "")
    assert caught.value.details == {"credential": "fantasypros_api_key"}
    assert http.calls == []


def test_building_a_client_resolves_nothing_until_the_first_live_request(
    monkeypatch: pytest.MonkeyPatch, http: RecordedFantasyPros, store: CacheStore
):
    """A cache hit must not consult the Keychain, so neither may construction."""
    calls: list[str] = []
    monkeypatch.setattr(
        fantasypros, "resolve_credentials", lambda *a, **k: calls.append("resolved") or _env_set()
    )
    client = FantasyProsClient(
        season=SEASON, store=store, http=http, pacer=_pacer(), now=lambda: NOW
    )
    assert calls == []
    client.experts()
    assert calls == ["resolved"]


def _env_set() -> CredentialSet:
    return resolve_credentials(
        FANTASYPROS_CREDENTIALS,
        environ={"FANTASY_SPORTS_FANTASYPROS_API_KEY": FAKE_FP_KEY},
        keychain_reader=lambda s: None,
    )


def _pacer() -> Pacer:
    return Pacer(monotonic=lambda: 0.0, sleep=lambda _: None)


# --------------------------------------------------------------------------- #
# The wire: header, timeout, no retry, one request at a time
# --------------------------------------------------------------------------- #


def test_the_key_travels_only_in_the_x_api_key_header(http: RecordedFantasyPros, store: CacheStore):
    _client(http, store).players()
    (call,) = http.calls
    assert call["headers"] == {API_KEY_HEADER: FAKE_FP_KEY, "Accept": "application/json"}
    assert FAKE_FP_KEY not in call["url"]
    assert FAKE_FP_KEY not in json.dumps(call["params"])
    assert call["timeout"] == DEFAULT_TIMEOUT


def test_the_default_transport_is_requests_get_with_the_header_and_a_timeout(
    monkeypatch: pytest.MonkeyPatch, http: RecordedFantasyPros, store: CacheStore
):
    import requests

    monkeypatch.setattr(requests, "get", http)
    client = FantasyProsClient(season=SEASON, store=store, pacer=_pacer(), now=lambda: NOW)
    response = client.experts()
    assert response.payload["count"] == 5
    (call,) = http.calls
    assert call["headers"][API_KEY_HEADER] == FAKE_FP_KEY
    assert call["timeout"] == DEFAULT_TIMEOUT


def test_a_transport_failure_is_provider_unavailable_without_a_retry(store: CacheStore):
    attempts = 0

    def _dead(url, params=None, headers=None, timeout=None):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("read timed out")

    client = _client(_dead, store)
    with pytest.raises(ProviderUnavailableError) as caught:
        client.experts()
    assert attempts == 1
    assert caught.value.retryable is True
    assert caught.value.details == {"cause": "TimeoutError", "endpoint": "experts"}
    # A request that timed out after dispatch may still have been served and
    # charged against the daily quota, so the budget counter counts it.
    assert client.requests_made == 1


def test_a_taxonomy_error_raised_by_the_transport_passes_through_untouched(store: CacheStore):
    """A seam that already classified its failure is not re-classified as unavailability."""

    def _classified(url, params=None, headers=None, timeout=None):
        raise RateLimitedError("already classified", retry_after=3)

    with pytest.raises(RateLimitedError) as caught:
        _client(_classified, store).experts()
    assert caught.value.retry_after == 3


def test_requests_are_spaced_at_least_a_second_apart_in_process(
    http: RecordedFantasyPros, store: CacheStore
):
    clock = {"t": 100.0}
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(monotonic=lambda: clock["t"], sleep=_sleep)
    client = _client(http, store, pacer=pacer)
    client.experts()
    clock["t"] += 0.25  # the caller came straight back
    client.news(limit=5, category="injury")
    clock["t"] += 3.0  # long enough: no wait
    client.rankings(week=WEEK)
    assert slept == [pytest.approx(0.75)]
    assert client.requests_made == 3


def test_concurrent_requests_on_one_pacer_queue_rather_than_send_together():
    import threading

    clock = {"t": 100.0}
    stamps: list[float] = []
    guard = threading.Lock()

    def _sleep(seconds: float) -> None:
        with guard:
            clock["t"] += seconds

    pacer = Pacer(monotonic=lambda: clock["t"], sleep=_sleep)
    pacer.wait()  # the first request stamps t=100
    barrier = threading.Barrier(4)

    def _go() -> None:
        barrier.wait()
        pacer.wait()
        stamps.append(pacer.last_request_at or 0.0)

    threads = [threading.Thread(target=_go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ordered = sorted(stamps)
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(ordered, ordered[1:], strict=False))
    assert len(set(stamps)) == 4


@pytest.mark.parametrize("position", ["OP", "IDP", "DL", "LB", "DB", "FLX"])
def test_superflex_and_idp_formats_reach_the_wire(store: CacheStore, position: str):
    """Not refused locally. The stub answers with a body only the server could give."""
    wire = RecordedFantasyPros(
        overrides={"consensus-rankings": (200, {"message": "stub: not recorded"})}
    )
    with pytest.raises(ConfigInvalidError):
        _client(wire, store).consensus_rankings(week=WEEK, position=position)
    assert [c["params"]["position"] for c in wire.calls] == [position]


def test_the_pacer_is_shared_across_clients_in_one_process():
    assert fantasypros.shared_pacer() is fantasypros.shared_pacer()
    a = FantasyProsClient(season=SEASON)
    b = FantasyProsClient(season=SEASON)
    assert a.pacer is b.pacer


def test_the_pacer_waits_with_a_real_clock_when_none_is_injected(monkeypatch: pytest.MonkeyPatch):
    """The defaults are resolved at call time, so a module-level patch reaches them."""
    import time

    ticks = iter([10.0, 10.0, 11.0])
    slept: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", slept.append)
    pacer = Pacer(min_interval=1.0)
    assert pacer.wait() == 0.0
    assert pacer.wait() == pytest.approx(1.0)
    assert slept == [pytest.approx(1.0)]


# --------------------------------------------------------------------------- #
# The cache is the only read path
# --------------------------------------------------------------------------- #


def test_a_repeat_read_is_a_cache_hit_that_sends_nothing_and_waits_for_nothing(
    http: RecordedFantasyPros, store: CacheStore
):
    waits: list[float] = []
    pacer = Pacer(monotonic=lambda: 0.0, sleep=waits.append)
    client = _client(http, store, pacer=pacer)
    first = client.consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    second = client.consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert first.cached is False and second.cached is True
    assert len(http.calls) == 1, "the hit must not count toward the daily budget"
    assert client.requests_made == 1
    assert second.payload == first.payload
    assert second.fetched_at == datetime.fromtimestamp(1_000_000.0, tz=UTC)
    assert first.fetched_at == NOW
    assert waits == []


def test_a_cache_hit_does_not_need_the_key_at_all(http: RecordedFantasyPros, store: CacheStore):
    _client(http, store).experts()
    absent = resolve_credentials(
        FANTASYPROS_CREDENTIALS, environ={}, keychain_reader=lambda s: None, config={}
    )
    hit = _client(http, store, credentials=absent).experts()
    assert hit.cached is True
    assert len(http.calls) == 1


def test_fresh_refetches_and_no_cache_bypasses(http: RecordedFantasyPros, store: CacheStore):
    _client(http, store).experts()
    refreshed = _client(http, store, mode=CacheMode.FRESH).experts()
    bypassed = _client(http, store, mode=CacheMode.BYPASS).experts()
    assert refreshed.cached is False and bypassed.cached is False
    assert len(http.calls) == 3


@pytest.mark.parametrize(
    ("call", "resource", "facets"),
    [
        (
            lambda c: c.players(),
            Resource.FANTASYPROS_PLAYERS,
            {},
        ),
        (
            lambda c: c.consensus_rankings(week=WEEK, position="rb", scoring="ppr"),
            Resource.FANTASYPROS_CONSENSUS,
            {"week": "6", "position": "RB", "scoring": "PPR"},
        ),
        (lambda c: c.rankings(week=WEEK), Resource.FANTASYPROS_RANKINGS, {"week": "6"}),
        (
            lambda c: c.projections(week=WEEK, positions=["RB"]),
            Resource.FANTASYPROS_PROJECTIONS,
            {"week": "6", "position": "RB"},
        ),
        (
            lambda c: c.injuries(week=WEEK),
            Resource.FANTASYPROS_INJURIES,
            {"week": "6"},
        ),
        (
            lambda c: c.news(limit=5, category="injury"),
            Resource.FANTASYPROS_NEWS,
            {"category": "injury"},
        ),
        (lambda c: c.experts(), Resource.FANTASYPROS_EXPERTS, {}),
        (
            lambda c: c.player_points(position="RB", scoring="PPR", start=5, end=5),
            Resource.FANTASYPROS_PLAYER_POINTS,
            {"position": "RB", "scoring": "PPR", "weeks": "5-5"},
        ),
    ],
    ids=["players", "consensus", "rankings", "projections", "injuries", "news", "experts", "pts"],
)
def test_each_endpoint_is_tagged_by_sport_season_and_its_facets_with_its_ttl(
    http: RecordedFantasyPros, store: CacheStore, call: Any, resource: Resource, facets: dict
):
    call(_client(http, store))
    store.close()
    connection = sqlite3.connect(store.path)
    try:
        ((expires_at,),) = connection.execute("SELECT expires_at FROM entries").fetchall()
        tags = {row[0] for row in connection.execute("SELECT tag FROM entry_tags").fetchall()}
    finally:
        connection.close()
    expected = {season_tag(SOURCE, SEASON), facet_tag(SOURCE, SEASON, "sport", "nfl")}
    expected |= {facet_tag(SOURCE, SEASON, name, value) for name, value in facets.items()}
    assert tags == expected
    assert expires_at == 1_000_000.0 + TTL_SECONDS[resource]
    assert all(scope_of(tag) is TagScope.SEASON for tag in tags)


def test_a_weeks_entries_are_purgeable_by_their_week_facet_and_never_by_a_league(
    http: RecordedFantasyPros, store: CacheStore
):
    client = _client(http, store)
    client.consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    client.experts()
    week = facet_tag(SOURCE, SEASON, "week", str(WEEK))
    with pytest.raises(ValueError):
        store.purge_by_league_tag(week)
    assert store.purge_by_tag(week) == 1
    assert len(_rows(store)) == 1  # the experts entry, which has no week, survives


def test_every_fantasypros_resource_is_season_scoped_with_the_documented_ttl():
    assert FANTASYPROS_RESOURCES <= SEASON_SCOPED_RESOURCES
    hours = {r: TTL_SECONDS[r] / 3600 for r in FANTASYPROS_RESOURCES}
    assert hours[Resource.FANTASYPROS_PLAYERS] == 24
    assert hours[Resource.FANTASYPROS_RANKINGS] == 6
    assert hours[Resource.FANTASYPROS_CONSENSUS] == 6
    assert hours[Resource.FANTASYPROS_PROJECTIONS] == 6
    assert hours[Resource.FANTASYPROS_INJURIES] == 1
    assert hours[Resource.FANTASYPROS_NEWS] == 0.25


# --------------------------------------------------------------------------- #
# Shapes: every endpoint the next issues consume
# --------------------------------------------------------------------------- #


def test_players_carries_the_espn_join_key(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).players()
    assert isinstance(response, SourceResponse)
    assert response.tier == "premium"
    rows = response.payload["players"]
    assert response.payload["count"] == len(rows) == 5
    for row in rows:
        assert {"player_id", "player_name", "position_id", "team_id", "espn_id"} <= set(row)
        assert row["espn_id"]
    assert http.calls[0]["params"] == {"external_ids": "espn"}
    assert http.paths() == ["nfl/players"]


def test_players_can_narrow_to_one_fantasypros_id(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).players(player_id=DOCTOR_PROBE_PLAYER_ID)
    (row,) = response.payload["players"]
    assert row["player_id"] == DOCTOR_PROBE_PLAYER_ID
    assert http.calls[0]["params"] == {"external_ids": "espn", "player": DOCTOR_PROBE_PLAYER_ID}


def test_consensus_rankings_is_the_rich_one(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    payload = response.payload
    assert payload["position_id"] == "RB" and payload["scoring"] == "PPR"
    assert payload["type"] == "Weekly PPR"
    assert isinstance(payload["total_experts"], int) and payload["total_experts"] > 0
    assert payload["week"] == str(WEEK)
    for row in payload["players"]:
        assert {
            "player_id",
            "player_name",
            "rank_ecr",
            "rank_min",
            "rank_max",
            "rank_ave",
            "rank_std",
            "player_ecr_delta",
            "player_owned_espn",
            "player_owned_yahoo",
            "player_bye_week",
            "player_opponent",
            "player_game_kickoff_ts",
            "player_game_status",
            "player_yahoo_id",
        } <= set(row)
    assert http.calls[0]["params"] == {"week": WEEK, "position": "RB", "scoring": "PPR"}


def test_consensus_rankings_with_experts_shown_carries_per_expert_ranks(
    http: RecordedFantasyPros, store: CacheStore
):
    response = _client(http, store).consensus_rankings(
        week=WEEK, position="RB", scoring="STD", experts=True
    )
    assert http.calls[0]["params"]["experts"] == "show"
    assert all("experts" in row for row in response.payload["players"])
    assert "expert_names" in response.payload


def test_rankings_carries_every_scoring_and_position_in_one_call(
    http: RecordedFantasyPros, store: CacheStore
):
    response = _client(http, store).rankings(week=WEEK)
    assert http.calls[0]["params"] == {"week": WEEK, "rankstats": "true", "range": "true"}
    payload = response.payload
    scorings = {"STD", "PPR", "HALF", "ROS-STD", "ROS-PPR", "ROS-HALF", "DYN"}
    assert scorings <= set(payload["experts"])
    row = payload["players"][0]
    assert set(row["rank"]) >= {"ECR", "ECR_MIN", "ECR_MAX", "ECR_AVG", "ECR_STD"}
    assert set(row["rank"]["ECR"]) >= {"STD", "PPR", "HALF"}


def test_projections_carry_three_point_totals_and_never_send_scoring(
    http: RecordedFantasyPros, store: CacheStore
):
    response = _client(http, store).projections(week=WEEK, positions=["rb"])
    params = http.calls[0]["params"]
    assert params == {"week": WEEK, "position": "RB"}
    assert "scoring" not in params, "FantasyPros answers 403 to scoring= on projections"
    for row in response.payload["players"]:
        assert {"fpid", "name", "position_id", "team_id", "stats"} <= set(row)
        assert {"points", "points_ppr", "points_half"} <= set(row["stats"])
    assert "ros_projections" not in response.payload


def test_rest_of_season_projections_say_so(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).projections(positions=["RB"], ros=True)
    assert http.calls[0]["params"] == {"position": "RB", "ros": "true"}
    assert response.payload["ros_projections"] is True


def test_projections_join_positions_and_players_with_colons(
    http: RecordedFantasyPros, store: CacheStore
):
    override = RecordedFantasyPros(overrides={"projections": (200, _premium({"players": []}))})
    _client(override, store).projections(week=WEEK, positions=["RB", "WR"], players=[1, 2])
    assert override.calls[0]["params"] == {"week": WEEK, "positions": "RB:WR", "players": "1:2"}


def test_injuries_carry_practice_reports_and_a_probability(
    http: RecordedFantasyPros, store: CacheStore
):
    response = _client(http, store).injuries(week=WEEK)
    expected = {"year": SEASON, "week": WEEK, "include_probabilities": "true"}
    assert http.calls[0]["params"] == expected
    for row in response.payload["injuries"]:
        assert {
            "player_id",
            "name",
            "status",
            "status_short",
            "injury_type",
            "probability_of_playing",
            "practice_1",
            "practice_2",
            "practice_3",
            "ir_weeks",
            "injury_update_date",
        } <= set(row)


def test_injuries_narrow_by_player_ids_and_team(store: CacheStore):
    override = RecordedFantasyPros(overrides={"injuries": (200, _premium({"injuries": []}))})
    _client(override, store).injuries(
        week=WEEK, player_ids=[23070, 1], team_id="sea", include_probabilities=False
    )
    assert override.calls[0]["params"] == {
        "year": SEASON,
        "week": WEEK,
        "player_ids": "23070:1",
        "team_id": "SEA",
    }


def test_news_items_carry_the_impact_line(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).news(limit=5, category="injury")
    assert http.calls[0]["params"] == {"limit": 5, "category": "injury"}
    for item in response.payload["items"]:
        assert {
            "title",
            "desc",
            "impact",
            "author",
            "created",
            "player_id",
            "team_id",
            "categories",
            "link",
        } <= set(item)


def test_news_passes_player_and_order_through(store: CacheStore):
    override = RecordedFantasyPros(overrides={"news": (200, _premium({"items": []}))})
    _client(override, store).news(limit=3, player_id=25459, order_by="created")
    assert override.calls[0]["params"] == {"limit": 3, "fpid": 25459, "order_by": "created"}


def test_experts_carry_accuracy_ranks(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).experts()
    assert http.paths() == [f"nfl/{SEASON}/rankings/experts"]
    for row in response.payload["experts"]:
        assert {"expert_id", "name", "source", "accuracy_weekly", "accuracy_draft"} <= set(row)


def test_player_points_are_actuals_by_week(http: RecordedFantasyPros, store: CacheStore):
    response = _client(http, store).player_points(position="RB", scoring="PPR", start=5, end=5)
    assert http.calls[0]["params"] == {"position": "RB", "scoring": "PPR", "start": 5, "end": 5}
    rows = response.payload["players"]
    scored = [row for row in rows if "games" in row]
    assert scored, "the fixture holds at least one player who played"
    for row in rows:
        assert {"player_id", "player_name", "position_id", "team_id"} <= set(row)
    for row in scored:
        # A player with no game in the range carries only identity -- that is
        # the shape FantasyPros sends, not a defect in the fixture.
        assert {"games", "points", "average", "weeks"} <= set(row)


def test_the_source_entry_names_fantasypros_and_the_endpoint(
    http: RecordedFantasyPros, store: CacheStore
):
    """The API terms want FantasyPros named wherever its data goes (ADR-0011)."""
    source = _client(http, store).experts().as_source()
    assert source.name == "fantasypros:experts"
    assert source.cached is False
    assert source.fetched_at == NOW
    assert "http" not in source.name


# --------------------------------------------------------------------------- #
# Refusals before anything is sent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.consensus_rankings(week=WEEK, position="QUARTERBACK"),
        lambda c: c.consensus_rankings(week=WEEK, position="RB", scoring="full"),
        lambda c: c.consensus_rankings(week=0, position="RB"),
        lambda c: c.consensus_rankings(week=True, position="RB"),
        lambda c: c.rankings(week="6"),
        lambda c: c.projections(positions=["RUNNING BACK"]),
        lambda c: c.injuries(week=-1),
        lambda c: c.player_points(position="RB", scoring="PPR", start=5, end=4),
    ],
    ids=["position", "scoring", "week-zero", "week-bool", "week-str", "positions", "neg", "range"],
)
def test_an_argument_fantasypros_would_refuse_is_refused_here_without_a_request(
    http: RecordedFantasyPros, store: CacheStore, call: Any
):
    with pytest.raises(ConfigInvalidError) as caught:
        call(_client(http, store))
    assert caught.value.kind == "argument"
    assert caught.value.details["kind"] == "argument"
    assert caught.value.retryable is False
    assert http.calls == []


# --------------------------------------------------------------------------- #
# The three answers FantasyPros gives that are not data
# --------------------------------------------------------------------------- #


def _premium(extra: dict[str, Any]) -> dict[str, Any]:
    return {"sport": "NFL", "public_api": True, "tier": "premium", **extra}


def _consensus_body(http: RecordedFantasyPros) -> dict[str, Any]:
    key = next(k for k in http.bodies if k[0].endswith("consensus-rankings"))
    return json.loads(http.bodies[key])


def test_a_free_tier_answer_is_config_invalid_kind_tier_naming_the_upgrade(
    http: RecordedFantasyPros, store: CacheStore
):
    body = _consensus_body(http)
    body["tier"] = "free"
    body["limit"] = 10
    truncated = RecordedFantasyPros(overrides={"consensus-rankings": (200, body)})
    with pytest.raises(ConfigInvalidError) as caught:
        _client(truncated, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    error = caught.value.to_dict()
    assert error["code"] == "CONFIG_INVALID"
    assert error["retryable"] is False
    assert error["details"] == {
        "endpoint": "consensus-rankings",
        "tier": "free",
        "limit": 10,
        "kind": "tier",
    }
    assert "Hall of Fame" in error["remediation"]
    assert "10 rows" in error["message"]


def test_a_limit_key_alone_is_truncation_even_on_premium(
    http: RecordedFantasyPros, store: CacheStore
):
    body = _consensus_body(http)
    body["limit"] = 25
    limited = RecordedFantasyPros(overrides={"consensus-rankings": (200, body)})
    with pytest.raises(ConfigInvalidError) as caught:
        _client(limited, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert caught.value.kind == "tier"
    assert caught.value.details["limit"] == 25


def test_a_truncated_answer_is_never_cached_so_an_upgrade_takes_effect_at_once(
    http: RecordedFantasyPros, store: CacheStore
):
    body = _consensus_body(http)
    body["tier"] = "free"
    truncated = RecordedFantasyPros(overrides={"consensus-rankings": (200, body)})
    with pytest.raises(ConfigInvalidError):
        _client(truncated, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert _rows(store) == []
    # The account is upgraded; the very next call reads the premium answer.
    response = _client(http, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert response.tier == "premium" and response.cached is False


def test_a_200_with_a_message_is_config_invalid_kind_argument_carrying_it_verbatim(
    store: CacheStore,
):
    rejected = RecordedFantasyPros(
        overrides={"consensus-rankings": (200, {"message": "Invalid Position", "public_api": True})}
    )
    client = _client(rejected, store)
    # Our own validation would refuse an unknown position first, so the test
    # drives a position FantasyPros accepts and has the stub answer as it does
    # for one it does not.
    with pytest.raises(ConfigInvalidError) as caught:
        client.consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert caught.value.kind == "argument"
    assert "Invalid Position" in caught.value.message
    assert caught.value.details["message"] == "Invalid Position"
    assert caught.value.retryable is False
    assert _rows(store) == [], "an error body is not cached"


def test_a_400_with_a_message_is_the_same_argument_error_with_the_parameter_named(
    store: CacheStore,
):
    """What FantasyPros answered on 2026-10-16 when probed with ``position=XX``."""
    body = {"message": "Invalid Position", "parameter": "position", "valid_format": "QB, RB, ..."}
    refused = RecordedFantasyPros(overrides={"consensus-rankings": (400, body)})
    with pytest.raises(ConfigInvalidError) as caught:
        _client(refused, store).consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert caught.value.kind == "argument"
    assert caught.value.retryable is False
    assert caught.value.details == {
        "endpoint": "consensus-rankings",
        "status": 400,
        "message": "Invalid Position",
        "parameter": "position",
        "valid_format": "QB, RB, ...",
        "kind": "argument",
    }
    assert _rows(store) == []


@pytest.mark.parametrize("body", ["Bad Request", b"\xff\xfe", {"error": "no message key"}, [1]])
def test_a_4xx_without_a_message_is_unclassifiable_and_lands_on_availability(
    body: Any, store: CacheStore
):
    down = RecordedFantasyPros(overrides={"experts": (400, body)})
    with pytest.raises(ProviderUnavailableError) as caught:
        _client(down, store).experts()
    assert caught.value.details == {"status": 400, "endpoint": "experts"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_is_auth_expired_kind_auth(status: int, store: CacheStore):
    refused = RecordedFantasyPros(overrides={"experts": (status, "Forbidden")})
    with pytest.raises(AuthExpiredError) as caught:
        _client(refused, store).experts()
    assert caught.value.details == {"kind": "auth", "status": status, "endpoint": "experts"}
    assert caught.value.retryable is False
    assert "FANTASY_SPORTS_FANTASYPROS_API_KEY" in (caught.value.remediation or "")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"Retry-After": "7"}, 7.0),
        ({"retry-after": "2.5"}, 2.5),
        ({"Retry-After": "soon"}, None),
        ({"Retry-After": "-1"}, None),
        ({}, None),
    ],
)
def test_retry_after_is_read_case_insensitively_and_only_when_it_is_a_duration(
    header: dict, expected: float | None, store: CacheStore
):
    def _throttled(url, params=None, headers=None, timeout=None):
        from _harness import FakeResponse

        answer = FakeResponse("slow down", status_code=429)
        answer.headers = header
        return answer

    with pytest.raises(RateLimitedError) as caught:
        _client(_throttled, store).experts()
    assert caught.value.retry_after == expected


def test_projections_by_player_ids_alone_send_only_the_ids(store: CacheStore):
    override = RecordedFantasyPros(overrides={"projections": (200, _premium({"players": []}))})
    _client(override, store).projections(players=[7, 8])
    assert override.calls[0]["params"] == {"players": "7:8"}


def test_a_429_is_rate_limited_with_retry_after(store: CacheStore):
    def _throttled(url, params=None, headers=None, timeout=None):
        from _harness import FakeResponse

        answer = FakeResponse("slow down", status_code=429)
        answer.headers = {"Retry-After": "7"}
        return answer

    with pytest.raises(RateLimitedError) as caught:
        _client(_throttled, store).experts()
    assert caught.value.retry_after == 7.0
    assert caught.value.details["retry_after"] == 7.0
    assert caught.value.retryable is True


@pytest.mark.parametrize("status", [500, 502, 503, 404])
def test_any_other_status_is_provider_unavailable_and_not_retried_here(
    status: int, store: CacheStore
):
    down = RecordedFantasyPros(overrides={"experts": (status, "nope")})
    with pytest.raises(ProviderUnavailableError) as caught:
        _client(down, store).experts()
    assert caught.value.details == {"status": status, "endpoint": "experts"}
    assert len(down.calls) == 1


def test_a_body_that_is_not_json_is_schema_drift(store: CacheStore):
    drifted = RecordedFantasyPros(overrides={"experts": (200, "<html>maintenance</html>")})
    with pytest.raises(SchemaDriftError) as caught:
        _client(drifted, store).experts()
    assert caught.value.provider == SOURCE
    assert caught.value.path == ("experts",)


def test_a_body_that_is_a_json_list_is_schema_drift(store: CacheStore):
    drifted = RecordedFantasyPros(overrides={"experts": (200, [1, 2, 3])})
    with pytest.raises(SchemaDriftError):
        _client(drifted, store).experts()


def test_a_body_with_no_tier_is_schema_drift_not_truncation(store: CacheStore):
    drifted = RecordedFantasyPros(overrides={"experts": (200, {"experts": [], "public_api": True})})
    with pytest.raises(SchemaDriftError) as caught:
        _client(drifted, store).experts()
    assert caught.value.path == ("experts", "tier")


def test_a_body_missing_its_list_is_schema_drift(store: CacheStore):
    drifted = RecordedFantasyPros(overrides={"experts": (200, _premium({"experts": "none"}))})
    with pytest.raises(SchemaDriftError) as caught:
        _client(drifted, store).experts()
    assert caught.value.path == ("experts", "experts")


def test_a_corrupt_cached_body_is_schema_drift_rather_than_a_crash(
    http: RecordedFantasyPros, store: CacheStore
):
    """The second parse, on the cache hit, is defended the same as the first."""
    _client(http, store).experts()
    store.close()
    connection = sqlite3.connect(store.path)
    with connection:
        connection.execute("UPDATE entries SET body = 'not json'")
    connection.close()
    with pytest.raises(SchemaDriftError):
        _client(http, store).experts()


# --------------------------------------------------------------------------- #
# The key never leaks: errors, the cache, a cassette
# --------------------------------------------------------------------------- #


def test_the_key_is_scrubbed_from_an_error_message_that_echoes_it(store: CacheStore):
    echo = RecordedFantasyPros(
        overrides={"experts": (200, {"message": f"bad key {FAKE_FP_KEY}", "public_api": True})}
    )
    with pytest.raises(ConfigInvalidError) as caught:
        _client(echo, store).experts()
    rendered = json.dumps(caught.value.to_dict()) + str(caught.value) + repr(caught.value)
    assert FAKE_FP_KEY not in rendered
    assert "***redacted***" in caught.value.message


def test_a_body_that_echoes_the_key_reaches_sqlite_redacted(store: CacheStore):
    body = _premium({"experts": [{"name": "x", "note": f"key={FAKE_FP_KEY}"}]})
    compressed = gzip.compress(json.dumps(body).encode())
    echo = RecordedFantasyPros(overrides={"experts": (200, compressed)})
    response = _client(echo, store).experts()
    assert FAKE_FP_KEY not in json.dumps(response.payload)
    ((url, stored),) = _rows(store)
    assert FAKE_FP_KEY not in stored and FAKE_FP_KEY not in url
    assert "***redacted***" in stored


def test_a_recorded_cassette_cannot_carry_the_key_in_header_or_body(tmp_path: Path):
    """Through the real vcrpy hooks, read back off disk (``docs/testing.md`` §7)."""
    from conftest import build_vcr, scan_file
    from vcr.request import Request

    from fantasy_sports.auth.chain import Secret

    Secret(FAKE_FP_KEY)  # registered, as the chain registers the real one
    path = tmp_path / "fantasypros.yaml"
    recorder = build_vcr(record_mode="all")
    with recorder.use_cassette(str(path)) as cassette:
        request = Request(
            method="GET",
            uri=f"{BASE_URL}/nfl/players?external_ids=espn",
            body=None,
            headers={API_KEY_HEADER: FAKE_FP_KEY, "Accept": "application/json"},
        )
        response = {
            "status": {"code": 200, "message": "OK"},
            "headers": {"Content-Type": ["application/json"]},
            "body": {"string": json.dumps({"echo": FAKE_FP_KEY, "tier": "premium"}).encode()},
        }
        cassette.append(request, response)
    text = path.read_text(encoding="utf-8")
    assert FAKE_FP_KEY not in text
    assert "x-api-key" in text, "the header is present with its value replaced, not deleted"
    assert scan_file(path) == []


def test_the_committed_cassette_holds_the_header_redacted_and_no_key_shape():
    from conftest import CASSETTE_LIBRARY_DIR, scan_file

    path = CASSETTE_LIBRARY_DIR / "fantasypros" / "premium_2026.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count("x-api-key") == 11
    assert "- REDACTED" in text
    assert scan_file(path) == []


def test_the_committed_cassette_replays_through_real_requests(
    monkeypatch: pytest.MonkeyPatch, store: CacheStore
):
    """The fixture is a faithful vcrpy recording: real ``requests``, write-protected."""
    from conftest import CASSETTE_LIBRARY_DIR, build_vcr

    monkeypatch.delenv("FANTASY_SPORTS_FANTASYPROS_API_KEY", raising=False)
    credentials = _env_set()
    recorder = build_vcr(record_mode="none")
    client = FantasyProsClient(
        credentials, season=SEASON, store=store, pacer=_pacer(), now=lambda: NOW
    )
    with recorder.use_cassette(str(CASSETTE_LIBRARY_DIR / "fantasypros" / "premium_2026.yaml")):
        response = client.players(player_id=DOCTOR_PROBE_PLAYER_ID)
    assert response.payload["players"][0]["player_id"] == DOCTOR_PROBE_PLAYER_ID


# --------------------------------------------------------------------------- #
# Odds and ends
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("moment", "season"),
    [
        (datetime(2026, 10, 16, tzinfo=UTC), 2026),
        (datetime(2027, 1, 20, tzinfo=UTC), 2026),
        (datetime(2027, 2, 28, tzinfo=UTC), 2026),
        (datetime(2027, 3, 1, tzinfo=UTC), 2027),
    ],
)
def test_the_default_season_follows_the_nfl_calendar(moment: datetime, season: int):
    assert default_season(moment) == season
    assert FantasyProsClient(now=lambda: moment).season == season


def test_the_default_store_is_the_shared_xdg_cache(isolated_home: Path):
    client = FantasyProsClient(season=SEASON)
    assert client.store.path == isolated_home / "cache" / "fantasy-sports" / "http-cache.sqlite3"


def test_the_module_imports_no_http_stack_at_import_time():
    import subprocess
    import sys

    probe = (
        "import sys, json, fantasy_sports.sources.fantasypros; "
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    roots = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not roots & {"requests", "keyring", "typer", "click", "rich", "espn_api"}


def test_consensus_rankings_ros_asks_for_the_ros_type_and_sends_no_week(
    http: RecordedFantasyPros, store: CacheStore
):
    body = json.dumps(_consensus_body(http))
    served = RecordedFantasyPros(overrides={"consensus-rankings": (200, body)})
    client = _client(served, store)
    client.consensus_rankings(position="rb", scoring="ppr", ros=True)
    assert served.calls[0]["params"] == {"type": "ros", "position": "RB", "scoring": "PPR"}
    # A ROS list and a week's list are different entries, not one.
    client.consensus_rankings(week=WEEK, position="RB", scoring="PPR")
    assert len(served.calls) == 2


def test_consensus_rankings_ros_refuses_a_week(http: RecordedFantasyPros, store: CacheStore):
    with pytest.raises(ConfigInvalidError) as caught:
        _client(http, store).consensus_rankings(week=WEEK, position="RB", ros=True)
    assert caught.value.details["kind"] == "argument"
    assert http.calls == []


def test_consensus_rankings_without_ros_still_requires_a_week(
    http: RecordedFantasyPros, store: CacheStore
):
    with pytest.raises(ConfigInvalidError):
        _client(http, store).consensus_rankings(position="RB")
    assert http.calls == []
