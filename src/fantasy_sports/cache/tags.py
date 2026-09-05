"""What a cache entry is *about*: its tags and its TTL.

Tags exist so a future write can invalidate exactly what it changed. v0.1 is
read-only (ARCHITECTURE §9), so nothing calls :func:`purge_by_tag` yet — the
tagging has to be right now anyway, because entries written today outlive the
release that adds writes, and a completed week is cached **forever**.

Two tag classes, not one
------------------------

The obvious design tags every entry with the league it was fetched for. It is
wrong for two ESPN views, and the wrongness is invisible until a write lands.

``players_wl`` and ``proTeamSchedules_wl`` are fetched from a **season**
endpoint — ``/apis/v3/games/ffl/seasons/2026/players`` — which carries no
league id at all. Both are re-fetched by ``free_agents()`` and by
``box_scores()``, in every league. Tagging them league-scoped breaks one of two
ways depending on how the key is built:

* if the league goes in the tag but not the key, one league's entry is
  purged when *another* league is written to, and the whole-season player map —
  the single most expensive object ESPN serves — is evicted for a roster move
  it has nothing to do with;
* if the league goes in the key as well, every league keeps its own copy of an
  identical multi-megabyte payload and the cross-league hit is lost.

So they get their own class: **season-scoped and league-independent**. One
entry, shared by every league in that season, and
:meth:`~fantasy_sports.cache.store.CacheStore.purge_by_league_tag` cannot touch
it because it carries no league tag to match.

The two classes are distinguishable from the tag string alone — see
:func:`scope_of` — so the store can refuse a league purge that was handed a
season tag, rather than quietly wiping the shared entries.

This module imports only the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "FOREVER",
    "SEASON_SCOPED_RESOURCES",
    "TTL_SECONDS",
    "RequestContext",
    "Resource",
    "TagScope",
    "league_tag",
    "scope_of",
    "scoring_period_tag",
    "season_tag",
    "tags_for",
    "ttl_for",
]

FOREVER: Final[None] = None
"""The TTL of something that cannot change again: a completed week, a past season."""


class TagScope(StrEnum):
    """Which of the two tag classes a tag belongs to."""

    LEAGUE = "league"
    """Names one league. A write to that league may purge it."""

    SEASON = "season"
    """Names only a season. Shared across every league; never league-purged."""


class Resource(StrEnum):
    """The kind of thing a request fetches. Drives both tags and TTL."""

    LEAGUE_SETTINGS = "league_settings"
    ROSTER = "roster"
    FREE_AGENTS = "free_agents"
    STANDINGS = "standings"
    MATCHUPS = "matchups"
    TRANSACTIONS = "transactions"
    PLAYERS = "players"
    """ESPN's ``players_wl`` — the whole-season player map. Season-scoped."""

    PRO_TEAM_SCHEDULES = "pro_team_schedules"
    """ESPN's ``proTeamSchedules_wl`` — the season's NFL schedule. Season-scoped."""

    UNKNOWN = "unknown"
    """A raw passthrough (``fetch_raw``). Cached briefly and league-scoped."""


SEASON_SCOPED_RESOURCES: Final[frozenset[Resource]] = frozenset(
    {Resource.PLAYERS, Resource.PRO_TEAM_SCHEDULES}
)
"""The resources that belong to a season rather than to a league.

Membership here is the whole two-class distinction. Adding a resource means
asserting that its payload is byte-identical for every league in the season —
if it is not, two leagues would serve each other stale or wrong data.
"""

TTL_SECONDS: Final[dict[Resource, int]] = {
    # ARCHITECTURE §8. Rosters and free agents move minute to minute during a
    # game; settings change a couple of times a season.
    Resource.ROSTER: 5 * 60,
    Resource.FREE_AGENTS: 5 * 60,
    Resource.STANDINGS: 15 * 60,
    Resource.MATCHUPS: 15 * 60,
    Resource.TRANSACTIONS: 15 * 60,
    Resource.LEAGUE_SETTINGS: 24 * 60 * 60,
    # The season player map and NFL schedule are large and change slowly, but
    # not never: players are added mid-season and games are flexed.
    Resource.PLAYERS: 24 * 60 * 60,
    Resource.PRO_TEAM_SCHEDULES: 24 * 60 * 60,
    # An unrecognised passthrough gets the shortest TTL. Guessing long on
    # something whose volatility we do not know is the expensive direction.
    Resource.UNKNOWN: 5 * 60,
}


@dataclass(frozen=True)
class RequestContext:
    """What the caller knows about the request it is making.

    The provider adapter fills this in; the cache never inspects a URL to guess
    it. Deriving ``resource`` from a URL shape would put ESPN's routing
    conventions inside a provider-agnostic layer, and would be wrong the first
    time ESPN moves an endpoint.
    """

    provider: str
    """``"espn"``. Part of every tag, so two providers cannot collide."""

    resource: Resource
    season: int

    league_id: str | None = None
    """Required for a league-scoped resource; ignored for a season-scoped one."""

    week: int | None = None
    """The scoring period, where the request has one."""

    completed: bool = False
    """This week is finished and its data can no longer change."""

    current_season: int | None = None
    """The season in progress, when the caller knows it.

    Supplied only so :func:`ttl_for` can recognise a historical season. A
    caller that does not know leaves it ``None`` and gets ordinary TTLs, which
    is merely slower, never wrong.
    """


def league_tag(provider: str, league_id: str, season: int) -> str:
    """Everything fetched for one league in one season."""
    return f"{TagScope.LEAGUE.value}:{provider}:{season}:{league_id}"


def scoring_period_tag(provider: str, league_id: str, season: int, week: int) -> str:
    """One league's data for one scoring period. League-scoped, finer grained."""
    return f"period:{provider}:{season}:{league_id}:{week}"


def season_tag(provider: str, season: int) -> str:
    """Everything belonging to a season regardless of league."""
    return f"{TagScope.SEASON.value}:{provider}:{season}"


_LEAGUE_PREFIXES: Final[tuple[str, ...]] = (f"{TagScope.LEAGUE.value}:", "period:")
_SEASON_PREFIXES: Final[tuple[str, ...]] = (f"{TagScope.SEASON.value}:",)


def scope_of(tag: str) -> TagScope:
    """Which class ``tag`` belongs to, from the tag string alone.

    Raises ``ValueError`` for a tag this module did not mint. A purge that
    cannot classify its argument must not run: silently treating an unknown tag
    as league-scoped is how the shared season entries would get evicted.
    """
    if tag.startswith(_LEAGUE_PREFIXES):
        return TagScope.LEAGUE
    if tag.startswith(_SEASON_PREFIXES):
        return TagScope.SEASON
    raise ValueError(f"not a tag this cache mints: {tag!r}")


def tags_for(context: RequestContext) -> tuple[str, ...]:
    """Every tag the entry for ``context`` carries.

    A season-scoped resource gets **only** the season tag, even when the caller
    supplied a ``league_id`` — it did the fetch on behalf of some league, but
    the payload is not that league's.
    """
    if context.resource in SEASON_SCOPED_RESOURCES:
        return (season_tag(context.provider, context.season),)

    if not context.league_id:
        raise ValueError(
            f"{context.resource.value} is league-scoped and needs a league_id; "
            "a season-scoped resource belongs in SEASON_SCOPED_RESOURCES"
        )

    tags = [
        league_tag(context.provider, context.league_id, context.season),
        season_tag(context.provider, context.season),
    ]
    if context.week is not None:
        tags.append(
            scoring_period_tag(context.provider, context.league_id, context.season, context.week)
        )
    return tuple(tags)


def ttl_for(context: RequestContext) -> int | None:
    """How long ``context``'s entry stays fresh, in seconds. ``None`` is forever.

    Forever is not an optimisation, it is a statement about the data: a
    completed week and a finished season cannot change, so re-fetching them is
    pure cost. It is also why the stored body must be redacted — a forever
    entry sits in the cache directory indefinitely.
    """
    if context.completed:
        return FOREVER
    if context.current_season is not None and context.season < context.current_season:
        return FOREVER
    return TTL_SECONDS[context.resource]
