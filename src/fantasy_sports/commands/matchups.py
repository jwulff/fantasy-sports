"""``matchups`` — head-to-head pairings for one week.

``--week`` is a **scoring period**: the NFL week, the number a human means.
ESPN's schedule is indexed by *matchup* period, and the two diverge exactly when
a playoff round spans two NFL weeks — which is league-configurable rather than a
calendar fact, and which is why passing the scoring period straight to
``espn-api``'s ``scoreboard()`` silently returns the wrong round.

**That resolution is not re-derived here.** The adapter reads ESPN's own
``settings.matchup_periods`` table and puts both identifiers on every returned
matchup. A second copy of the mapping in this layer would be a second thing to
be wrong, and it would be wrong in the direction that only shows up in the
playoffs (``docs/memory/espn-api-is-a-shape-reader-not-a-client.md``).

Omitting ``--week`` reads the league's current week. That costs no extra
request: the league bootstrap the adapter already performed carries it, and the
adapter memoises the league object.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fantasy_sports.commands.context import open_read, success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["matchups"]


def matchups(
    *,
    week: int | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Pairings for ``week``, or for the current week.

    ``data`` is a list of matchup mappings. Each is symmetric (``team_a`` /
    ``team_b``, not home/away) and carries both ``scoring_period_id`` and
    ``matchup_period_id``; they are equal in the regular season, which is
    exactly why the split is easy to miss.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    scoring_period = (
        week if week is not None else ctx.provider.fetch_league(*ctx.target).current_week
    )
    found = ctx.provider.fetch_matchups(*ctx.target, scoring_period)
    return success(ctx, [item.to_dict() for item in found], command="matchups")
