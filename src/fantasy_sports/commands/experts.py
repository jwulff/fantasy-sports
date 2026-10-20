"""``experts`` -- FantasyPros rankings, projections, injuries, and news.

Rankings and projections by FantasyPros. These four commands are the reads an
agent makes on waiver night that ESPN's own data cannot answer: where the
expert panel ranks a player, what it projects for him this week and for the
rest of the season, who is hurt and how likely he is to play, and what the
wire says about him (jwulff/fantasy-sports#129, ADR-0011).

FantasyPros is a **source**, not a provider (ADR-0011): it has no leagues, so
these commands do not open a league. ``--league`` is accepted by the two that
score points (``rankings`` and ``projections``) for exactly one reason, to read
the league's receptions setting and pick PPR, half-PPR or standard scoring; it
is never required. Every envelope names FantasyPros in ``sources[]`` as
``fantasypros:<endpoint>``, which is both what the API terms require and how a
consumer tells these reads from ESPN's by prefix.

**The row shapes are a contract.** ``jwulff/league-gazette`` builds its facts
from these rows, so the field names below are an API: the issue's acceptance
rows and its 2026-10-20 contract comment are the specification, and a rename
is a breaking change. ``data`` is always a list of rows, like ``standings``.

**Every row carries ``espn_id``** (a string, or null when FantasyPros has no
ESPN id for the player), joined from the cached players table. That table is a
4 MB read, so it costs one request a day, and it is also where the default
week comes from: FantasyPros reports the week in progress on it.

**``--player`` takes three spellings** and resolves them through the same
table: a FantasyPros id, an ESPN id, or a name (exact, else a unique prefix).
A token that fits more than one player is ``CONFIG_INVALID`` with
``kind=argument`` and the candidates listed, never a guess.

**Past weeks are served.** FantasyPros answers a closed week with the final
pre-game publish, ``start_sit_grade``, ``r2p_pts`` and ``game_status: closed``
intact, so ``--week N`` is passed through whatever N is.

Nothing here imports typer (ADR-0003), and the HTTP stack is reached only
through the client, which imports it inside its transport.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fantasy_sports.commands.context import require_shape
from fantasy_sports.core.errors import ConfigInvalidError, SchemaDriftError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import DataSource, Envelope
    from fantasy_sports.sources.fantasypros import FantasyProsClient, SourceResponse

__all__ = ["injuries", "news", "projections", "rankings"]

PROVIDER: Final[str] = "fantasypros"

RANKING_POSITIONS: Final[tuple[str, ...]] = ("QB", "RB", "WR", "TE", "K", "DST", "FLX")
"""What ``consensus-rankings`` and ``projections`` accept (``FLX`` is the flex pool)."""

INJURY_POSITIONS: Final[tuple[str, ...]] = ("QB", "RB", "WR", "TE", "K", "DST")
"""What ``--pos`` filters injuries to. ``FLX`` is a ranking pool, not a position."""

FLEX_POSITIONS: Final[tuple[str, ...]] = ("RB", "WR", "TE")
"""Who ``FLX`` means when it narrows a ``--player`` name."""

SCORINGS: Final[tuple[str, ...]] = ("STD", "HALF", "PPR")

_POSITION_ALIASES: Final[Mapping[str, str]] = {"D/ST": "DST", "DEF": "DST", "D": "DST"}
"""ESPN's own spellings for the defense, accepted so a position copied from an
ESPN payload does not need translating."""

_RECEPTIONS_STAT_ID: Final[int] = 53
"""ESPN's stat id for a reception; its ``points`` in ``scoringItems`` is the PPR setting."""

_MAX_CANDIDATES: Final[int] = 10


# --------------------------------------------------------------------------- #
# The four commands
# --------------------------------------------------------------------------- #


def rankings(
    *,
    pos: str,
    week: int | None = None,
    scoring: str | None = None,
    ros: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Consensus expert rankings for one position, best first.

    One FantasyPros call per position, which is why ``pos`` is required.
    ``ros`` asks for the rest-of-season ranking instead of a week's, and sends
    no week. Ordered by ``rank_ecr`` ascending.
    """
    position = _choice(pos, RANKING_POSITIONS, name="pos")
    if ros and week is not None:
        raise _ros_with_week()
    wanted = None if scoring is None else _choice(scoring, SCORINGS, name="scoring")

    run = _Run.open(league, season, fresh=fresh, no_cache=no_cache, want_scoring=wanted is None)
    chosen = wanted or run.scoring
    table = run.players()
    response = run.client.consensus_rankings(
        week=None if ros else _week(week, table), position=position, scoring=chosen, ros=ros
    )
    rows = [_ranking_row(raw, table) for raw in _list(response, "players")]
    rows.sort(key=lambda row: _last(row["rank_ecr"]))
    return run.envelope(rows, "experts rankings", (table.response, response))


def projections(
    *,
    pos: str | None = None,
    week: int | None = None,
    ros: bool = False,
    player: str | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Expert projections with standard, half-PPR and PPR points, best first.

    ``pos`` is required because the endpoint is read per position, unless
    ``player`` names the one player wanted. Ordered by the league's scoring
    when ``league`` is given, PPR otherwise. ``ros`` asks for rest-of-season
    totals instead of a week's.
    """
    position = None if pos is None else _choice(pos, RANKING_POSITIONS, name="pos")
    if position is None and player is None:
        raise ConfigInvalidError(
            "experts projections needs --pos, or --player to name one player.",
            kind="argument",
            remediation="Pass --pos QB|RB|WR|TE|K|DST|FLX (FantasyPros is read one position "
            "at a time), or --player with a FantasyPros id, an ESPN id, or a name.",
            details={"argument": "pos"},
        )
    if ros and week is not None:
        raise _ros_with_week()

    run = _Run.open(league, season, fresh=fresh, no_cache=no_cache, want_scoring=True)
    table = run.players()
    wanted = None if player is None else table.resolve(player, position=position)
    response = run.client.projections(
        week=None if ros else _week(week, table),
        # A named player is the whole request: the position only narrowed his name.
        positions=() if position is None or wanted is not None else (position,),
        players=() if wanted is None else (wanted,),
        ros=ros,
    )
    rows = [
        _projection_row(raw, table)
        for raw in _list(response, "players")
        if wanted is None or _int(raw.get("fpid")) == wanted
    ]
    key = {"STD": "points_std", "HALF": "points_half", "PPR": "points_ppr"}[run.scoring]
    rows.sort(key=lambda row: _last(row[key], descending=True))
    return run.envelope(rows, "experts projections", (table.response, response))


def injuries(
    *,
    pos: str | None = None,
    team: str | None = None,
    player: str | None = None,
    week: int | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Injuries with practice reports and probability of playing, newest first.

    Only injuries: a player who is not listed here is not thereby healthy (a
    suspension or the Commissioner's Exempt List is not an injury).
    """
    position = None if pos is None else _choice(pos, INJURY_POSITIONS, name="pos")
    run = _Run.open(None, season, fresh=fresh, no_cache=no_cache, want_scoring=False)
    table = run.players()
    wanted = None if player is None else table.resolve(player, position=position)
    response = run.client.injuries(
        week=_week(week, table),
        player_ids=() if wanted is None else (wanted,),
        team_id=team,
    )
    rows = [
        _injury_row(raw, table)
        for raw in _list(response, "injuries")
        if (wanted is None or _int(raw.get("player_id")) == wanted)
        and (position is None or raw.get("position_id") == position)
    ]
    rows.sort(key=lambda row: row["name"] or "")
    rows.sort(key=lambda row: row["updated_at"] or "", reverse=True)  # stable; undated last
    return run.envelope(rows, "experts injuries", (table.response, response))


def news(
    *,
    player: str | None = None,
    category: str | None = None,
    limit: int = 25,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """The player-news wire, newest first, with the fantasy-impact line.

    There is no ``season`` option: the wire is live and ``/nfl/news`` takes
    no year, so the envelope reports the current season and nothing else
    would be true.

    One request per player when ``player`` is given: FantasyPros ignores a
    list of players, so a caller wanting twenty players makes twenty calls.
    The title, summary and impact are third-party prose and are labelled
    ``untrusted`` in the envelope.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ConfigInvalidError(
            f"--limit must be a positive whole number, got {limit!r}.",
            kind="argument",
            remediation="Pass --limit 1 or more.",
            details={"limit": repr(limit)},
        )
    run = _Run.open(None, None, fresh=fresh, no_cache=no_cache, want_scoring=False)
    table = run.players()
    wanted = None if player is None else table.resolve(player)
    response = run.client.news(limit=limit, category=(category or None), player_id=wanted)
    rows = [_news_row(raw, table) for raw in _list(response, "items")]
    rows.sort(key=lambda row: (row["created_at"] or "", row["id"] or 0), reverse=True)
    untrusted = {
        f"[{index}].{field}": str(row[field])
        for index, row in enumerate(rows)
        for field in ("title", "summary", "impact")
        if row[field]
    }
    return run.envelope(rows, "experts news", (table.response, response), untrusted=untrusted)


# --------------------------------------------------------------------------- #
# One run: the client, the season, the scoring, the players table
# --------------------------------------------------------------------------- #


class _Run:
    """What the four commands share: a client, a season, and where scoring came from."""

    def __init__(
        self,
        client: FantasyProsClient,
        *,
        season: int,
        league_id: str | None,
        scoring: str,
        espn_sources: Sequence[DataSource],
    ) -> None:
        self.client = client
        self.season = season
        self.league_id = league_id
        self.scoring = scoring
        self._espn_sources = tuple(espn_sources)
        self._players: _Players | None = None

    @classmethod
    def open(
        cls,
        league: str | None,
        season: int | None,
        *,
        fresh: bool,
        no_cache: bool,
        want_scoring: bool,
    ) -> _Run:
        """Resolve everything that can fail before a FantasyPros request is made.

        A league is looked up first, and its scoring read from ESPN, because an
        unknown ``--league`` must not cost a 4 MB download.
        """
        from fantasy_sports.cache.store import CacheMode, CacheStore
        from fantasy_sports.sources.fantasypros import FantasyProsClient, default_season

        scoring = "PPR"
        league_id: str | None = None
        espn_sources: Sequence[DataSource] = ()
        resolved_season = season
        if league is not None:
            from fantasy_sports.commands.context import open_read

            ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
            league_id, resolved_season = ctx.league_id, ctx.season
            if want_scoring:
                found = ctx.provider.fetch_league(*ctx.target)
                scoring = scoring_from_settings(found.raw)
                record = getattr(ctx.provider, "last_fetch", None)
                espn_sources = () if record is None else record.sources
        mode = CacheMode.BYPASS if no_cache else (CacheMode.FRESH if fresh else CacheMode.DEFAULT)
        client = FantasyProsClient(
            season=resolved_season if resolved_season is not None else default_season(),
            store=CacheStore(),
            mode=mode,
        )
        return cls(
            client,
            season=client.season,
            league_id=league_id,
            scoring=scoring,
            espn_sources=espn_sources,
        )

    def players(self) -> _Players:
        if self._players is None:
            self._players = _Players(self.client.players())
        return self._players

    def envelope(
        self,
        rows: list[dict[str, Any]],
        command: str,
        responses: Sequence[SourceResponse],
        *,
        untrusted: Mapping[str, str] | None = None,
    ) -> Envelope:
        from fantasy_sports.output.envelope import Envelope

        require_shape(rows, command=command)
        return Envelope.success(
            provider=PROVIDER,
            league_id=self.league_id,
            season=self.season,
            data=rows,
            sources=[*self._espn_sources, *(r.as_source() for r in responses)],
            untrusted=untrusted,
        )


def scoring_from_settings(raw: Mapping[str, Any]) -> str:
    """``PPR``, ``HALF`` or ``STD`` from an ESPN league's receptions setting.

    The setting is the ``points`` of the ``scoringItems`` entry whose
    ``statId`` is 53: 1.0 is PPR, 0.5 is half-PPR, and anything else
    (including no entry at all) is standard. A league whose settings carry no
    ``scoringSettings`` at all is a changed payload, not a standard league, so
    it is ``SCHEMA_DRIFT`` rather than a quiet guess.
    """
    settings = raw.get("settings") if isinstance(raw, Mapping) else None
    scoring = settings.get("scoringSettings") if isinstance(settings, Mapping) else None
    items = scoring.get("scoringItems") if isinstance(scoring, Mapping) else None
    if not isinstance(items, list):
        raise SchemaDriftError(
            "ESPN's league settings no longer carry scoringSettings.scoringItems, so the "
            "league's receptions setting cannot be read.",
            path=["mSettings", "settings", "scoringSettings", "scoringItems"],
            provider="espn",
            remediation="Pass --scoring explicitly, or file an issue with this error payload.",
            details={"operation": "scoring_from_settings"},
        )
    points = next(
        (
            _float(item.get("points"))
            for item in items
            if isinstance(item, Mapping) and _int(item.get("statId")) == _RECEPTIONS_STAT_ID
        ),
        None,
    )
    if points is not None and math.isclose(points, 1.0):
        return "PPR"
    if points is not None and math.isclose(points, 0.5):
        return "HALF"
    return "STD"


# --------------------------------------------------------------------------- #
# The players table: the espn_id join and --player resolution
# --------------------------------------------------------------------------- #


class _Players:
    """FantasyPros' players table, indexed three ways."""

    def __init__(self, response: SourceResponse) -> None:
        self.response = response
        self.week = _int(response.payload.get("week"))
        self.rows: list[Mapping[str, Any]] = [
            row for row in response.payload.get("players", []) if isinstance(row, Mapping)
        ]
        self._by_fp: dict[int, Mapping[str, Any]] = {}
        self._by_espn: dict[str, Mapping[str, Any]] = {}
        for row in self.rows:
            fp_id = _int(row.get("player_id"))
            if fp_id is not None:
                self._by_fp[fp_id] = row
            espn = _text(row.get("espn_id"))
            if espn is not None:
                self._by_espn[espn] = row

    def espn_id(self, fp_id: int | None) -> str | None:
        row = self._by_fp.get(fp_id) if fp_id is not None else None
        return None if row is None else _text(row.get("espn_id"))

    def name(self, fp_id: int | None) -> str | None:
        row = self._by_fp.get(fp_id) if fp_id is not None else None
        return None if row is None else _text(row.get("player_name"))

    def resolve(self, token: str, *, position: str | None = None) -> int:
        """The FantasyPros id a ``--player`` token names, or ``CONFIG_INVALID``."""
        text = str(token).strip()
        if not text:
            raise self._refuse(text, "is empty", [])
        eligible = FLEX_POSITIONS if position == "FLX" else (position,)
        pool = [row for row in self.rows if position is None or row.get("position_id") in eligible]
        if text.isdigit():
            by_fp = self._by_fp.get(int(text))
            by_espn = self._by_espn.get(text)
            found = [row for row in (by_fp, by_espn) if row is not None and row in pool]
            unique = {_int(row.get("player_id")) for row in found}
            if len(unique) == 1:
                (only,) = unique
                assert only is not None
                return only
            if not unique:
                raise self._refuse(text, "is not a FantasyPros or ESPN player id", [])
            raise self._refuse(text, "is both a FantasyPros id and an ESPN id", found)

        wanted = _fold(text)
        exact = [row for row in pool if _fold(row.get("player_name")) == wanted]
        matches = exact or [row for row in pool if _fold(row.get("player_name")).startswith(wanted)]
        if len(matches) == 1:
            fp_id = _int(matches[0].get("player_id"))
            if fp_id is not None:
                return fp_id
        if not matches:
            raise self._refuse(text, "matches no player", [])
        raise self._refuse(text, f"matches {len(matches)} players", matches)

    @staticmethod
    def _refuse(token: str, why: str, matches: Sequence[Mapping[str, Any]]) -> ConfigInvalidError:
        ordered = sorted(
            matches,
            key=lambda row: (_fold(row.get("player_name")), _int(row.get("player_id")) or 0),
        )
        candidates = [
            {
                "fp_id": _int(row.get("player_id")),
                "name": _text(row.get("player_name")),
                "position": _text(row.get("position_id")),
                "pro_team": _text(row.get("team_id")),
                "espn_id": _text(row.get("espn_id")),
            }
            for row in ordered[:_MAX_CANDIDATES]
        ]
        listing = "; ".join(
            f"{c['name']} ({c['position']} {c['pro_team']}, "
            f"fp {c['fp_id']}, espn {c['espn_id'] or '-'})"
            for c in candidates
        )
        more = len(ordered) - len(candidates)
        message = f"--player {token!r} {why}."
        if candidates:
            message += f" Candidates: {listing}" + (f"; and {more} more." if more > 0 else ".")
        return ConfigInvalidError(
            message,
            kind="argument",
            remediation="Pass the player's FantasyPros id or ESPN id, or a longer, unambiguous "
            "name (add --pos to narrow it).",
            details={"player": token, "candidates": candidates, "total": len(ordered)},
        )


# --------------------------------------------------------------------------- #
# Rows -- the contract
# --------------------------------------------------------------------------- #


def _ranking_row(raw: Mapping[str, Any], table: _Players) -> dict[str, Any]:
    fp_id = _int(raw.get("player_id"))
    return {
        "fp_id": fp_id,
        "name": _text(raw.get("player_name")),
        "pro_team": _text(raw.get("player_team_id")),
        "position": _text(raw.get("player_position_id")),
        "rank_ecr": _int(raw.get("rank_ecr")),
        "rank_min": _int(raw.get("rank_min")),
        "rank_max": _int(raw.get("rank_max")),
        "rank_ave": _float(raw.get("rank_ave")),
        "rank_std": _float(raw.get("rank_std")),
        "tier": raw.get("tier"),
        "ecr_delta": _int(raw.get("player_ecr_delta")),
        "opponent": _text(raw.get("player_opponent")),
        "kickoff": _utc_epoch(raw.get("player_game_kickoff_ts")),
        "bye_week": _int(raw.get("player_bye_week")),
        "owned_espn_pct": _float(raw.get("player_owned_espn")),
        "espn_id": table.espn_id(fp_id),
        "start_sit_grade": _text(raw.get("start_sit_grade")),
        "pos_rank": _text(raw.get("pos_rank")),
        "r2p_pts": _float(raw.get("r2p_pts")),
        "game_status": _text(raw.get("player_game_status")),
    }


def _projection_row(raw: Mapping[str, Any], table: _Players) -> dict[str, Any]:
    fp_id = _int(raw.get("fpid"))
    stats = raw.get("stats") if isinstance(raw.get("stats"), Mapping) else {}
    return {
        "fp_id": fp_id,
        "name": _text(raw.get("name")),
        "pro_team": _text(raw.get("team_id")),
        "position": _text(raw.get("position_id")),
        "espn_id": table.espn_id(fp_id),
        "points_std": _float(stats.get("points")),
        "points_half": _float(stats.get("points_half")),
        "points_ppr": _float(stats.get("points_ppr")),
        "stats": dict(stats),
    }


def _injury_row(raw: Mapping[str, Any], table: _Players) -> dict[str, Any]:
    fp_id = _int(raw.get("player_id"))
    weeks = raw.get("ir_weeks")
    return {
        "fp_id": fp_id,
        "name": _text(raw.get("name")),
        "pro_team": _text(raw.get("team_id")),
        "position": _text(raw.get("position_id")),
        "espn_id": table.espn_id(fp_id),
        "status": _text(raw.get("status")),
        "status_short": _text(raw.get("status_short")),
        "injury_type": _text(raw.get("injury_type"))
        or _text(raw.get("practice_report_injury_type")),
        "probability_of_playing": _float(raw.get("probability_of_playing")),
        "practice": [_text(raw.get(f"practice_{n}")) for n in (1, 2, 3)],
        "ir_weeks": list(weeks) if isinstance(weeks, list) else [],
        "updated_at": _utc_text(raw.get("injury_update_date")),
    }


def _news_row(raw: Mapping[str, Any], table: _Players) -> dict[str, Any]:
    fp_id = _int(raw.get("player_id"))
    return {
        "id": _int(raw.get("id")),
        "created_at": _utc_text(raw.get("created")),
        "title": _text(raw.get("title")),
        "summary": _text(raw.get("desc")),
        "impact": _text(raw.get("impact")),
        "player": (
            None
            if fp_id is None
            else {"fp_id": fp_id, "name": table.name(fp_id), "espn_id": table.espn_id(fp_id)}
        ),
        "team": _text(raw.get("team_id")),
        "categories": [str(c) for c in raw.get("categories") or []],
        "url": _text(raw.get("link")),
    }


# --------------------------------------------------------------------------- #
# Small, total coercions: FantasyPros sends numbers as strings, and "" for none
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str | None:
    """``value`` as a stripped string, or ``None`` for null, empty, or blank."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    """``value`` as a finite float, or ``None``. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    """``value`` as a whole number (``"11"`` and ``11.0`` both), or ``None``."""
    number = _float(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _utc_epoch(value: Any) -> str | None:
    """Epoch seconds as ``YYYY-MM-DDTHH:MM:SSZ``; ``None`` for absent or zero."""
    from fantasy_sports.output.envelope import from_epoch_seconds, utc_timestamp

    seconds = _float(value)
    if not seconds:
        return None
    return utc_timestamp(from_epoch_seconds(seconds))


def _utc_text(value: Any) -> str | None:
    """FantasyPros' ``YYYY-MM-DD HH:MM:SS`` (its wire is UTC) as ``...Z``.

    A value in any other shape is ``None`` rather than passed through: a
    timestamp a consumer cannot parse is worse than a missing one.
    """
    from datetime import UTC

    from fantasy_sports.output.envelope import utc_timestamp

    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return utc_timestamp(parsed)


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _last(value: float | int | None, *, descending: bool = False) -> tuple[bool, float]:
    """A sort key that puts missing values last in either direction."""
    if value is None:
        return (True, 0.0)
    return (False, -float(value) if descending else float(value))


def _list(response: SourceResponse, key: str) -> list[Mapping[str, Any]]:
    return [row for row in response.payload.get(key, []) if isinstance(row, Mapping)]


def _choice(value: str, choices: tuple[str, ...], *, name: str) -> str:
    cleaned = str(value).strip().upper()
    cleaned = _POSITION_ALIASES.get(cleaned, cleaned)
    if cleaned not in choices:
        raise ConfigInvalidError(
            f"--{name} {value!r} is not recognised.",
            kind="argument",
            remediation=f"Use one of: {', '.join(choices)}.",
            details={name: value, "choices": list(choices)},
        )
    return cleaned


def _week(week: int | None, table: _Players) -> int:
    """The week asked for, else the week FantasyPros says is in progress."""
    if week is not None:
        return week
    if table.week is None or table.week < 1:
        raise ConfigInvalidError(
            "FantasyPros did not report a current week, so there is no default.",
            kind="argument",
            remediation="Pass --week with the NFL week you want.",
            details={"argument": "week"},
        )
    return table.week


def _ros_with_week() -> ConfigInvalidError:
    return ConfigInvalidError(
        "--ros reads the rest of the season, which has no week; --week and --ros disagree.",
        kind="argument",
        remediation="Drop --week to read rest-of-season, or drop --ros to read that week.",
        details={"week": "given", "ros": True},
    )
