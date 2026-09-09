"""``free-agents`` — unrostered players, filtered and bounded.

``--pos`` is validated by the adapter against ESPN's own slot vocabulary and an
unknown value is **refused rather than sent**. ESPN scopes this read by an
``x-fantasy-filter`` header, and an unrecognised position there is not an error:
ESPN answers 200 with its own default player set, which looks exactly like the
filtered result you asked for and is not one.

``--limit`` bounds both ends. It is passed to the adapter as the upstream page
size, so asking for ten players fetches ten rather than fetching fifty and
throwing forty away, and it is applied again to the returned list so the
promise holds however the provider interprets a page size.

**Why a refused position reports ``CONFIG_INVALID``.** The adapter raises a bare
``ValueError`` for a position ESPN would ignore, leaving the classification to
its caller. Of the seven codes that exist, ``CONFIG_INVALID`` is the only one
whose semantics are "a human must change something; retrying unchanged cannot
work", which is exactly this failure. ``PROVIDER_UNAVAILABLE`` — where an
unclassified ``ValueError`` would otherwise land — would tell an agent to retry
an invocation that can never succeed. This is a settled decision, not a
placeholder: jwulff/fantasy-sports#48 (ADR-0004 amended by ADR-0009) weighed
adding an eighth ``ARGUMENT_INVALID`` code against reusing this one and chose
reuse, because both causes tell an agent the identical thing — stop, ask a
human, do not retry. ``kind="argument"`` on the raised error marks which cause
this is for a consumer that wants the distinction without a second exit
status.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.core.errors import ConfigInvalidError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["free_agents"]


def free_agents(
    *,
    pos: str | None = None,
    limit: int = 25,
    week: int | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Unrostered players, most-owned first as the provider ranks them.

    ``data`` is a list of free-agent mappings, each carrying the player, that
    player's eligible slots and kickoff, and ``percent_owned`` where the
    provider tracks it.
    """
    wanted = max(0, int(limit))
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache, free_agent_limit=max(wanted, 1))
    scoring_period = (
        week if week is not None else ctx.provider.fetch_league(*ctx.target).current_week
    )
    try:
        found = ctx.provider.fetch_free_agents(*ctx.target, scoring_period, pos)
    except ValueError as exc:
        # The adapter refuses a position ESPN would silently ignore. That is a
        # bad argument, not a provider failure, so it must not reach the
        # taxonomy's catch-all as PROVIDER_UNAVAILABLE with a retry hint.
        raise ConfigInvalidError(
            str(exc),
            kind="argument",
            remediation="Pass a position ESPN recognises (see the valid values above), "
            "or omit --pos to get ESPN's default set.",
            details={"pos": str(pos)},
        ) from exc
    return success(ctx, [item.to_dict() for item in found[:wanted]], command="free-agents")
