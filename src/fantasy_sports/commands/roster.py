"""``roster`` — one team's lineup, and the id-or-name resolution behind ``--team``.

``--team`` takes either. An agent that has just read ``teams`` has ids; a human
has the name on the screen and should not have to look one up. Resolution runs
against the same memoised league object the roster read uses, so accepting both
costs no extra ESPN request.

The match is deliberately narrow: exact id, then exact name, then a unique
case-insensitive prefix, then a unique substring. Anything matching two teams
is an error naming both, never a silent pick — a roster is the input to a
lineup decision, and quietly reading the wrong team's is the expensive way to
be wrong.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.core.errors import LeagueNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["resolve_team_id", "roster"]


def roster(
    *,
    team: str,
    week: int | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """One team's roster slots.

    ``data`` is a list of slot mappings, each carrying the player, the lineup
    slot it occupies, the slots that player is eligible for, the kickoff of
    their game, and whether the slot can still be changed (R3).

    ``--week`` reads a historical or in-progress lineup; omitted, it is the
    current roster.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    team_id = resolve_team_id(ctx.provider.fetch_teams(*ctx.target), team)
    found = ctx.provider.fetch_roster(*ctx.target, team_id, week)
    return success(ctx, [slot.to_dict() for slot in found], command="roster")


def resolve_team_id(teams: Sequence[Any], wanted: str) -> str:
    """The provider id for ``wanted``, which may be an id or a name.

    :raises LeagueNotFoundError: when nothing matches, or when more than one
        team does. Both carry the league's ids and names in the message, so a
        caller can fix the invocation without a second command.
    """
    needle = wanted.strip()
    if not needle:
        raise _no_such_team(teams, wanted, "an empty --team matches nothing")

    for team in teams:
        if str(team.provider_id) == needle:
            return str(team.provider_id)

    folded = needle.casefold()
    for match in (
        [t for t in teams if t.name.casefold() == folded],
        [t for t in teams if t.name.casefold().startswith(folded)],
        [t for t in teams if folded in t.name.casefold()],
    ):
        if len(match) == 1:
            return str(match[0].provider_id)
        if len(match) > 1:
            names = ", ".join(sorted(f"{t.name!r} (id {t.provider_id})" for t in match))
            raise LeagueNotFoundError(
                f"--team {wanted!r} matches more than one team: {names}.",
                remediation="Pass the team id, or a name fragment that matches one team.",
                details={"team": wanted, "matches": len(match)},
            )

    raise _no_such_team(teams, wanted, f"no team matches --team {wanted!r}")


def _no_such_team(teams: Sequence[Any], wanted: str, reason: str) -> LeagueNotFoundError:
    listing = ", ".join(f"{t.provider_id}={t.name!r}" for t in teams) or "none"
    return LeagueNotFoundError(
        f"{reason}. Teams in this league: {listing}.",
        remediation="Run `fantasy-sports teams` to list the ids in this league.",
        details={"team": wanted},
    )
