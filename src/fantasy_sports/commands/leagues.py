"""``leagues discover`` -- list the leagues the stored SWID belongs to.

Step 1 of "First run" used to be fishing a ``leagueId=`` out of a URL and
hand-writing ``config.toml``. fan.api (``providers/espn_fan.py``) names the
account's leagues from the SWID alone, so this does it for you.

**This is not a credential check.** fan.api answers ``200`` with no cookies at
all, for any SWID, so success here says nothing about whether ``espn_s2`` is
valid. Use ``auth status`` or any league read for that. Only the SWID is
resolved; a missing ``espn_s2`` does not stop discovery.

``data`` is a list with one row per league: ``league_id`` (a string), ``season``,
``sport`` (``ffl`` is ``football``; an abbreviation we do not map is passed
through, and the row's ``raw`` carries it), ``name``, ``team_id`` (the
account's own team), ``is_manager``, plus ``provider``, ``provider_id`` and
``raw``. An account with no fantasy leagues is a success with an empty list;
the envelope has no warnings channel, so the warning goes to **stderr**, where
it cannot corrupt a JSON parser reading stdout (as ``auth login``'s guidance
does).

``--config-snippet`` adds a ``config_snippet`` string to every row: a
ready-to-paste ``[leagues.<slug>]`` block. Print them with
``fantasy-sports leagues discover --config-snippet | jq -r '.data[].config_snippet'``.
It never writes ``config.toml`` (that is a separate command).

The SWID is never printed; rows hold league ids and names only.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING

from fantasy_sports.core.models import collect_untrusted

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.core.models import DiscoveredLeague
    from fantasy_sports.output.envelope import Envelope

__all__ = ["discover", "slugs", "snippet"]


def discover(*, config_snippet: bool = False) -> Envelope:
    """The account's leagues, from fan.api, keyed by the stored SWID."""
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, require_credentials
    from fantasy_sports.output.envelope import DataSource, Envelope, utc_now
    from fantasy_sports.providers import espn_fan

    swid_spec = next(spec for spec in ESPN_CREDENTIALS if spec.name == "swid")
    credentials = require_credentials((swid_spec,))
    found = espn_fan.fetch_leagues(credentials.reveal("swid"))
    rows = [league.to_dict() for league in found]
    if not rows:
        print(
            "warning: this account has no fantasy leagues (fan.api lists none). "
            "Check that the stored SWID belongs to the account that plays.",
            file=sys.stderr,
        )
    if config_snippet:
        for row, slug in zip(rows, slugs(found), strict=True):
            row["config_snippet"] = snippet(slug, row)
    return Envelope.success(
        provider=espn_fan.PROVIDER,
        data=rows,
        sources=[DataSource(name=espn_fan.SOURCE_NAME, fetched_at=utc_now())],
        untrusted=collect_untrusted(found),
    )


def slugs(leagues: list[DiscoveredLeague]) -> list[str]:
    """One config table name per league: lowercase, ``-`` separated, unique.

    A name with nothing sluggable in it falls back to ``league-<id>``. A repeat
    gets ``-2``, ``-3``, ... in listing order.
    """
    taken: dict[str, int] = {}
    result = []
    for league in leagues:
        base = re.sub(r"[^a-z0-9]+", "-", league.name.lower()).strip("-")
        base = base or f"league-{league.league_id}"
        taken[base] = taken.get(base, 0) + 1
        result.append(base if taken[base] == 1 else f"{base}-{taken[base]}")
    return result


def snippet(slug: str, row: dict[str, object]) -> str:
    """A ``[leagues.<slug>]`` block, ready to paste into ``config.toml``."""
    # JSON string escapes are valid TOML basic-string escapes, and league_id and
    # sport come from ESPN's response, not from us.
    return (
        f"[leagues.{slug}]\n"
        f"provider = {json.dumps(row['provider'])}\n"
        f"league_id = {json.dumps(row['league_id'])}\n"
        f"season = {int(row['season'])}\n"  # type: ignore[call-overload]
        f"sport = {json.dumps(row['sport'])}\n"
    )
