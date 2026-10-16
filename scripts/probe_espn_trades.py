#!/usr/bin/env python
"""Probe ESPN's trade-proposal write surface in the throwaway league, one request at a time.

    uv run python scripts/probe_espn_trades.py --league test rosters --with 3
    uv run python scripts/probe_espn_trades.py --league test propose t1-propose --with 3 \\
        --give 123 --get 456 --note "..."
    uv run python scripts/probe_espn_trades.py --league test read t1-pending \\
        --view mPendingTransactions
    uv run python scripts/probe_espn_trades.py --league test cancel t2-cancel --related <uuid>
    uv run python scripts/probe_espn_trades.py --league test raw t3-x --body '{"type": ...}'

Written for jwulff/fantasy-sports#112. **This script sends real write requests
to ESPN**, with the guards of ``probe_espn_add_drop.py`` (#108) adapted to a
transaction that names a second team:

* ``--league`` must be the config alias ``test``; any other is refused before
  anything is read. Every POST URL must name the test league and no other
  configured league.
* The body's ``teamId`` must be the one team ``mNav`` says the session owns.
  Every ``TRADE`` item must move a player between that team and the one
  ``--with`` team, in either direction -- never between two other teams.
* At least :data:`MIN_GAP_SECONDS` between writes across invocations, one
  write per invocation, and a probe name is never re-sent: a timed-out write
  is reconciled by reading, not by sending again.

``propose`` and ``cancel`` go through :class:`EspnProvider`'s own
``propose_trade`` / ``cancel_trade``, so the shape under test is the one the
code encodes. Captures land in ``tests/cassettes/private/112/`` (gitignored)
with the cookie redacted and credentials scrubbed; they still name the real
league and its players, so they are never committed as-is.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from scripts.probe_espn_add_drop import (  # noqa: E402
    Refused,
    _check_url,
    _credentials,
    _scrubbed,
    league_ids,
)

PRIVATE = REPO / "tests" / "cassettes" / "private" / "112"
STATE = PRIVATE / "state.json"
MIN_GAP_SECONDS = 5.0
COMMENT = "Automated API test from team 7; it will be cancelled within a minute. Please ignore."


def _state() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"writes": []}


def _save_state(state: dict[str, Any]) -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _check_trade_body(body: dict[str, Any], owned: tuple[str, ...], counterparty: str | None):
    team = str(body.get("teamId"))
    if team not in owned:
        raise Refused(f"body teamId {team} is not a team this session owns ({owned})")
    for item in body.get("items") or []:
        if item.get("type") != "TRADE":
            raise Refused(f"item type {item.get('type')!r} is not TRADE")
        sides = {str(item.get("fromTeamId")), str(item.get("toTeamId"))}
        if counterparty is None or sides != {team, counterparty}:
            raise Refused(f"item moves a player between {sides}, not {team} and {counterparty}")


class Recorder:
    """``http_post`` for the provider: guards, sends once, and records."""

    def __init__(self, name, note, league, others, owned, counterparty):
        self.name, self.note = name, note
        self.league, self.others, self.owned = league, others, owned
        self.counterparty = counterparty
        self.capture: dict[str, Any] | None = None

    def __call__(self, url: str, *, json: dict[str, Any], headers, cookies, timeout):  # noqa: A002
        import requests

        _check_url(url, self.league, self.others)
        _check_trade_body(json, self.owned, self.counterparty)
        if set(cookies) != {"espn_s2"}:
            raise Refused("only espn_s2 may be sent")
        PRIVATE.mkdir(parents=True, exist_ok=True)
        with (PRIVATE / "write.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _state()
            if state["writes"]:
                last = datetime.fromisoformat(state["writes"][-1]["at"])
                gap = (datetime.now(UTC) - last).total_seconds()
                if gap < MIN_GAP_SECONDS:
                    time.sleep(MIN_GAP_SECONDS - gap)
            try:
                with (PRIVATE / f"{self.name}.attempt").open("x") as handle:
                    handle.write(datetime.now(UTC).isoformat())
            except FileExistsError as exc:
                raise Refused(f"{self.name} was already attempted; read, do not re-send") from exc
            sent_at = datetime.now(UTC)
            state["writes"].append({"at": sent_at.isoformat(), "name": self.name})
            _save_state(state)
            started = time.monotonic()
            response = requests.post(
                url, json=json, headers=headers, cookies=cookies, timeout=timeout
            )
            elapsed = int((time.monotonic() - started) * 1000)
        prepared = dict(response.request.headers)
        prepared["Cookie"] = "<redacted>"
        self.capture = {
            "name": self.name,
            "note": self.note,
            "captured_at": sent_at.replace(microsecond=0).isoformat(),
            "request": {"method": "POST", "url": url, "headers": prepared, "body": json},
            "response": {
                "status": response.status_code,
                "body": _scrubbed(response.content),
                "elapsed_ms": elapsed,
            },
        }
        (PRIVATE / f"{self.name}.json").write_text(__import__("json").dumps(self.capture, indent=2))
        return response


def _provider(http_post=None):
    from fantasy_sports.providers.espn import EspnProvider

    return EspnProvider(_credentials(), http_post=http_post)


def _owned(provider, league: str, season: int) -> tuple[str, ...]:
    owned = tuple(str(t) for t in provider.session_role(league, season).owned_team_ids)
    if len(owned) != 1:
        raise Refused(f"expected exactly one owned team, got {owned}")
    return owned


def _send(args, league, season, others, call) -> int:
    from fantasy_sports.core.errors import FantasySportsError

    if (PRIVATE / f"{args.name}.json").exists() or (PRIVATE / f"{args.name}.attempt").exists():
        raise Refused(f"{args.name} was already sent or attempted; probes are not re-run")
    owned = _owned(_provider(), league, season)
    counterparty = getattr(args, "with_team", None)
    if counterparty is not None and str(counterparty) in owned:
        raise Refused("--with names the session's own team")
    recorder = Recorder(args.name, args.note, league, others, owned, counterparty)
    try:
        result = call(_provider(http_post=recorder), owned[0])
        outcome = {
            "ok": True,
            "status": result.status,
            "is_pending": result.is_pending,
            "transaction_id": result.provider_transaction_id,
        }
    except FantasySportsError as exc:
        outcome = {"ok": False, "error": exc.to_dict()}
    sent = recorder.capture is not None
    body = recorder.capture["response"] if sent else None
    print(json.dumps({"sent": sent, "response": body, "outcome": outcome}, indent=1, default=str))
    return 0 if sent else 1


def cmd_propose(args, league, season, others) -> int:
    def call(provider, team):
        return provider.propose_trade(
            league,
            season,
            team,
            counterparty_id=str(args.with_team),
            give=args.give,
            get=args.get,
            expires_at=datetime.now(UTC) + timedelta(days=2),
            comment=COMMENT,
        )

    return _send(args, league, season, others, call)


def cmd_cancel(args, league, season, others) -> int:
    def call(provider, team):
        return provider.cancel_trade(league, season, team, args.related)

    return _send(args, league, season, others, call)


def cmd_raw(args, league, season, others) -> int:
    envelope = json.loads(args.body)
    if "teamId" in envelope or "scoringPeriodId" in envelope:
        raise Refused("teamId and scoringPeriodId are filled in by the script")

    def call(provider, team):
        write = provider._begin_write(league, season, team)  # noqa: SLF001
        return provider._post(write, envelope)  # noqa: SLF001

    return _send(args, league, season, others, call)


def cmd_read(args, league, season, others) -> int:
    """GET one view of the test league and keep it, scrubbed, under ``PRIVATE``."""
    provider = _provider()
    with provider._read(league, season, "probe_read") as espn:  # noqa: SLF001
        params: dict[str, Any] = {"view": args.view}
        if args.period is not None:
            params["scoringPeriodId"] = args.period
        payload = espn.espn_request.league_get(params=params)
    scrubbed = _scrubbed(json.dumps(payload))
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE / f"{args.name}.json").write_text(json.dumps(scrubbed, indent=2))
    print(json.dumps(scrubbed, indent=1)[:6000])
    return 0


def cmd_rosters(args, league, season, others) -> int:
    provider = _provider()
    owned = _owned(provider, league, season)
    for team in (owned[0], str(args.with_team)):
        print(f"--- team {team}")
        for slot in provider.fetch_roster(league, season, team):
            player = slot.player
            print(slot.provider_id, slot.slot, getattr(player, "position", None), player.name)
    return 0


OUT = REPO / "docs" / "research" / "07-espn-trade-surface"
COMMITTED = (
    "t1-propose",
    "t1-pending",
    "t2-cancel",
    "t2-pending",
    "t3-give-not-mine",
    "t4-recancel",
)
OTHER_TEAM = 2
"""The counterparty's team number in committed captures: a stand-in, like 06's withheld team."""


def cmd_redact(args, league, season, others) -> int:
    """Rewrite the private captures into ``OUT`` with no real league, member,
    player, or transaction identity left: league ``99``, SWID
    ``{SWID-REDACTED}``, team 7's player ``9000101``, the counterparty's team
    ``OTHER_TEAM`` and its players ``0`` / ``1``, transaction ids ``<uuid-N>``
    in order of first appearance, and player names in messages replaced."""
    import re

    owned = _owned(_provider(), league, season)[0]
    counterparty = str(args.with_team)
    texts = {name: (PRIVATE / f"{name}.json").read_text() for name in COMMITTED}
    uuids: dict[str, str] = {}
    for name in COMMITTED:
        for found in re.findall(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", texts[name]
        ):
            uuids.setdefault(found, f"<uuid-{len(uuids) + 1}>")
    players = {int(p): v for p, v in json.loads(args.players).items()}
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        text = text.replace(league, "99")
        for real, fake in uuids.items():
            text = text.replace(real, fake)
        for real, (fake, label) in players.items():
            text = re.sub(rf"\b{real}\b", str(fake), text)
            text = text.replace(label[0], label[1]) if label else text
        text = re.sub(r"\{[0-9A-F-]{36}\}", "{SWID-REDACTED}", text)
        data = json.loads(text)

        def swap_team(node: Any) -> Any:
            if isinstance(node, dict):
                out = {}
                for key, value in node.items():
                    if key in ("fromTeamId", "toTeamId", "teamId") and str(value) == counterparty:
                        value = OTHER_TEAM
                    if key == "teamActions" and isinstance(value, dict):
                        value = {
                            (str(OTHER_TEAM) if k == counterparty else k): v
                            for k, v in value.items()
                        }
                    out[key] = swap_team(value)
                return out
            if isinstance(node, list):
                return [swap_team(v) for v in node]
            return node

        data = swap_team(data)
        if isinstance(data, dict) and "request" in data:
            data["request"].pop("headers", None)
        if name.endswith("pending"):
            data = {"pendingTransactions": data.get("pendingTransactions")}
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    assert owned == "7", "captures name team 7; check the brief before redacting another team"
    print(f"wrote {len(texts)} files to {OUT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--league", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    rosters = sub.add_parser("rosters")
    rosters.add_argument("--with", dest="with_team", required=True)
    for name in ("propose", "cancel", "raw", "read"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--note", default="")
        if name in ("propose", "raw"):
            p.add_argument("--with", dest="with_team", required=name == "propose")
        if name == "propose":
            p.add_argument("--give", action="append", required=True)
            p.add_argument("--get", action="append", required=True)
        if name == "cancel":
            p.add_argument("--related", required=True)
        if name == "raw":
            p.add_argument("--body", required=True)
        if name == "read":
            p.add_argument("--view", required=True)
            p.add_argument("--period", type=int)
    redact = sub.add_parser("redact")
    redact.add_argument("--with", dest="with_team", required=True)
    redact.add_argument(
        "--players",
        required=True,
        help='JSON {"real_id": [fake_id, ["Real Name", "<label>"] or null]}',
    )
    args = parser.parse_args(argv)
    league, season, others = league_ids(args.league)
    handler = {
        "rosters": cmd_rosters,
        "propose": cmd_propose,
        "cancel": cmd_cancel,
        "raw": cmd_raw,
        "read": cmd_read,
        "redact": cmd_redact,
    }[args.command]
    return handler(args, league, season, others)


if __name__ == "__main__":
    raise SystemExit(main())
