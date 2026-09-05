"""``league info``, ``teams``, and ``standings`` — the three whole-league reads.

They share one provider call each and differ only in what they ask for, which
is why they share a module. ``standings`` is *not* ``teams`` sorted here: ESPN
returns no ordered standings and computes the order through a multi-rule
tiebreaker cascade, so the ordering and its tiebreaker source live in the
adapter. A shared "sort by wins" in this layer would be an ESPN-shaped
algorithm that Yahoo — whose ranking is server-side — would disagree with
(ARCHITECTURE §14 item 15).

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fantasy_sports.commands.context import open_read, success

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["info", "standings", "teams"]


def info(
    *,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """The league itself: name, season, sport, team count, week, roster slots.

    ``data`` is a mapping. ``roster_slots`` is what makes a legal target lineup
    constructible from normalized output alone (R3a).
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    found = ctx.provider.fetch_league(*ctx.target)
    return success(ctx, found.to_dict(), command="league info")


def teams(
    *,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Every team, in the provider's own order — which is by team id, not rank.

    ``data`` is a list of team mappings. ``owner_names`` is plural on every
    provider: a co-managed team has more than one.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    found = ctx.provider.fetch_teams(*ctx.target)
    return success(ctx, [team.to_dict() for team in found], command="teams")


def standings(
    *,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Teams in rank order, each carrying its 1-based ``standing``.

    ``data`` is a list of team mappings, first place first. The order is the
    provider's own, tiebreakers included; do not re-sort it.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    found = ctx.provider.fetch_standings(*ctx.target)
    return success(ctx, [team.to_dict() for team in found], command="standings")
