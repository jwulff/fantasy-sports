"""What every read command does around its one provider call.

Four things happen here rather than in eight command modules, because each of
them is a place a command could silently disagree with its siblings.

**The order of the preflight is load-bearing.** A league is resolved from
``config.toml``, then a provider class is selected, then credentials are
required — and only then is a provider constructed. Every one of those steps
can fail, and every one of them fails *before a socket is opened*: an unknown
``--league`` must not cost an ESPN round trip, and reporting ``AUTH_MISSING``
after a request has already gone out would be reporting it from the wrong
evidence. ``tests/unit/test_commands.py`` asserts the no-network half directly.

**A cache store is always constructed, even for ``--no-cache``.** The store is
what scrubs a response body, so a provider built without one hands the adapter
*unscrubbed* bytes — and ``--no-cache`` would then parse a different shape from
a cache hit. :class:`~fantasy_sports.cache.store.CacheMode.BYPASS` is the
supported way to not use the cache, and it still scrubs
(``docs/memory/cache-redaction-and-tag-classes.md``).

**Sources come from the provider, not from a counter here.** Every command
reports ``FetchRecord.sources``, so a composite read that fanned out into three
ESPN requests reports three entries with three ages, and the envelope's
``data_as_of`` is the oldest of them (R1, R4).

**The shape of ``data`` is checked, not trusted.** :func:`success` looks the
command up in the registry and refuses to build an envelope whose ``data``
disagrees with the declared :class:`~fantasy_sports.commands.DataShape`. A
convention that is only true because everyone remembered it is not a contract,
and ``jwulff/league-gazette`` parses this output.

Nothing here imports typer, and nothing imports a provider or an HTTP stack at
module scope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.core.errors import ConfigInvalidError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.cache.store import CacheStore
    from fantasy_sports.config.leagues import LeagueProfile
    from fantasy_sports.output.envelope import DataSource, Envelope

__all__ = [
    "CACHE_VARIANT_KEYS",
    "PROVIDERS",
    "DataShapeError",
    "ReadContext",
    "open_read",
    "success",
]

PROVIDERS: Mapping[str, str] = {
    "espn": "fantasy_sports.providers.espn:EspnProvider",
}
"""``provider`` value in ``config.toml`` -> dotted path to its adapter class.

A dotted path rather than a class, so naming a provider costs no import. v0.1
ships one; Sleeper and Yahoo are deferred (ADR-0002).
"""

CACHE_VARIANT_KEYS: frozenset[str] = frozenset(
    {"generated_at", "data_as_of", "data_age_seconds", "fetched_at", "age_seconds", "cached"}
)
"""The only envelope keys a cache hit may differ on, at any depth.

Fixed here and asserted in ``tests/unit/test_commands.py``: a cache hit and a
live fetch must produce byte-identical envelopes once these are removed. That
equality is what proves the cache decorator is transparent to this layer — if
a fifth key ever has to join this set, the cache stopped being a decorator and
started being part of the contract.
"""


class DataShapeError(TypeError):
    """A command produced ``data`` that disagrees with its declared shape.

    Not a taxonomy code: this is a programming error in a command, in the same
    class as :class:`~fantasy_sports.output.envelope.NaiveDatetimeError`. It is
    loud on purpose — the alternative is a consumer discovering the
    disagreement in production, on the command we tested least.
    """


@dataclass(frozen=True)
class ReadContext:
    """One resolved league, and a provider pointed at it."""

    profile: LeagueProfile
    provider: Any
    store: CacheStore

    @property
    def league_id(self) -> str:
        return self.profile.league_id

    @property
    def season(self) -> int:
        return self.profile.season

    @property
    def target(self) -> tuple[str, int]:
        """The ``(league_id, season)`` pair every ``fetch_*`` method takes."""
        return (self.profile.league_id, self.profile.season)


def open_read(
    league: str | None = None,
    season: int | None = None,
    *,
    fresh: bool = False,
    no_cache: bool = False,
    **provider_options: Any,
) -> ReadContext:
    """Resolve the league, require credentials, and build a provider.

    Every failure here happens before a request is made. ``provider_options``
    are passed to the adapter's constructor — ``free_agents`` uses it to set
    the upstream page size — and are never CLI-visible.
    """
    from fantasy_sports.auth.chain import require_credentials
    from fantasy_sports.cache.store import CacheMode, CacheStore
    from fantasy_sports.config.leagues import resolve_league

    profile = resolve_league(league, season)
    factory = _provider_class(profile.provider)

    # A bare adapter describes what it needs; it holds no credentials and makes
    # no request. The instance that does the reading is built below, once the
    # chain has produced a complete set.
    credentials = require_credentials(factory().credential_specs())

    # Always a store, even on a bypass: the store is what scrubs the body, so
    # a provider without one would hand the adapter unscrubbed bytes and make
    # `--no-cache` parse a different shape from a cache hit.
    store = CacheStore()
    mode = CacheMode.BYPASS if no_cache else (CacheMode.FRESH if fresh else CacheMode.DEFAULT)

    return ReadContext(
        profile=profile,
        provider=factory(credentials, cache=store, cache_mode=mode, **provider_options),
        store=store,
    )


def _provider_class(name: str) -> Any:
    """The adapter class for a configured provider name.

    An unknown name is ``CONFIG_INVALID`` rather than ``LEAGUE_NOT_FOUND``: the
    league profile itself parsed fine, and no other ``--league`` value can fix
    a provider this build does not have.
    """
    path = PROVIDERS.get(name.lower())
    if path is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigInvalidError(
            f"Unknown provider {name!r}; this build supports: {known}.",
            remediation="Fix the 'provider' key for this league in config.toml.",
            details={"provider": name},
        )
    from importlib import import_module

    module_name, _, attr = path.partition(":")
    return getattr(import_module(module_name), attr)


def success(
    ctx: ReadContext,
    data: Any,
    *,
    command: str,
    sources: Sequence[DataSource] | None = None,
) -> Envelope:
    """Wrap ``data`` in an envelope, with the sources the provider recorded.

    ``sources`` defaults to the provider's own ``last_fetch``, which is right
    for every command that makes one call. ``raw`` passes its own, because it
    issues one request per ``--view`` and each rebuilds the record.

    :raises DataShapeError: if ``data`` does not match the shape ``command``
        declares in the registry.
    """
    from fantasy_sports.output.envelope import Envelope

    require_shape(data, command=command)
    if sources is None:
        record = getattr(ctx.provider, "last_fetch", None)
        sources = () if record is None else record.sources
    return Envelope.success(
        provider=ctx.provider.name,
        league_id=ctx.league_id,
        season=ctx.season,
        data=data,
        sources=sources,
    )


def require_shape(data: Any, *, command: str) -> None:
    """Enforce the registered :class:`DataShape` for ``command``."""
    shape = REGISTRY[command].shape
    if shape is DataShape.COLLECTION and not isinstance(data, list):
        raise DataShapeError(
            f"{command!r} is registered as a collection, so its `data` must be a list; "
            f"got {type(data).__name__}."
        )
    if shape is DataShape.OBJECT and not isinstance(data, Mapping):
        raise DataShapeError(
            f"{command!r} is registered as a single object, so its `data` must be a "
            f"mapping; got {type(data).__name__}."
        )
