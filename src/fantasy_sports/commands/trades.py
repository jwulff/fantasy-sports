"""``trades``, ``trade propose``, and ``trade cancel`` (jwulff/fantasy-sports#112).

Three atomic commands (ADR-0011): one read of the open proposals, one write
that makes a proposal, one write that withdraws it. Accepting and declining an
offer made *to* this team are #142. Valuing a trade is not this tool's job; an
agent reads rosters, projections, and ``trades``, and decides.

What an agent can rely on
-------------------------

* **The queue is ESPN's own.** ``trades`` and ``trade cancel`` read
  ``view=mPendingTransactions`` through
  :meth:`~fantasy_sports.providers.base.WritingProvider.fetch_pending_trades`.
  ``transactions`` is the wrong place to look: a cancelled proposal's own
  ``mTransactions2`` row keeps reading ``PENDING``
  (``docs/research/07-espn-trade-surface.md``).
* **Sides are named from the reading team.** Each ``trades`` row carries
  ``direction`` (``outgoing``: this team proposed it; ``incoming``: it was
  proposed to this team), ``other_team_id``, ``giving`` (players that would
  leave this team) and ``receiving`` (players that would join it), next to
  the proposer-relative fields of
  :class:`~fantasy_sports.core.models.PendingTrade`.
* **A proposal is irreversible.** The other manager can accept it the moment
  it lands, and an accepted trade enters the league's review window out of
  this tool's reach. So ``trade propose`` takes ``--acknowledge-irreversible
  "<why>"`` exactly as ``add``/``drop``/``claim`` do, journals the reason, and
  runs the owner's notify hook.
* **Everything checkable from a read is checked first.** ``--team`` must be
  the session's own (``WRITE_REJECTED``, ``not_owned``), ``--with`` another
  team; every ``--give`` must be on this roster and every ``--get`` on the
  counterparty's, by id or by a name unique there. ESPN's own answer to a
  player who is not where the item says is ``TRAN_PLAYER_NOT_ON_TEAM``, which
  the shared rejection vocabulary calls ``already_dropped`` -- misleading for
  a trade, so it is refused here, by name, before anything is sent.
* **Read back.** A proposal must appear in the queue under the id ESPN
  answered with (``verified: true``); not yet listed is ``verified: null``
  with a note, never ``true``. A cancel must leave it.
* **``trade cancel`` withdraws only this team's own proposal**, needs no
  acknowledgement (it gives nothing up), and is not announced -- the
  ``cancel-claim`` rules (#109).

Nothing here imports typer (ADR-0003); the journal, the notifier, and the
provider are imported inside the functions that use them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.claims import _team_or_own
from fantasy_sports.commands.context import open_read, success
from fantasy_sports.commands.writes import close_entry, credential_block, require_owned, settle
from fantasy_sports.core.errors import ConfigInvalidError, FantasySportsError, WriteDivergedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.commands.context import ReadContext
    from fantasy_sports.core.models import PendingTrade
    from fantasy_sports.output.envelope import Envelope

__all__ = ["cancel_trade", "list_trades", "propose_trade", "trade_view"]

ACKNOWLEDGE_FLAG = "--acknowledge-irreversible"

DEFAULT_EXPIRES_HOURS = 48
"""How long an offer stays open unless ``--expires-hours`` says otherwise:
the two days community clients send (brief 07 §1)."""

MAX_EXPIRES_HOURS = 24 * 14

PROPOSE_DRY_RUN_NOTE = (
    "Dry run: nothing was sent and nothing was journaled. Both rosters were read and every "
    "player is where the items say; ESPN has no validate-only mode, so whether it would "
    "accept the proposal (trade deadline, roster limits, locked players) was not checked. "
    f"A real run needs {ACKNOWLEDGE_FLAG}."
)

CANCEL_DRY_RUN_NOTE = (
    "Dry run: nothing was sent and nothing was journaled. The proposal is in the queue as "
    "just read; whether ESPN would still accept the cancel (the other team may answer "
    "first) was not checked."
)

NOT_IN_QUEUE_NOTE = (
    "{trade_id} was accepted as PENDING but is not in the trade queue yet; run "
    "`fantasy-sports trades --team {team_id}` to confirm."
)


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #


def list_trades(
    *,
    team: str | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Open trade proposals one team made or received, newest first.

    ``data`` is a list of :class:`~fantasy_sports.core.models.PendingTrade`
    mappings plus ``direction``, ``other_team_id``, ``giving`` and
    ``receiving`` from ``--team``'s side (see :func:`trade_view`).
    ``provider_id`` is what ``trade cancel --id`` takes. ``--team`` defaults to
    the session's own team; reading another team's proposals is allowed.
    ``comment`` is free text another manager wrote and is labeled untrusted;
    ESPN's queue does not return it, so it is empty here.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    team_id = _team_or_own(ctx, team)
    found = _newest_first(ctx.provider.fetch_pending_trades(*ctx.target, team_id))
    from fantasy_sports.core.models import collect_untrusted

    return success(
        ctx,
        [trade_view(trade, team_id) for trade in found],
        command="trades",
        untrusted=collect_untrusted(found),
    )


def trade_view(trade: PendingTrade, team_id: str) -> dict[str, Any]:
    """``trade`` as a mapping, with its sides restated from ``team_id``'s."""
    outgoing = trade.proposing_team_id == str(team_id)
    data = trade.to_dict()
    data.update(
        direction="outgoing" if outgoing else "incoming",
        other_team_id=trade.counterparty_team_id if outgoing else trade.proposing_team_id,
        giving=list(trade.players_offered if outgoing else trade.players_requested),
        receiving=list(trade.players_requested if outgoing else trade.players_offered),
    )
    return data


def _newest_first(trades: Sequence[PendingTrade]) -> list[PendingTrade]:
    dated = sorted(
        (t for t in trades if t.proposed_at is not None),
        key=lambda t: t.proposed_at,  # type: ignore[arg-type, return-value]
        reverse=True,
    )
    return dated + [t for t in trades if t.proposed_at is None]


# --------------------------------------------------------------------------- #
# trade propose
# --------------------------------------------------------------------------- #


def propose_trade(
    *,
    team: str,
    with_team: str,
    give: list[str],
    get: list[str],
    acknowledge_irreversible: str | None = None,
    comment: str | None = None,
    expires_hours: int = DEFAULT_EXPIRES_HOURS,
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Offer players from one of the session's own teams for another team's.

    ``data`` is one mapping with the keys every write returns -- ``team_id``,
    ``kind`` (``"trade"``), ``provider_transaction_id`` (the proposal's id),
    ``status``, ``is_pending``, ``proposed_at``, ``items``,
    ``journal_entry_id``, ``reversible`` (false), ``acknowledged_irreversible``,
    ``cost``, ``credentials``, ``read_back``, ``notified``, ``dry_run`` -- plus
    ``counterparty_team_id``, ``giving``, ``receiving``, ``expires_at`` and
    ``comment``.
    """
    del fresh  # a write always reads fresh; accepted only as a common option
    command = ["fantasy-sports", "trade", "propose", "--team", str(team), "--with", str(with_team)]
    for player in give:
        command += ["--give", str(player)]
    for player in get:
        command += ["--get", str(player)]
    if comment is not None:
        command += ["--comment", str(comment)]
    command += ["--expires-hours", str(expires_hours)]
    if acknowledge_irreversible is not None:
        # Journaled as invoked, so the stored command reproduces the operation
        # (as `add`/`drop`/`claim` do).
        command += [ACKNOWLEDGE_FLAG, str(acknowledge_irreversible)]
    if dry_run:
        command.append("--dry-run")
    if isinstance(expires_hours, bool) or not 1 <= int(expires_hours) <= MAX_EXPIRES_HOURS:
        raise _argument(
            f"--expires-hours must be between 1 and {MAX_EXPIRES_HOURS}; got {expires_hours!r}.",
            f"Omit it for {DEFAULT_EXPIRES_HOURS} hours, or pass a whole number of hours.",
            details={"expires_hours": str(expires_hours)},
        )
    if not give or not get:
        raise _argument(
            "A trade proposal needs at least one --give and one --get.",
            "Name the players leaving your team with --give and the ones you want with --get; "
            "each flag repeats.",
            details={"give": list(give), "get": list(get)},
        )

    ctx = open_read(league, season, fresh=True, no_cache=no_cache)
    from fantasy_sports.commands.roster import resolve_team_id

    provider = ctx.provider
    teams = provider.fetch_teams(*ctx.target)
    team_id = resolve_team_id(teams, team)
    role = provider.session_role(*ctx.target)
    # Before any roster is read, so a team the session does not own is refused
    # as not_owned whatever the players are.
    require_owned(role, team_id)
    other_id = resolve_team_id(teams, with_team)
    if str(other_id) == str(team_id):
        raise _argument(
            "--with names your own team; a trade needs another team.",
            "Run `fantasy-sports teams` and pass the team you want to trade with.",
            details={"team_id": str(team_id), "with": str(with_team)},
        )
    mine = provider.fetch_roster(*ctx.target, str(team_id))
    theirs = provider.fetch_roster(*ctx.target, str(other_id))
    giving = [_on_roster(mine, wanted, team_id, "--give") for wanted in give]
    receiving = [_on_roster(theirs, wanted, other_id, "--get") for wanted in get]
    ids = [str(slot.provider_id) for slot in giving + receiving]
    if len(set(ids)) != len(ids):
        raise _argument(
            "The same player is named more than once.",
            "Name each player once.",
            details={"player_ids": ids},
        )

    expires_at = provider_now(provider) + timedelta(hours=int(expires_hours))
    period = getattr(provider.fetch_league(*ctx.target), "current_week", None)
    give_ids = [str(slot.provider_id) for slot in giving]
    get_ids = [str(slot.provider_id) for slot in receiving]
    statement = irreversible_because(
        [_label(s.player) for s in giving], [_label(s.player) for s in receiving], other_id
    )
    items = [
        {
            "player_id": pid,
            "type": "trade",
            "from_team_id": str(team_id),
            "to_team_id": str(other_id),
        }
        for pid in give_ids
    ] + [
        {
            "player_id": pid,
            "type": "trade",
            "from_team_id": str(other_id),
            "to_team_id": str(team_id),
        }
        for pid in get_ids
    ]
    data: dict[str, Any] = {
        "team_id": str(team_id),
        "kind": "trade",
        "provider_transaction_id": None,
        "status": None,
        "is_pending": None,
        "proposed_at": None,
        "items": items,
        "journal_entry_id": None,
        "reversible": False,
        "acknowledged_irreversible": acknowledge_irreversible,
        "cost": {"bid_amount": None, "displaced_player_id": None, "players_offered": give_ids},
        "credentials": None,
        "read_back": None,
        "notified": None,
        "dry_run": dry_run,
        "counterparty_team_id": str(other_id),
        "giving": give_ids,
        "receiving": get_ids,
        "expires_at": expires_at,
        "comment": comment,
    }
    if dry_run:
        data["credentials"] = credential_block(provider)
        data["irreversible_because"] = statement
        data["note"] = PROPOSE_DRY_RUN_NOTE
        return success(ctx, data, command="trade propose")

    if acknowledge_irreversible is None or not str(acknowledge_irreversible).strip():
        raise _argument(
            f"This trade proposal cannot be taken back once accepted, and {ACKNOWLEDGE_FLAG} "
            f"was not given. {statement}",
            f'Pass {ACKNOWLEDGE_FLAG} "<why>" with the reason for this offer; it is journaled '
            "verbatim. An autonomous agent must clear /sanity-gate before passing it. Use "
            "--dry-run to preview the proposal without the flag.",
            details={
                "flag": ACKNOWLEDGE_FLAG,
                "move": "trade",
                "irreversible_because": statement,
                "cost": dict(data["cost"]),
            },
        )

    from fantasy_sports.commands.moves import _announce
    from fantasy_sports.core.journal import Journal, JournalEntry, new_entry_id

    warnings: list[str] = []
    with Journal.open() as journal:
        # Raises CONFIG_INVALID if the journal cannot be written; nothing is
        # sent without its record.
        entry = journal.begin(
            JournalEntry(
                id=new_entry_id(),
                provider=provider.name,
                league_id=ctx.league_id,
                season=ctx.season,
                scoring_period=period,
                command=list(command),
                role=role.role,
                team_id=str(team_id),
                kind="trade",
                reversible=False,
                prior_state={
                    "giving": [_slot_state(s) for s in giving],
                    "receiving": [_slot_state(s) for s in receiving],
                },
                request={
                    "team_id": str(team_id),
                    "counterparty_team_id": str(other_id),
                    "items": items,
                    "expires_at": expires_at.isoformat(),
                    "comment": comment,
                    "acknowledged_irreversible": acknowledge_irreversible,
                },
                cost=data["cost"],
            )
        )
        data["journal_entry_id"] = entry.id
        try:
            result = provider.propose_trade(
                *ctx.target,
                str(team_id),
                counterparty_id=str(other_id),
                give=give_ids,
                get=get_ids,
                expires_at=expires_at,
                comment=comment,
            )
        except WriteDivergedError as exc:
            # Applied, perhaps as another account: record it and tell the owner.
            items_echoed = list(exc.details.get("items") or [])
            settle(exc, journal.diverge, entry.id, {"items": items_echoed}, exc)
            exc._record_detail("notified", _announce(journal, entry.id))
            raise
        except FantasySportsError as exc:
            # Only an outcome the adapter marks unknown (a timeout, a 5xx, an
            # unreadable 200) leaves the entry pending; it is never retried:
            # a second proposal is a second offer.
            if exc.details.get("transaction_state") != "unknown":
                settle(exc, journal.reject, entry.id, exc)
            raise

        data.update(
            provider_transaction_id=result.provider_transaction_id,
            status=result.status,
            is_pending=result.is_pending,
            proposed_at=result.proposed_at,
            credentials=dict(result.credentials),
        )
        read_back = _proposal_read_back(ctx, str(team_id), result.provider_transaction_id)
        close_entry(
            journal,
            entry.id,
            warnings,
            applied_state={
                "items": [dict(item) for item in result.items],
                "is_pending": result.is_pending,
                "read_back": read_back,
            },
            member_id=result.member_id,
            provider_transaction_id=result.provider_transaction_id,
            proposed_at=result.proposed_at,
        )
        data["read_back"] = read_back
        data["notified"] = _announce(journal, entry.id)

    if read_back.get("error") is not None:
        warnings.append(
            f"ESPN accepted the proposal but the trade queue could not be read back "
            f"({read_back['error']}). Run `fantasy-sports trades` before proposing again."
        )
    if warnings:
        data["warnings"] = warnings
    return success(ctx, data, command="trade propose")


def irreversible_because(giving: Sequence[str], receiving: Sequence[str], other: str) -> str:
    """What this proposal cannot undo, in one sentence an agent can repeat."""
    return (
        f"Team {other} can accept the offer of {', '.join(giving)} for {', '.join(receiving)} "
        "at any moment until it is cancelled or expires; once accepted the trade enters the "
        "league's review window and this tool cannot reverse it."
    )


def provider_now(provider: Any) -> Any:
    """The provider's clock, so a test's frozen time drives the expiry too."""
    clock = getattr(provider, "_now", None)
    if callable(clock):
        return clock()
    from fantasy_sports.output.envelope import utc_now

    return utc_now()


def _on_roster(roster: Sequence[Any], wanted: str, team_id: str, flag: str) -> Any:
    """``wanted`` resolved on one team's roster, worded for a trade."""
    from fantasy_sports.commands.moves import resolve_rostered

    try:
        return resolve_rostered(roster, wanted)
    except ConfigInvalidError as exc:
        if "matches" in exc.details:
            raise
        raise _argument(
            f"{flag} {wanted!r} is not on team {team_id}'s roster.",
            f"Run `fantasy-sports roster --team {team_id}` for its player ids and names; "
            "--give names players on your team, --get players on the other team.",
            details={"player": str(wanted), "team_id": str(team_id), "flag": flag},
        ) from exc


def _slot_state(slot: Any) -> dict[str, Any]:
    return {"player_id": str(slot.provider_id), "slot": slot.slot}


def _label(player: Any) -> str:
    return f"{player.name} ({player.provider_id})"


def _proposal_read_back(ctx: ReadContext, team_id: str, trade_id: str) -> dict[str, Any]:
    """Look for the proposal in the queue by the id ESPN answered with."""
    found = queue_read_back(ctx, team_id, trade_id, gone=False)
    if found["verified"] or found.get("error") is not None:
        return found
    return {
        "verified": None,
        "diverged": [],
        "queue_size": found["queue_size"],
        "note": NOT_IN_QUEUE_NOTE.format(trade_id=trade_id, team_id=team_id),
    }


def queue_read_back(ctx: ReadContext, team_id: str, trade_id: str, *, gone: bool) -> dict[str, Any]:
    """Re-read the team's open proposals for ``trade_id``.

    ``gone=True`` (a cancel) verifies when it is absent; ``gone=False`` (a
    proposal) when it is listed. A read that fails is ``verified: false``
    with the error code, not raised: ESPN already answered the write.
    """
    try:
        queue = ctx.provider.fetch_pending_trades(*ctx.target, str(team_id))
    except FantasySportsError as exc:
        return {"verified": False, "diverged": [], "error": exc.code.value}
    listed = any(t.provider_id == trade_id for t in queue)
    verified = listed is not gone
    return {
        "verified": verified,
        "diverged": [] if verified else [trade_id],
        "matched_by": ["provider_transaction_id"],
        "queue_size": len(queue),
    }


# --------------------------------------------------------------------------- #
# trade cancel
# --------------------------------------------------------------------------- #


def cancel_trade(
    *,
    team: str,
    id: str,  # noqa: A002 - the CLI flag is --id, and the registry names params by flag
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Withdraw one of the session's own pending trade proposals.

    ``data`` has the keys every write returns, with ``kind``
    ``"trade_cancel"`` and ``provider_transaction_id`` the **cancel's** new
    id, plus ``cancelled_trade_id`` and ``counterparty_team_id``.
    """
    del fresh  # a write always reads fresh; accepted only as a common option
    command = ["fantasy-sports", "trade", "cancel", "--team", str(team), "--id", str(id)]
    if dry_run:
        command.append("--dry-run")
    wanted = str(id or "").strip()
    if not wanted:
        raise _argument(
            "--id is empty; a cancel needs the transaction id of the proposal it withdraws.",
            "Run `fantasy-sports trades --team ...` and pass a `provider_id` from it.",
            details={"id": str(id)},
        )

    ctx = open_read(league, season, fresh=True, no_cache=no_cache)
    from fantasy_sports.commands.roster import resolve_team_id

    provider = ctx.provider
    team_id = str(resolve_team_id(provider.fetch_teams(*ctx.target), team))
    role = provider.session_role(*ctx.target)
    require_owned(role, team_id)
    queue = provider.fetch_pending_trades(*ctx.target, team_id)
    trade = next((t for t in queue if t.provider_id == wanted), None)
    if trade is None or trade.proposing_team_id != team_id:
        incoming = trade is not None
        raise _argument(
            (
                f"Proposal {wanted!r} was made to team {team_id}, not by it; only the proposer "
                "can cancel. Nothing was sent."
                if incoming
                else f"No open proposal {wanted!r} was made by team {team_id}, so there is "
                "nothing to cancel. Nothing was sent."
            ),
            "Run `fantasy-sports trades --team ...` for this team's outgoing proposal ids. One "
            "that was accepted, declined, cancelled, or expired is no longer open."
            + (" Declining an incoming offer is not supported yet (#142)." if incoming else ""),
            details={
                "id": wanted,
                "team_id": team_id,
                "outgoing_ids": [t.provider_id for t in queue if t.proposing_team_id == team_id],
            },
        )

    period = getattr(provider.fetch_league(*ctx.target), "current_week", None)
    data: dict[str, Any] = {
        "team_id": team_id,
        "kind": "trade_cancel",
        "provider_transaction_id": None,
        "cancelled_trade_id": trade.provider_id,
        "counterparty_team_id": trade.counterparty_team_id,
        "status": None,
        "is_pending": None,
        "proposed_at": None,
        "items": _trade_items(trade),
        "journal_entry_id": None,
        "reversible": False,
        # No --acknowledge-irreversible: a cancel gives nothing up.
        "acknowledged_irreversible": None,
        "cost": {"bid_amount": None, "displaced_player_id": None},
        "credentials": None,
        "read_back": None,
        # Like cancel-claim: nothing spent or released, so nothing to announce.
        "notified": None,
        "dry_run": dry_run,
    }
    if dry_run:
        data["credentials"] = credential_block(provider)
        data["note"] = CANCEL_DRY_RUN_NOTE
        return success(ctx, data, command="trade cancel")

    from fantasy_sports.core.journal import Journal, JournalEntry, new_entry_id

    warnings: list[str] = []
    with Journal.open() as journal:
        entry = journal.begin(
            JournalEntry(
                id=new_entry_id(),
                provider=provider.name,
                league_id=ctx.league_id,
                season=ctx.season,
                scoring_period=period,
                command=list(command),
                role=role.role,
                team_id=team_id,
                kind="trade_cancel",
                reversible=False,
                prior_state={"trade": _trade_state(trade)},
                request={"team_id": team_id, "cancelled_trade_id": trade.provider_id},
            )
        )
        data["journal_entry_id"] = entry.id
        try:
            result = provider.cancel_trade(*ctx.target, team_id, trade.provider_id)
        except WriteDivergedError as exc:
            items = list(exc.details.get("items") or [])
            settle(exc, journal.diverge, entry.id, {"items": items}, exc)
            raise
        except FantasySportsError as exc:
            if exc.details.get("transaction_state") != "unknown":
                settle(exc, journal.reject, entry.id, exc)
            raise

        related = result.raw.get("relatedTransactionId") if result.raw else None
        data.update(
            provider_transaction_id=result.provider_transaction_id,
            status=result.status,
            is_pending=result.is_pending,
            proposed_at=result.proposed_at,
            credentials=dict(result.credentials),
        )
        read_back = queue_read_back(ctx, team_id, trade.provider_id, gone=True)
        close_entry(
            journal,
            entry.id,
            warnings,
            applied_state={
                "cancelled_trade_id": trade.provider_id,
                "related_transaction_id": related if isinstance(related, str) else None,
                "items": [dict(item) for item in result.items],
                "read_back": read_back,
            },
            member_id=result.member_id,
            provider_transaction_id=result.provider_transaction_id,
            proposed_at=result.proposed_at,
        )
        data["read_back"] = read_back

    if read_back.get("error") is not None:
        warnings.append(
            f"ESPN accepted the cancel but the trade queue could not be read back "
            f"({read_back['error']}). Run `fantasy-sports trades` before proposing again."
        )
    elif read_back["verified"] is False:
        warnings.append(
            f"ESPN answered {result.status} but still lists proposal {trade.provider_id} as "
            "open. Run `fantasy-sports trades` before acting on it; do not re-cancel blind."
        )
    if warnings:
        data["warnings"] = warnings
    return success(ctx, data, command="trade cancel")


def _trade_items(trade: PendingTrade) -> list[dict[str, Any]]:
    proposer, other = trade.proposing_team_id, trade.counterparty_team_id
    return [
        {"player_id": pid, "type": "trade", "from_team_id": proposer, "to_team_id": other}
        for pid in trade.players_offered
    ] + [
        {"player_id": pid, "type": "trade", "from_team_id": other, "to_team_id": proposer}
        for pid in trade.players_requested
    ]


def _trade_state(trade: PendingTrade) -> dict[str, Any]:
    """The proposal as the journal stores it: JSON-plain, without ``raw``."""
    return {
        "provider_transaction_id": trade.provider_id,
        "proposing_team_id": trade.proposing_team_id,
        "counterparty_team_id": trade.counterparty_team_id,
        "players_offered": list(trade.players_offered),
        "players_requested": list(trade.players_requested),
        "proposed_at": None if trade.proposed_at is None else trade.proposed_at.isoformat(),
        "expires_at": None if trade.expires_at is None else trade.expires_at.isoformat(),
        "status": trade.status,
        "team_actions": dict(trade.team_actions),
    }


def _argument(message: str, remediation: str, *, details: dict[str, Any]) -> ConfigInvalidError:
    return ConfigInvalidError(message, kind="argument", remediation=remediation, details=details)
