"""The ESPN adapter, offline.

Two cassettes and one stub transport, each covering what the others cannot.

``espn/canary_2018.yaml`` is a **real recording** of ESPN's public test league
(``1234``, ``2018``), the one ``espn-api``'s own integration test has hit daily
for years. It is what proves this adapter reads the shapes ESPN actually sends.
It cannot cover free agents, the activity feed, or a playoff week: ``espn-api``
refuses free agents and the activity feed before 2019, league 1234 exists only
for 2018, and every one of its matchup periods is 1:1 with a scoring period.

``espn/synthetic_2026.yaml`` is **hand-authored** (``scripts/build_synthetic_cassette.py``)
and covers exactly those three. A synthetic payload proves the adapter reads the
shape it was told about, not that ESPN still sends it — which is why the split
exists rather than one fixture doing both.

Neither can produce an HTTP failure, so the status-code and drift tests inject a
transport instead. That is deliberate and not a shortcut: recording a 429 would
mean provoking one from ESPN, and a cassette of a 401 would record the *absence*
of a credential, which is indistinguishable from a recording where the scrub ran.

Nothing here touches the network. ``pytest-socket`` is on, so a cassette miss
fails loudly instead of quietly calling ESPN.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import build_vcr

from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    ErrorCode,
    LeagueNotFoundError,
    NotAvailableError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.models import FreeAgent, League, Matchup, RosterSlot, Team, Transaction
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output.envelope import Envelope, NaiveDatetimeError
from fantasy_sports.providers.base import PROVIDER_METHODS, Provider
from fantasy_sports.providers.espn import (
    AUTH_REASONS,
    AuthReason,
    EspnProvider,
    _retry_after_seconds,
)

CANARY = ("1234", 2018)
SYNTHETIC = ("99", 2026)

#: The two kickoff instants `scripts/build_synthetic_cassette.py` writes, as
#: epoch milliseconds. Asserting against these is what catches a naive,
#: host-local datetime leaking through from `espn-api`.
KICKOFF_EARLY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
KICKOFF_LATE = datetime(2026, 9, 13, 20, 15, tzinfo=UTC)
TRADE_AT = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
WAIVER_AT = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)


@contextmanager
def _cassette(name: str) -> Iterator[None]:
    with build_vcr().use_cassette(name, record_mode="none", allow_playback_repeats=True):
        yield


@pytest.fixture(scope="module")
def canary() -> Iterator[EspnProvider]:
    """One provider against the real recording, shared across the module.

    Shared on purpose: building the ``espn-api`` league costs four requests and
    the adapter memoises it, so this fixture also demonstrates that the
    memoisation holds.
    """
    with _cassette("espn/canary_2018.yaml"):
        yield EspnProvider()


@pytest.fixture(scope="module")
def synthetic() -> Iterator[EspnProvider]:
    with _cassette("espn/synthetic_2026.yaml"):
        yield EspnProvider()


# --------------------------------------------------------------------------- #
# A stub transport, for what a cassette cannot record
# --------------------------------------------------------------------------- #


@dataclass
class FakeResponse:
    """The three attributes this adapter reads off a ``requests`` response."""

    status_code: int = 200
    body: Any = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def content(self) -> bytes:
        if isinstance(self.body, bytes | str):
            return self.body.encode() if isinstance(self.body, str) else self.body
        return json.dumps(self.body).encode()

    def json(self) -> Any:
        return json.loads(self.content)


class ReplayHttp:
    """Serve a committed cassette through the transport seam, with overrides.

    Some behaviours are a *payload* variation on an otherwise healthy league —
    a scoring period with no transactions, a view whose shape drifted — and a
    second cassette per variation would be a second thing to keep in step with
    the first. Replaying the fixture and substituting one view keeps the
    variation next to the assertion that needs it.
    """

    def __init__(self, name: str, overrides: Mapping[str, Any] | None = None) -> None:
        import yaml
        from conftest import CASSETTE_LIBRARY_DIR

        document = yaml.safe_load((CASSETTE_LIBRARY_DIR / name).read_text())
        self.bodies = {
            _query_key(interaction["request"]["uri"]): interaction["response"]["body"]["string"]
            for interaction in document["interactions"]
        }
        self.overrides = dict(overrides or {})

    def __call__(self, url: str, params=None, headers=None, cookies=None) -> FakeResponse:
        pairs: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            values = value if isinstance(value, list | tuple) else [value]
            pairs.extend((str(name), str(item)) for item in values)
        key = _query_key(url, pairs)
        view = dict(pairs).get("view")
        if view in self.overrides:
            return FakeResponse(body=self.overrides[view])
        body = self.bodies.get(key)
        if body is None:
            raise AssertionError(f"no recorded response for {key}")
        return FakeResponse(body=body)


def _query_key(url: str, extra: list[tuple[str, str]] | None = None) -> tuple[Any, ...]:
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    pairs = list(parse_qsl(parts.query)) + list(extra or [])
    return (parts.path, tuple(sorted(pairs)))


class FakeHttp:
    """A transport that answers from a queue and records what it was asked."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str, params=None, headers=None, cookies=None) -> FakeResponse:
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


@pytest.fixture
def no_alternate_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make ``espn-api``'s library-owned 401 retry fail, and record that it ran.

    The alternate-URL-shape probe lives inside ``checkRequestStatus`` and issues
    its own ``requests.get``, which our transport never sees. Stubbing the
    module's ``requests`` is the only way to observe it — and observing it
    matters, because the probe being mandatory rather than optional is the whole
    of ARCHITECTURE §14 item 1.
    """
    from espn_api.requests import espn_requests

    attempted: list[str] = []

    class _Requests:
        @staticmethod
        def get(url, params=None, headers=None, cookies=None):
            attempted.append(url)
            return FakeResponse(status_code=401, body=_denied())

    monkeypatch.setattr(espn_requests, "requests", _Requests)
    return attempted


@pytest.fixture
def alternate_probe_succeeds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The other half: the retry works, so the 401 was never about credentials."""
    from espn_api.requests import espn_requests

    attempted: list[str] = []

    class _Requests:
        @staticmethod
        def get(url, params=None, headers=None, cookies=None):
            attempted.append(url)
            return FakeResponse(status_code=200, body={"recovered": True})

    monkeypatch.setattr(espn_requests, "requests", _Requests)
    return attempted


def _denied() -> dict[str, Any]:
    """ESPN's real 401 body, as probed on 2026-09-05."""
    return {
        "messages": ["You are not authorized to view this League."],
        "details": [
            {
                "message": "You are not authorized to view this League.",
                "shortMessage": "You are not authorized to view this League.",
                "resolution": None,
                "type": "AUTH_LEAGUE_NOT_VISIBLE",
                "metaData": None,
            }
        ],
    }


CREDENTIALS = {"espn_s2": "AEB" + "x" * 200, "swid": "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"}


@pytest.fixture(autouse=True)
def _clean_scrub_registry() -> Iterator[None]:
    """The scrub set is process-global; a test that registers must not leak it."""
    yield
    forget_secrets()


# --------------------------------------------------------------------------- #
# Conformance — every method, against the real recording
# --------------------------------------------------------------------------- #


def test_the_adapter_satisfies_the_protocol_by_answering_every_method(canary: EspnProvider):
    """``isinstance`` is a smoke test; calling every method is the contract.

    ``isinstance(x, Provider)`` checks that members *exist* and never their
    signatures or return types, so an adapter whose ``fetch_matchups`` forgot
    ``week`` would pass it. Both assertions are here so the weak one cannot be
    mistaken for the strong one.
    """
    assert isinstance(canary, Provider)
    assert set(PROVIDER_METHODS) <= set(dir(canary))

    assert isinstance(canary.fetch_league(*CANARY), League)
    assert all(isinstance(item, Team) for item in canary.fetch_teams(*CANARY))
    assert all(isinstance(item, Team) for item in canary.fetch_standings(*CANARY))
    assert all(isinstance(item, RosterSlot) for item in canary.fetch_roster(*CANARY, "1"))
    assert all(isinstance(item, Matchup) for item in canary.fetch_matchups(*CANARY, 1))
    assert all(isinstance(item, Transaction) for item in canary.fetch_transactions(*CANARY))
    assert isinstance(canary.fetch_raw(*CANARY, view="mSettings"), dict)
    assert canary.credential_specs()[0].name == "espn_s2"


def test_a_repeated_view_does_not_overwrite_its_own_record(canary: EspnProvider):
    """R1's harder half: a view asked the same question seventeen times.

    ``fetch_transactions(since=...)`` sweeps ``mTransactions2`` across every
    scoring period in the season. Filing all of them under the view name keeps
    the last and silently drops the rest, which is the data loss R1 exists to
    prevent -- and it is invisible, because the *normalized* output is still
    complete.
    """
    canary.fetch_transactions(*CANARY, since=datetime(1970, 1, 1, tzinfo=UTC))
    record = canary.last_fetch
    assert record is not None
    swept = [key for key in record.responses if key.startswith("mTransactions2")]
    assert len(swept) > 1
    assert record.payload("mTransactions2") is not None, "a bare view name still resolves"
    assert len({id(record.responses[key].payload) for key in swept}) == len(swept)


def test_a_read_records_every_contributing_response_keyed_by_request(canary: EspnProvider):
    """R1: a composite read fans out, and one payload would drop data."""
    canary.fetch_league(*CANARY)
    record = canary.last_fetch
    assert record is not None
    assert set(record.responses) >= {
        "mTeam+mRoster+mMatchup+mSettings+mStandings",
        "players_wl",
        "proTeamSchedules_wl",
        "mDraftDetail",
    }
    # Every contributing fetch reports its own age, rather than one blended
    # number that hides a stale sub-fetch.
    assert len(record.sources) == len(record.responses)
    assert all(source.fetched_at.tzinfo is not None for source in record.sources)
    assert record.payload("players_wl") is not None
    assert record.payload("nothing-fetched-this") is None


def test_the_league_carries_its_roster_slot_configuration(canary: EspnProvider):
    """R3a: a legal target lineup must be constructible from normalized output.

    Derived from ``rosterSettings.lineupSlotCounts`` rather than from
    ``espn-api``'s ``position_slot_counts``, which zips counts against
    ``list(POSITION_MAP.values())[:n]`` and so depends on the source ordering of
    a dict holding both directions of the id/name map.
    """
    league = canary.fetch_league(*CANARY)
    assert league.provider == "espn"
    assert league.provider_id == "1234"
    assert league.season == 2018
    assert league.team_count == 10
    assert league.roster_slots["QB"] == 1
    assert league.roster_slots["RB"] == 2
    assert league.roster_slots["BE"] == 7
    # The league's own slice, not the whole bootstrap: teams and schedule are
    # what `fetch_teams` and `fetch_matchups` return.
    assert "settings" in league.raw
    assert "teams" not in league.raw
    assert "schedule" not in league.raw


def test_a_team_carries_its_own_payload_without_the_roster(canary: EspnProvider):
    """``raw`` is the object's own slice; nothing is lost, it moved up a level.

    A canary team's roster is 74 KB of the 75 KB ESPN sends for it. Carrying it
    on every ``Team`` would make ``teams`` answer three quarters of a megabyte.
    """
    teams = canary.fetch_teams(*CANARY)
    assert len(teams) == 10
    team = teams[0]
    assert team.provider == "espn"
    assert "record" in team.raw
    assert "roster" not in team.raw
    # ...and the untouched response is still there, one level up.
    record = canary.last_fetch
    assert record is not None
    bootstrap = record.payload("mTeam+mRoster+mMatchup+mSettings+mStandings")
    assert "roster" in bootstrap["teams"][0]


def test_owner_names_are_plural_and_absence_is_not_drift(canary: EspnProvider):
    """ESPN's ``owners`` is a list from day one, so a ``str`` loses a co-manager.

    The canary's ``members`` carry ids and *no display names at all* — probed
    against real ESPN on 2026-09-05 — so the honest answer here is an empty
    tuple rather than an invented one. Names are exercised against the
    synthetic fixture, where members have them.
    """
    for team in canary.fetch_teams(*CANARY):
        assert isinstance(team.owner_names, tuple)


def test_the_owner_join_survives_redaction_in_the_recording(canary: EspnProvider):
    """The stable-pseudonym guarantee (#38), verified through this adapter.

    ESPN uses the SWID as a **join key inside a single payload**:
    ``teams[].owners`` points at ``members[].id``. Before #38 every one of them
    was rewritten to one shared ``{SWID-REDACTED}``, which turned ten teams and
    ten members into a ten-by-ten ambiguity and made ``owner_names``
    underivable. This asserts on the recording as it exists on disk, so it goes
    red if a future scrubber change flattens the key again.
    """
    teams = canary.fetch_teams(*CANARY)
    record = canary.last_fetch
    assert record is not None
    bootstrap = record.payload("mTeam+mRoster+mMatchup+mSettings+mStandings")

    owners = [tuple(team.raw["owners"]) for team in teams]
    assert all(len(item) == 1 for item in owners), "a canary team has exactly one owner"
    assert len({item[0] for item in owners}) == len(teams), "owner ids collapsed to one value"

    # Redacted, not real: no brace-wrapped GUID survives, and every pseudonym
    # carries #38's reserved sentinel first group.
    assert all(item[0].startswith("{00000000-") for item in owners)

    # ...and each of those still resolves to exactly one member, which is the
    # join itself rather than a property of the ids.
    members = {member["id"] for member in bootstrap["members"]}
    assert all(item[0] in members for item in owners)


def test_standings_are_ordered_and_ranked_by_the_adapter(canary: EspnProvider):
    """§14 item 15: the tiebreaker source lives in the adapter, never in core/.

    ESPN returns no sorted standings resource at all. ``standing`` here is this
    adapter's own 1-based position in the order it chose, so it stays meaningful
    in a league whose seed field is unset.
    """
    standings = canary.fetch_standings(*CANARY)
    assert [team.standing for team in standings] == list(range(1, 11))
    assert standings[0].wins >= standings[-1].wins


def test_a_roster_slot_carries_slot_eligibility_and_lock_state(canary: EspnProvider):
    """R3: an agent cannot construct a legal lineup from position alone."""
    roster = canary.fetch_roster(*CANARY, "1")
    assert roster
    starters = [slot for slot in roster if slot.is_starter]
    assert starters and len(starters) < len(roster)
    assert all(slot.slot for slot in starters)
    assert all(slot.player.eligible_slots for slot in roster)
    assert all("BE" not in (slot.slot,) for slot in starters)


def test_a_past_week_roster_reads_that_week_not_the_current_one(canary: EspnProvider):
    """``load_roster_week`` replaces the object model's rosters in place.

    So the bootstrap's copy is now the wrong week, and the raw entries have to
    come from the standalone ``mRoster`` response instead.
    """
    week_one = canary.fetch_roster(*CANARY, "1", 1)
    assert week_one
    record = canary.last_fetch
    assert record is not None
    assert "mRoster@1" in record.responses
    assert record.payload("mRoster") is not None


def test_transactions_come_back_normalized_and_time_ordered(canary: EspnProvider):
    """The full-season sweep, against a real ``mTransactions2`` recording."""
    swept = canary.fetch_transactions(*CANARY, since=datetime(1970, 1, 1, tzinfo=UTC))
    assert len(swept) > 50
    assert {item.type for item in swept} <= {"add", "drop", "trade", "waiver_claim"}
    stamps = [item.timestamp for item in swept if item.timestamp is not None]
    assert stamps == sorted(stamps)
    assert all(stamp.tzinfo is not None for stamp in stamps)


def test_since_filters_the_swept_transactions(canary: EspnProvider):
    everything = canary.fetch_transactions(*CANARY, since=datetime(1970, 1, 1, tzinfo=UTC))
    cutoff = max(item.timestamp for item in everything if item.timestamp) - timedelta(days=1)
    recent = canary.fetch_transactions(*CANARY, since=cutoff)
    assert recent
    assert len(recent) < len(everything)
    assert all(item.timestamp >= cutoff for item in recent if item.timestamp)


def test_fetch_raw_is_a_passthrough_and_needs_a_view(canary: EspnProvider):
    payload = canary.fetch_raw(*CANARY, view="mSettings")
    assert payload["id"] == 1234
    assert "settings" in payload
    with pytest.raises(ValueError, match="no default view"):
        canary.fetch_raw(*CANARY)


# --------------------------------------------------------------------------- #
# What the canary cannot record
# --------------------------------------------------------------------------- #


def test_the_owner_to_member_join_produces_a_name_per_team(synthetic: EspnProvider):
    """``teams[].owners`` joined against ``members[].id``.

    The canary cannot show this: its members have no display names, and its real
    SWIDs are collapsed to one placeholder by the scrub hook that guards
    recorded fixtures. Distinct, non-GUID owner ids in the synthetic fixture are
    what keep the join observable — and are the shape the stable-pseudonym work
    (jwulff/fantasy-sports#38) produces.
    """
    teams = {team.provider_id: team for team in synthetic.fetch_teams(*SYNTHETIC)}
    # `firstName`/`lastName` beat `displayName`: in a real league half the
    # display names are account handles that name nobody (#29).
    assert teams["1"].owner_names == ("Ann Alpha",)
    assert teams["2"].owner_names == ("Bo Bravo",)


def test_a_playoff_week_keeps_the_scoring_and_matchup_periods_distinguishable(
    synthetic: EspnProvider,
):
    """The split only bites when a matchup period spans two scoring periods.

    Scoring period 14 belongs to matchup period 13 in this league.
    ``espn-api``'s ``scoreboard(week)`` filters on ``matchupPeriodId``, so
    passing the scoring period straight through would silently return the wrong
    round — or nothing.
    """
    playoff = synthetic.fetch_matchups(*SYNTHETIC, 14)
    assert len(playoff) == 1
    assert playoff[0].scoring_period_id == 14
    assert playoff[0].matchup_period_id == 13
    assert playoff[0].week == 13
    assert playoff[0].is_playoff is True

    regular = synthetic.fetch_matchups(*SYNTHETIC, 2)
    assert regular[0].scoring_period_id == regular[0].matchup_period_id == 2
    assert regular[0].is_playoff is False
    # Identical in the regular season, which is exactly why the split is easy
    # to miss — and why both values are carried even when they agree.
    assert regular[0].team_a_score and regular[0].team_b_score


def test_a_trade_present_only_in_the_activity_feed_still_appears(synthetic: EspnProvider):
    """ESPN's two transaction surfaces, reconciled inside the adapter.

    The synthetic ``mTransactions2`` payload carries a waiver claim and no
    trade; the activity feed carries the trade and no waiver claim. An adapter
    reading either surface alone returns one row.
    """
    found = synthetic.fetch_transactions(*SYNTHETIC)
    kinds = sorted(item.type for item in found)
    assert kinds == ["trade", "trade", "waiver_claim"]

    trades = [item for item in found if item.type == "trade"]
    # Two rows, one per team, from the feed's TRADE_SENT/TRADE_RECEIVED pair —
    # neither of which is a string `mTransactions2` ever uses.
    assert {trade.team_provider_id for trade in trades} == {"1", "2"}
    sent = next(trade for trade in trades if trade.players_out)
    received = next(trade for trade in trades if trade.players_in)
    assert sent.players_out == ("2002",)
    assert received.players_in == ("2002",)
    assert sent.timestamp == TRADE_AT

    claim = next(item for item in found if item.type == "waiver_claim")
    assert claim.players_in == ("3001",)
    assert claim.players_out == ("2003",)
    assert claim.faab_spent == 17
    assert claim.timestamp == WAIVER_AT


def test_a_trade_proposal_is_not_a_roster_move(synthetic: EspnProvider):
    """``TRADE_PROPOSAL`` has no honest home in the four-value vocabulary.

    The narrowing is lossy on purpose and documented on ``core.Transaction``:
    filtering on ``type == "trade"`` will not show a proposal, which stays
    readable through ``fetch_raw``.
    """
    found = synthetic.fetch_transactions(*SYNTHETIC)
    assert all("PROPOSAL" not in json.dumps(item.raw) for item in found)


def test_free_agents_carry_ownership_and_projection(synthetic: EspnProvider):
    agents = synthetic.fetch_free_agents(*SYNTHETIC, 2)
    assert len(agents) == 2
    assert all(isinstance(item, FreeAgent) for item in agents)
    assert agents[0].percent_owned == pytest.approx(61.5)
    assert agents[0].player.projected_points == pytest.approx(8.4)
    assert agents[0].raw["status"] == "FREEAGENT"


def test_a_position_espn_would_silently_ignore_is_refused(synthetic: EspnProvider):
    """ESPN answers an unknown ``filterSlotIds`` with its *default* player set.

    Not an error — a plausible-looking list that is not what was asked for,
    which is the worst failure mode available. Refusing here is the only place
    it can be caught.
    """
    with pytest.raises(ValueError, match="not an ESPN position"):
        synthetic.fetch_free_agents(*SYNTHETIC, 2, position="PUNTER")
    assert synthetic.fetch_free_agents(*SYNTHETIC, 2, position="wr")


def test_a_season_without_free_agents_is_refused_by_name_not_returned_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    """The free-agents twin of the box-scores refusal (jwulff/fantasy-sports#45).

    ``espn-api`` refuses ``free_agents()`` before 2019 the same way it refuses
    ``box_scores()`` — a bare ``Exception`` naming the season, raised before
    ESPN is ever asked. It must land on ``NOT_AVAILABLE`` with
    ``retryable=False``, not on ``PROVIDER_UNAVAILABLE``'s bounded-retry
    instruction, which would tell an agent to keep asking ESPN for something it
    will never serve.
    """
    from fantasy_sports.providers import espn as adapter

    class _Refuses:
        def free_agents(self, **kwargs: Any):
            raise Exception("Cant use free agents before 2019")

    @contextmanager
    def _ctx(value: Any) -> Iterator[Any]:
        yield value

    monkeypatch.setattr(adapter.EspnProvider, "_read", lambda self, *a, **k: _ctx(_Refuses()))
    with pytest.raises(NotAvailableError) as err:
        adapter.EspnProvider().fetch_free_agents("99", 2018, 1)
    assert err.value.code == ErrorCode.NOT_AVAILABLE
    assert err.value.retryable is False
    assert "2018" in err.value.details["season"]
    assert "2019" in err.value.remediation


def test_a_position_filtered_read_gets_its_own_recording(synthetic: EspnProvider):
    """The corpus-level proof that the ``x-fantasy-filter`` matcher works.

    ``kona_player_info`` is scoped by the header, not the URL, so the filtered
    and unfiltered reads are the *same* URL. Under vcrpy's default matcher —
    method/scheme/host/port/path/query, headers ignored entirely — the second
    one replays the first one's body and the assertion below passes against the
    wrong payload. It is the cassette twin of the cache-key bug that put
    ``x-fantasy-filter`` into ``cache_key``'s ``extra``.
    """
    everyone = synthetic.fetch_free_agents(*SYNTHETIC, 2)
    receivers = synthetic.fetch_free_agents(*SYNTHETIC, 2, position="wr")

    assert {agent.player.name for agent in everyone} == {"Barbara Liskov", "Radia Perlman"}
    assert {agent.player.name for agent in receivers} == {"Barbara Liskov"}


# --------------------------------------------------------------------------- #
# Timezones — §14 item 8
# --------------------------------------------------------------------------- #


def test_kickoffs_are_utc_instants_and_do_not_move_with_the_host_timezone(
    synthetic: EspnProvider, monkeypatch: pytest.MonkeyPatch
):
    """``espn-api`` builds every datetime with no ``tz=``; nothing here uses one.

    Kickoff is re-derived from the raw epoch milliseconds in
    ``proTeamSchedules_wl``. The two instants below are written by
    ``scripts/build_synthetic_cassette.py`` and must render identically whatever
    the host's clock is set to — the failure this guards against is a league
    that reads differently in Seattle and on a UTC CI runner, with nothing
    anywhere reporting an error.
    """
    import time

    for zone in ("UTC", "America/Los_Angeles", "Pacific/Kiritimati"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        roster = synthetic.fetch_roster(*SYNTHETIC, "1")
        kickoffs = {slot.player.name: slot.player.kickoff for slot in roster}
        assert kickoffs["Ada Lovelace"] == KICKOFF_EARLY
        assert kickoffs["Katherine Johnson"] == KICKOFF_LATE
    monkeypatch.delenv("TZ")
    time.tzset()


def test_lock_state_follows_the_kickoff_that_was_re_derived(synthetic: EspnProvider):
    """R3: whether the slot can still be changed, from the kickoff instant."""
    before = EspnProvider(now=lambda: KICKOFF_EARLY - timedelta(hours=1))
    after = EspnProvider(now=lambda: KICKOFF_LATE + timedelta(hours=1))
    with _cassette("espn/synthetic_2026.yaml"):
        assert all(slot.is_locked is False for slot in before.fetch_roster(*SYNTHETIC, "1"))
        assert all(slot.is_locked is True for slot in after.fetch_roster(*SYNTHETIC, "1"))


def test_nothing_the_adapter_returns_can_reach_the_envelope_naive(synthetic: EspnProvider):
    """The output layer refuses a naive datetime, so this fails loudly or passes.

    Rendering the real objects through the real envelope is the assertion: a
    naive value anywhere in the graph raises ``NaiveDatetimeError`` rather than
    quietly emitting a timestamp wrong by the host's UTC offset.
    """
    record = synthetic.fetch_roster(*SYNTHETIC, "1")
    envelope = Envelope.success(
        provider="espn", data=[slot.to_dict() for slot in record], sources=[]
    )
    rendered = envelope.to_dict()
    assert rendered["data"][0]["player"]["kickoff"].endswith("Z")

    with pytest.raises(NaiveDatetimeError):
        Envelope.success(data={"kickoff": datetime(2026, 9, 13, 17, 0)}).to_dict()


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #


def _provider(*responses: FakeResponse, credentials: Mapping[str, str] | None = None):
    return EspnProvider(credentials, http=FakeHttp(*responses))


def test_a_401_that_succeeds_on_the_alternate_url_shape_returns_data(
    alternate_probe_succeeds: list[str],
):
    """The double-probe is mandatory, not a fallback (§14 item 1).

    ESPN's 401 body says ``AUTH_LEAGUE_NOT_VISIBLE`` whether the cookies are
    bad or the season simply wants the other URL shape, so the body cannot
    settle it and only retrying the other shape can. Here the retry works, and
    the read must succeed rather than report an auth failure.
    """
    provider = _provider(FakeResponse(status_code=401, body=_denied()))
    payload = provider.fetch_raw(*CANARY, view="mSettings")
    assert payload == {"recovered": True}
    assert alternate_probe_succeeds, "the library-owned alternate-shape probe never ran"
    assert "/leagueHistory/" in alternate_probe_succeeds[0]


def test_a_401_with_no_credentials_reports_auth_missing(no_alternate_probe: list[str]):
    """A fact about us, needing no evidence from ESPN."""
    provider = _provider(FakeResponse(status_code=401, body=_denied()))
    with pytest.raises(AuthMissingError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert no_alternate_probe, "the alternate-shape probe must run before classifying"
    payload = err.value.to_dict()
    assert payload["code"] == "AUTH_MISSING"
    assert payload["details"]["espn_reason"] == "AUTH_LEAGUE_NOT_VISIBLE"
    assert sorted(payload["details"]["missing"]) == ["espn_s2", "swid"]
    assert "auth login" in payload["remediation"]


def test_half_a_cookie_pair_is_missing_credentials_not_rejected_ones(
    no_alternate_probe: list[str],
):
    """``espn-api`` sends cookies only when it has both.

    One cookie on its own therefore reaches ESPN as an *unauthenticated*
    request. Reporting that as "your credentials were rejected" would send a
    user to re-extract a value that was never sent.
    """
    provider = _provider(
        FakeResponse(status_code=401, body=_denied()),
        credentials={"swid": CREDENTIALS["swid"]},
    )
    with pytest.raises(AuthMissingError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert err.value.details["missing"] == ["espn_s2"]


def test_a_401_with_credentials_refuses_to_guess_between_expiry_and_membership(
    no_alternate_probe: list[str],
):
    """``AUTH_LEAGUE_NOT_VISIBLE`` is evidence about nothing.

    Probed against a real private league on 2026-09-05, one variable at a time:
    no cookies, a valid SWID with an invalid ``espn_s2``, and either cookie
    alone all return a byte-identical 401 with this same reason. Reporting
    ``AUTH_EXPIRED`` from it would send users to re-extract cookies that were
    fine — the exact misdiagnosis §14 item 1 exists to prevent — so this
    reports ``LEAGUE_NOT_FOUND``, whose agent action covers both possibilities.
    """
    provider = _provider(FakeResponse(status_code=401, body=_denied()), credentials=CREDENTIALS)
    with pytest.raises(LeagueNotFoundError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    payload = err.value.to_dict()
    assert payload["code"] == "LEAGUE_NOT_FOUND"
    assert payload["details"]["reason"] == "credentials_or_membership"
    assert payload["details"]["espn_reason"] == "AUTH_LEAGUE_NOT_VISIBLE"
    assert "will not guess" in payload["message"]


def test_a_reason_that_proves_the_credential_failed_reports_auth_expired(
    no_alternate_probe: list[str], monkeypatch: pytest.MonkeyPatch
):
    """The branch that exists so a future observation is a one-line change.

    No ESPN reason type has ever been seen that positively proves the credential
    failed. When one is, adding a row to ``AUTH_REASONS`` is the whole change —
    this test pins that.
    """
    monkeypatch.setitem(
        AUTH_REASONS,
        "AUTH_LEAGUE_NOT_VISIBLE",
        AuthReason(explanation="Pretend ESPN told us.", proves_credentials_bad=True),
    )
    provider = _provider(FakeResponse(status_code=401, body=_denied()), credentials=CREDENTIALS)
    with pytest.raises(AuthExpiredError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert err.value.code.value == "AUTH_EXPIRED"


def test_an_unreadable_401_body_is_not_evidence(no_alternate_probe: list[str]):
    """No reason type is not the same as a reason type we did not recognise."""
    provider = _provider(
        FakeResponse(status_code=401, body=b"<html>go away</html>"), credentials=CREDENTIALS
    )
    with pytest.raises(LeagueNotFoundError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert "espn_reason" not in err.value.details
    assert "no machine-readable reason" in err.value.message


def test_an_unrecognised_401_reason_says_so_rather_than_guessing(
    no_alternate_probe: list[str],
):
    body = _denied()
    body["details"][0]["type"] = "AUTH_SOMETHING_NEW"
    provider = _provider(FakeResponse(status_code=401, body=body), credentials=CREDENTIALS)
    with pytest.raises(LeagueNotFoundError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert "has not seen before" in err.value.message
    assert err.value.details["espn_reason"] == "AUTH_SOMETHING_NEW"


def test_a_404_without_credentials_reports_that_they_may_be_required():
    """ESPN's pre-2018 gating surfaces as a 404, not a 401.

    Reporting a bare not-found sends the user to check an id that was correct,
    which is the same class of misdiagnosis the 401 probe exists to prevent.
    """
    provider = _provider(FakeResponse(status_code=404, body={"messages": ["Not Found"]}))
    with pytest.raises(LeagueNotFoundError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    payload = err.value.to_dict()
    assert payload["details"]["reason"] == "credentials_may_be_required"
    assert "auth login" in payload["remediation"]


def test_a_404_with_credentials_is_a_plain_not_found():
    provider = _provider(
        FakeResponse(status_code=404, body={"messages": ["Not Found"]}), credentials=CREDENTIALS
    )
    with pytest.raises(LeagueNotFoundError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert "reason" not in err.value.details


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        ("  30 ", 30.0),
        ("not-a-number", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
        (None, None),
        ("", None),
    ],
)
def test_retry_after_accepts_both_forms_and_refuses_to_guess(header, expected):
    """An invented backoff is worse than none, because a caller will trust it."""
    assert _retry_after_seconds(header) == expected


def test_a_429_becomes_rate_limited_with_the_retry_after_header():
    """``espn-api`` never reads ``Retry-After`` and folds 429 into a generic error.

    The header lives only on the response object, which the library's exception
    does not carry, so this can only happen before its status check.
    """
    provider = _provider(
        FakeResponse(status_code=429, body={}, headers={"Retry-After": "45"}),
        credentials=CREDENTIALS,
    )
    with pytest.raises(RateLimitedError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    payload = err.value.to_dict()
    assert payload["code"] == "RATE_LIMITED"
    assert payload["retryable"] is True
    assert payload["details"]["retry_after"] == 45.0


def test_a_429_recovered_from_the_library_message_has_no_invented_retry_after(
    monkeypatch: pytest.MonkeyPatch,
):
    """Belt and braces: the alternate-shape probe issues a request we never see.

    ``espn-api`` folds its status into English -- ``ESPN returned an HTTP 429``
    -- and keeps no structured attribute, so the status is recovered from the
    message. There is no ``Retry-After`` on this path because the header did not
    survive, and reporting one we do not have would be worse than reporting
    none.
    """
    from espn_api.requests.espn_requests import ESPNUnknownError

    from fantasy_sports.providers.espn import _unknown_status

    throttled = _unknown_status(ESPNUnknownError("ESPN returned an HTTP 429"), view="mTeam")
    assert isinstance(throttled, RateLimitedError)
    assert throttled.retry_after is None
    assert throttled.to_dict()["details"]["status"] == 429

    unclassifiable = _unknown_status(ESPNUnknownError("something else entirely"), view="mTeam")
    assert isinstance(unclassifiable, ProviderUnavailableError)
    assert "status" not in unclassifiable.details


def test_an_unclassifiable_status_is_unavailable_never_throttled():
    """R12: guessing ``RATE_LIMITED`` would teach an agent to back off forever."""
    provider = _provider(FakeResponse(status_code=503, body={}))
    with pytest.raises(ProviderUnavailableError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    payload = err.value.to_dict()
    assert payload["code"] == "PROVIDER_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["details"]["status"] == 503


def test_a_body_that_is_not_json_is_schema_drift():
    provider = _provider(FakeResponse(status_code=200, body=b"<html>maintenance</html>"))
    with pytest.raises(SchemaDriftError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    assert err.value.path == ("mSettings",)


def test_a_missing_key_is_schema_drift_carrying_the_offending_path():
    """``espn-api`` has no exception for this; every constructor is unguarded.

    A bare ``KeyError('record')`` from six frames down is indistinguishable
    from a bug in our own call. The view, the library frame, and the key
    together are what make the canary's auto-filed issue actionable.
    """
    bootstrap = {
        "gameId": 1,
        "id": 1234,
        "members": [],
        "schedule": [],
        "scoringPeriodId": 1,
        "seasonId": 2018,
        "status": {
            "currentMatchupPeriod": 1,
            "finalScoringPeriod": 16,
            "firstScoringPeriod": 1,
            "latestScoringPeriod": 16,
            "previousSeasons": [],
        },
        "settings": {
            "acquisitionSettings": {"isUsingAcquisitionBudget": False},
            "draftSettings": {"keeperCount": 0},
            "name": "Drifted",
            "rosterSettings": {"lineupSlotCounts": {}},
            "scheduleSettings": {
                "matchupPeriodCount": 1,
                "matchupPeriods": {"1": [1]},
                "playoffSeedingRule": "TOTAL_POINTS_SCORED",
                "playoffTeamCount": 2,
            },
            "scoringSettings": {"matchupTieRule": "NONE", "playoffMatchupTieRule": "NONE"},
            "size": 1,
            "tradeSettings": {"vetoVotesRequired": 0},
        },
        # ESPN renamed `record`; every constructor reads it unguarded.
        "teams": [{"id": 1, "abbrev": "AAA", "name": "Drifted", "divisionId": 0, "owners": []}],
    }
    provider = _provider(
        FakeResponse(body=bootstrap),
        FakeResponse(body=[{"id": 1, "fullName": "Nobody"}]),
        FakeResponse(body={"settings": {"proTeams": []}}),
    )
    with pytest.raises(SchemaDriftError) as err:
        provider.fetch_league(*CANARY)
    payload = err.value.to_dict()
    assert payload["code"] == "SCHEMA_DRIFT"
    assert payload["retryable"] is False
    assert "record" in payload["details"]["path"]
    assert any("espn_api" in part for part in payload["details"]["path"])
    assert payload["details"]["provider"] == "espn"


def test_a_half_built_league_is_not_memoised():
    """The next call would fail three layers from the real cause otherwise."""
    provider = _provider(FakeResponse(status_code=503, body={}))
    with pytest.raises(ProviderUnavailableError):
        provider.fetch_league(*CANARY)
    assert provider._leagues == {}


def test_an_empty_transaction_period_is_a_result_not_a_failure():
    """``espn-api`` raises a bare ``Exception('No transactions found')`` for it.

    That is what a quiet scoring period looks like -- the key is simply absent
    from an otherwise healthy 200. Reporting it as ``PROVIDER_UNAVAILABLE``
    would tell an agent to retry an answer that was already correct.
    """
    provider = EspnProvider(
        http=ReplayHttp(
            "espn/synthetic_2026.yaml",
            overrides={
                "mTransactions2": {"id": 99, "seasonId": 2026},
                "kona_league_communication": {"topics": []},
            },
        )
    )
    assert provider.fetch_transactions(*SYNTHETIC) == []


def test_an_activity_feed_that_will_not_load_degrades_rather_than_failing():
    """ESPN stopped serving the feed for historical seasons (issue #546).

    A transactions command that cannot read a 2018 league at all is a worse
    answer than one that reads the surface still being served.
    """
    provider = EspnProvider(
        http=ReplayHttp(
            "espn/synthetic_2026.yaml",
            overrides={"kona_league_communication": {"unexpected": "shape"}},
        )
    )
    found = provider.fetch_transactions(*SYNTHETIC)
    assert [item.type for item in found] == ["waiver_claim"]


def test_library_exception_text_never_carries_a_credential_shape():
    """``espn-api`` interpolated the raw ``espn_s2`` into its own message.

    Not hypothetical: it shipped that way for over a year, until commit
    ``78c239a`` in 2026-02. Two mechanisms cover it — the error base scrubs
    values this process was handed, and the pattern scrubber covers shapes we
    never held — and both are asserted here.
    """
    leaky = f"League cannot be accessed with espn_s2={CREDENTIALS['espn_s2']}"

    class _Boom:
        def __call__(self, url, params=None, headers=None, cookies=None):
            raise RuntimeError(leaky)

    provider = EspnProvider(CREDENTIALS, http=_Boom())
    with pytest.raises(ProviderUnavailableError) as err:
        provider.fetch_raw(*CANARY, view="mSettings")
    rendered = json.dumps(err.value.to_dict())
    assert CREDENTIALS["espn_s2"] not in rendered
    assert CREDENTIALS["swid"] not in rendered


def test_a_non_numeric_league_id_is_reported_before_a_request_is_made():
    provider = _provider()
    with pytest.raises(LeagueNotFoundError, match="numeric"):
        provider.fetch_league("not-a-league", 2018)
    with pytest.raises(LeagueNotFoundError, match="numeric"):
        provider.fetch_raw("not-a-league", 2018, view="mSettings")


def test_an_unknown_team_id_is_reported_as_not_found(synthetic: EspnProvider):
    with pytest.raises(LeagueNotFoundError, match="No team 77"):
        synthetic.fetch_roster(*SYNTHETIC, "77")
    with pytest.raises(LeagueNotFoundError, match="numeric"):
        synthetic.fetch_roster(*SYNTHETIC, "left-side")


def test_a_non_integer_week_is_refused(synthetic: EspnProvider):
    with pytest.raises(LeagueNotFoundError, match="scoring period"):
        synthetic.fetch_matchups(*SYNTHETIC, "week two")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Credentials and the cache seam
# --------------------------------------------------------------------------- #


def test_credential_specs_describe_visibility_not_a_predicted_lifetime():
    """No source states an ESPN cookie lifetime, so none is invented."""
    specs = {spec.name: spec for spec in EspnProvider().credential_specs()}
    assert set(specs) == {"espn_s2", "swid"}
    assert all(spec.required for spec in specs.values())
    assert "expiry is never predicted" in specs["espn_s2"].staleness


def test_credentials_are_revealed_once_and_accepted_from_the_auth_chain():
    """``Secret.reveal()`` belongs at the transport boundary and nowhere else."""
    from fantasy_sports.auth.chain import (
        CredentialSet,
        CredentialSource,
        ResolvedCredential,
        Secret,
    )

    resolved = CredentialSet(
        resolved={
            name: ResolvedCredential(name=name, source=CredentialSource.ENV, secret=Secret(value))
            for name, value in CREDENTIALS.items()
        }
    )
    assert EspnProvider(resolved).has_credentials
    assert EspnProvider({"espn_s2": Secret(CREDENTIALS["espn_s2"])}).missing_credentials == (
        "swid",
    )
    assert EspnProvider({"espn_s2": None}).missing_credentials == ("espn_s2", "swid")


def test_the_cache_sits_below_the_composite_calls(tmp_path):
    """§14 item 4: a cache above them still pays every round trip on a miss.

    The season-scoped views are the proof. ``players_wl`` and
    ``proTeamSchedules_wl`` come from an endpoint carrying no league id and are
    re-fetched by every league, so a second provider against a *different*
    league in the same season must serve them from the store rather than
    re-fetching — which only works if the cache is keyed below the library's
    composite calls.
    """
    from fantasy_sports.cache.store import CacheStore
    from fantasy_sports.cache.tags import TagScope, scope_of

    store = CacheStore(tmp_path / "cache.sqlite3")
    with _cassette("espn/synthetic_2026.yaml"):
        first = EspnProvider(cache=store)
        first.fetch_league(*SYNTHETIC)
        assert all(not response.cached for response in first.last_fetch.responses.values())

        second = EspnProvider(cache=store)
        second.fetch_league(*SYNTHETIC)
        assert second.last_fetch.responses["players_wl"].cached is True
        assert second.last_fetch.responses["proTeamSchedules_wl"].cached is True

    entry = store.get(
        next(iter(store._connect().execute("SELECT key FROM entries LIMIT 1").fetchone()))
    )
    assert entry is not None
    assert any(scope_of(tag) is TagScope.SEASON for tag in entry.tags)


def test_a_season_scoped_entry_is_never_league_purged(tmp_path):
    """A purge that cannot classify its argument must not run."""
    from fantasy_sports.cache.store import CacheStore
    from fantasy_sports.cache.tags import season_tag

    store = CacheStore(tmp_path / "cache.sqlite3")
    with _cassette("espn/synthetic_2026.yaml"):
        EspnProvider(cache=store).fetch_league(*SYNTHETIC)
    with pytest.raises(ValueError, match="not league-scoped"):
        store.purge_by_league_tag(season_tag("espn", 2026))


def test_a_cache_hit_and_a_cache_miss_return_the_same_bytes(tmp_path):
    """Otherwise the adapter's output depends on cache state — on the *second* run."""
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore(tmp_path / "cache.sqlite3")
    with _cassette("espn/synthetic_2026.yaml"):
        cold = EspnProvider(cache=store).fetch_teams(*SYNTHETIC)
        warm = EspnProvider(cache=store).fetch_teams(*SYNTHETIC)
    assert [team.to_dict() for team in cold] == [team.to_dict() for team in warm]


# --------------------------------------------------------------------------- #
# Freshness (jwulff/fantasy-sports#51)
# --------------------------------------------------------------------------- #
#
# Verified live against the `supper-club` league on 2026-09-05: two reads two
# and a half minutes apart from the same cache entry both reported
# `age_seconds: 0`, because `fetched_at` was stamped with the *read's* clock
# rather than the *write's*. These pin the fix at the one seam that mattered —
# `_Transport._body` deciding what a hit's `fetched_at` is — with a clock
# neither side can fudge.


class _FrozenClock:
    """One epoch, read by two clocks that disagree on shape.

    :class:`~fantasy_sports.cache.store.CacheStore` timestamps a *write* with
    an epoch float; :class:`EspnProvider` timestamps a *live fetch* with a
    timezone-aware ``datetime``. A test proving the two agree on one entry's
    age has to drive both from the same number, or a passing test would only
    prove the two clocks happened to be close.
    """

    def __init__(self, epoch: float = 1_700_000_000.0) -> None:
        self.epoch = epoch

    def store_now(self) -> float:
        return self.epoch

    def provider_now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, tz=UTC)

    def advance(self, seconds: float) -> None:
        self.epoch += seconds


def test_a_cache_hit_reports_the_true_age_not_zero(tmp_path):
    """AC1/AC2: a hit's `fetched_at` is the write time; age is `now - fetched_at`."""
    from fantasy_sports.cache.store import CacheStore

    clock = _FrozenClock()
    store = CacheStore(tmp_path / "cache.sqlite3", now=clock.store_now)

    with _cassette("espn/synthetic_2026.yaml"):
        EspnProvider(cache=store, now=clock.provider_now).fetch_teams(*SYNTHETIC)
        written_at = clock.provider_now()

        clock.advance(150)
        hit = EspnProvider(cache=store, now=clock.provider_now)
        hit.fetch_teams(*SYNTHETIC)

    assert hit.last_fetch is not None
    assert all(source.cached for source in hit.last_fetch.sources)
    assert all(source.fetched_at == written_at for source in hit.last_fetch.sources)

    payload = Envelope.success(
        sources=hit.last_fetch.sources, generated_at=clock.provider_now()
    ).to_dict()
    assert all(source["age_seconds"] == 150 for source in payload["sources"])
    assert payload["data_age_seconds"] == 150


def test_repeated_cache_hits_report_a_nonzero_and_growing_age(tmp_path):
    """AC4: a second call inside the TTL must report a nonzero, growing age."""
    from fantasy_sports.cache.store import CacheStore

    clock = _FrozenClock()
    store = CacheStore(tmp_path / "cache.sqlite3", now=clock.store_now)

    with _cassette("espn/synthetic_2026.yaml"):
        EspnProvider(cache=store, now=clock.provider_now).fetch_teams(*SYNTHETIC)

        clock.advance(150)
        first_hit = EspnProvider(cache=store, now=clock.provider_now)
        first_hit.fetch_teams(*SYNTHETIC)
        first_payload = Envelope.success(
            sources=first_hit.last_fetch.sources, generated_at=clock.provider_now()
        ).to_dict()

        clock.advance(90)
        second_hit = EspnProvider(cache=store, now=clock.provider_now)
        second_hit.fetch_teams(*SYNTHETIC)
        second_payload = Envelope.success(
            sources=second_hit.last_fetch.sources, generated_at=clock.provider_now()
        ).to_dict()

    assert all(source.cached for source in second_hit.last_fetch.sources)
    first_ages = {s["name"]: s["age_seconds"] for s in first_payload["sources"]}
    second_ages = {s["name"]: s["age_seconds"] for s in second_payload["sources"]}
    assert all(age == 150 for age in first_ages.values())
    assert all(age == 240 for age in second_ages.values())
    for name, first_age in first_ages.items():
        assert second_ages[name] > first_age, "age must grow while the entry sits in cache"
    assert second_payload["data_age_seconds"] == 240


def test_fresh_and_no_cache_both_report_cached_false_and_zero_age(tmp_path):
    """AC5: `--fresh` and `--no-cache` are never mistaken for a hit."""
    from fantasy_sports.cache.store import CacheMode, CacheStore

    clock = _FrozenClock()
    store = CacheStore(tmp_path / "cache.sqlite3", now=clock.store_now)

    with _cassette("espn/synthetic_2026.yaml"):
        EspnProvider(cache=store, now=clock.provider_now).fetch_teams(*SYNTHETIC)
        clock.advance(400)

        refreshed = EspnProvider(cache=store, cache_mode=CacheMode.FRESH, now=clock.provider_now)
        refreshed.fetch_teams(*SYNTHETIC)

        bypassed = EspnProvider(cache=store, cache_mode=CacheMode.BYPASS, now=clock.provider_now)
        bypassed.fetch_teams(*SYNTHETIC)

    for provider in (refreshed, bypassed):
        assert provider.last_fetch is not None
        assert all(not source.cached for source in provider.last_fetch.sources)
        payload = Envelope.success(
            sources=provider.last_fetch.sources, generated_at=clock.provider_now()
        ).to_dict()
        assert all(source["age_seconds"] == 0 for source in payload["sources"])
        assert payload["data_age_seconds"] == 0


def test_a_multi_source_read_reports_the_oldest_contributing_fetch(tmp_path):
    """AC3: `data_as_of`/`data_age_seconds` are the oldest source's, not the newest.

    One provider, one read: the four bootstrap views come back from cache at
    age 200, and asking the same instance for a scoring period it has never
    fetched forces a genuinely live sub-request at age 0 in the same envelope.
    """
    from fantasy_sports.cache.store import CacheStore

    clock = _FrozenClock()
    store = CacheStore(tmp_path / "cache.sqlite3", now=clock.store_now)

    with _cassette("espn/synthetic_2026.yaml"):
        EspnProvider(cache=store, now=clock.provider_now).fetch_teams(*SYNTHETIC)

        clock.advance(200)
        mixed = EspnProvider(cache=store, now=clock.provider_now)
        mixed.fetch_teams(*SYNTHETIC)
        mixed.fetch_matchups(*SYNTHETIC, week=14)

        payload = Envelope.success(
            sources=mixed.last_fetch.sources, generated_at=clock.provider_now()
        ).to_dict()

    ages = {source["name"]: source["age_seconds"] for source in payload["sources"]}
    assert any(age == 200 for age in ages.values()), "the cache hits"
    assert any(age == 0 for age in ages.values()), "the fresh sub-request"
    assert payload["data_age_seconds"] == 200, "the oldest contributing fetch, not the newest"
    oldest = min(payload["sources"], key=lambda source: source["fetched_at"])
    assert payload["data_as_of"] == oldest["fetched_at"]


def test_the_free_agent_filter_header_is_part_of_the_cache_key(tmp_path):
    """Two ``x-fantasy-filter`` values against one URL are two payloads.

    Omitting the header from the key would serve one position's players for
    another's; putting the whole header set in would key on the ``Cookie``.
    """
    from fantasy_sports.cache.store import cache_key

    url = "https://example.invalid/leagues/1"
    unfiltered = cache_key(url, {"view": "kona_player_info"})
    wide_receivers = cache_key(
        url, {"view": "kona_player_info"}, extra={"x-fantasy-filter": '{"slot":4}'}
    )
    assert unfiltered != wide_receivers


# --------------------------------------------------------------------------- #
# Defensive parsing
# --------------------------------------------------------------------------- #
#
# Every helper below reads a payload ESPN controls. They all follow one rule: a
# shape we do not recognise produces an empty result or a typed error, never a
# crash and never an invented value. They are exercised directly because
# provoking each guard through a full read would need a cassette per guard, and
# a fixture nobody can relate to a real ESPN response is worth less than the
# assertion it enables.

from fantasy_sports.providers.espn import (  # noqa: E402 - grouped with its section
    RawResponse,
    _auth_reason,
    _filter_dimension,
    _kickoff_map,
    _matchup_period_for,
    _missing_key,
    _owner_names,
    _player_id_of,
    _remember,
    _roster_slots,
    _schedule_entries,
    _scoring_periods,
    _team_id_of,
    _teams_by_id,
    _unpack_action,
    _view_of,
)


def _recorded(view: str, payload: Any) -> dict[str, RawResponse]:
    return {view: RawResponse(view=view, payload=payload, fetched_at=KICKOFF_EARLY)}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (KeyError("record"), "record"),
        # A KeyError's argument is normally a field name, which is what
        # `details.path` is for -- but `player_map[playerId]` raises with an id,
        # and an id is provider data. Reported by type only.
        (KeyError(12345), "<int>"),
        (KeyError(), None),
        (TypeError("not subscriptable"), None),
    ],
)
def test_only_a_field_name_reaches_the_drift_path(exc, expected):
    assert _missing_key(exc) == expected


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b'"a bare string"',
        b'{"details": "not a list"}',
        b'{"details": [{"message": "no type key"}]}',
        b"{}",
    ],
)
def test_an_unrecognised_401_body_yields_no_reason_rather_than_a_guess(body):
    """An unreadable body is not evidence of anything."""
    assert _auth_reason(FakeResponse(status_code=401, body=body)) is None


def test_the_request_descriptor_names_the_view_or_the_combination():
    assert _view_of(None) == "unknown"
    assert _view_of({"scoringPeriodId": 3}) == "unknown"
    assert _view_of({"view": "mTeam"}) == "mTeam"
    assert _view_of({"view": ["mMatchupScore", "mScoreboard"]}) == "mMatchupScore+mScoreboard"


def test_only_the_fantasy_filter_header_becomes_a_cache_dimension():
    """The rest of the header set carries the ``Cookie``."""
    assert _filter_dimension(None) is None
    assert _filter_dimension({"Cookie": "espn_s2=secret"}) is None
    assert _filter_dimension({"X-Fantasy-Filter": '{"a":1}'}) == {"x-fantasy-filter": '{"a":1}'}


@pytest.mark.parametrize(
    "responses",
    [
        {},
        _recorded("proTeamSchedules_wl", ["not", "a", "mapping"]),
        _recorded("proTeamSchedules_wl", {"settings": {"proTeams": "not a list"}}),
        _recorded("proTeamSchedules_wl", {"settings": {"proTeams": ["not a mapping"]}}),
        _recorded("proTeamSchedules_wl", {"settings": {"proTeams": [{"id": 1}]}}),
        _recorded(
            "proTeamSchedules_wl",
            {"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {"x": []}}]}},
        ),
        _recorded(
            "proTeamSchedules_wl",
            {"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {"2": [{}]}}]}},
        ),
    ],
)
def test_a_kickoff_that_cannot_be_read_is_absent_rather_than_wrong(responses):
    """Absence is ordinary data; a fabricated kickoff would lock a lineup."""
    assert _kickoff_map(responses) == {}


def test_a_kickoff_that_can_be_read_is_epoch_milliseconds():
    responses = _recorded(
        "proTeamSchedules_wl",
        {"settings": {"proTeams": [{"id": 12, "proGamesByScoringPeriod": {"2": [{"date": 5}]}}]}},
    )
    assert _kickoff_map(responses) == {(12, 2): 5}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"settings": "not a mapping"},
        {"settings": {"rosterSettings": {"lineupSlotCounts": "not a mapping"}}},
        # Slot 99 has no name in POSITION_MAP, and a count of 0 is not a slot.
        {"settings": {"rosterSettings": {"lineupSlotCounts": {"99": 1, "0": 0, "x": 1}}}},
    ],
)
def test_an_unreadable_roster_configuration_is_empty_not_invented(payload):
    assert _roster_slots(payload) == {}


def test_teams_and_schedules_that_are_not_lists_read_as_empty():
    assert _teams_by_id({"teams": "not a list"}) == {}
    assert _teams_by_id({"teams": [{"no": "id"}]}) == {}
    assert _schedule_entries({}, 1) == {}
    assert _schedule_entries(_recorded("mMatchupScore", {"schedule": "not a list"}), 1) == {}
    assert _schedule_entries(_recorded("mMatchupScore", ["not a mapping"]), 1) == {}


def test_owner_names_tolerate_every_shape_espn_has_used():
    """Bare strings, member objects, ids with no name, and nothing at all."""

    class _Team:
        owners = [
            "Ann Alpha",
            {"displayName": "  bravo  "},
            {"firstName": "Cy", "lastName": "Charlie"},
            {"id": "{OWNER-DELTA}"},
            12345,
            {"displayName": "Ann Alpha"},
        ]

    assert _owner_names(_Team()) == ("Ann Alpha", "bravo", "Cy Charlie")

    class _Ownerless:
        owners = None

    assert _owner_names(_Ownerless()) == ()


def test_a_matchup_period_falls_back_to_the_scoring_period_when_unmapped():
    """The identity case is what an unmapped week actually looks like."""

    class _League:
        settings = None

    assert _matchup_period_for(_League(), 7) == 7


def test_an_explicit_scoring_period_is_not_swept():
    class _League:
        current_week = 5
        scoringPeriodId = 5
        firstScoringPeriod = 1

    assert _scoring_periods(_League(), None, 3) == [3]
    assert _scoring_periods(_League(), None, None) == [5]
    assert _scoring_periods(_League(), KICKOFF_EARLY, None) == [1, 2, 3, 4, 5]


def test_an_unresolved_matchup_side_is_a_bare_team_id():
    """``espn-api`` leaves a side it could not resolve as an integer."""

    class _Team:
        team_id = 4

    assert _team_id_of(7) == 7
    assert _team_id_of(_Team()) == 4
    assert _team_id_of(None) == 0


def test_an_activity_row_shorter_than_four_fields_is_padded():
    assert _unpack_action((None, "DROPPED")) == (None, "DROPPED", None, None)


def test_a_player_the_feed_could_not_resolve_reads_as_an_id_or_nothing():
    class _Player:
        playerId = 42

    assert _player_id_of(_Player()) == "42"
    assert _player_id_of(99) == "99"
    assert _player_id_of("Unknown") == ""


def test_identity_for_the_merge_is_the_move_not_the_provider_id():
    """The two surfaces number the same move differently.

    Matching on the provider id would make every reconciled trade a duplicate.
    """
    seen: set[tuple[Any, ...]] = set()
    first = Transaction(
        provider="espn",
        provider_id="from-mTransactions2",
        type="trade",
        team_provider_id="1",
        players_out=("2002",),
        timestamp=TRADE_AT,
        raw={},
    )
    second = Transaction(
        provider="espn",
        provider_id="from-the-activity-feed",
        type="trade",
        team_provider_id="1",
        players_out=("2002",),
        timestamp=TRADE_AT,
        raw={},
    )
    assert _remember(seen, first) is True
    assert _remember(seen, second) is False


def test_fetch_raw_passes_a_fantasy_filter_through_as_the_header():
    """Several views return nothing useful without one (research §7.1)."""
    captured: dict[str, Any] = {}

    def _http(url, params=None, headers=None, cookies=None):
        captured["headers"] = headers
        captured["params"] = params
        return FakeResponse(body={"players": []})

    provider = EspnProvider(http=_http)
    provider.fetch_raw(
        *SYNTHETIC,
        view="kona_player_info",
        scoringPeriodId=2,
        x_fantasy_filter={"players": {"limit": 5}},
    )
    assert captured["headers"] == {"x-fantasy-filter": '{"players": {"limit": 5}}'}
    assert captured["params"] == {"view": "kona_player_info", "scoringPeriodId": 2}

    provider.fetch_raw(*SYNTHETIC, view="kona_player_info", x_fantasy_filter='{"raw":true}')
    assert captured["headers"] == {"x-fantasy-filter": '{"raw":true}'}


def test_the_historical_url_shape_answers_with_a_list_and_is_unwrapped():
    """``/leagueHistory/`` returns a JSON *list*, and every caller assumes a dict.

    ``espn-api`` unwraps it in ``league_get``, so our replacement has to as
    well -- forgetting is how you get ``list indices must be integers`` from
    somewhere unrelated (research §7.8).
    """
    provider = EspnProvider(http=lambda *a, **k: FakeResponse(body=[{"id": 1234}, {"id": 5}]))
    assert provider.fetch_raw("1234", 2015, view="mSettings") == {"id": 1234}

    # Anything that unwraps to something other than an object still has to
    # satisfy the `dict` return type rather than crash the caller.
    provider = EspnProvider(http=lambda *a, **k: FakeResponse(body=["maintenance"]))
    assert provider.fetch_raw("1234", 2015, view="mSettings") == {"data": "maintenance"}


def test_a_taxonomy_error_from_a_transaction_surface_is_not_swallowed():
    """The best-effort fallbacks must not hide a 401 or a rate limit.

    Both surfaces catch broadly on purpose -- ``espn-api`` raises bare
    ``Exception`` for ordinary conditions -- and a catch that broad is exactly
    how a real failure disappears.
    """

    class _Throttled:
        current_week = 2
        scoringPeriodId = 2

        def transactions(self, **_):
            raise RateLimitedError("throttled")

        def recent_activity(self, **_):
            raise RateLimitedError("throttled")

    provider = EspnProvider()
    with pytest.raises(RateLimitedError):
        provider._espn_transactions(_Throttled(), 2)
    with pytest.raises(RateLimitedError):
        provider._activity(_Throttled())
