"""``claims`` and ``cancel-claim``, offline (jwulff/fantasy-sports#109).

The adapter half parses ``view=mPendingTransactions`` from brief 06's own
capture (``pending-transactions.json``); the command half runs the real
commands against the real ESPN adapter and :class:`_harness.WritableEspn`,
which queues a ``WAIVER`` body where that view lists it and answers a
``CANCEL`` the way p5 was captured: a new id, ``CANCELED``, the claim's items
echoed, and ``relatedTransactionId``. No test sends a live write.

Team 1 is the session's; team 2 is another member's. 3002 Radia Perlman is
the one pool player on waivers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    ADD_DROP_CAPTURES,
    CANCEL_TRANSACTION_ID,
    FAKE_SWID,
    PROPOSED_DATE_MS,
    TRANSACTION_ID,
    WritableEspn,
    install_writable_espn,
    isolate_home,
)

from fantasy_sports.cache.tags import Resource
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.core.errors import (
    ConfigInvalidError,
    ProviderUnavailableError,
    SchemaDriftError,
    WriteDivergedError,
    WriteRejectedError,
)
from fantasy_sports.core.journal import Journal, JournalEntry
from fantasy_sports.core.models import PendingClaim
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output.errors import exit_code_for

WHY = "Perlman is the best TE on waivers this week (sanity-gate: PROCEED)"
OTHER_MEMBER = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"

CANCEL_KEYS = {
    "team_id",
    "kind",
    "provider_transaction_id",
    "cancelled_claim_id",
    "status",
    "is_pending",
    "proposed_at",
    "items",
    "journal_entry_id",
    "reversible",
    "acknowledged_irreversible",
    "cost",
    "credentials",
    "read_back",
    "notified",
    "dry_run",
}

CLAIM_KEYS = {
    "provider",
    "provider_id",
    "team_id",
    "players_in",
    "players_out",
    "bid_amount",
    "proposed_at",
    "processes_at",
    "status",
    "sub_order",
    "raw",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> WritableEspn:
    return install_writable_espn(monkeypatch)


def _run(command: str, **kwargs: Any) -> dict[str, Any]:
    return REGISTRY[command].resolve()(**kwargs).to_dict()


def _claim(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("team", "1")
    kwargs.setdefault("player", "Radia Perlman")
    kwargs.setdefault("acknowledge_irreversible", WHY)
    return _run("claim", **kwargs)["data"]


def _claims(**kwargs: Any) -> list[dict[str, Any]]:
    return _run("claims", **kwargs)["data"]


def _cancel(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("team", "1")
    kwargs.setdefault("id", TRANSACTION_ID)
    return _run("cancel-claim", **kwargs)["data"]


def _entries() -> list[JournalEntry]:
    with Journal.open() as journal:
        return journal.list(limit=50)


def _journal_file(home: Path) -> Path:
    return home / "state" / "fantasy-sports" / "journal.sqlite"


def _queued(claim_id: str, team: int, *, proposed: int = PROPOSED_DATE_MS) -> dict[str, Any]:
    """A pending claim another invocation (or another team) queued."""
    return {
        "id": claim_id,
        "teamId": team,
        "type": "WAIVER",
        "status": "PENDING",
        "isPending": True,
        "scoringPeriodId": 2,
        "proposedDate": proposed,
        "bidAmount": 0,
        "items": [{"playerId": 3002, "type": "ADD"}],
    }


def _capture() -> dict[str, Any]:
    return json.loads((ADD_DROP_CAPTURES / "pending-transactions.json").read_text("utf-8"))


# --------------------------------------------------------------------------- #
# The adapter: ESPN's queue, parsed from the capture
# --------------------------------------------------------------------------- #


def test_the_captured_queue_row_becomes_a_pending_claim():
    """Brief 06's p7c claim, as ``mPendingTransactions`` listed it while live."""
    from fantasy_sports.providers.espn import _pending_claims

    rows = _capture()["snapshots"]["after-p7c-mPendingTransactions"]["team_7_rows"]
    (claim,) = _pending_claims({"pendingTransactions": rows}, team_id=None)
    assert claim.provider == "espn"
    assert claim.provider_id == "<uuid-3>"
    assert claim.team_id == "7"
    assert claim.players_in == ("9000030",) and claim.players_out == ("9000023",)
    assert claim.bid_amount == 0, "ESPN records a bidless claim as 0"
    assert claim.proposed_at == datetime.fromtimestamp(1792046096566 / 1000, tz=UTC)
    assert claim.processes_at == datetime.fromtimestamp(1792134000000 / 1000, tz=UTC)
    assert claim.status == "PENDING"
    assert claim.sub_order is None, "the queue view carries no subOrder"
    assert claim.raw == rows[0]


def test_the_captured_empty_queues_are_empty():
    from fantasy_sports.providers.espn import _pending_claims

    for name in ("after-p5-mPendingTransactions", "after-p7c-r-mPendingTransactions"):
        rows = _capture()["snapshots"][name]["team_7_rows"]
        assert _pending_claims({"pendingTransactions": rows}, team_id="7") == []


def test_an_empty_queue_is_an_empty_list_and_an_absent_one_is_drift():
    """ESPN sends ``pendingTransactions: []`` for an empty queue (seen live in
    the test league), so a body without the key is not "nothing queued"."""
    from fantasy_sports.providers.espn import _pending_claims

    assert _pending_claims({"pendingTransactions": []}, team_id=None) == []
    with pytest.raises(SchemaDriftError, match="has no pendingTransactions") as caught:
        _pending_claims({"id": 99}, team_id=None)
    assert caught.value.details["path"] == ["mPendingTransactions", "pendingTransactions"]


def test_only_pending_waiver_rows_are_claims_and_team_narrows():
    from fantasy_sports.providers.espn import _pending_claims

    rows = [
        _queued("a", 1),
        _queued("b", 2),
        {**_queued("c", 1), "status": "CANCELED"},
        {**_queued("d", 1), "type": "TRADE_PROPOSAL"},
        # Not a claim, so not validated: a row nobody reads is not drift.
        {"type": "TRADE_PROPOSAL", "status": "PENDING"},
    ]
    payload = {"pendingTransactions": rows}
    assert [c.provider_id for c in _pending_claims(payload, team_id=None)] == ["a", "b"]
    assert [c.provider_id for c in _pending_claims(payload, team_id="2")] == ["b"]


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ([], ["mPendingTransactions"]),
        ({"pendingTransactions": {}}, ["mPendingTransactions", "pendingTransactions"]),
        ({"pendingTransactions": ["x"]}, ["mPendingTransactions", "pendingTransactions[0]"]),
        (
            {"pendingTransactions": [{**_queued("a", 1), "id": None}]},
            ["mPendingTransactions", "pendingTransactions[0].id"],
        ),
        (
            {"pendingTransactions": [{**_queued("a", 1), "teamId": "seven"}]},
            ["mPendingTransactions", "pendingTransactions[0].teamId"],
        ),
        (
            {"pendingTransactions": [_queued("a", 1), {**_queued("b", 1), "items": None}]},
            ["mPendingTransactions", "pendingTransactions[1].items"],
        ),
    ],
)
def test_a_queue_of_another_shape_is_drift_with_a_path(payload: Any, path: list[str]):
    from fantasy_sports.providers.espn import _pending_claims

    with pytest.raises(SchemaDriftError) as caught:
        _pending_claims(payload, team_id=None)
    assert caught.value.details["path"] == path
    assert "raw --view mPendingTransactions" in (caught.value.remediation or "")


def test_the_adapter_reads_the_queue_view_through_the_transactions_ttl(espn: WritableEspn):
    from fantasy_sports.providers.espn import _VIEW_RESOURCES, EspnProvider

    assert _VIEW_RESOURCES["mPendingTransactions"] is Resource.TRANSACTIONS
    espn.pending.append(_queued("q", 1))
    espn._publish(espn.bootstrap)
    provider = EspnProvider({"espn_s2": "AEB" + "q" * 200, "swid": FAKE_SWID})
    (claim,) = provider.fetch_pending_claims("99", 2026, "1")
    assert claim.provider_id == "q"
    assert "mPendingTransactions" in espn.views()
    assert provider.fetch_pending_claims("99", 2026, "2") == []


# --------------------------------------------------------------------------- #
# The adapter: a body that omits the queue (jwulff/fantasy-sports#144)
# --------------------------------------------------------------------------- #


def _omitted_body() -> dict[str, Any]:
    """``view=mPendingTransactions`` as a private league served it on
    2026-10-18 with nothing queued: the envelope, no ``pendingTransactions``."""
    capture = json.loads(
        (ADD_DROP_CAPTURES / "pending-transactions-omitted.json").read_text("utf-8")
    )
    return capture["response"]["body"]


def _logged_claim(claim_id: str, team: int, **extra: Any) -> dict[str, Any]:
    """A claim's own ``mTransactions2`` row, which reads ``PENDING`` for good."""
    return {
        "id": claim_id,
        "teamId": team,
        "type": "WAIVER",
        "status": "PENDING",
        "isPending": True,
        "executionType": "EXECUTE",
        "scoringPeriodId": 2,
        "proposedDate": PROPOSED_DATE_MS,
        "bidAmount": 0,
        "items": [{"playerId": 3002, "type": "ADD"}],
        **extra,
    }


def _retiring_row(claim_id: str, team: int, *, execution: str, status: str) -> dict[str, Any]:
    """The ``CANCEL`` or ``PROCESS`` row ESPN adds, naming the claim it retired."""
    return {
        **_logged_claim(f"{execution.lower()}-of-{claim_id}", team),
        "executionType": execution,
        "status": status,
        "isPending": False,
        "relatedTransactionId": claim_id,
    }


def _provider() -> Any:
    from fantasy_sports.providers.espn import EspnProvider

    return EspnProvider({"espn_s2": "AEB" + "q" * 200, "swid": FAKE_SWID})


def test_the_captured_omitted_body_is_this_leagues_envelope_without_the_queue():
    """The parser still refuses it; only the adapter, with the log's help, may
    call it empty."""
    from fantasy_sports.providers.espn import _pending_claims, _queue_omitted

    body = _omitted_body()
    assert "pendingTransactions" not in body
    assert {"id", "seasonId", "scoringPeriodId", "status"} <= set(body)
    assert _queue_omitted(body, league_id="99", season=2026) is True
    assert _queue_omitted(body, league_id="100", season=2026) is False, "another league"
    assert _queue_omitted(body, league_id="99", season=2025) is False, "another season"
    assert _queue_omitted({**body, "pendingTransactions": []}, league_id="99", season=2026) is False
    assert _queue_omitted({**body, "status": None}, league_id="99", season=2026) is False
    assert _queue_omitted({"id": 99}, league_id="99", season=2026) is False
    assert _queue_omitted([], league_id="99", season=2026) is False
    with pytest.raises(SchemaDriftError, match="has no pendingTransactions"):
        _pending_claims(body, team_id=None)


@pytest.mark.parametrize(
    ("current", "transaction", "periods"),
    [
        (6, 6, [5, 6]),  # the captured body: the transaction period and the one before
        (6, 7, [6, 7]),  # the week has rolled over for writes but not for play
        (7, 6, [5, 6, 7]),  # the other way round
        (1, 1, [1]),  # nothing before the first period
        (3, None, [2, 3]),  # no transaction period: bracket the current one
        (None, None, [None]),  # neither: ESPN's default period, once
    ],
)
def test_the_log_is_read_for_the_periods_a_queued_claim_can_carry(current, transaction, periods):
    from fantasy_sports.providers.espn import _LOG_LOOKBACK, _log_periods

    body = {"scoringPeriodId": current, "status": {"transactionScoringPeriod": transaction}}
    assert _log_periods(body, lookback=_LOG_LOOKBACK["WAIVER"]) == periods
    assert _log_periods(body) == periods, "one period back is the default"


def test_the_trade_lookback_covers_the_longest_open_proposal():
    """A proposal may stay open for ``MAX_EXPIRES_HOURS``; the log read for an
    omitted trade queue must reach back that far plus the rollover period."""
    from fantasy_sports.commands.trades import MAX_EXPIRES_HOURS
    from fantasy_sports.providers.espn import _LOG_LOOKBACK, _log_periods

    hours_per_period = 7 * 24
    assert (_LOG_LOOKBACK["TRADE_PROPOSAL"] - 1) * hours_per_period >= MAX_EXPIRES_HOURS
    body = {"scoringPeriodId": 6, "status": {"transactionScoringPeriod": 6}}
    assert _log_periods(body, lookback=_LOG_LOOKBACK["TRADE_PROPOSAL"]) == [3, 4, 5, 6]
    assert _log_periods({"scoringPeriodId": 2, "status": {}}, lookback=3) == [1, 2]


def test_an_omitted_queue_is_empty_when_the_log_holds_no_live_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    """No claim was ever queued, so the log has no ``transactions`` key at all,
    which is ESPN's quiet period and not drift."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    assert "pendingTransactions" not in espn.overrides["mPendingTransactions"]
    assert espn.overrides["mPendingTransactions"]["id"] == 99
    assert "mTransactions2" not in espn.overrides

    assert _provider().fetch_pending_claims("99", 2026, "1") == []

    views = espn.views()
    assert "mPendingTransactions" in views
    log_calls = [c for c in espn.calls if c["params"].get("view") == "mTransactions2"]
    assert [c["params"]["scoringPeriodId"] for c in log_calls] == ["1", "2"]
    for call in log_calls:
        assert json.loads(call["headers"]["x-fantasy-filter"]) == {
            "transactions": {"filterType": {"value": ["WAIVER"]}}
        }


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="an empty log"),
        pytest.param(
            [_logged_claim("a", 1), _retiring_row("a", 1, execution="CANCEL", status="CANCELED")],
            id="a cancelled claim, still PENDING in its own row",
        ),
        pytest.param(
            [_logged_claim("a", 1), _retiring_row("a", 1, execution="PROCESS", status="EXECUTED")],
            id="a processed claim, still PENDING in its own row",
        ),
        pytest.param(
            [
                _logged_claim("a", 1),
                _retiring_row("a", 1, execution="PROCESS", status="FAILED_INVALIDPLAYERSOURCE"),
            ],
            id="a failed claim, still PENDING in its own row",
        ),
        pytest.param(
            [_retiring_row("gone", 1, execution="PROCESS", status="EXECUTED")],
            id="a processed claim whose own row the log no longer carries",
        ),
        pytest.param([_logged_claim("theirs", 2)], id="another team's live claim"),
        pytest.param(
            [{**_logged_claim("a", 1), "status": "EXECUTED", "isPending": False}],
            id="a row that is not pending",
        ),
    ],
)
def test_an_omitted_queue_is_empty_when_every_logged_claim_is_retired(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
):
    """Seen live in three leagues (#144): every ``PENDING`` row was named by a
    later ``CANCEL`` or ``PROCESS`` row's ``relatedTransactionId``."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mTransactions2"] = {"transactions": rows}
    assert _provider().fetch_pending_claims("99", 2026, "1") == []


def test_an_omitted_queue_with_a_live_claim_in_the_log_is_drift_naming_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """A ``PENDING`` row nothing retired is a claim ESPN still holds that the
    queue view did not list -- the one case "nothing queued" would be a lie."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mTransactions2"] = {
        "transactions": [
            _logged_claim("live", 1),
            _logged_claim("theirs", 2),
            _logged_claim("done", 1),
            _retiring_row("done", 1, execution="PROCESS", status="EXECUTED"),
        ]
    }
    with pytest.raises(SchemaDriftError) as caught:
        _provider().fetch_pending_claims("99", 2026, "1")
    error = caught.value
    assert "mTransactions2 still lists 1 pending WAIVER" in str(error)
    assert error.details["path"] == ["mPendingTransactions", "pendingTransactions"]
    assert error.details["corroboration"] == "mTransactions2"
    assert error.details["unlisted"] == [{"id": "live", "team_id": "1", "scoring_period": 2}]
    assert "Do not resubmit or cancel blind" in (error.remediation or "")
    assert exit_code_for(error) == 9

    # Every team's queue: the other team's live claim counts too.
    with pytest.raises(SchemaDriftError) as caught:
        _provider().fetch_pending_claims("99", 2026)
    assert [u["id"] for u in caught.value.details["unlisted"]] == ["live", "theirs"]


@pytest.mark.parametrize(
    ("log", "what"),
    [
        (42, "is not an object"),
        ({"transactions": {}}, "non-list transactions"),
        ({"transactions": ["x"]}, "transactions[0] that is not an object"),
    ],
)
def test_a_log_that_cannot_corroborate_an_omitted_queue_is_drift(
    monkeypatch: pytest.MonkeyPatch, log: Any, what: str
):
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mTransactions2"] = log
    with pytest.raises(SchemaDriftError) as caught:
        _provider().fetch_pending_claims("99", 2026, "1")
    assert "corroborating mTransactions2" in str(caught.value)
    assert what in str(caught.value)
    assert caught.value.details["path"] == ["mPendingTransactions", "pendingTransactions"]


def test_the_corroborating_log_reads_bypass_the_cache(monkeypatch: pytest.MonkeyPatch):
    """A completed period's log caches forever, so a cached body could never
    show a claim queued after it was stored and would corroborate an empty
    queue over a live one. Every corroborating read goes to ESPN."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    assert _claims() == []
    first = len([c for c in espn.calls if c["params"].get("view") == "mTransactions2"])
    assert first == 2, "one read per bracketed period"
    assert _claims() == []
    second = len([c for c in espn.calls if c["params"].get("view") == "mTransactions2"])
    assert second == 2 * first, "read live again, not served from the store"
    # The second omitted body came from the store, so it was read again live
    # before the log was trusted (see the rollover test below).
    assert espn.views().count("mPendingTransactions") == 2


def test_an_omitted_queue_served_from_the_cache_is_read_again_live(
    monkeypatch: pytest.MonkeyPatch,
):
    """The queue view keeps a 15-minute cache, but the log check is live. A
    cached omitted body could predate a claim queued in the ESPN app, or a
    weekly rollover, so its periods would bracket the wrong log and
    corroborate an empty queue over a live claim. A cached omitted body is
    re-read live, and the log periods come from that fresh body."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    assert _claims() == []
    assert espn.views().count("mPendingTransactions") == 1, "a live read is not repeated"

    rolled = espn.queue_envelope()
    rolled["scoringPeriodId"] = 3
    rolled["status"] = {**rolled["status"], "transactionScoringPeriod": 3}
    espn.overrides["mPendingTransactions"] = rolled
    espn.overrides["mTransactions2"] = {
        "transactions": [_logged_claim("queued-in-the-app", 1, scoringPeriodId=3)]
    }
    before = len(espn.calls)
    with pytest.raises(SchemaDriftError) as caught:
        _claims()
    assert [u["id"] for u in caught.value.details["unlisted"]] == ["queued-in-the-app"]
    assert espn.views().count("mPendingTransactions") == 2
    periods = [
        c["params"]["scoringPeriodId"]
        for c in espn.calls[before:]
        if c["params"].get("view") == "mTransactions2"
    ]
    assert periods == ["2", "3"], "bracketed from the fresh envelope, not the cached one"


@pytest.mark.parametrize(
    "team", [pytest.param(None, id="null"), pytest.param("x", id="not a number")]
)
def test_a_live_logged_claim_with_no_readable_team_is_drift_for_a_team_read(
    monkeypatch: pytest.MonkeyPatch, team: Any
):
    """A pending row that names no team cannot be ruled out as this team's, so
    it is not skipped: it fails the corroboration rather than vanishing."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mTransactions2"] = {
        "transactions": [{**_logged_claim("unowned", 1), "teamId": team}]
    }
    with pytest.raises(SchemaDriftError) as caught:
        _provider().fetch_pending_claims("99", 2026, "1")
    assert caught.value.details["unlisted"] == [
        {"id": "unowned", "team_id": None, "scoring_period": 2}
    ]


_NO_STATUS = object()


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(_NO_STATUS, id="absent"),
        pytest.param(None, id="null"),
        pytest.param(7, id="not a string"),
        pytest.param("QUEUED", id="a status no capture has shown"),
    ],
)
def test_a_logged_claim_with_an_unrecognised_status_is_drift_not_empty(
    monkeypatch: pytest.MonkeyPatch, status: Any
):
    """Only ``PENDING`` is live, and only ``EXECUTED``, ``CANCELED``, and
    ``FAILED_*`` have been seen settled. A row with any other status might be
    the very claim being corroborated, so it fails the check (cloud review on
    #145) rather than being read as settled."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    row = _logged_claim("odd", 1)
    if status is _NO_STATUS:
        del row["status"]
    else:
        row["status"] = status
    espn.overrides["mTransactions2"] = {"transactions": [row]}
    with pytest.raises(SchemaDriftError, match="status no capture has shown") as caught:
        _provider().fetch_pending_claims("99", 2026, "1")
    assert caught.value.details["path"] == ["mPendingTransactions", "pendingTransactions"]
    assert caught.value.details["unrecognised"] == [
        {"id": "odd", "status": None if status is _NO_STATUS else status}
    ]


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [
                {**_logged_claim("odd", 1), "status": "QUEUED"},
                _retiring_row("odd", 1, execution="PROCESS", status="EXECUTED"),
            ],
            id="retired by a later row",
        ),
        pytest.param([{**_logged_claim("odd", 2), "status": "QUEUED"}], id="another team's"),
        pytest.param(
            [{**_retiring_row("x", 1, execution="PROCESS", status="QUEUED")}],
            id="a retiring row's own status",
        ),
    ],
)
def test_an_unrecognised_status_that_cannot_be_this_teams_live_claim_is_not_drift(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
):
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mTransactions2"] = {"transactions": rows}
    assert _provider().fetch_pending_claims("99", 2026, "1") == []


def test_a_live_logged_proposal_naming_no_team_is_not_skipped():
    from fantasy_sports.providers.espn import _unretired_pending

    row = {"id": "p", "type": "TRADE_PROPOSAL", "status": "PENDING", "items": [{}]}
    found = _unretired_pending([row], kind="TRADE_PROPOSAL", team_id="1", now=datetime.now(UTC))
    assert [u["id"] for u in found] == ["p"]


def test_a_body_without_the_key_that_is_not_this_leagues_envelope_is_still_drift(
    monkeypatch: pytest.MonkeyPatch,
):
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    espn.overrides["mPendingTransactions"] = {**espn.queue_envelope(), "id": 98}
    with pytest.raises(SchemaDriftError, match="has no pendingTransactions") as caught:
        _provider().fetch_pending_claims("99", 2026, "1")
    assert "unlisted" not in caught.value.details
    assert not any(c["params"].get("view") == "mTransactions2" for c in espn.calls)


def test_the_pending_claim_model_is_frozen_and_built_from_a_payload():
    import dataclasses

    claim = PendingClaim.from_payload(
        {"provider": "espn", "provider_id": "x", "team_id": "1", "status": "PENDING", "raw": {}}
    )
    assert claim.players_in == () and claim.bid_amount is None
    assert set(claim.to_dict()) == CLAIM_KEYS
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.status = "EXECUTED"  # type: ignore[misc]
    with pytest.raises(SchemaDriftError):
        PendingClaim.from_payload({"provider": "espn", "provider_id": "x"})


def test_the_writing_fake_serves_its_queue_by_team():
    from _fake_provider import WritingFake

    queued = [
        PendingClaim(provider="fake", provider_id=n, team_id=t, status="PENDING", raw={})
        for n, t in (("a", "1"), ("b", "2"))
    ]
    fake = WritingFake(pending_claims=queued)
    assert [c.provider_id for c in fake.fetch_pending_claims("99", 2026, "2")] == ["b"]
    assert len(fake.fetch_pending_claims("99", 2026)) == 2


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "shape", "params"),
    [
        ("claims", DataShape.COLLECTION, ["team"]),
        ("cancel-claim", DataShape.OBJECT, ["team", "id", "dry_run"]),
    ],
)
def test_both_are_top_level_commands_that_take_a_league(command, shape, params):
    spec = REGISTRY[command]
    assert spec.group is None and spec.shape is shape and spec.takes_league
    assert [p.name for p in spec.params] == params


def test_claim_and_cancel_claim_both_parse_on_the_cli(espn: WritableEspn):
    """``cancel-claim`` rather than a ``claim cancel`` group, which would shadow
    ``claim`` itself so that ``claim --player`` stopped parsing."""
    from typer.testing import CliRunner

    from fantasy_sports.cli.app import build_app

    cli = CliRunner()
    claim = ["claim", "--team", "1", "--player", "3002", "--acknowledge-irreversible", WHY]
    assert cli.invoke(build_app(), [*claim, "-o", "json"]).exit_code == 0
    listed = cli.invoke(build_app(), ["claims", "-o", "json"])
    assert listed.exit_code == 0, listed.stderr
    assert [c["provider_id"] for c in json.loads(listed.stdout)["data"]] == [TRANSACTION_ID]
    cancel = ["cancel-claim", "--team", "1", "--id", TRANSACTION_ID, "-o", "json"]
    result = cli.invoke(build_app(), cancel)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["data"]["kind"] == "cancel"


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #


def test_claims_lists_the_sessions_own_queue_by_default(espn: WritableEspn):
    assert _claims() == []
    _claim(bid=7, drop="2003")
    (claim,) = _claims()
    assert set(claim) == CLAIM_KEYS
    assert claim["provider_id"] == TRANSACTION_ID
    assert claim["team_id"] == "1"
    assert claim["players_in"] == ["3002"] and claim["players_out"] == ["2003"]
    assert claim["bid_amount"] == 7
    assert claim["status"] == "PENDING"
    assert claim["proposed_at"] == "2026-09-10T12:00:01Z"  # PROPOSED_DATE_MS
    assert claim["processes_at"] == "2026-09-13T09:00:00Z"  # PROCESS_DATE_MS


def test_claims_are_newest_first_with_undated_ones_last(espn: WritableEspn):
    espn.pending += [
        _queued("old", 1, proposed=PROPOSED_DATE_MS - 60_000),
        _queued("new", 1, proposed=PROPOSED_DATE_MS + 60_000),
        {**_queued("undated", 1), "proposedDate": None},
        _queued("mid", 1),
    ]
    espn._publish(espn.bootstrap)
    assert [c["provider_id"] for c in _claims()] == ["new", "mid", "old", "undated"]


def test_claims_reads_another_teams_queue_when_asked(espn: WritableEspn):
    """A read ESPN allows any member, and useful to an agent planning around it."""
    espn.pending += [_queued("mine", 1), _queued("theirs", 2)]
    espn._publish(espn.bootstrap)
    assert [c["provider_id"] for c in _claims(team="Team Bravo")] == ["theirs"]
    assert [c["provider_id"] for c in _claims(team="1")] == ["mine"]


def test_claims_is_empty_when_espn_omits_an_empty_queue(monkeypatch: pytest.MonkeyPatch):
    """#144's acceptance criterion: ``claims --team 3`` returns an empty list,
    not exit 9, for the private-league shape."""
    install_writable_espn(monkeypatch, omit_empty_queue=True)
    assert _claims() == []
    assert _claims(team="Team Bravo") == []


def test_a_queued_claim_is_listed_and_a_processed_one_is_gone(monkeypatch: pytest.MonkeyPatch):
    """The key comes back the moment something is queued; after the waiver run
    the claim's own log row still reads ``PENDING`` and the queue is empty
    again, and ``claims`` says so rather than resurrecting the ghost."""
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    _claim(bid=3)
    assert [c["provider_id"] for c in _claims()] == [TRANSACTION_ID]
    espn.process(TRANSACTION_ID)
    assert sorted(r["status"] for r in espn.overrides["mTransactions2"]["transactions"]) == [
        "EXECUTED",
        "PENDING",
    ]
    assert "pendingTransactions" not in espn.overrides["mPendingTransactions"]
    # Fresh: the waiver run is ESPN's, not a write of ours, so nothing purged
    # the 15-minute cache the first read filled.
    assert _claims(fresh=True) == []


def test_a_cancel_against_an_omitted_queue_is_refused_and_never_sent(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
):
    espn = install_writable_espn(monkeypatch, omit_empty_queue=True)
    with pytest.raises(ConfigInvalidError, match="nothing to cancel") as caught:
        _cancel(id=TRANSACTION_ID)
    assert caught.value.details["pending_ids"] == []
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_a_claim_espn_accepted_but_left_out_of_the_queue_is_unverified_drift(
    monkeypatch: pytest.MonkeyPatch,
):
    """ESPN answered ``PENDING``, the log holds the claim, and the queue view
    omitted it: the read-back reports drift rather than ``verified: null``,
    because a body saying "nothing queued" over a live claim is the lie this
    check exists to catch. The write is still journaled ``executed``."""
    install_writable_espn(monkeypatch, pending_view=False, omit_empty_queue=True)
    data = _claim()
    assert data["read_back"] == {"verified": False, "diverged": [], "error": "SCHEMA_DRIFT"}
    assert any("could not be read back" in w for w in data.get("warnings", []))
    (entry,) = _entries()
    assert entry.kind == "claim" and entry.status == "executed"


def test_claims_without_team_needs_a_team_the_session_owns(monkeypatch: pytest.MonkeyPatch):
    install_writable_espn(monkeypatch, swid=OTHER_MEMBER)
    with pytest.raises(ConfigInvalidError) as caught:
        _claims()
    assert caught.value.details["kind"] == "argument"
    assert caught.value.details["owned_team_ids"] == []
    assert "--team" in (caught.value.remediation or "")


def test_claims_without_team_is_ambiguous_for_a_session_owning_two(espn: WritableEspn):
    espn.overrides["mNav"]["teams"][1]["owners"] = [FAKE_SWID]
    with pytest.raises(ConfigInvalidError) as caught:
        _claims()
    assert caught.value.details["owned_team_ids"] == ["1", "2"]
    assert "1, 2" in (caught.value.remediation or "")


# --------------------------------------------------------------------------- #
# cancel-claim: the happy path
# --------------------------------------------------------------------------- #


def test_a_cancel_withdraws_the_claim_journals_both_ids_and_reads_back(espn: WritableEspn):
    claimed = _claim(bid=7)
    assert claimed["provider_transaction_id"] == TRANSACTION_ID

    data = _cancel()
    assert set(data) == CANCEL_KEYS
    assert data["kind"] == "cancel"
    assert data["provider_transaction_id"] == CANCEL_TRANSACTION_ID, "the cancel's own id"
    assert data["cancelled_claim_id"] == TRANSACTION_ID
    assert data["status"] == "CANCELED" and data["is_pending"] is False
    assert data["reversible"] is False
    assert data["acknowledged_irreversible"] is None
    assert data["notified"] is None
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": None}
    assert data["items"] == [
        {"player_id": "3002", "type": "add", "from_slot": None, "to_slot": None}
    ]
    assert data["read_back"] == {
        "verified": True,
        "diverged": [],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 0,
    }
    assert data["dry_run"] is False and "warnings" not in data

    body = espn.posts[-1]
    assert body == {
        "isLeagueManager": False,
        "teamId": 1,
        "type": "WAIVER",
        "scoringPeriodId": body["scoringPeriodId"],
        "executionType": "CANCEL",
        "relatedTransactionId": TRANSACTION_ID,
    }
    assert _claims() == []

    cancel, claim = _entries()
    assert cancel.kind == "cancel" and cancel.status == "executed"
    assert cancel.reversible is False
    assert cancel.provider_transaction_id == CANCEL_TRANSACTION_ID
    assert cancel.prior_state["claim"]["provider_transaction_id"] == TRANSACTION_ID
    assert cancel.prior_state["claim"]["bid_amount"] == 7
    assert cancel.request == {"team_id": "1", "cancelled_claim_id": TRANSACTION_ID}
    assert cancel.applied_state is not None
    assert cancel.applied_state["cancelled_claim_id"] == TRANSACTION_ID
    assert cancel.applied_state["related_transaction_id"] == TRANSACTION_ID
    assert cancel.applied_state["read_back"]["verified"] is True
    assert cancel.command == (
        "fantasy-sports",
        "cancel-claim",
        "--team",
        "1",
        "--id",
        TRANSACTION_ID,
    )
    assert claim.kind == "claim" and claim.provider_transaction_id == TRANSACTION_ID


def test_after_a_cancel_the_transaction_log_still_reads_the_claim_pending(espn: WritableEspn):
    """Why the queue is the source: ``transactions`` shows the ghost (brief 06 §2)."""
    _claim()
    _cancel()
    statuses = sorted(row["status"] for row in espn.overrides["mTransactions2"]["transactions"])
    assert statuses == ["CANCELED", "PENDING"]
    assert _claims() == []


def test_a_cancel_needs_no_acknowledgement_and_is_never_announced(
    espn: WritableEspn, isolated_home: Path
):
    told = isolated_home / "told.json"
    config = isolated_home / "config" / "fantasy-sports" / "config.toml"
    config.write_text(
        config.read_text("utf-8") + f"\n[notify]\nirreversible_command = \"cat > '{told}'\"\n",
        encoding="utf-8",
    )
    _claim()
    told.unlink()  # the claim itself was announced
    data = _cancel()
    assert data["notified"] is None
    assert not told.exists()


def test_cancel_takes_a_team_by_name(espn: WritableEspn):
    _claim()
    assert _cancel(team="Team Alpha")["team_id"] == "1"


def test_a_dry_run_checks_the_queue_and_sends_and_journals_nothing(
    espn: WritableEspn, isolated_home: Path
):
    _claim()
    _journal_file(isolated_home).unlink()
    data = _cancel(dry_run=True)
    assert data["dry_run"] is True
    assert data["provider_transaction_id"] is None and data["journal_entry_id"] is None
    assert data["cancelled_claim_id"] == TRANSACTION_ID
    assert set(data["credentials"]) == {"espn_s2_age_seconds", "warning"}
    assert "nothing was sent" in data["note"]
    assert len(espn.posts) == 1, "only the claim"
    assert not _journal_file(isolated_home).exists()
    assert [c["provider_id"] for c in _claims()] == [TRANSACTION_ID]


# --------------------------------------------------------------------------- #
# cancel-claim: refused before anything is sent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("queued", "wanted"),
    [
        ([], TRANSACTION_ID),  # nothing queued: processed, already cancelled, or a typo
        ([_queued("theirs", 2)], "theirs"),  # another team's claim
        ([_queued("mine", 1)], "nonesuch"),
    ],
)
def test_an_id_not_in_this_teams_queue_is_refused_and_never_sent(
    espn: WritableEspn, isolated_home: Path, queued: list[dict[str, Any]], wanted: str
):
    espn.pending += queued
    espn._publish(espn.bootstrap)
    with pytest.raises(ConfigInvalidError) as caught:
        _cancel(id=wanted)
    error = caught.value
    assert error.details["kind"] == "argument"
    assert error.details["id"] == wanted and error.details["team_id"] == "1"
    assert error.details["pending_ids"] == [q["id"] for q in queued if q["teamId"] == 1]
    assert "fantasy-sports claims" in (error.remediation or "")
    assert exit_code_for(error) == 6
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_an_already_cancelled_claim_cannot_be_cancelled_twice(espn: WritableEspn):
    _claim()
    _cancel()
    with pytest.raises(ConfigInvalidError, match="nothing to cancel"):
        _cancel()
    assert [body["executionType"] for body in espn.posts] == ["EXECUTE", "CANCEL"]


def test_a_team_the_session_does_not_own_is_refused_before_the_queue_is_read(
    espn: WritableEspn, isolated_home: Path
):
    espn.pending.append(_queued("theirs", 2))
    espn._publish(espn.bootstrap)
    with pytest.raises(WriteRejectedError) as caught:
        _cancel(team="2", id="theirs")
    assert caught.value.kind == "not_owned"
    assert "mPendingTransactions" not in espn.views()
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_id_is_refused_before_any_read(espn: WritableEspn, blank: str):
    with pytest.raises(ConfigInvalidError, match="--id is empty"):
        _cancel(id=blank)
    assert espn.calls == []


# --------------------------------------------------------------------------- #
# cancel-claim: what ESPN says back
# --------------------------------------------------------------------------- #


def test_a_rejected_cancel_is_journaled_rejected(monkeypatch: pytest.MonkeyPatch):
    espn = install_writable_espn(monkeypatch)
    _claim()
    espn.mode, espn.reject_type = "reject", "TRAN_LINEUP_LOCKED"
    with pytest.raises(WriteRejectedError):
        _cancel()
    cancel, _ = _entries()
    assert cancel.kind == "cancel" and cancel.status == "rejected"


def test_a_cancel_that_times_out_is_never_retried_and_stays_pending(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    import requests

    _claim()
    attempts: list[Any] = []

    def timeout(*_: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs.get("json"))
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(ProviderUnavailableError) as caught:
        _cancel()
    assert len(attempts) == 1
    assert caught.value.details["transaction_state"] == "unknown"
    cancel, _ = _entries()
    assert cancel.status == "pending"


def test_a_cancel_applied_as_another_account_is_journaled_diverged(espn: WritableEspn):
    _claim()
    espn.echo_member = OTHER_MEMBER
    with pytest.raises(WriteDivergedError) as caught:
        _cancel()
    assert caught.value.details["transaction_state"] == "applied"
    assert "notified" not in caught.value.details
    cancel, _ = _entries()
    assert cancel.status == "diverged"


def test_a_claim_still_listed_after_the_cancel_is_unverified_with_a_warning(
    espn: WritableEspn,
):
    _claim()
    espn.mode = "ignore"  # 200 CANCELED, and the queue does not change
    data = _cancel()
    assert data["status"] == "CANCELED"
    assert data["read_back"] == {
        "verified": False,
        "diverged": [TRANSACTION_ID],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 1,
    }
    assert "still lists claim" in data["warnings"][0]
    cancel, _ = _entries()
    assert cancel.status == "executed"
    assert cancel.applied_state is not None
    assert cancel.applied_state["read_back"]["verified"] is False


def test_a_queue_that_cannot_be_read_back_is_unverified_with_a_warning(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    from fantasy_sports.providers.espn import EspnProvider

    _claim()
    original = EspnProvider.fetch_pending_claims
    calls: list[int] = []

    def flaky(self: EspnProvider, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise ProviderUnavailableError("down")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EspnProvider, "fetch_pending_claims", flaky)
    data = _cancel()
    assert data["read_back"] == {
        "verified": False,
        "diverged": [],
        "error": "PROVIDER_UNAVAILABLE",
    }
    assert "could not be read back" in data["warnings"][0]


def test_a_journal_that_cannot_be_completed_is_a_warning(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    _claim()

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "complete", broken)
    data = _cancel()
    assert data["provider_transaction_id"] == CANCEL_TRANSACTION_ID
    assert "stays 'pending'" in data["warnings"][0]


def test_a_claim_journal_entry_points_at_cancel_claim(espn: WritableEspn):
    from fantasy_sports.core.errors import NotAvailableError

    entry = _claim()["journal_entry_id"]
    with pytest.raises(NotAvailableError) as caught:
        _run("journal reverse", id=entry)
    assert "fantasy-sports cancel-claim" in (caught.value.remediation or "")
