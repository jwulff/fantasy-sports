"""The ESPN adapter — the one provider that ships in v0.1.

It wraps ``cwendt94/espn-api`` rather than re-deriving ESPN's undocumented
payload shapes: the library's ``PLAYER_STATS_MAP``, its per-week ``proTeamId``
resolution for traded players, and its standings tiebreaker cascade are years of
reverse engineering we would otherwise repeat badly. What the library does
**not** do is everything this module exists for.

The five things this module adds on top of ``espn-api``
-------------------------------------------------------

**1. A transport seam under the library** (:class:`_Transport`). ``espn-api``
calls ``requests.get`` directly and hands the status code to
``checkRequestStatus``, which special-cases 401 and 404 and folds *everything
else* — 429 included — into one ``ESPNUnknownError`` carrying the status as
English. So ``RATE_LIMITED`` and its ``retry_after`` are ours to write, and they
have to be written *before* the library sees the status, because ``Retry-After``
is a header and the header does not survive into the exception. The same seam is
where the HTTP cache attaches (ARCHITECTURE §14.4): ``box_scores()`` and
``free_agents()`` fan out into two or three sequential requests with no internal
dedup, so a cache above them pays every round trip on a miss.

**2. Schema-drift detection.** ``espn-api`` offers nothing here. Every model
constructor does unguarded dict access — ``data['record']['overall']['wins']`` —
so a renamed field surfaces as a bare ``KeyError`` from six frames down, and a
``KeyError`` is indistinguishable from a bug in our own call. :func:`_mapped`
catches those, walks the traceback for the deepest ``espn_api`` frame, and
raises :class:`~fantasy_sports.core.errors.SchemaDriftError` carrying the view,
that frame, and the missing key as ``details.path``. That path is what makes the
canary's auto-filed issue actionable instead of a stack trace.

**3. Honest 401 classification.** A bare 401 from ESPN is ambiguous between
expired cookies and the wrong current-vs-historical URL shape for the season
(ARCHITECTURE §14 item 1). The double-probe that resolves it is **library-owned**
— ``checkRequestStatus`` swaps ``/leagueHistory/`` for ``/seasons/`` and retries
before raising — so our code never observes a bare 401 and re-implementing the
probe would double the request cost against a provider whose throttle behaviour
is unconfirmed. It is also **mandatory rather than a fallback**: ESPN's 401 body
does carry a machine-readable ``details[].type``, and on this path that type is a
*constant* — ``AUTH_LEAGUE_NOT_VISIBLE`` comes back identically for no cookies,
a bad ``espn_s2``, and a league the account was never in. The type is read
anyway, before the retry runs, because it varies elsewhere and is cheap. What we
add is the classification above it, which never reports expiry from evidence
that proves nothing. See :data:`AUTH_REASONS`.

**4. Timezone correctness.** ``espn-api`` builds every datetime with
``datetime.fromtimestamp()`` and no ``tz=``, producing naive, host-local values:
the same league renders a different kickoff in Seattle and on a UTC CI runner.
Nothing in this module passes one of those through. Kickoff times are re-derived
from the raw epoch milliseconds in the ``proTeamSchedules_wl`` payload and
transaction timestamps from the raw ``processDate``/``proposedDate``, both via
:func:`~fantasy_sports.output.envelope.from_epoch_millis`. The output layer
refuses a naive datetime with ``NaiveDatetimeError``, so a regression here fails
loudly rather than silently shifting every timestamp.

**5. Transaction reconciliation across ESPN's two surfaces.** ``mTransactions2``
and the ``kona_league_communication`` activity feed carry non-overlapping
vocabularies, and the activity feed synthesises *two* rows per trade
(``TRADE_SENT`` and ``TRADE_RECEIVED``). Building ``transactions`` on one surface
silently drops whatever routes only through the other, and
``core.Transaction`` must never inherit that asymmetry (research §6 item 2).
:meth:`EspnProvider.fetch_transactions` reads both and merges them.

What ``raw`` carries, and where the whole response went
------------------------------------------------------

Each normalized object's ``raw`` is **the provider's own sub-object** — the slice
of the response that describes that thing — not the whole payload. A league
bootstrap is 800 KB, of which 730 KB is rosters; putting all of it on every
``Team`` would make ``fantasy-sports teams`` unreadable and unusable.

Nothing is lost, because R1 is satisfied a level up: every upstream response that
contributed to a call is retained verbatim, keyed by the request that produced
it, on :attr:`EspnProvider.last_fetch`. That is also where the envelope's
``sources`` come from, and it is why composite reads report each contributing
fetch separately instead of collapsing to one.

Constraints this module lives under
-----------------------------------

``espn_api`` and ``requests`` are imported **inside functions**, never at module
scope: ADR-0008 budgets ``--help`` at 50 ms and ``tests/unit/test_imports.py``
asserts both are absent from ``sys.modules`` on the cheap paths. Nothing here
may import ``typer``, ``click``, or ``rich`` (an AST test enforces it), and
nothing here may log, format, or record a credential — library exception text is
run through :func:`~fantasy_sports.core.redaction.scrub_credential_patterns`
before it reaches an error message, because ``espn-api`` interpolated the raw
``espn_s2`` value into its own access-denied message until 2026-02.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fantasy_sports.cache.tags import TTL_SECONDS, RequestContext, Resource
from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    FantasySportsError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.models import (
    CredentialSpec,
    FreeAgent,
    League,
    Matchup,
    Player,
    RosterSlot,
    Team,
    Transaction,
)
from fantasy_sports.core.redaction import remember_secret, scrub_credential_patterns
from fantasy_sports.output.envelope import DataSource, from_epoch_millis, utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.cache.store import CacheMode, CacheStore

__all__ = [
    "AUTH_REASONS",
    "PROVIDER",
    "STARTER_EXCLUDED_SLOTS",
    "EspnProvider",
    "FetchRecord",
    "RawResponse",
]

PROVIDER: Final[str] = "espn"

STARTER_EXCLUDED_SLOTS: Final[frozenset[str]] = frozenset({"BE", "IR", ""})
"""Lineup slots that are not a starting spot.

ESPN has no ``is_starter`` field; every provider makes you derive it. ``BE`` is
the bench, ``IR`` is injured reserve, and the empty string is what
``POSITION_MAP`` yields for slot 22, which ESPN uses as a filler.
"""

_ALL_TRANSACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "FREEAGENT",
        "WAIVER",
        "WAIVER_ERROR",
        "TRADE_ACCEPT",
        "TRADE_UPHOLD",
        "ROSTER",
        "DRAFT",
    }
)
"""The ``mTransactions2`` filter we send.

Broader than ``espn-api``'s default of waivers and free agents, because trades
live under ``TRADE_ACCEPT`` and a transactions command that cannot show a trade
is not a transactions command. ``TRADE_PROPOSAL``, ``TRADE_DECLINE``,
``TRADE_VETO``, and ``TRADE_ERROR`` are deliberately absent: they are proposals
and failures, not roster moves, and ``core.Transaction``'s four-value vocabulary
has no honest home for them (they remain readable through :meth:`fetch_raw`).
"""

_TRANSACTION_TYPE_MAP: Final[dict[str, str]] = {
    "WAIVER": "waiver_claim",
    "TRADE_ACCEPT": "trade",
    "TRADE_UPHOLD": "trade",
}
"""ESPN's transaction ``type`` to our four-value vocabulary.

``FREEAGENT`` is absent on purpose: it is ``add`` or ``drop`` depending on what
its items say, and deciding that needs the items rather than the type.
"""

_ACTIVITY_TYPE_MAP: Final[dict[str, str]] = {
    "FA ADDED": "add",
    "WAIVER ADDED": "waiver_claim",
    "DROPPED": "drop",
    "TRADE_SENT": "trade",
    "TRADE_RECEIVED": "trade",
}
"""The activity feed's *separate* vocabulary. This is the asymmetry research §6
item 2 warns about: neither surface's strings appear in the other's."""


@dataclass(frozen=True)
class AuthReason:
    """What one ESPN 401 ``details[].type`` value actually proves."""

    explanation: str
    proves_credentials_bad: bool = False
    """Whether this reason is positive evidence that the *credential* failed.

    ``False`` everywhere so far, and that is the finding, not an oversight.
    """


AUTH_REASONS: Final[dict[str, AuthReason]] = {
    "AUTH_LEAGUE_NOT_VISIBLE": AuthReason(
        explanation=(
            "ESPN says this league is not visible to whatever was sent. Probed "
            "against a real private league on 2026-09-05, that response is "
            "byte-identical for no cookies, a valid SWID with an invalid espn_s2, "
            "and either cookie alone -- so it distinguishes nothing."
        ),
        proves_credentials_bad=False,
    ),
}
"""Observed ``details[].type`` values in ESPN's 401 body, and what each proves.

ESPN's 401 body is not bare -- it carries ``details[].type``, a machine-readable
discriminator -- and reading it costs nothing. But on the league-read path it is
a **constant**: probing a real private league on 2026-09-05, one variable at a
time, ``AUTH_LEAGUE_NOT_VISIBLE`` came back identically for no cookies at all, a
valid ``SWID`` with an invalid ``espn_s2``, and each cookie sent alone. ESPN does
not distinguish "your cookie is bad" from "this league is not yours" from "you
sent nothing".

So this table exists to be *read*, not to be trusted as evidence about the
credential, and no entry in it maps to ``AUTH_EXPIRED``. Telling a user to
re-extract cookies that were fine is precisely the misdiagnosis ARCHITECTURE
§14 item 1 exists to prevent, and it is the reason the alternate-URL-shape
double-probe stays mandatory rather than becoming a fallback.

The honest asymmetry, and what this module encodes: *nothing was sent* is
cheaply distinguishable and reports ``AUTH_MISSING``; *expired* versus
*malformed* versus *not your league* is not distinguishable from this response
and reports ``LEAGUE_NOT_FOUND``, whose ``agent_action`` -- confirm the id and
the access -- is right for all three. ``AUTH_EXPIRED`` is reachable only from a
reason type that positively proves the credential failed, and no such type has
been observed yet. Add one here the first time ESPN produces it.

See ``docs/memory/espn-401-tells-you-nothing.md``, which also records the
``fan.api.espn.com`` membership probe: it answers "is this league even mine?",
but it answers ``200`` with no cookies at all, so it is not a credential check.
"""

_VIEW_RESOURCES: Final[dict[str, Resource]] = {
    "mTeam": Resource.LEAGUE_SETTINGS,
    "mSettings": Resource.LEAGUE_SETTINGS,
    "mDraftDetail": Resource.LEAGUE_SETTINGS,
    "mRoster": Resource.ROSTER,
    "mMatchup": Resource.MATCHUPS,
    "mMatchupScore": Resource.MATCHUPS,
    "mScoreboard": Resource.MATCHUPS,
    "mStandings": Resource.STANDINGS,
    "mTransactions2": Resource.TRANSACTIONS,
    "kona_league_communication": Resource.TRANSACTIONS,
    "kona_player_info": Resource.FREE_AGENTS,
    "mPositionalRatings": Resource.FREE_AGENTS,
    "players_wl": Resource.PLAYERS,
    "proTeamSchedules_wl": Resource.PRO_TEAM_SCHEDULES,
}
"""ESPN view name to cache resource class.

The cache never inspects a URL to guess this — deriving it from a URL shape
would put ESPN's routing conventions inside a provider-agnostic layer
(``cache/tags.py``). ``players_wl`` and ``proTeamSchedules_wl`` map to the
season-scoped class: they come from a season endpoint carrying no league id and
are re-fetched by every league, so tagging them league-scoped would either evict
one league's copy on a write to another or lose the cross-league hit entirely.
"""

_SEASON_SCOPED_VIEWS: Final[frozenset[str]] = frozenset({"players_wl", "proTeamSchedules_wl"})


# --------------------------------------------------------------------------- #
# What one call fetched
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawResponse:
    """One upstream ESPN response, keyed by the request that produced it."""

    view: str
    """The ``view=`` value, or ``+``-joined values for a combined request. This
    is the request *descriptor*, never a URL: a URL can carry an ``espn_s2``
    query parameter and this name reaches stdout through the envelope."""

    payload: Any
    fetched_at: datetime
    cached: bool = False

    def as_source(self) -> DataSource:
        return DataSource(name=self.view, fetched_at=self.fetched_at, cached=self.cached)


@dataclass(frozen=True)
class FetchRecord:
    """Every upstream response that contributed to one read (origin R1).

    A composite read fans out — a box score is three requests, free agents is
    three — and a single payload would drop data. Keyed by request descriptor so
    a caller can tell which view a field came from, and so the envelope can
    report each contributing fetch's own age rather than one blended number.
    """

    responses: Mapping[str, RawResponse]

    @property
    def sources(self) -> tuple[DataSource, ...]:
        """The envelope's ``sources``, oldest first."""
        return tuple(
            response.as_source()
            for response in sorted(self.responses.values(), key=lambda r: r.fetched_at)
        )

    def payload(self, view: str) -> Any:
        """The raw payload recorded for ``view``, or ``None``.

        Accepts a bare view name as well as a full key, so a caller that does
        not care which scoring period a repeated view was fetched for does not
        have to know the key shape. The most recent fetch wins.
        """
        found = self.responses.get(view) or _latest(self.responses, view)
        return None if found is None else found.payload


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _clean(text: object) -> str:
    """Library text with credential *shapes* removed.

    Two mechanisms, both needed. ``FantasySportsError`` scrubs values this
    process was handed, which covers our own cookies; this covers the shapes we
    were never handed. ``espn-api`` interpolated ``self.cookies['espn_s2']``
    into its access-denied message until commit ``78c239a`` (2026-02-15), so
    passing library text through verbatim is a known leak path, not a
    hypothetical one.
    """
    return scrub_credential_patterns(str(text))


def _espn_frame(exc: BaseException) -> str | None:
    """The deepest ``espn_api`` frame in ``exc``'s traceback, as ``module.func``.

    This is the context capture that makes ``SCHEMA_DRIFT`` actionable. A bare
    ``KeyError('record')`` says a key is missing; ``espn_api.football.team.__init__``
    plus that key says *ESPN changed the team payload*, which is a filable
    issue rather than a stack trace.
    """
    frame: str | None = None
    traceback = exc.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        if module.startswith("espn_api"):
            frame = f"{module}.{traceback.tb_frame.f_code.co_name}"
        traceback = traceback.tb_next
    return frame


def _missing_key(exc: BaseException) -> str | None:
    """The key a ``KeyError`` names, when it is safe to report.

    A ``KeyError``'s argument is normally a field name, which is exactly what
    ``details.path`` is for. It can also be an *id* — ``player_map[playerId]``
    raises with an integer — and an id is provider data rather than a field
    name, so a non-string key is reported by type only. Nothing that could
    carry a credential ever reaches the payload.
    """
    if not isinstance(exc, KeyError) or not exc.args:
        return None
    key = exc.args[0]
    if isinstance(key, str):
        return key
    return f"<{type(key).__name__}>"


def _retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` as seconds. Accepts the delta form and the HTTP-date form.

    Returns ``None`` for anything unparseable rather than guessing: an invented
    backoff is worse than none, because a caller will trust it.
    """
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - utc_now()).total_seconds())


# --------------------------------------------------------------------------- #
# The transport seam
# --------------------------------------------------------------------------- #


class _Transport:
    """``espn-api``'s HTTP calls, routed through us.

    Installed by replacing ``league_get`` and ``get`` on one
    ``EspnFantasyRequests`` *instance* — not by patching the class or the
    ``requests`` module, both of which would leak into every other league object
    in the process and into any other library sharing ``requests``.

    Three things happen here that cannot happen anywhere else:

    * **429 is intercepted before the library's status check.** ``espn-api``
      folds every non-200/401/404 into ``ESPNUnknownError("ESPN returned an
      HTTP 429")`` and never reads ``Retry-After``. The header exists only on
      the response object, which the exception does not carry, so this is the
      only place ``retry_after`` can be obtained.
    * **A 401 body is read before the library retries.** ``checkRequestStatus``
      swaps the URL shape and retries — correctly, and we let it — but it does
      not surface ESPN's ``details[].type``. It is captured here first.
    * **The cache attaches below the composite calls**, keyed on URL plus params
      plus the ``x-fantasy-filter`` header, which is a dimension that changes
      the response and does not appear in the URL.
    """

    def __init__(
        self,
        espn_request: Any,
        *,
        season: int,
        league_id: str,
        store: CacheStore | None = None,
        mode: CacheMode | None = None,
        http: Callable[..., Any] | None = None,
        now: Callable[[], datetime] = utc_now,
        on_auth_reason: Callable[[str | None], None] | None = None,
    ) -> None:
        self._request = espn_request
        self._season = season
        self._league_id = league_id
        self._store = store
        self._mode = mode
        self._http = http
        self._now = now
        self._on_auth_reason = on_auth_reason
        self.responses: dict[str, RawResponse] = {}
        self.completed_through: int | None = None
        """Scoring periods at or below this are finished and cache forever."""

        # Instance-level replacement, so nothing outside this league is touched.
        espn_request.league_get = self.league_get
        espn_request.get = self.get

    # --- the two methods espn-api calls ----------------------------------- #

    def league_get(
        self,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        extend: str = "",
    ) -> Any:
        payload = self._fetch(
            self._request.LEAGUE_ENDPOINT + extend,
            params,
            headers,
            extend=extend,
            league_scoped=True,
        )
        # The pre-2018 ``/leagueHistory/`` shape answers with a JSON *list*.
        # ``espn-api`` unwraps it here and every caller downstream assumes the
        # unwrap has happened.
        return payload[0] if isinstance(payload, list) else payload

    def get(
        self,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        extend: str = "",
    ) -> Any:
        # The season endpoint. Not unwrapped: ``players_wl`` legitimately
        # answers with a list and ``_fetch_players`` iterates it.
        return self._fetch(
            self._request.ENDPOINT + extend,
            params,
            headers,
            extend="",
            league_scoped=False,
        )

    # --- the seam --------------------------------------------------------- #

    def _fetch(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        league_scoped: bool,
    ) -> Any:
        view = _view_of(params)
        body, cached = self._body(url, params, headers, extend=extend, view=view)
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise SchemaDriftError(
                f"ESPN's {view} response is not JSON.",
                path=[view],
                provider=PROVIDER,
                details={"view": view},
            ) from exc
        self.responses[_record_key(view, params)] = RawResponse(
            view=view,
            payload=payload,
            fetched_at=self._now(),
            cached=cached,
        )
        return payload

    def _body(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        view: str,
    ) -> tuple[str, bool]:
        """The response body, from the cache where one is configured."""
        if self._store is None:
            return _as_text(self._live(url, params, headers, extend=extend, view=view)), False

        from fantasy_sports.cache.store import CachingFetcher

        fetcher = CachingFetcher(
            lambda fetch_url, fetch_params: self._live(
                fetch_url, fetch_params, headers, extend=extend, view=view
            ),
            self._store,
            **({} if self._mode is None else {"mode": self._mode}),
        )
        result = fetcher.fetch(
            url,
            params,
            context=self._context(view, params),
            extra=_filter_dimension(headers),
        )
        return result.body, result.cached

    def _context(self, view: str, params: Mapping[str, Any] | None) -> RequestContext:
        """What the cache needs to know about this request.

        A combined request takes the **shortest** TTL of its views. The league
        bootstrap sends ``mSettings`` (a day) alongside ``mRoster`` (five
        minutes) in one call, and serving a day-old roster because the request
        also happened to carry settings is the expensive direction to be wrong
        in.
        """
        views = view.split("+")
        resource = min(
            (_VIEW_RESOURCES.get(name, Resource.UNKNOWN) for name in views),
            key=lambda item: TTL_SECONDS[item],
            default=Resource.UNKNOWN,
        )
        week = _as_int(None if params is None else params.get("scoringPeriodId"))
        season_scoped = resource in (Resource.PLAYERS, Resource.PRO_TEAM_SCHEDULES)
        return RequestContext(
            provider=PROVIDER,
            resource=resource,
            season=self._season,
            league_id=None if season_scoped else self._league_id,
            week=week,
            completed=(
                week is not None
                and self.completed_through is not None
                and week <= self.completed_through
            ),
        )

    def _live(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        view: str,
    ) -> Any:
        """One real HTTP request, with the two things ``espn-api`` does not do."""
        response = self._send(url, params, headers)
        status = response.status_code

        if status == 429:
            # Before ``checkRequestStatus``, which would collapse this into a
            # generic ``ESPNUnknownError`` and discard the header.
            raise RateLimitedError(
                "ESPN throttled this request (HTTP 429).",
                retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
                remediation="Wait for `details.retry_after` seconds, then retry.",
                details={"status": status, "view": view},
            )

        if status == 401:
            # Captured before the library's alternate-shape retry, which does
            # not surface the body. If the retry succeeds this is discarded.
            self._report_auth_reason(_auth_reason(response))

        alternate = self._request.checkRequestStatus(
            status, extend=extend, params=params, headers=headers
        )
        if alternate is not None:
            # The alternate URL shape worked: this was never an auth failure,
            # it was the current-vs-historical season shape. This is the whole
            # reason the double-probe is mandatory rather than a fallback --
            # the 401 body says the same thing either way.
            self._report_auth_reason(None)
            return json.dumps(alternate)
        return response.content

    def _report_auth_reason(self, reason: str | None) -> None:
        if self._on_auth_reason is not None:
            self._on_auth_reason(reason)

    def _send(self, url: str, params: Mapping[str, Any] | None, headers: Mapping[str, str] | None):
        if self._http is not None:
            return self._http(url, params=params, headers=headers, cookies=self._request.cookies)
        import requests

        return requests.get(url, params=params, headers=headers, cookies=self._request.cookies)


def _as_text(body: Any) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return str(body)


def _as_int(value: Any) -> int | None:
    """``value`` as an ``int``, refusing ``bool``.

    ``isinstance(True, int)`` is ``True``, and a ``week=True`` that becomes
    ``1`` is a wrong answer that looks like a right one.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _view_of(params: Mapping[str, Any] | None) -> str:
    """The request descriptor: the ``view=`` value, or ``+``-joined values."""
    if not params or "view" not in params:
        return "unknown"
    view = params["view"]
    if isinstance(view, str):
        return view
    return "+".join(str(item) for item in view)


def _record_key(view: str, params: Mapping[str, Any] | None) -> str:
    """Where one response is filed on :class:`FetchRecord`.

    The view name alone is not enough. ``fetch_transactions(since=...)`` asks
    ``mTransactions2`` about every scoring period in the season, and filing
    seventeen distinct responses under one key keeps the last one and silently
    drops sixteen -- which is exactly the data loss R1 exists to prevent. The
    scoring period is the only parameter that varies for a repeated view, so it
    is the only one in the key; ``RawResponse.view`` keeps the bare name for
    lookups that do not care.
    """
    period = None if params is None else _as_int(params.get("scoringPeriodId"))
    return view if period is None else f"{view}@{period}"


def _latest(responses: Mapping[str, RawResponse], view: str) -> RawResponse | None:
    """The most recently fetched response for ``view``, whatever its key.

    Later wins, because a repeated view is a repeated *fetch*: after
    ``load_roster_week(3)`` the week-3 ``mRoster`` is the one the object model
    is actually holding.
    """
    found = [item for item in responses.values() if item.view == view]
    return max(found, key=lambda item: item.fetched_at) if found else None


def _filter_dimension(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """The ``x-fantasy-filter`` header, as a cache-key dimension.

    Free agents, transactions, the activity feed and the message board are all
    scoped by this header rather than by the URL. Two filters against one URL
    are two different payloads, so omitting it would serve the wrong body; the
    rest of the header set is excluded because it carries the ``Cookie``.
    """
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() == "x-fantasy-filter":
            return {"x-fantasy-filter": value}
    return None


def _auth_reason(response: Any) -> str | None:
    """ESPN's machine-readable 401 reason, from ``details[].type``.

    Read before the library's alternate-shape probe, which does not surface the
    body. Returns ``None`` when the body is not the documented shape — an
    unreadable body is not evidence of anything, and guessing from a 401 status
    alone is the misdiagnosis the double-probe exists to avoid.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unparseable 401 body is simply no evidence
        return None
    if not isinstance(body, Mapping):
        return None
    details = body.get("details")
    if not isinstance(details, Sequence) or isinstance(details, str | bytes):
        return None
    for detail in details:
        if isinstance(detail, Mapping) and isinstance(detail.get("type"), str):
            return detail["type"]
    return None


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #


class EspnProvider:
    """The ESPN adapter. Satisfies :class:`~fantasy_sports.providers.base.Provider`.

    ``isinstance(EspnProvider(), Provider)`` passes, and proves only that the
    methods *exist* — a ``runtime_checkable`` Protocol never checks a signature
    or a return type (``docs/memory/runtime-checkable-proves-less-than-it-looks.md``).
    The conformance test that matters calls every method against cassettes and
    asserts on what comes back.

    One instance memoises one ``espn-api`` ``League`` per ``(league_id,
    season)``. Constructing one costs four ESPN requests — the five-view
    bootstrap, the season player map, the pro schedule, and the draft — so a
    command that reads teams and then standings pays for them once. Pass a
    :class:`~fantasy_sports.cache.store.CacheStore` to make that hold across
    processes as well.
    """

    name = PROVIDER

    #: Position strings ESPN's free-agent filter understands. Anything else is
    #: silently ignored by ESPN — it answers with its *default* player set
    #: rather than an error — so an unknown value is refused here instead.
    POSITIONS: Final[tuple[str, ...]] = (
        "QB",
        "RB",
        "WR",
        "TE",
        "D/ST",
        "K",
        "FLEX",
        "DT",
        "DE",
        "LB",
        "DL",
        "CB",
        "S",
        "DB",
        "DP",
        "HC",
    )

    def __init__(
        self,
        credentials: Mapping[str, Any] | None = None,
        *,
        cache: CacheStore | None = None,
        cache_mode: CacheMode | None = None,
        http: Callable[..., Any] | None = None,
        now: Callable[[], datetime] = utc_now,
        free_agent_limit: int = 50,
    ) -> None:
        self._credentials = _revealed(credentials)
        for value in self._credentials.values():
            # Register with the scrubber so that if `espn-api` interpolates a
            # cookie into an exception message, `FantasySportsError` blanks it
            # at construction. Values arriving through the auth chain are
            # already registered; a caller passing plain strings is not.
            remember_secret(value)
        self._cache = cache
        self._cache_mode = cache_mode
        self._http = http
        self._now = now
        self._free_agent_limit = free_agent_limit
        self._leagues: dict[tuple[str, int], tuple[Any, _Transport]] = {}
        self._auth_reason: str | None = None
        self.last_fetch: FetchRecord | None = None
        """Every upstream response the most recent read used (origin R1)."""

    # --- auth -------------------------------------------------------------- #

    def credential_specs(self) -> list[CredentialSpec]:
        """ESPN's two manually-extracted cookies.

        ``staleness`` describes how expiry becomes *visible*, not a predicted
        lifetime. No ESPN documentation, library source, or community post
        states a concrete cookie lifetime, so a fabricated one would either cry
        wolf on a live cookie or stay silent past a dead one.
        """
        from fantasy_sports.auth.chain import ESPN_CREDENTIALS

        staleness = (
            "Invisible until a read fails. ESPN answers 401 identically for an "
            "expired cookie, a malformed one, and a league the account was never "
            "in, and publishes no cookie lifetime -- so age is reported and "
            "expiry is never predicted."
        )
        return [replace(spec, staleness=staleness) for spec in ESPN_CREDENTIALS]

    @property
    def has_credentials(self) -> bool:
        """Whether a *complete* cookie pair was configured.

        Both or neither: ``espn-api`` sends cookies only when it has both, so
        one cookie on its own reaches ESPN as an unauthenticated request and
        must be reported as missing rather than as rejected.
        """
        return bool(self._credentials.get("espn_s2") and self._credentials.get("swid"))

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        """Which of the two cookies is absent. Names only, never values."""
        return tuple(name for name in ("espn_s2", "swid") if not self._credentials.get(name))

    def _note_auth_reason(self, reason: str | None) -> None:
        """Record ESPN's typed 401 reason for the classifier. Never a value."""
        self._auth_reason = reason

    # --- reads ------------------------------------------------------------- #

    def fetch_league(self, league_id: str, season: int) -> League:
        """The league itself, including its roster-slot configuration (R3a)."""
        with self._read(league_id, season, "fetch_league") as league:
            data = self._bootstrap_payload(league_id, season)
            return League(
                provider=PROVIDER,
                provider_id=str(league_id),
                name=league.settings.name,
                season=int(season),
                sport="nfl",
                team_count=len(league.teams),
                current_week=int(league.current_week),
                roster_slots=_roster_slots(data),
                raw=_league_raw(data),
            )

    def fetch_teams(self, league_id: str, season: int) -> list[Team]:
        """Every team in the league, unordered (ESPN returns them by team id)."""
        with self._read(league_id, season, "fetch_teams") as league:
            data = self._bootstrap_payload(league_id, season)
            by_id = _teams_by_id(data)
            return [self._team(team, by_id.get(team.team_id, {})) for team in league.teams]

    def fetch_standings(self, league_id: str, season: int) -> list[Team]:
        """Teams in ESPN's own rank order.

        The tiebreaker source lives **here**, not in ``core/`` (ARCHITECTURE
        §14 item 15). ESPN's API returns no sorted standings; ``espn-api``
        computes the order from ``playoffSeed`` and ``rankCalculatedFinal``,
        which are the values ESPN's own site displays, and it also ships a full
        local tiebreaker cascade in ``standings_weekly``. Copying either into
        ``core/`` as "the" standings algorithm would build logic Yahoo — whose
        ranking *is* server-side — would then disagree with.

        ``standing`` on each returned team is this adapter's own 1-based
        position in that order, not ESPN's raw seed field, so it is meaningful
        even in a league where the seed is unset.
        """
        with self._read(league_id, season, "fetch_standings") as league:
            data = self._bootstrap_payload(league_id, season)
            by_id = _teams_by_id(data)
            ordered = league.standings()
            return [
                self._team(team, by_id.get(team.team_id, {}), standing=rank)
                for rank, team in enumerate(ordered, start=1)
            ]

    def fetch_roster(
        self, league_id: str, season: int, team_id: str, week: int | None = None
    ) -> list[RosterSlot]:
        """A team's roster. ``week=None`` is the current roster (R3).

        Every slot carries its lineup slot, the player's eligible slots, the
        kickoff of that player's game, and whether the slot is still
        changeable. Kickoff is re-derived from the raw epoch milliseconds in
        ``proTeamSchedules_wl`` — never from ``espn-api``'s ``Player.schedule``,
        whose datetimes are naive and host-local.
        """
        with self._read(league_id, season, "fetch_roster") as league:
            wanted = _as_int(team_id)
            if wanted is None:
                raise LeagueNotFoundError(
                    f"ESPN team ids are numeric; got {team_id!r}.",
                    remediation="Run `fantasy-sports teams` to list the ids in this league.",
                    details={"team_id": str(team_id)},
                )
            scoring_period = _as_int(week)
            if scoring_period is not None:
                league.load_roster_week(scoring_period)
            else:
                scoring_period = _as_int(league.current_week)

            team = next((item for item in league.teams if item.team_id == wanted), None)
            if team is None:
                raise LeagueNotFoundError(
                    f"No team {wanted} in ESPN league {league_id} for {season}.",
                    remediation="Run `fantasy-sports teams` to list the ids in this league.",
                    details={"team_id": str(wanted), "league_id": str(league_id)},
                )

            entries = _roster_entries(self._transport(league_id, season), wanted, scoring_period)
            kickoffs = _kickoff_map(self._transport(league_id, season).responses)
            now = self._now()
            return [
                _roster_slot(
                    player, entries.get(player.playerId, {}), kickoffs, scoring_period, now
                )
                for player in team.roster
            ]

    def fetch_matchups(self, league_id: str, season: int, week: int) -> list[Matchup]:
        """Head-to-head pairings for the scoring period ``week``.

        ``week`` is a **scoring period** — the NFL week, the number a human
        means. ESPN's schedule is indexed by *matchup* period, and the two
        diverge exactly when a playoff round spans two NFL weeks, which is
        league-configurable rather than a calendar fact. ``espn-api``'s
        ``scoreboard(week)`` filters on ``matchupPeriodId`` and would silently
        return the wrong round, so the scoring period is resolved through
        ``settings.matchup_periods`` first. Both identifiers survive onto the
        returned object, and the untouched provider values stay in ``raw``.
        """
        with self._read(league_id, season, "fetch_matchups") as league:
            scoring_period = _as_int(week)
            if scoring_period is None:
                raise LeagueNotFoundError(
                    f"A week must be an integer scoring period; got {week!r}.",
                    details={"week": str(week)},
                )
            matchup_period = _matchup_period_for(league, scoring_period)
            transport = self._transport(league_id, season)
            transport.completed_through = max(_as_int(league.current_week) or 1, 1) - 1
            found = league.scoreboard(matchup_period)
            raw_entries = _schedule_entries(transport.responses, matchup_period)
            return [
                _matchup(
                    item,
                    raw_entries,
                    scoring_period=scoring_period,
                    matchup_period=matchup_period,
                    index=index,
                )
                for index, item in enumerate(found)
            ]

    def fetch_transactions(
        self,
        league_id: str,
        season: int,
        since: datetime | None = None,
        *,
        scoring_period: int | None = None,
    ) -> list[Transaction]:
        """Roster moves from **both** of ESPN's transaction surfaces.

        ``mTransactions2`` and the ``kona_league_communication`` activity feed
        carry non-overlapping vocabularies, and the feed synthesises two rows
        per trade. Reading one surface silently drops whatever routes only
        through the other, so both are read and merged here -- inside the
        adapter, so ``core.Transaction`` never inherits ESPN's asymmetry
        (research §6 item 2).

        The two surfaces are also shaped differently, and each is used for what
        it is good at. ``mTransactions2`` is **scoped to one scoring period**;
        the activity feed is a rolling recent list across periods. So the
        default read is one period plus the feed -- fast, and complete for
        "what just happened". Asking for ``since`` widens the ``mTransactions2``
        half to sweep every scoring period in the season, because ESPN offers no
        way to ask a date range and a sweep is the only honest answer. Completed
        weeks cache forever, so the sweep is paid once.

        The activity feed is best-effort: ``espn-api`` refuses it before 2019
        and ESPN stopped serving it for historical seasons (issue #546, open
        since 2024). A feed that will not load degrades to the
        ``mTransactions2`` result rather than failing the read -- the
        alternative is a transactions command that cannot read a 2018 league at
        all.
        """
        with self._read(league_id, season, "fetch_transactions") as league:
            found: list[Transaction] = []
            seen: set[tuple[Any, ...]] = set()

            for period in _scoring_periods(league, since, scoring_period):
                for item in self._espn_transactions(league, period):
                    normalized = _transaction(item)
                    if normalized is not None and _remember(seen, normalized):
                        found.append(normalized)

            for activity in self._activity(league):
                for normalized in _activity_transactions(activity):
                    if _remember(seen, normalized):
                        found.append(normalized)

            if since is not None:
                found = [
                    item for item in found if item.timestamp is None or item.timestamp >= since
                ]
            found.sort(key=lambda item: (item.timestamp is None, item.timestamp or _EPOCH))
            return found

    def fetch_free_agents(
        self, league_id: str, season: int, week: int, position: str | None = None
    ) -> list[FreeAgent]:
        """Unrostered players, optionally filtered to one position.

        ESPN scopes this by the ``x-fantasy-filter`` header rather than the URL,
        and an unrecognised position is not an error there — ESPN answers with
        its own default player set, which looks like free agents and is not
        filtered the way you asked. An unknown position is therefore refused
        here rather than sent.
        """
        slot = None if position is None else _position_key(position)
        if position is not None and slot is None:
            raise ValueError(
                f"{position!r} is not an ESPN position. Valid values: {', '.join(self.POSITIONS)}."
            )
        with self._read(league_id, season, "fetch_free_agents") as league:
            scoring_period = _as_int(week) or _as_int(league.current_week)
            players = league.free_agents(
                week=scoring_period, size=self._free_agent_limit, position=slot
            )
            transport = self._transport(league_id, season)
            entries = _free_agent_entries(transport.responses)
            kickoffs = _kickoff_map(transport.responses)
            now = self._now()
            return [
                _free_agent(player, entries.get(player.playerId, {}), kickoffs, scoring_period, now)
                for player in players
            ]

    # --- the escape hatch -------------------------------------------------- #

    def fetch_raw(self, league_id: str, season: int, **provider_params: Any) -> dict:
        """Direct passthrough to ESPN's own API (ADR-0002).

        Takes ``view=`` — a string or a list of them — plus any other query
        parameter ESPN accepts, and an optional ``x_fantasy_filter`` mapping
        that becomes the header several views need to return anything useful.

        Does **not** bootstrap the league: a passthrough that costs four
        requests before answering is not a passthrough.
        """
        view = provider_params.pop("view", None)
        if not view:
            raise ValueError("fetch_raw needs a `view=` — ESPN has no default view.")
        fantasy_filter = provider_params.pop("x_fantasy_filter", None)
        headers = None
        if fantasy_filter is not None:
            headers = {
                "x-fantasy-filter": (
                    fantasy_filter
                    if isinstance(fantasy_filter, str)
                    else json.dumps(fantasy_filter)
                )
            }

        params: dict[str, Any] = {"view": view}
        params.update({key: value for key, value in provider_params.items() if value is not None})

        with self._raw_league(league_id, season) as (request, transport):
            with _mapped(view=_view_of(params), operation="fetch_raw", provider=self):
                payload = request.league_get(params=params, headers=headers)
            self.last_fetch = FetchRecord(responses=dict(transport.responses))
            return payload if isinstance(payload, dict) else {"data": payload}

    # --- plumbing ---------------------------------------------------------- #

    @contextmanager
    def _read(self, league_id: str, season: int, operation: str) -> Iterator[Any]:
        """Build (or reuse) the league, map every failure, and record sources."""
        with _mapped(view=operation, operation=operation, provider=self):
            league = self._league(league_id, season)
            yield league
        self.last_fetch = FetchRecord(responses=dict(self._transport(league_id, season).responses))

    def _league(self, league_id: str, season: int) -> Any:
        key = (str(league_id), int(season))
        found = self._leagues.get(key)
        if found is not None:
            return found[0]

        from espn_api.football import League as EspnLeague

        numeric = _as_int(league_id)
        if numeric is None:
            raise LeagueNotFoundError(
                f"ESPN league ids are numeric; got {league_id!r}.",
                remediation="Copy the leagueId from the URL of your ESPN league page.",
                details={"league_id": str(league_id)},
            )

        league = EspnLeague(
            league_id=numeric,
            year=int(season),
            espn_s2=self._credentials.get("espn_s2"),
            swid=self._credentials.get("swid"),
            fetch_league=False,
        )
        transport = self._install(league, league_id=str(league_id), season=int(season))
        self._leagues[key] = (league, transport)
        try:
            league.fetch_league()
        except BaseException:
            # A half-built league must not be memoised: the next call would
            # reuse an object whose `teams` never loaded and fail as a
            # `SCHEMA_DRIFT` three layers from the real cause.
            self._leagues.pop(key, None)
            raise
        transport.completed_through = max(_as_int(league.current_week) or 1, 1) - 1
        return league

    @contextmanager
    def _raw_league(self, league_id: str, season: int) -> Iterator[tuple[Any, _Transport]]:
        """An ``EspnFantasyRequests`` with our transport, and no bootstrap."""
        from espn_api.requests.espn_requests import EspnFantasyRequests

        numeric = _as_int(league_id)
        if numeric is None:
            raise LeagueNotFoundError(
                f"ESPN league ids are numeric; got {league_id!r}.",
                details={"league_id": str(league_id)},
            )
        cookies = None
        if self.has_credentials:
            cookies = {
                "espn_s2": self._credentials["espn_s2"],
                "SWID": self._credentials["swid"],
            }
        request = EspnFantasyRequests(
            sport="nfl", year=int(season), league_id=numeric, cookies=cookies
        )
        transport = _Transport(
            request,
            season=int(season),
            league_id=str(league_id),
            store=self._cache,
            mode=self._cache_mode,
            http=self._http,
            now=self._now,
            on_auth_reason=self._note_auth_reason,
        )
        yield request, transport

    def _install(self, league: Any, *, league_id: str, season: int) -> _Transport:
        return _Transport(
            league.espn_request,
            season=season,
            league_id=league_id,
            store=self._cache,
            mode=self._cache_mode,
            http=self._http,
            now=self._now,
            on_auth_reason=self._note_auth_reason,
        )

    def _transport(self, league_id: str, season: int) -> _Transport:
        return self._leagues[(str(league_id), int(season))][1]

    def _bootstrap_payload(self, league_id: str, season: int) -> Mapping[str, Any]:
        responses = self._transport(league_id, season).responses
        for response in responses.values():
            if "mTeam" in response.view.split("+") and isinstance(response.payload, Mapping):
                return response.payload
        return {}

    def _team(self, team: Any, raw: Mapping[str, Any], *, standing: int | None = None) -> Team:
        return Team(
            provider=PROVIDER,
            provider_id=str(team.team_id),
            name=team.team_name,
            owner_names=_owner_names(team),
            wins=int(team.wins),
            losses=int(team.losses),
            ties=int(team.ties),
            points_for=float(team.points_for),
            points_against=float(team.points_against),
            standing=standing if standing is not None else _as_int(team.standing),
            raw=_team_raw(raw),
        )

    def _espn_transactions(self, league: Any, scoring_period: int | None) -> list[Any]:
        """``mTransactions2``, treating "none this period" as an empty result.

        ``espn-api`` raises a bare ``Exception('No transactions found')`` when
        the key is absent, which is what a quiet scoring period looks like. It
        is a successful empty result, not a failure, and reporting it as
        ``PROVIDER_UNAVAILABLE`` would tell an agent to retry an answer that is
        already correct.
        """
        try:
            return list(
                league.transactions(
                    scoring_period=scoring_period, types=set(_ALL_TRANSACTION_TYPES)
                )
            )
        except FantasySportsError:
            raise
        except Exception as exc:  # noqa: BLE001 - see docstring; classified below
            if "no transactions found" in str(exc).lower():
                return []
            raise

    def _activity(self, league: Any) -> list[Any]:
        """The activity feed, best-effort. See :meth:`fetch_transactions`."""
        try:
            return list(league.recent_activity())
        except FantasySportsError:
            raise
        except Exception:  # noqa: BLE001 - a missing second surface is not a failure
            return []


_EPOCH: Final[datetime] = from_epoch_millis(0)


def _revealed(credentials: Mapping[str, Any] | None) -> dict[str, str]:
    """Credential values as plain strings, accepting a ``CredentialSet``.

    ``reveal()`` is called at exactly one place — here, on the way to the
    transport — which is the discipline ``auth.chain.Secret`` exists to enforce.
    """
    if credentials is None:
        return {}
    if hasattr(credentials, "as_mapping"):
        return dict(credentials.as_mapping())
    revealed: dict[str, str] = {}
    for name, value in credentials.items():
        if value is None:
            continue
        revealed[name] = value.reveal() if hasattr(value, "reveal") else str(value)
    return revealed


# --------------------------------------------------------------------------- #
# Failure mapping
# --------------------------------------------------------------------------- #


@contextmanager
def _mapped(*, view: str, operation: str, provider: EspnProvider) -> Iterator[None]:
    """Turn anything ``espn-api`` raises into a taxonomy error.

    The order of the ``except`` clauses is the whole design:

    * our own errors pass through untouched — they were classified by whoever
      had the evidence;
    * ``KeyError``/``TypeError``/``IndexError``/``AttributeError`` are shape
      problems from inside a constructor doing unguarded dict access, and
      become ``SCHEMA_DRIFT`` with the offending path;
    * the library's three HTTP exceptions map per ARCHITECTURE §5, with the 401
      classified from ESPN's typed reason where there is one;
    * **everything else** becomes ``PROVIDER_UNAVAILABLE``. ``espn-api`` raises
      bare ``Exception`` for ordinary conditions, so this catch-all is not
      defensive padding — it is the common path for anything the library did
      not think to type.
    """
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
        ESPNUnknownError,
    )

    try:
        yield
    except FantasySportsError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise _drift(exc, view=view, operation=operation) from exc
    except ESPNAccessDenied as exc:
        raise _access_denied(exc, view=view, provider=provider) from exc
    except ESPNInvalidLeague as exc:
        raise _not_found(exc, view=view, provider=provider) from exc
    except ESPNUnknownError as exc:
        raise _unknown_status(exc, view=view) from exc
    except Exception as exc:  # noqa: BLE001 - the honest landing place (R12)
        raise ProviderUnavailableError(
            f"ESPN request failed during {operation}: {_clean(exc)}",
            details={"view": view, "cause": type(exc).__name__},
        ) from exc


def _drift(exc: BaseException, *, view: str, operation: str) -> SchemaDriftError:
    """Build the ``SCHEMA_DRIFT`` error, with enough context to file an issue."""
    frame = _espn_frame(exc)
    key = _missing_key(exc)
    path = [part for part in (view, frame, key) if part]
    what = f"missing key {key!r}" if key else f"{type(exc).__name__}: {_clean(exc)}"
    return SchemaDriftError(
        f"ESPN's {view} response no longer has the shape espn-api reads "
        f"({what}" + (f", in {frame}" if frame else "") + ").",
        path=path,
        provider=PROVIDER,
        remediation=(
            "File an issue with this error payload; the provider's response shape changed."
        ),
        details={"view": view, "operation": operation, "cause": type(exc).__name__},
    )


def _access_denied(exc: BaseException, *, view: str, provider: EspnProvider) -> FantasySportsError:
    """Classify a 401 that survived ``espn-api``'s alternate-shape retry.

    The retry matters and is why this is not a status-code mapping: by the time
    this runs the library has already tried the other current-vs-historical URL
    shape and failed on both, so the season-boundary ambiguity ARCHITECTURE §14
    item 1 warns about is genuinely resolved. **That probe is mandatory, not a
    fallback** -- see :data:`AUTH_REASONS` for the measurement that settles it.

    What is left is a question this response cannot answer. Probed against a
    real private league on 2026-09-05, one variable at a time, the 401 body is
    byte-identical for no cookies, a valid ``SWID`` with a bad ``espn_s2``, and
    either cookie alone. So:

    * nothing (or half of the pair) was configured -> ``AUTH_MISSING``, which is
      a fact about us and needs no evidence from ESPN;
    * a reason type that positively proves the credential failed ->
      ``AUTH_EXPIRED``. None has ever been observed; the branch exists so that
      adding one to :data:`AUTH_REASONS` is the whole change;
    * otherwise -> ``LEAGUE_NOT_FOUND``, whose ``agent_action`` -- confirm the
      league id and this account's access -- is correct for *all* of the
      remaining possibilities. Reporting ``AUTH_EXPIRED`` here would send a
      user to re-extract cookies that may have been fine, which is exactly the
      failure the double-probe exists to prevent.
    """
    reason = provider._auth_reason
    details: dict[str, Any] = {"view": view, "status": 401}
    if reason:
        details["espn_reason"] = reason

    if not provider.has_credentials:
        missing = provider.missing_credentials
        details["missing"] = list(missing)
        return AuthMissingError(
            "ESPN refused this league, and "
            + (
                "no credentials were configured"
                if len(missing) == 2
                else f"only half the cookie pair was configured (missing {missing[0]})"
            )
            + ". ESPN sends cookies only when it has both, so this request "
            "reached ESPN unauthenticated. Private leagues and pre-2018 history "
            "both require them.",
            remediation=(
                "Run `fantasy-sports auth login`, or set FANTASY_SPORTS_ESPN_S2 "
                "and FANTASY_SPORTS_SWID in the environment."
            ),
            details=details,
        )

    known = AUTH_REASONS.get(reason or "")
    if known is not None and known.proves_credentials_bad:
        return AuthExpiredError(
            f"ESPN rejected the configured credentials. {known.explanation}",
            remediation=(
                "Re-extract espn_s2 and SWID from a logged-in browser session and "
                "run `fantasy-sports auth login`."
            ),
            details=details,
        )

    explanation = (
        known.explanation
        if known is not None
        else (
            f"ESPN gave the reason {reason!r}, which this tool has not seen before."
            if reason
            else "ESPN's 401 carried no machine-readable reason."
        )
    )
    details["reason"] = "credentials_or_membership"
    return LeagueNotFoundError(
        "ESPN refused this league for the configured credentials. "
        f"{explanation} That leaves two possibilities this response cannot tell "
        "apart: this account is not a member of the league, or the cookies no "
        "longer work. This tool will not guess between them.",
        remediation=(
            "Confirm the league id, and that the ESPN account whose cookies are "
            "configured is a member of it. If it is, re-extract the cookies and "
            "run `fantasy-sports auth login`."
        ),
        details=details,
    )


def _not_found(exc: BaseException, *, view: str, provider: EspnProvider) -> LeagueNotFoundError:
    """Classify a 404, which is not only "you typed the id wrong".

    ESPN moved pre-2018 league history behind authentication some time in 2025
    and the gating surfaces as a **404**, not a 401. Reporting a bare
    not-found there sends the user off to check an id that was correct, which
    is the same class of misdiagnosis the 401 double-probe exists to avoid — so
    a 404 with no credentials attached says so.
    """
    details: dict[str, Any] = {"view": view, "status": 404}
    if provider.has_credentials:
        return LeagueNotFoundError(
            _clean(exc),
            remediation="Confirm the league id, and that this account can see that season.",
            details=details,
        )
    details["reason"] = "credentials_may_be_required"
    return LeagueNotFoundError(
        f"{_clean(exc)} No credentials were sent, and ESPN answers 404 — not 401 — "
        "for a league or season that requires them. Historical seasons in "
        "particular moved behind authentication in 2025.",
        remediation=(
            "Check the league id first; if it is right, run `fantasy-sports auth login` "
            "and try again."
        ),
        details=details,
    )


def _unknown_status(exc: BaseException, *, view: str) -> FantasySportsError:
    """Recover the HTTP status ``espn-api`` folded into an English message.

    Belt and braces for 429: the transport intercepts it before the library
    sees it, but the library's own alternate-shape retry issues a request our
    transport never touches, so a 429 can still arrive by this path. It has no
    ``Retry-After`` — the header did not survive — and reporting one we do not
    have would be worse than reporting none.
    """
    text = _clean(exc)
    status = _as_int(text.rsplit(" ", 1)[-1]) if text else None
    if status == 429:
        return RateLimitedError(
            "ESPN throttled this request (HTTP 429).",
            remediation="Retry with bounded exponential backoff.",
            details={"view": view, "status": status},
        )
    details: dict[str, Any] = {"view": view}
    if status is not None:
        details["status"] = status
    return ProviderUnavailableError(text, details=details)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def _teams_by_id(data: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    teams = data.get("teams") if isinstance(data, Mapping) else None
    if not isinstance(teams, list):
        return {}
    return {team["id"]: team for team in teams if isinstance(team, Mapping) and "id" in team}


def _team_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A team's own payload, without its roster.

    The roster is 74 KB of the 75 KB ESPN sends per team, it is a different
    normalized concept with its own read, and carrying it here would make
    ``teams`` output three quarters of a megabyte of data nobody asked for.
    It is reachable through ``fetch_roster`` and through the recorded response
    on :attr:`EspnProvider.last_fetch`.
    """
    return {key: value for key, value in raw.items() if key != "roster"}


def _league_raw(data: Mapping[str, Any]) -> dict[str, Any]:
    """The league-level slice of the bootstrap: everything but teams and schedule."""
    return {key: value for key, value in data.items() if key not in {"teams", "schedule"}}


def _owner_names(team: Any) -> tuple[str, ...]:
    """Display names for a team's owners, plural from day one.

    ESPN's ``owners`` is a list and both ESPN and Yahoo support co-management
    natively, so a single ``owner_name`` silently loses a manager (research §6
    item 1). ``espn-api`` resolves the team's owner SWIDs against the league's
    ``members``, and the *member* objects are where a display name would live —
    but many leagues carry none at all, in which case this is empty rather than
    invented. An empty tuple is ordinary data, not drift.
    """
    names: list[str] = []
    for owner in getattr(team, "owners", []) or []:
        if isinstance(owner, str):
            name = owner.strip()
        elif isinstance(owner, Mapping):
            name = _member_name(owner)
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _member_name(member: Mapping[str, Any]) -> str:
    display = member.get("displayName")
    if isinstance(display, str) and display.strip():
        return display.strip()
    parts = [
        str(member.get(key, "")).strip()
        for key in ("firstName", "lastName")
        if str(member.get(key, "")).strip()
    ]
    return " ".join(parts)


def _roster_slots(data: Mapping[str, Any]) -> dict[str, int]:
    """Slot name to count, from ``rosterSettings.lineupSlotCounts`` (R3a).

    Derived here rather than read off ``espn-api``'s ``position_slot_counts``,
    which zips slot counts against ``list(POSITION_MAP.values())[:n]`` and so
    depends on the literal source ordering of a dict that holds *both*
    directions of the id/name mapping (research §7.4). Mapping each slot id
    through ``POSITION_MAP`` directly is the same information without the
    ordering dependency.
    """
    from espn_api.football.constant import POSITION_MAP

    settings = data.get("settings") if isinstance(data, Mapping) else None
    if not isinstance(settings, Mapping):
        return {}
    counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
    if not isinstance(counts, Mapping):
        return {}
    slots: dict[str, int] = {}
    for slot_id, count in counts.items():
        number = _as_int(slot_id)
        amount = _as_int(count)
        if number is None or not amount:
            continue
        name = POSITION_MAP.get(number)
        if isinstance(name, str) and name:
            slots[name] = slots.get(name, 0) + amount
    return slots


def _kickoff_map(responses: Mapping[str, RawResponse]) -> dict[tuple[int, int], int]:
    """``(proTeamId, scoringPeriod)`` to kickoff in **epoch milliseconds**.

    Built from the raw ``proTeamSchedules_wl`` payload rather than from
    ``espn-api``'s ``Player.schedule``, whose values are
    ``datetime.fromtimestamp(ms / 1000)`` with no ``tz=`` — naive, host-local,
    and different on every machine. This is the re-derivation ARCHITECTURE §14
    item 8 requires; the output layer refuses a naive datetime outright, so
    reaching for the library's value fails loudly rather than silently.
    """
    response = _latest(responses, "proTeamSchedules_wl")
    kickoffs: dict[tuple[int, int], int] = {}
    if response is None or not isinstance(response.payload, Mapping):
        return kickoffs
    pro_teams = response.payload.get("settings", {}).get("proTeams", [])
    if not isinstance(pro_teams, list):
        return kickoffs
    for pro_team in pro_teams:
        if not isinstance(pro_team, Mapping):
            continue
        team_id = _as_int(pro_team.get("id"))
        games = pro_team.get("proGamesByScoringPeriod", {})
        if team_id is None or not isinstance(games, Mapping):
            continue
        for period, entries in games.items():
            week = _as_int(period)
            if week is None or not isinstance(entries, list) or not entries:
                continue
            first = entries[0]
            when = _as_int(first.get("date")) if isinstance(first, Mapping) else None
            if when is not None:
                kickoffs[(team_id, week)] = when
    return kickoffs


def _roster_entries(
    transport: _Transport, team_id: int, week: int | None
) -> dict[int, Mapping[str, Any]]:
    """Raw roster entries for one team, keyed by player id.

    Prefers the standalone ``mRoster`` payload when a specific week was loaded,
    because that request replaced the bootstrap's rosters in the object model
    and the bootstrap's copy is now the wrong week.
    """
    for view in ("mRoster", "mTeam+mRoster+mMatchup+mSettings+mStandings"):
        response = _latest(transport.responses, view)
        if response is None or not isinstance(response.payload, Mapping):
            continue
        for team in response.payload.get("teams", []) or []:
            if not isinstance(team, Mapping) or team.get("id") != team_id:
                continue
            entries = team.get("roster", {}).get("entries", [])
            return {
                entry["playerId"]: entry
                for entry in entries
                if isinstance(entry, Mapping) and "playerId" in entry
            }
    return {}


def _free_agent_entries(responses: Mapping[str, RawResponse]) -> dict[int, Mapping[str, Any]]:
    response = _latest(responses, "kona_player_info")
    if response is None or not isinstance(response.payload, Mapping):
        return {}
    players = response.payload.get("players", [])
    if not isinstance(players, list):
        return {}
    return {entry["id"]: entry for entry in players if isinstance(entry, Mapping) and "id" in entry}


def _player(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    week: int | None,
) -> Player:
    """One normalized player, with the context R3 requires for a lineup call."""
    from espn_api.football.constant import PRO_TEAM_MAP

    pro_team_id = next(
        (key for key, value in PRO_TEAM_MAP.items() if value == player.proTeam), None
    )
    kickoff_ms = (
        kickoffs.get((pro_team_id, week)) if pro_team_id is not None and week is not None else None
    )
    projected = player.stats.get(week, {}).get("projected_points") if week is not None else None
    return Player(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        name=player.name,
        position=player.position,
        eligible_slots=tuple(player.eligibleSlots),
        status=getattr(player, "active_status", None),
        injury_status=player.injuryStatus,
        pro_team=player.proTeam,
        projected_points=None if projected is None else float(projected),
        kickoff=None if kickoff_ms is None else from_epoch_millis(kickoff_ms),
        raw=dict(raw),
    )


def _roster_slot(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    week: int | None,
    now: datetime,
) -> RosterSlot:
    normalized = _player(player, raw, kickoffs, week)
    slot = player.lineupSlot or ""
    return RosterSlot(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        player=normalized,
        slot=slot,
        is_starter=slot not in STARTER_EXCLUDED_SLOTS,
        is_locked=None if normalized.kickoff is None else normalized.kickoff <= now,
        raw=dict(raw),
    )


def _free_agent(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    week: int | None,
    now: datetime,
) -> FreeAgent:
    owned = getattr(player, "percent_owned", None)
    return FreeAgent(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        player=_player(player, raw, kickoffs, week),
        percent_owned=None if owned is None or owned < 0 else float(owned),
        raw=dict(raw),
    )


def _scoring_periods(league: Any, since: datetime | None, scoring_period: int | None) -> list[int]:
    """Which scoring periods to ask ``mTransactions2`` about.

    ESPN's transaction view takes one period and offers no date range, so
    ``since`` can only be served by sweeping. The sweep is bounded by the
    league's own ``firstScoringPeriod`` and its current week -- never by a
    guess about how long a season is.
    """
    explicit = _as_int(scoring_period)
    if explicit is not None:
        return [explicit]
    current = _as_int(league.current_week) or _as_int(league.scoringPeriodId) or 1
    if since is None:
        return [current]
    first = _as_int(getattr(league, "firstScoringPeriod", None)) or 1
    return list(range(min(first, current), current + 1))


def _matchup_period_for(league: Any, scoring_period: int) -> int:
    """Which matchup period contains ``scoring_period``.

    ``settings.matchup_periods`` is ESPN's own mapping, shaped
    ``{matchupPeriodId: [scoringPeriodId, ...]}``. They are 1:1 for a normal
    week and diverge when a playoff round spans two NFL weeks, which is exactly
    the path an in-season ESPN-only test never exercises. Falling back to the
    scoring period when the mapping has no entry is right: that is what the
    identity case looks like.
    """
    periods = getattr(getattr(league, "settings", None), "matchup_periods", None) or {}
    for matchup_id, scoring_periods in periods.items():
        if scoring_period in (scoring_periods or []):
            resolved = _as_int(matchup_id)
            if resolved is not None:
                return resolved
    return scoring_period


def _schedule_entries(
    responses: Mapping[str, RawResponse], matchup_period: int
) -> dict[tuple[int, int], Mapping[str, Any]]:
    """Raw ``schedule[]`` entries for one matchup period, keyed by team-id pair."""
    for view in ("mMatchupScore", "mTeam+mRoster+mMatchup+mSettings+mStandings"):
        response = _latest(responses, view)
        if response is None or not isinstance(response.payload, Mapping):
            continue
        schedule = response.payload.get("schedule", [])
        if not isinstance(schedule, list):
            continue
        found: dict[tuple[int, int], Mapping[str, Any]] = {}
        for entry in schedule:
            if not isinstance(entry, Mapping) or entry.get("matchupPeriodId") != matchup_period:
                continue
            home = _as_int(entry.get("home", {}).get("teamId")) or 0
            away = _as_int(entry.get("away", {}).get("teamId")) or 0
            found[(home, away)] = entry
        if found:
            return found
    return {}


def _matchup(
    matchup: Any,
    raw_entries: Mapping[tuple[int, int], Mapping[str, Any]],
    *,
    scoring_period: int,
    matchup_period: int,
    index: int,
) -> Matchup:
    home_id = _team_id_of(matchup.home_team)
    away_id = _team_id_of(matchup.away_team)
    raw = raw_entries.get((home_id, away_id), {})
    provider_id = str(raw.get("id")) if raw.get("id") is not None else f"{matchup_period}-{index}"
    return Matchup(
        provider=PROVIDER,
        provider_id=provider_id,
        week=matchup_period,
        team_a_provider_id=str(home_id),
        team_a_score=float(matchup.home_score or 0.0),
        team_b_provider_id=str(away_id),
        team_b_score=float(matchup.away_score or 0.0),
        is_playoff=bool(matchup.is_playoff),
        scoring_period_id=scoring_period,
        matchup_period_id=matchup_period,
        raw=dict(raw),
    )


def _team_id_of(team: Any) -> int:
    """``espn-api`` leaves an unresolved side as a bare team id integer."""
    if isinstance(team, int):
        return team
    return _as_int(getattr(team, "team_id", None)) or 0


def _remember(seen: set[tuple[Any, ...]], transaction: Transaction) -> bool:
    """Record a transaction's identity; ``False`` if the other surface had it.

    Identity is deliberately not the provider id: ``mTransactions2`` and the
    activity feed number the same move differently, and matching on the id
    would make every merged trade a duplicate.
    """
    key = (
        transaction.type,
        transaction.team_provider_id,
        tuple(sorted(transaction.players_in)),
        tuple(sorted(transaction.players_out)),
        None if transaction.timestamp is None else int(transaction.timestamp.timestamp()),
    )
    if key in seen:
        return False
    seen.add(key)
    return True


def _transaction(item: Any) -> Transaction | None:
    """One ``mTransactions2`` row, or ``None`` where we have no honest type.

    ``TRADE_PROPOSAL``, ``TRADE_DECLINE``, ``TRADE_VETO`` and ``WAIVER_ERROR``
    are provider-specific *states*, not roster moves, and ``core.Transaction``'s
    four-value vocabulary has no equivalent for them (ADR-0002). They are
    dropped from normalized output and remain readable through
    :meth:`EspnProvider.fetch_raw` — the narrowing is lossy on purpose, and
    documented on the model.
    """
    players_in = [str(entry.playerId) for entry in item.items if entry.type == "ADD"]
    players_out = [str(entry.playerId) for entry in item.items if entry.type == "DROP"]

    kind = _TRANSACTION_TYPE_MAP.get(item.type)
    if kind is None and item.type == "FREEAGENT":
        kind = "add" if players_in else "drop"
    if kind is None:
        return None

    return Transaction(
        provider=PROVIDER,
        provider_id=str(getattr(item, "id", "") or f"{item.type}-{item.scoring_period}"),
        type=kind,
        team_provider_id=_team_provider_id(item.team),
        players_in=tuple(players_in),
        players_out=tuple(players_out),
        faab_spent=_as_int(item.bid_amount),
        timestamp=None if item.date is None else from_epoch_millis(item.date),
        raw={
            "type": item.type,
            "status": item.status,
            "scoringPeriodId": item.scoring_period,
            "bidAmount": item.bid_amount,
            "date": item.date,
            "items": [
                {"type": entry.type, "playerId": entry.playerId, "player": entry.player}
                for entry in item.items
            ],
        },
    )


def _activity_transactions(activity: Any) -> list[Transaction]:
    """The activity feed's rows, normalized into the same vocabulary.

    A trade arrives as **two** rows — ``TRADE_SENT`` from one team and
    ``TRADE_RECEIVED`` by the other — so each becomes one ``Transaction`` from
    that team's point of view, with the player moving out of the sender and
    into the receiver. That is the reconciliation research §6 item 2 asks for:
    ``core.Transaction`` sees one vocabulary, and the caller never learns that
    ESPN has two.
    """
    when = _as_int(getattr(activity, "date", None))
    timestamp = None if when is None else from_epoch_millis(when)
    found: list[Transaction] = []
    for index, action in enumerate(getattr(activity, "actions", []) or []):
        team, verb, player, bid = _unpack_action(action)
        kind = _ACTIVITY_TYPE_MAP.get(verb)
        if kind is None:
            continue
        player_id = _player_id_of(player)
        outgoing = verb in {"DROPPED", "TRADE_SENT"}
        found.append(
            Transaction(
                provider=PROVIDER,
                provider_id=f"activity-{when}-{index}",
                type=kind,
                team_provider_id=_team_provider_id(team),
                players_in=() if outgoing else (player_id,),
                players_out=(player_id,) if outgoing else (),
                faab_spent=_as_int(bid),
                timestamp=timestamp,
                raw={"source": "kona_league_communication", "action": verb, "date": when},
            )
        )
    return found


def _unpack_action(action: Any) -> tuple[Any, str, Any, Any]:
    items = list(action)
    while len(items) < 4:
        items.append(None)
    return items[0], str(items[1] or ""), items[2], items[3]


def _team_provider_id(team: Any) -> str | None:
    team_id = _as_int(getattr(team, "team_id", None))
    return None if team_id is None else str(team_id)


def _player_id_of(player: Any) -> str:
    player_id = _as_int(getattr(player, "playerId", None))
    if player_id is None:
        player_id = _as_int(player)
    return "" if player_id is None else str(player_id)


def _position_key(position: str) -> str | None:
    """``position`` as ESPN spells it, or ``None`` if ESPN has no such slot."""
    from espn_api.football.constant import POSITION_MAP

    # `POSITION_MAP` holds both directions in one dict, so a *string* key with
    # an `int` value is a name ESPN's filter understands; an `int` key with a
    # string value is the reverse lookup and is not what we want here.
    for candidate in (position, position.upper()):
        if candidate in POSITION_MAP and isinstance(POSITION_MAP[candidate], int):
            return candidate
    return None
