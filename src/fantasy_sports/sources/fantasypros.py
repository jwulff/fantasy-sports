"""The FantasyPros expert source (ADR-0011, jwulff/fantasy-sports#128).

FantasyPros' public API (``https://api.fantasypros.com/public/v2/json``,
authenticated by an ``x-api-key`` header) is the expert-consensus layer ESPN's
own data lacks: per-position consensus rankings with best/worst/average/std
across ~40 weekly experts, weekly and rest-of-season projections with
STD/HALF/PPR points, injuries with practice-report status, player news with a
fantasy-impact line, and a players table whose ``espn_id`` is the join onto
every ESPN read this tool already does.

It is a **source**, not a provider. It has no leagues, so it does not implement
``providers/base.py::Provider``; it shares the credential chain and the HTTP
cache and nothing else. Commands (#129) and the ESPN join (#130) sit above it.

Four facts about the API shape every method here is built around
---------------------------------------------------------------

1. **Premium is 1 request/second and 500 requests/day**, and no rate-limit
   header is returned to say so (the limits come from the approval email). So
   every read goes through :class:`~fantasy_sports.cache.store.CachingFetcher`
   -- a hit costs nothing against the daily budget -- and every live request
   goes through one process-wide :class:`Pacer` that enforces the spacing.
   Two clients in one process share the pacer, because the budget is the
   account's, not the client's.
2. **The free tier truncates every list to 10 rows and says so**, with
   ``"tier": "free", "limit": 10`` in the body. A truncated pool that looks
   like a complete answer is the worst available failure for a lineup
   decision, so anything other than ``"tier": "premium"`` with no ``limit``
   key raises ``CONFIG_INVALID`` with ``details.kind = "tier"`` naming the
   upgrade -- before the body can reach the cache, so an upgrade takes effect
   on the next call rather than after the TTL.
3. **A bad argument comes back as an error body, not an HTTP error we can
   classify by status alone.** The 2026-10-16 capture saw a *200* carrying
   ``{"message": "Invalid Position", ...}`` and no data; a probe the same
   day saw a *400* with ``message``, ``parameter`` and ``valid_format``.
   Both are ``CONFIG_INVALID`` with ``kind = "argument"`` and the message
   verbatim (ADR-0009): a human must change the argument, retrying cannot
   work. Known positions and scorings are refused here before anything is
   sent, for the same reason the ESPN adapter refuses an unknown ``--pos``.
4. **``scoring=`` on projections is a 403**, so no method here sends it; the
   three point totals are already in every row's ``stats``. A 401 or 403 is
   therefore the key being refused, and maps to ``AUTH_EXPIRED`` with
   ``details.kind = "auth"``.

The key is carried as a :class:`~fantasy_sports.auth.chain.Secret`, which
registers it with the redaction set the moment it is resolved, so every error
message, every cache body and every cassette the test harness records is
scrubbed of it (``docs/memory/credential-leak-channels.md``). It is revealed
exactly once, into the request header.

Nothing here imports ``requests`` at module scope (ADR-0008); it is imported
inside :meth:`FantasyProsClient._send`. Nothing here imports typer, click, rich
or ``espn_api``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from fantasy_sports.auth.chain import (
    FANTASYPROS_API_KEY,
    FANTASYPROS_CREDENTIALS,
    CredentialSet,
    resolve_credentials,
)
from fantasy_sports.cache.store import CacheMode, CacheStore, CachingFetcher
from fantasy_sports.cache.tags import RequestContext, Resource
from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    FantasySportsError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.redaction import UnscrubbableResponseError, decode_body
from fantasy_sports.output.envelope import DataSource, from_epoch_seconds, utc_now

__all__ = [
    "API_KEY_HEADER",
    "BASE_URL",
    "DEFAULT_TIMEOUT",
    "DOCTOR_PROBE_PLAYER_ID",
    "MIN_INTERVAL_SECONDS",
    "POSITIONS",
    "PREMIUM_TIER",
    "SCORINGS",
    "SOURCE",
    "SPORT",
    "TERMS",
    "FantasyProsClient",
    "Pacer",
    "SourceResponse",
    "default_season",
    "shared_pacer",
]

SOURCE: Final[str] = "fantasypros"
"""The ``provider`` string in every cache tag and the ``sources[]`` name prefix."""

BASE_URL: Final[str] = "https://api.fantasypros.com/public/v2/json"
API_KEY_HEADER: Final[str] = "x-api-key"
SPORT: Final[str] = "nfl"
"""The only sport this source reads. MLB/NBA/NHL are out of scope (#128)."""

PREMIUM_TIER: Final[str] = "premium"
"""The only ``tier`` whose lists are complete. Anything else is truncated."""

POSITIONS: Final[tuple[str, ...]] = (
    "QB",
    "RB",
    "WR",
    "TE",
    "K",
    "DST",
    "FLX",
    "OP",
    "IDP",
    "DL",
    "LB",
    "DB",
)
"""The ranking formats FantasyPros lists for itself (the premium capture's
``ecr_experts`` keys): the offensive slots, ``OP`` for superflex, and the IDP
groups. Anything else is a 200 or 400 with ``{"message": "Invalid Position"}``,
so a typo is refused before sending. The server stays the authority: a format
this list misses still comes back as ``CONFIG_INVALID`` carrying its message."""

SCORINGS: Final[tuple[str, ...]] = ("STD", "HALF", "PPR")

MIN_INTERVAL_SECONDS: Final[float] = 1.0
"""Premium's rate limit, enforced in-process. Cache hits are not requests."""

DEFAULT_TIMEOUT: Final[tuple[float, float]] = (5.0, 30.0)
"""``requests`` connect and read timeouts. The players table is 4 MB."""

DOCTOR_PROBE_PLAYER_ID: Final[int] = 22947
"""The one-row ``/nfl/players?player=`` request ``doctor --live`` spends to read
the tier. A FantasyPros id, chosen because it is small and stable."""

TERMS: Final[str] = (
    "FantasyPros data is for personal, non-commercial use under the FantasyPros API terms; "
    "every payload that carries it names fantasypros in sources[]."
)

_TIER_REMEDIATION: Final[str] = (
    "Upgrade the FantasyPros account to Hall of Fame, which unlocks the API's premium tier "
    "(complete lists, 1 request/second, 500 requests/day), then retry. The free tier "
    "truncates every list to 10 rows."
)

_AUTH_REMEDIATION: Final[str] = (
    "Check the FantasyPros API key: set FANTASY_SPORTS_FANTASYPROS_API_KEY, or run "
    "`fantasy-sports auth login` and paste it at the FantasyPros prompt. If the key is "
    "right, the account's API access may have lapsed."
)

#: Endpoint descriptor -> the key that must hold a list in a healthy response.
_LIST_KEYS: Final[dict[str, str]] = {
    "players": "players",
    "consensus-rankings": "players",
    "rankings": "players",
    "projections": "players",
    "injuries": "injuries",
    "news": "items",
    "experts": "experts",
    "player-points": "players",
}


def default_season(now: datetime | None = None) -> int:
    """The NFL season in progress: the calendar year, except January and
    February belong to the season that started the previous autumn."""
    moment = now or utc_now()
    return moment.year - 1 if moment.month < 3 else moment.year


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #


class Pacer:
    """At most one request per :attr:`min_interval` seconds, in-process.

    Blocking rather than refusing: the caller asked for the data, and a
    one-second wait is cheaper than a ``RATE_LIMITED`` round trip. The clock
    and the sleep are injectable so the tests can prove the spacing without
    spending wall time.
    """

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_SECONDS,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self.last_request_at: float | None = None
        """When the previous request went out, on the monotonic clock."""
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Block until the next request may go, then stamp it. Returns the wait."""
        import time

        # Resolved at call time, not bound as a default, so a test can swap
        # them on the instance (``docs/memory/default-args-defeat-monkeypatch.md``).
        monotonic = self._monotonic or time.monotonic
        sleep = self._sleep or time.sleep

        # Held through the sleep and the stamp: two threads sharing the pacer
        # must queue, or both read the same last_request_at and send together.
        with self._lock:
            now = monotonic()
            waited = 0.0
            if self.last_request_at is not None:
                remaining = self.last_request_at + self.min_interval - now
                if remaining > 0:
                    sleep(remaining)
                    waited = remaining
                    now = monotonic()
            self.last_request_at = now
            return waited


_SHARED_PACER: Pacer | None = None


def shared_pacer() -> Pacer:
    """The process-wide pacer every client uses unless handed its own.

    One per process rather than one per client, because the 1 request/second
    limit is the account's: two clients built in one command must not each
    believe they have the whole second.
    """
    global _SHARED_PACER
    if _SHARED_PACER is None:
        _SHARED_PACER = Pacer()
    return _SHARED_PACER


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceResponse:
    """One FantasyPros payload and where it came from."""

    endpoint: str
    """The request descriptor -- ``consensus-rankings``, never a URL."""

    payload: Mapping[str, Any]
    """The parsed JSON body, tier-checked and shape-checked."""

    fetched_at: datetime
    """When the bytes left FantasyPros: the cache entry's write time on a hit,
    now on a miss (ADR-0004, the ``fetched_at`` amendment)."""

    cached: bool

    @property
    def tier(self) -> str:
        return str(self.payload.get("tier"))

    def as_source(self) -> DataSource:
        """The ``sources[]`` entry a command puts in its envelope. Always names
        FantasyPros, which is what the API terms require (ADR-0011)."""
        return DataSource(
            name=f"{SOURCE}:{self.endpoint}", fetched_at=self.fetched_at, cached=self.cached
        )


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class FantasyProsClient:
    """Reads from FantasyPros, through the cache, one request at a time.

    ``credentials`` is a resolved :class:`~fantasy_sports.auth.chain.CredentialSet`;
    ``None`` resolves the chain on the first request. ``store`` is the shared
    HTTP cache; ``None`` opens the default one. ``http`` is the transport
    seam the tests use in place of ``requests.get`` -- called as
    ``http(url, params=..., headers=..., timeout=...)`` and expected to answer
    with ``status_code``, ``headers`` and ``content``.
    """

    def __init__(
        self,
        credentials: CredentialSet | None = None,
        *,
        season: int | None = None,
        store: CacheStore | None = None,
        mode: CacheMode = CacheMode.DEFAULT,
        http: Callable[..., Any] | None = None,
        pacer: Pacer | None = None,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._credentials = credentials
        self._now = now or utc_now
        self.season = default_season(self._now()) if season is None else season
        self._store = store
        self.mode = mode
        self._http = http
        self._pacer = pacer
        self._timeout = timeout
        self.requests_made = 0
        """Live requests this client has sent -- cache hits excluded. The
        number that counts against the 500/day budget."""

    # --- the endpoints ---------------------------------------------------- #

    def players(self, *, player_id: int | None = None) -> SourceResponse:
        """``/nfl/players?external_ids=espn``: every player with ``espn_id``.

        8,564 rows and 4 MB; there is no filter by ESPN id or name, so the
        whole table is fetched once and cached for a day, and the join (#130)
        happens locally. ``player_id`` narrows to one FantasyPros id, which is
        how ``doctor --live`` spends a one-row request on the tier.
        """
        params: dict[str, Any] = {"external_ids": "espn"}
        facets: list[tuple[str, str]] = []
        if player_id is not None:
            params["player"] = int(player_id)
            facets.append(("player", str(int(player_id))))
        return self._get(
            f"{SPORT}/players",
            params,
            endpoint="players",
            resource=Resource.FANTASYPROS_PLAYERS,
            facets=facets,
        )

    def consensus_rankings(
        self,
        *,
        week: int,
        position: str,
        scoring: str = "PPR",
        experts: bool = False,
    ) -> SourceResponse:
        """``/nfl/{season}/consensus-rankings``: one position, one scoring.

        The rich one: ``rank_ecr``, ``rank_min``, ``rank_max``, ``rank_ave``,
        ``rank_std``, ownership and opponent per player, ``total_experts`` on
        top. ``experts=True`` adds each expert's rank per player.
        """
        pos = _require_choice(position, POSITIONS, name="position")
        score = _require_choice(scoring, SCORINGS, name="scoring")
        week = _require_week(week)
        params: dict[str, Any] = {"week": week, "position": pos, "scoring": score}
        if experts:
            params["experts"] = "show"
        return self._get(
            f"{SPORT}/{self.season}/consensus-rankings",
            params,
            endpoint="consensus-rankings",
            resource=Resource.FANTASYPROS_CONSENSUS,
            week=week,
            facets=[("week", str(week)), ("position", pos), ("scoring", score)],
        )

    def rankings(self, *, week: int) -> SourceResponse:
        """``/nfl/{season}/rankings?rankstats=true&range=true``: every position
        and scoring in one 800 KB call, ``rank.ECR.{STD,PPR,...}.{POS}`` per player."""
        week = _require_week(week)
        return self._get(
            f"{SPORT}/{self.season}/rankings",
            {"week": week, "rankstats": "true", "range": "true"},
            endpoint="rankings",
            resource=Resource.FANTASYPROS_RANKINGS,
            week=week,
            facets=[("week", str(week))],
        )

    def projections(
        self,
        *,
        week: int | None = None,
        positions: Iterable[str] = (),
        players: Iterable[int] = (),
        ros: bool = False,
    ) -> SourceResponse:
        """``/nfl/{season}/projections``: ``stats.{points, points_ppr, points_half, ...}``.

        **Never sends ``scoring=``** -- FantasyPros answers 403 -- because the
        three point totals are already in ``stats``. ``ros=True`` asks for
        rest-of-season totals (``ros_projections: true`` in the body).
        """
        chosen = tuple(_require_choice(p, POSITIONS, name="positions") for p in positions)
        ids = tuple(int(p) for p in players)
        params: dict[str, Any] = {}
        facets: list[tuple[str, str]] = []
        if week is not None:
            week = _require_week(week)
            params["week"] = week
            facets.append(("week", str(week)))
        if chosen:
            params["positions"] = ":".join(chosen)
            facets.append(("positions", ":".join(chosen)))
        if ids:
            params["players"] = ":".join(str(i) for i in ids)
        if ros:
            params["ros"] = "true"
            facets.append(("ros", "true"))
        return self._get(
            f"{SPORT}/{self.season}/projections",
            params,
            endpoint="projections",
            resource=Resource.FANTASYPROS_PROJECTIONS,
            week=week,
            facets=facets,
        )

    def injuries(
        self,
        *,
        week: int,
        player_ids: Iterable[int] = (),
        team_id: str | None = None,
        include_probabilities: bool = True,
    ) -> SourceResponse:
        """``/nfl/injuries?year=&week=``: status, practice reports, probability
        of playing. Only injuries -- a player on the Commissioner's Exempt List
        is not listed, so absence here is not health."""
        week = _require_week(week)
        params: dict[str, Any] = {"year": self.season, "week": week}
        facets: list[tuple[str, str]] = [("week", str(week))]
        ids = tuple(int(p) for p in player_ids)
        if ids:
            params["player_ids"] = ":".join(str(i) for i in ids)
        if team_id:
            params["team_id"] = str(team_id).upper()
            facets.append(("team", str(team_id).upper()))
        if include_probabilities:
            params["include_probabilities"] = "true"
        return self._get(
            f"{SPORT}/injuries",
            params,
            endpoint="injuries",
            resource=Resource.FANTASYPROS_INJURIES,
            week=week,
            facets=facets,
        )

    def news(
        self,
        *,
        limit: int = 25,
        category: str | None = None,
        player_id: int | None = None,
        order_by: str | None = None,
    ) -> SourceResponse:
        """``/nfl/news``: ``title``, ``desc``, ``impact``, ``author``, ``created``."""
        params: dict[str, Any] = {"limit": int(limit)}
        facets: list[tuple[str, str]] = []
        if category:
            params["category"] = category
            facets.append(("category", category))
        if player_id is not None:
            params["fpid"] = int(player_id)
            facets.append(("player", str(int(player_id))))
        if order_by:
            params["order_by"] = order_by
        return self._get(
            f"{SPORT}/news",
            params,
            endpoint="news",
            resource=Resource.FANTASYPROS_NEWS,
            facets=facets,
        )

    def experts(self) -> SourceResponse:
        """``/nfl/{season}/rankings/experts``: the panel, with accuracy ranks."""
        return self._get(
            f"{SPORT}/{self.season}/rankings/experts",
            {},
            endpoint="experts",
            resource=Resource.FANTASYPROS_EXPERTS,
        )

    def player_points(self, *, position: str, scoring: str, start: int, end: int) -> SourceResponse:
        """``/nfl/{season}/player-points``: actual points by week."""
        pos = _require_choice(position, POSITIONS, name="position")
        score = _require_choice(scoring, SCORINGS, name="scoring")
        first, last = _require_week(start), _require_week(end)
        if last < first:
            raise ConfigInvalidError(
                f"player-points: end week {last} is before start week {first}.",
                kind="argument",
                remediation="Give --start and --end as an ascending week range.",
                details={"start": first, "end": last},
            )
        return self._get(
            f"{SPORT}/{self.season}/player-points",
            {"position": pos, "scoring": score, "start": first, "end": last},
            endpoint="player-points",
            resource=Resource.FANTASYPROS_PLAYER_POINTS,
            facets=[("position", pos), ("scoring", score), ("weeks", f"{first}-{last}")],
        )

    # --- the seam --------------------------------------------------------- #

    def _get(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        endpoint: str,
        resource: Resource,
        week: int | None = None,
        facets: Iterable[tuple[str, str]] = (),
    ) -> SourceResponse:
        """One read, through the cache. The only path a request takes."""
        url = f"{BASE_URL}/{path}"
        context = RequestContext(
            provider=SOURCE,
            resource=resource,
            season=self.season,
            week=week,
            facets=(("sport", SPORT), *facets),
        )
        fetcher = CachingFetcher(
            lambda fetch_url, fetch_params: self._live(fetch_url, fetch_params, endpoint=endpoint),
            self.store,
            mode=self.mode,
        )
        result = fetcher.fetch(url, params, context=context)
        payload = _parse(result.body, endpoint=endpoint)
        fetched_at = (
            from_epoch_seconds(result.fetched_at)
            if result.cached and result.fetched_at is not None
            else self._now()
        )
        return SourceResponse(
            endpoint=endpoint, payload=payload, fetched_at=fetched_at, cached=result.cached
        )

    @property
    def store(self) -> CacheStore:
        if self._store is None:
            self._store = CacheStore()
        return self._store

    @property
    def pacer(self) -> Pacer:
        return self._pacer if self._pacer is not None else shared_pacer()

    def api_key(self) -> str:
        """The key, revealed. Called once per live request and nowhere else.

        Resolves the chain lazily so building a client costs nothing, and so a
        cache hit never consults the Keychain at all.
        """
        if self._credentials is None:
            self._credentials = resolve_credentials(FANTASYPROS_CREDENTIALS)
        found = self._credentials.get(FANTASYPROS_API_KEY)
        if found is None:
            raise AuthMissingError(
                "No FantasyPros API key is configured.",
                remediation=(
                    f"Set {FANTASYPROS_CREDENTIALS[0].env_vars[0]} in the environment, or run "
                    "`fantasy-sports auth login` and paste the key at the FantasyPros prompt. "
                    "The key comes from your FantasyPros account's API page."
                ),
                details={"credential": FANTASYPROS_API_KEY},
            )
        return found.reveal()

    def _live(self, url: str, params: Mapping[str, Any] | None, *, endpoint: str) -> bytes:
        """One real request: paced, authenticated, classified, and validated
        **before** the fetcher can store the body."""
        key = self.api_key()
        self.pacer.wait()
        # Counted at dispatch, not on return: a request that times out after
        # it was sent may still have been served and charged against the
        # 500-a-day quota, so the budget counter must not under-report it.
        self.requests_made += 1
        response = self._send(
            url,
            params,
            {API_KEY_HEADER: key, "Accept": "application/json"},
            endpoint=endpoint,
        )
        status = int(getattr(response, "status_code", 0))

        if status == 429:
            raise RateLimitedError(
                "FantasyPros throttled this request (HTTP 429).",
                retry_after=_retry_after_seconds(_header(response, "Retry-After")),
                remediation="Wait for `details.retry_after` seconds, then retry. Premium is "
                "1 request/second and 500 requests/day.",
                details={"status": status, "endpoint": endpoint},
            )
        if status in (401, 403):
            raise AuthExpiredError(
                f"FantasyPros rejected the API key (HTTP {status}) on {endpoint}.",
                remediation=_AUTH_REMEDIATION,
                details={"kind": "auth", "status": status, "endpoint": endpoint},
            )
        body = response.content
        if 400 <= status < 500:
            # A 4xx with FantasyPros' own ``message`` is a refused argument
            # (observed as a 400 with ``parameter`` and ``valid_format``), and
            # a human has to change it. Without one it is unclassifiable.
            _raise_if_message(body, endpoint=endpoint, status=status)
        if status != 200:
            # 5xx, and anything else we cannot positively classify (R12):
            # availability, with bounded retry, never a guess at throttling.
            raise ProviderUnavailableError(
                f"FantasyPros answered HTTP {status} on {endpoint}.",
                remediation="Retry with bounded exponential backoff.",
                details={"status": status, "endpoint": endpoint},
            )

        # Validated here, on the live path, so a free-tier or bad-argument
        # answer raises before `CachingFetcher.put` can write it. Otherwise an
        # upgrade would not take effect until the entry expired.
        _parse(body, endpoint=endpoint)
        return body

    def _send(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        *,
        endpoint: str,
    ) -> Any:
        """The transport. ``requests`` is imported here and only here."""
        if self._http is not None:
            http = self._http
        else:
            import requests

            http = requests.get
        try:
            return http(url, params=params, headers=headers, timeout=self._timeout)
        except FantasySportsError:
            raise
        except Exception as exc:  # noqa: BLE001 - a transport failure is unavailability
            # Timeout, connection refused, DNS, TLS: every one is "FantasyPros
            # could not be reached", and none is anything but retryable. The
            # type name is kept; the message is not, because a transport
            # library's message can carry the URL and its query.
            raise ProviderUnavailableError(
                f"FantasyPros is unreachable ({type(exc).__name__}) on {endpoint}.",
                remediation="Check the network, then retry with bounded exponential backoff.",
                details={"cause": type(exc).__name__, "endpoint": endpoint},
            ) from exc


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #


def _parse(body: bytes | str, *, endpoint: str) -> Mapping[str, Any]:
    """The body as a dict, or the typed error that says why it is not usable.

    Order matters: the bad-argument check runs before the tier check, because
    FantasyPros' ``{"message": ...}`` error body may not carry ``tier`` at all,
    and reporting that as drift would send a human to file an issue over a
    typo in ``--position``.
    """
    try:
        # ``decode_body`` gunzips first: ``requests`` normally hands back
        # decompressed content, but the cache decodes for itself and the two
        # paths must agree on what a body is.
        payload = json.loads(decode_body(body))
    except (ValueError, UnscrubbableResponseError) as exc:
        raise SchemaDriftError(
            f"FantasyPros' {endpoint} response is not JSON.",
            path=[endpoint],
            provider=SOURCE,
            details={"endpoint": endpoint},
        ) from exc
    if not isinstance(payload, dict):
        raise SchemaDriftError(
            f"FantasyPros' {endpoint} response is not a JSON object.",
            path=[endpoint],
            provider=SOURCE,
            details={"endpoint": endpoint},
        )

    list_key = _LIST_KEYS[endpoint]
    if isinstance(payload.get("message"), str) and list_key not in payload:
        # A 200 that is an error (the shape captured on 2026-10-16).
        raise _argument_error(payload, endpoint=endpoint, status=200)

    tier = payload.get("tier")
    if tier is None:
        raise SchemaDriftError(
            f"FantasyPros' {endpoint} response carries no `tier`; every response used to.",
            path=[endpoint, "tier"],
            provider=SOURCE,
            details={"endpoint": endpoint},
        )
    if tier != PREMIUM_TIER or "limit" in payload:
        limit = payload.get("limit")
        raise ConfigInvalidError(
            f"FantasyPros answered {endpoint} on the {tier!r} tier"
            + (f" with lists truncated to {limit} rows" if limit is not None else "")
            + "; refusing to return a partial pool as if it were complete.",
            kind="tier",
            remediation=_TIER_REMEDIATION,
            details={"endpoint": endpoint, "tier": tier, "limit": limit},
        )

    if not isinstance(payload.get(list_key), list):
        raise SchemaDriftError(
            f"FantasyPros' {endpoint} response has no `{list_key}` list.",
            path=[endpoint, list_key],
            provider=SOURCE,
            details={"endpoint": endpoint},
        )
    return payload


def _raise_if_message(body: bytes | str, *, endpoint: str, status: int) -> None:
    """Raise ``CONFIG_INVALID`` if a 4xx body carries FantasyPros' ``message``.

    Anything that is not a JSON object with a string ``message`` returns, and
    the caller lands the status on availability. A body we cannot read is not
    evidence of anything.
    """
    try:
        payload = json.loads(decode_body(body))
    except (ValueError, UnscrubbableResponseError):
        return
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        raise _argument_error(payload, endpoint=endpoint, status=status)


def _argument_error(
    payload: Mapping[str, Any], *, endpoint: str, status: int
) -> ConfigInvalidError:
    """FantasyPros refused an argument. Its message is what a human acts on, so
    it travels verbatim (scrubbed by the base class like every other string);
    ``parameter`` and ``valid_format`` ride along when the body names them."""
    message = str(payload["message"])
    details: dict[str, Any] = {"endpoint": endpoint, "status": status, "message": message}
    for key in ("parameter", "valid_format"):
        if isinstance(payload.get(key), str):
            details[key] = payload[key]
    return ConfigInvalidError(
        f"FantasyPros rejected the request on {endpoint}: {message}",
        kind="argument",
        remediation=(
            f"Change the argument FantasyPros refused ({message!r}). Positions are "
            f"{', '.join(POSITIONS)}; scorings are {', '.join(SCORINGS)}."
        ),
        details=details,
    )


def _require_choice(value: str, choices: tuple[str, ...], *, name: str) -> str:
    """``value`` upper-cased, if FantasyPros accepts it; ``CONFIG_INVALID`` if not.

    Refused here rather than sent, because FantasyPros answers a bad position
    with a 200 and an error message, and a request that can never succeed
    should not spend one of the day's 500.
    """
    cleaned = str(value).strip().upper()
    if cleaned not in choices:
        raise ConfigInvalidError(
            f"FantasyPros does not recognise {name} {value!r}.",
            kind="argument",
            remediation=f"Use one of: {', '.join(choices)}.",
            details={name: value, "choices": list(choices)},
        )
    return cleaned


def _require_week(week: Any) -> int:
    """A positive integer week, refusing ``bool`` (``isinstance(True, int)``)."""
    if isinstance(week, bool) or not isinstance(week, int) or week < 1:
        raise ConfigInvalidError(
            f"week must be a positive integer, got {week!r}.",
            kind="argument",
            remediation="Pass the NFL week as a whole number, 1 or more.",
            details={"week": repr(week)},
        )
    return week


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
    except AttributeError:  # pragma: no cover - a plain mapping always has .get
        return None
    if value is None:
        value = next((v for k, v in headers.items() if str(k).lower() == name.lower()), None)
    return None if value is None else str(value)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
