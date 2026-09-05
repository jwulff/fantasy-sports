"""``transactions`` — recent roster moves, newest first.

ESPN's ``mTransactions2`` view is **scoped to one scoring period** and offers no
date range, so "the last N moves" can only be answered by walking scoring
periods backward from the current one. That walk is here rather than in the
adapter because it is a *command's* policy — how far back is worth looking —
and because the adapter's own sweep is keyed on ``since``, which is a different
question with a different cost.

**The walk is capped, and the cap is stated.** :data:`PERIOD_CAP` scoring
periods, and it stops as soon as ``--limit`` is satisfied. Uncapped, a quiet
league in week 15 would issue fifteen requests to answer a question the user
expected to be cheap; capped, the answer is honest about being a recent window
rather than the season. Each period costs one ``mTransactions2`` request plus
one activity-feed request, and the feed's repeat is a cache hit after the first
period unless ``--no-cache`` is in force.

Every one of those requests reaches the envelope. The adapter files repeated
views under a per-scoring-period key, so ``sources`` itemises each period's
fetch and ``data_as_of`` reports the **oldest** of them — an agent shown one
blended age would make exactly the deadline error freshness exists to prevent
(R1, R4).

The two surfaces ESPN publishes transactions on are reconciled inside the
adapter. What is deduplicated here is the *overlap between periods*: the
activity feed is a rolling recent list, so every period's read returns it
again. Identity is the move — type, team, players, timestamp — and never the
provider id, because ESPN's two surfaces number the same move differently.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["PERIOD_CAP", "transactions"]

PERIOD_CAP = 6
"""How many scoring periods the backward walk may read before giving up.

Six is a month and a half of NFL weeks: long enough that "recent moves" is a
useful answer in a quiet league, short enough that the worst case is a bounded
handful of requests rather than a season-long sweep.
"""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def transactions(
    *,
    limit: int = 25,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """The most recent roster moves, newest first.

    ``data`` is a list of transaction mappings, at most ``--limit`` long. Types
    are the four normalized ones — ``add``, ``drop``, ``trade``,
    ``waiver_claim``. ESPN states with no normalized equivalent (a vetoed
    trade, a failed waiver) are readable only through each item's ``raw``.

    A move with no processed date sorts last; that is ordinary ESPN data, not a
    missing value to be filled in.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    wanted = max(0, int(limit))
    current = int(ctx.provider.fetch_league(*ctx.target).current_week)

    found: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for period in range(current, max(0, current - PERIOD_CAP), -1):
        for item in ctx.provider.fetch_transactions(*ctx.target, scoring_period=period):
            key = _identity(item)
            if key in seen:
                continue
            seen.add(key)
            found.append(item)
        if len(found) >= wanted:
            break

    found.sort(key=_recency, reverse=True)
    return success(ctx, [item.to_dict() for item in found[:wanted]], command="transactions")


def _identity(transaction: Any) -> tuple[Any, ...]:
    """The move itself, so one period's read cannot duplicate another's.

    Mirrors the identity the adapter merges ESPN's two surfaces on: the
    provider id is deliberately excluded, because the surfaces number the same
    move differently and matching on the id would make every trade a duplicate.
    """
    return (
        transaction.type,
        transaction.team_provider_id,
        tuple(sorted(transaction.players_in)),
        tuple(sorted(transaction.players_out)),
        None if transaction.timestamp is None else int(transaction.timestamp.timestamp()),
    )


def _recency(transaction: Any) -> tuple[bool, datetime]:
    """Sort key for newest-first, with undated moves last under ``reverse``."""
    return (transaction.timestamp is not None, transaction.timestamp or _EPOCH)
