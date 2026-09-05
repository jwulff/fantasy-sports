"""``box-scores`` — one week's matchups with both lineups, player by player.

The unit a weekly recap is written from. ``matchups`` answers who played whom
and what the score was; this answers who was started, who was benched, what
each was projected for, and what each returned.

**Separate from ``matchups`` rather than a ``--detail`` flag on it.** Three
reasons, in order of weight. It costs extra upstream requests on every
provider surveyed, so most callers of ``matchups`` would pay for detail they
never read. Its ``data`` is a different shape, and shape is a runtime contract
here — a flag that changed it would defeat the check. And ESPN cannot serve it
at all for seasons before 2019, so a flag would make ``matchups`` fail for a
season where it otherwise works fine.

``--week`` is a scoring period, the number a human means. The adapter resolves
it to ESPN's matchup period; that mapping is not re-derived here.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fantasy_sports.commands.context import open_read, success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["box_scores"]


def box_scores(
    *,
    week: int | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Both lineups for every matchup in ``week``, or in the current week.

    ``data`` is a list of box-score mappings. Each carries ``team_a_lineup``
    and ``team_b_lineup``: one entry per roster slot, with the slot, the
    player, their own position, the pro opponent, projected points, actual
    points, and whether the slot counted toward the score.

    Bench points are not computed here. They are ordinary arithmetic over
    ``started`` and ``actual_points``, and a consumer that wants them can do it
    without this layer taking a position on what an optimal lineup would have
    been — that is judgment, and judgment belongs to the caller.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    scoring_period = (
        week if week is not None else ctx.provider.fetch_league(*ctx.target).current_week
    )
    found = ctx.provider.fetch_box_scores(*ctx.target, scoring_period)
    return success(ctx, [item.to_dict() for item in found], command="box-scores")
