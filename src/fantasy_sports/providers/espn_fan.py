"""ESPN's account listing (``fan.api.espn.com``): which leagues does this SWID belong to.

``GET https://fan.api.espn.com/apis/v2/fans/{url-encoded SWID}?featureFlags=fantasy``
describes the account. The league ids live in ``preferences[]``: one entry per
league with ``type.id == 9`` (``"Fantasy League Manager"``), whose
``metaData.entry`` carries ``seasonId``, ``entryId`` (the account's own team)
and ``groups[]`` -- each with ``groupId`` (the league id), ``groupName`` and
``groupManager``. The shape is documented in
``docs/memory/espn-401-tells-you-nothing.md`` and
``docs/research/05-espn-write-surface.md``.

**This is not a credential check.** fan.api answers ``200`` with no cookies at
all, for any SWID, so a success here says nothing about whether ``espn_s2`` is
valid. Only the SWID is sent, and only in the URL path.

**The SWID travels in the URL path, percent-encoded** (``%7B<guid>%7D``). That
is why :data:`~fantasy_sports.core.redaction.CREDENTIAL_PATTERNS` matches the
encoded spelling and why the registry also holds it: neither the URL nor an
exception interpolating it may reach a message, a cassette or a cache. Nothing
in this module puts a URL in an error.

This module is a sibling of ``providers/espn.py`` rather than part of it: it is
not league-scoped, has no cache, and shares nothing with the adapter's
``espn-api`` machinery. ``requests`` is imported inside :func:`_get`
and looked up as ``requests.get`` at call time (ADR-0008).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from fantasy_sports.core.errors import (
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.models import DiscoveredLeague
from fantasy_sports.core.redaction import scrub_credential_patterns

__all__ = ["BASE_URL", "PROVIDER", "READABLE_SPORTS", "SPORTS", "fetch_leagues", "sport_for"]

PROVIDER: Final[str] = "espn"

BASE_URL: Final[str] = "https://fan.api.espn.com/apis/v2/fans"

SOURCE_NAME: Final[str] = "fan.api:fans"
"""The envelope ``sources[]`` name. A descriptor, never a URL (it carries the SWID)."""

FANTASY_LEAGUE_MANAGER: Final[int] = 9
"""``preferences[].type.id`` of a league membership."""

READABLE_SPORTS: Final[frozenset[str]] = frozenset({"football"})
"""Sports the ESPN adapter can read today. ``EspnProvider`` ignores
``LeagueProfile.sport`` and always builds the football client against the ``nfl``
endpoint, so a pasted hockey profile would query the wrong resource."""

SPORTS: Final[Mapping[str, str]] = {
    "ffl": "football",
    "flb": "baseball",
    "fba": "basketball",
    "fhl": "hockey",
}
"""ESPN's fantasy game abbreviation -> the ``sport`` a ``config.toml`` profile uses."""

DEFAULT_TIMEOUT: Final[float] = 15.0


def sport_for(abbrev: str) -> str:
    """The sport for ESPN's game abbreviation, or the abbreviation itself if unmapped."""
    return SPORTS.get(abbrev.lower(), abbrev)


def fetch_leagues(swid: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[DiscoveredLeague]:
    """Every league the account named by ``swid`` belongs to.

    An account with no fantasy leagues has no type-9 preferences and returns
    ``[]``: an absence of rows, not drift. A body with no ``preferences`` list
    at all is ``SCHEMA_DRIFT``.
    """
    payload = _decode(_get(swid, timeout=timeout))
    preferences = payload.get("preferences") if isinstance(payload, Mapping) else None
    if not isinstance(preferences, list):
        raise SchemaDriftError(
            "fan.api's account response has no `preferences` list.",
            path=["preferences"],
            provider=PROVIDER,
        )
    leagues: list[DiscoveredLeague] = []
    for index, preference in enumerate(preferences):
        if _preference_type(preference) != FANTASY_LEAGUE_MANAGER:
            continue
        leagues.extend(_leagues_of(preference, index))
    return leagues


def _get(swid: str, *, timeout: float) -> Any:
    import requests

    url = f"{BASE_URL}/{quote(swid, safe='')}"
    try:
        response = requests.get(url, params={"featureFlags": "fantasy"}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a transport failure is unavailability
        # The message is ours, not the library's: a transport error can carry
        # the URL, and the URL carries the SWID.
        raise ProviderUnavailableError(
            f"fan.api is unreachable ({type(exc).__name__}).",
            remediation="Check the network, then retry with bounded exponential backoff.",
            details={"cause": type(exc).__name__},
        ) from exc
    status = getattr(response, "status_code", 200)
    if status == 429:
        # The adapter's own parser: one definition of Retry-After, not two.
        from fantasy_sports.providers.espn import _retry_after_seconds

        raise RateLimitedError(
            "fan.api throttled this request (HTTP 429).",
            retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
            remediation="Wait for `details.retry_after` seconds, then retry.",
            details={"status": status},
        )
    if status != 200:
        raise ProviderUnavailableError(
            f"fan.api answered HTTP {status}.",
            remediation="Retry with bounded exponential backoff; if it persists, ESPN is down.",
            details={"status": status},
        )
    return response


def _decode(response: Any) -> Any:
    try:
        return json.loads(response.content)
    except ValueError as exc:
        raise SchemaDriftError(
            "fan.api's account response is not JSON.", path=["<body>"], provider=PROVIDER
        ) from exc


def _preference_type(preference: Any) -> Any:
    kind = preference.get("type") if isinstance(preference, Mapping) else None
    return kind.get("id") if isinstance(kind, Mapping) else None


def _leagues_of(preference: Mapping[str, Any], index: int) -> list[DiscoveredLeague]:
    """One row per ``groups[]`` entry of one type-9 preference (normally exactly one)."""
    where = f"preferences[{index}]"
    entry = _mapping(preference.get("metaData"), f"{where}.metaData").get("entry")
    entry = _mapping(entry, f"{where}.metaData.entry")
    groups = entry.get("groups")
    if not isinstance(groups, list):
        raise _drift(f"{where}.metaData.entry.groups")
    season = _require(entry, "seasonId", f"{where}.metaData.entry")
    team_id = _require(entry, "entryId", f"{where}.metaData.entry")
    abbrev = _abbrev(entry)
    # The raw payload is scrubbed on the way in: it is rendered to stdout, and
    # an account listing is exactly where an unexpected SWID could ride along.
    raw = json.loads(scrub_credential_patterns(json.dumps(preference)))
    found = []
    for position, group in enumerate(groups):
        group = _mapping(group, f"{where}.metaData.entry.groups[{position}]")
        league_id = str(_require(group, "groupId", f"{where}.metaData.entry.groups[{position}]"))
        found.append(
            DiscoveredLeague(
                provider=PROVIDER,
                provider_id=league_id,
                league_id=league_id,
                season=_season(season, where),
                sport=sport_for(abbrev) if abbrev else "unknown",
                name=str(group.get("groupName") or ""),
                team_id=str(team_id),
                is_manager=bool(group.get("groupManager")),
                raw=raw,
            )
        )
    return found


def _abbrev(entry: Mapping[str, Any]) -> str:
    """ESPN's game abbreviation, lowercased (``ffl``).

    ``entry.abbrev`` (value ``"FFL"``) is observed live (2026-10-27); the other
    two spellings are fallbacks that have not been seen.
    """
    game = entry.get("game")
    nested = game.get("abbrev") if isinstance(game, Mapping) else None
    for candidate in (entry.get("abbrev"), entry.get("gameAbbrev"), nested):
        if isinstance(candidate, str) and candidate:
            return candidate.lower()
    return ""


def _season(value: Any, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _drift(f"{where}.metaData.entry.seasonId") from exc


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _drift(path)
    return value


def _require(container: Mapping[str, Any], key: str, path: str) -> Any:
    if container.get(key) is None:
        raise _drift(f"{path}.{key}")
    return container[key]


def _drift(path: str) -> SchemaDriftError:
    return SchemaDriftError(
        f"fan.api's league preference no longer has the expected shape at {path}.",
        path=path.split("."),
        provider=PROVIDER,
    )
