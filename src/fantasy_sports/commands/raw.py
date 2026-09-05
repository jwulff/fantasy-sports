"""``raw`` — the explicit passthrough, and the one thing it must never do.

ADR-0002 keeps scoring settings, draft logic, playoff formats, and player
identity out of the normalized model. ``raw`` is how they stay reachable: it
sends a view straight to ESPN and hands back the payload unmodified.

**One request per ``--view``, not one request for all of them.** ESPN merges
several ``view=`` parameters into a single document, which would make "each
view's payload" unrecoverable from the result. Fetching them separately keeps
every payload intact under its own key, and it makes each one its own entry in
the envelope's ``sources`` with its own age — which is what R1 and R4 ask for
and what a merged document cannot provide.

**An unfiltered view is labelled, never presented as authoritative.** Several
ESPN views are scoped by an ``x-fantasy-filter`` header rather than by the URL,
and without one ESPN does not error — it answers **200 with its own default
subset**. That is the dangerous case this command exists to defuse: a partial
player pool is indistinguishable from a complete one by inspection, and an
agent handed it as a passthrough result will treat it as the whole truth. So
every entry carries ``filtered`` and ``complete`` booleans and, when
incomplete, a ``warning`` saying so in words. Labelling rather than refusing is
deliberate: seeing what ESPN actually sends is the entire point of an escape
hatch, and a refusal would push a user toward reconstructing the request by
hand, with no labelling at all.

**Why a malformed ``--filter`` reports ``CONFIG_INVALID``.** A filter that is
not JSON produces exactly the failure above — ESPN ignores it and answers with
its default subset — so it cannot be passed through silently. The taxonomy has
no argument-invalid code and adding one is an API change, so this uses the one
code whose semantics are "a human must change something; retrying unchanged
cannot work". See ``commands/free_agents.py`` for the same decision.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.core.errors import ConfigInvalidError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["SUBSET_WITHOUT_FILTER", "UNFILTERED_WARNING", "raw"]

SUBSET_WITHOUT_FILTER: frozenset[str] = frozenset({"kona_player_info", "kona_playercard"})
"""Views ESPN answers with a *default subset* and a 200 when unfiltered.

Deliberately short. A view is listed here only where the truncation has been
observed, because a wrong entry costs a false warning on an authoritative
result — and the warning is worth having precisely because it is rare.
"""

UNFILTERED_WARNING = (
    "ESPN answered 200 with its own default subset because no --filter was given. "
    "This is a partial result and must not be treated as the complete set for this "
    "view; supply --filter with an x-fantasy-filter JSON body to scope it."
)


def raw(
    *,
    view: Sequence[str] | None = None,
    filter: str | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Fetch each ``--view`` from ESPN and return its payload unmodified.

    ``data`` is a mapping keyed by view name. Each entry is::

        {"view": "mSettings", "filtered": false, "complete": true,
         "warning": null, "payload": { ... exactly what ESPN sent ... }}

    ``payload`` is never reshaped, renamed, or trimmed. ``complete`` is false
    only where ESPN is known to answer an unfiltered request with a partial
    default set; check it before treating a payload as the whole truth.
    """
    views = _requested_views(view)
    header = _fantasy_filter(filter)

    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    sources: list[Any] = []
    data: dict[str, Any] = {}
    for name in views:
        payload = ctx.provider.fetch_raw(
            *ctx.target,
            view=name,
            **({} if header is None else {"x_fantasy_filter": header}),
        )
        data[name] = _entry(name, payload, filtered=header is not None)
        # A raw fetch builds its own transport, so `last_fetch` holds only the
        # view just requested. Accumulating here is what makes a repeated
        # `--view` report every contributing request rather than the last one.
        record = getattr(ctx.provider, "last_fetch", None)
        if record is not None:
            sources.extend(record.sources)

    return success(ctx, data, command="raw", sources=sources)


def _requested_views(view: Sequence[str] | None) -> list[str]:
    """The requested views, de-duplicated, in the order they were given."""
    names = [str(item).strip() for item in (view or []) if str(item).strip()]
    if not names:
        raise ConfigInvalidError(
            "`raw` needs at least one --view; ESPN has no default view.",
            remediation="Pass --view mSettings (repeat the flag for several views).",
        )
    seen: set[str] = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def _fantasy_filter(value: str | None) -> Any:
    """``--filter`` as a parsed JSON object, or ``None``."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError as exc:
        raise ConfigInvalidError(
            f"--filter must be JSON for the x-fantasy-filter header: {exc}",
            remediation='Pass something like --filter \'{"players":{"limit":50}}\'.',
        ) from exc


def _entry(name: str, payload: Any, *, filtered: bool) -> dict[str, Any]:
    complete = filtered or name not in SUBSET_WITHOUT_FILTER
    return {
        "view": name,
        "filtered": filtered,
        "complete": complete,
        "warning": None if complete else UNFILTERED_WARNING,
        "payload": payload,
    }
