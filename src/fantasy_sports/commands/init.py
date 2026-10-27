"""``init`` -- one command from a fresh install to a first league read.

Before this, a first run was three commands and a hand-edited ``config.toml``:
``auth login --from-browser`` (#156), ``leagues discover --config-snippet``
(#157), paste the snippets, then ``doctor``. ``init`` composes exactly those and
reimplements none of them.

1. **Credentials.** The browser import runs through ``auth login`` 's own logic.
   If no browser profile holds both cookies and stdin is a terminal, the
   interactive ``auth login`` prompt runs instead; without a terminal it fails
   ``AUTH_MISSING``, because a prompt nobody can answer would hang a cron job or
   an agent.
2. **Discovery.** ``fan.api`` names the account's leagues from the stored SWID.
3. **Config.** One ``[leagues.<slug>]`` table per league of the current season
   is added to ``config.toml``. Nothing is ever clobbered: a table for the same
   ``league_id`` and ``season`` is left alone and reported ``skipped``, every
   other key survives (``docs/memory/config-toml-is-a-shared-namespace.md``), and
   an existing ``default`` is never changed.
4. **Doctor.** The offline health checks run last and their result is included.
   Not on a dry run: the cache and health checks create their files, so a dry
   run reports ``doctor: null`` rather than write.

The *current season* is the latest season among the leagues in a sport this
version can read. It is derived from what ESPN lists rather than from the clock,
so the answer does not change on a boundary date. ``--season`` overrides it and
``--all-seasons`` takes every season listed.

``--dry-run`` writes nothing: not ``config.toml``, and not the Keychain either.
It reads the cookies from the browser (or the already-stored ones) so discovery
can run, and reports what it would have written.

The envelope names where the credentials came from and never holds a value; the
SWID reaches ``fetch_leagues`` and nothing else.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import require_shape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.config.leagues import LeagueConfig
    from fantasy_sports.core.models import DiscoveredLeague
    from fantasy_sports.output.envelope import Envelope

__all__ = ["init"]

PROVIDER = "espn"

SKIP_EXISTS = "already-configured"
SKIP_SPORT = "unsupported-sport"


def init(
    *, season: int | None = None, all_seasons: bool = False, dry_run: bool = False
) -> Envelope:
    """Import credentials, discover leagues, write ``config.toml``, run the doctor."""
    from fantasy_sports.config import leagues as league_config
    from fantasy_sports.core.errors import ConfigInvalidError
    from fantasy_sports.output.envelope import DataSource, Envelope, utc_now
    from fantasy_sports.providers import espn_fan

    if season is not None and all_seasons:
        raise ConfigInvalidError(
            "--season and --all-seasons cannot be combined.",
            kind="argument",
            remediation="Pass one season with --season, or every listed season with --all-seasons.",
        )

    # Parsed before anything is stored or fetched: a config that cannot be read
    # must fail before the Keychain has been touched.
    config = league_config.load()
    credentials, swid = _credentials(dry_run=dry_run)
    leagues = _discover(swid)
    fetched_at = utc_now()
    plan = _plan(config, leagues, season=season, all_seasons=all_seasons)
    path = _write(plan, dry_run=dry_run)

    data: dict[str, Any] = {
        "dry_run": dry_run,
        "credentials": credentials,
        "leagues": {"written": plan.written, "skipped": plan.skipped},
        "default": plan.default,
        "next_step": plan.next_step,
        "config_path": str(path),
        "doctor": None if dry_run else _doctor(),
    }
    require_shape(data, command="init")
    return Envelope.success(
        provider=PROVIDER,
        data=data,
        sources=[DataSource(name=espn_fan.SOURCE_NAME, fetched_at=fetched_at)],
    )


# --------------------------------------------------------------------------- #
# 1. Credentials
# --------------------------------------------------------------------------- #


def _stdin_is_tty() -> bool:
    """Whether a person can answer a prompt. A closed or odd stdin is "no"."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _credentials(*, dry_run: bool) -> tuple[dict[str, Any], str]:
    """Get the cookies stored (or, on a dry run, merely found); return the SWID.

    The report names a source and nothing secret. After a real run the SWID is
    resolved through the credential chain, as ``leagues discover`` does, so it is
    the value every later command will use. A dry run stores nothing, so it uses
    the SWID of the profile it found (the chain would answer with a different
    account's, or nothing); only with no browser profile does it fall back to the
    stored one.
    """
    if not dry_run:
        return _store_credentials(), _swid()
    report, found = _dry_run_credentials()
    return report, found or _swid()


def _store_credentials() -> dict[str, Any]:
    from fantasy_sports.commands import FROM_BROWSER_ANY
    from fantasy_sports.commands import auth as auth_commands
    from fantasy_sports.core.errors import AuthMissingError

    try:
        imported = auth_commands.login(from_browser=FROM_BROWSER_ANY)
    except AuthMissingError as missing:
        if not _stdin_is_tty():
            raise AuthMissingError(
                "No browser profile holds both ESPN cookies, and there is no terminal to "
                "prompt on. Nothing was stored.",
                remediation=(
                    "Log into fantasy.espn.com in a supported browser and run "
                    "`fantasy-sports init` again, or run `fantasy-sports auth login` in a "
                    "terminal to paste the cookies, or import them from the clipboard with "
                    "`fantasy-sports auth login --from-clipboard`. " + (missing.remediation or "")
                ).strip(),
                details=dict(missing.details or {}),
            ) from None
        auth_commands.login()
        return {"source": "prompt", "stored": True}
    source = imported.data["source"]
    return {
        "source": "browser",
        "browser": source["browser"],
        "profile": source["profile"],
        "stored": True,
    }


def _dry_run_credentials() -> tuple[dict[str, Any], str | None]:
    """Look, don't store: browser cookies if there are any, else what is stored.

    Returns the report and the found profile's SWID (``None`` for "use the stored one").
    """
    from datetime import UTC, datetime

    from fantasy_sports.auth import browser
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, require_credentials

    found = browser.find_espn_cookies(None, None).complete
    if found:
        floor = datetime.min.replace(tzinfo=UTC)
        chosen = max(found, key=lambda profile: profile.espn_s2_expires_at or floor)
        assert chosen.swid is not None  # `complete`
        report = {
            "source": "browser",
            "browser": chosen.browser,
            "profile": chosen.profile,
            "stored": False,
        }
        return report, chosen.swid.reveal()
    # Raises AUTH_MISSING when nothing is stored either; a dry run never prompts,
    # because the answer would have to be stored to be used.
    require_credentials(ESPN_CREDENTIALS)
    return {"source": "stored", "stored": False}, None


def _swid() -> str:
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, require_credentials

    swid_spec = next(spec for spec in ESPN_CREDENTIALS if spec.name == "swid")
    return require_credentials((swid_spec,)).reveal("swid")


# --------------------------------------------------------------------------- #
# 2. Discovery
# --------------------------------------------------------------------------- #


def _discover(swid: str) -> list[DiscoveredLeague]:
    from fantasy_sports.providers import espn_fan

    return espn_fan.fetch_leagues(swid)


# --------------------------------------------------------------------------- #
# 3. Config
# --------------------------------------------------------------------------- #


class _Plan:
    """What a run would add to ``config.toml``."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.default: str | None = None
        self.next_step: str | None = None

    def tables(self) -> dict[str, dict[str, Any]]:
        return {
            row["slug"]: {
                "provider": row["provider"],
                "league_id": row["league_id"],
                "season": row["season"],
                "sport": row["sport"],
            }
            for row in self.written
        }


def _row(slug: str | None, league: DiscoveredLeague, **extra: Any) -> dict[str, Any]:
    """A report row. The league *name* is deliberately absent: it is free text."""
    row: dict[str, Any] = {
        "slug": slug,
        "provider": league.provider,
        "league_id": league.league_id,
        "season": league.season,
        "sport": league.sport,
    }
    row.update(extra)
    return row


def _plan(
    config: LeagueConfig,
    leagues: list[DiscoveredLeague],
    *,
    season: int | None,
    all_seasons: bool,
) -> _Plan:
    from fantasy_sports.commands.leagues import slugs
    from fantasy_sports.providers.espn_fan import READABLE_SPORTS

    plan = _Plan()
    readable = [league for league in leagues if league.sport in READABLE_SPORTS]
    unreadable = [league for league in leagues if league.sport not in READABLE_SPORTS]

    if season is not None:
        readable = [league for league in readable if league.season == season]
        unreadable = [league for league in unreadable if league.season == season]
    elif not all_seasons and readable:
        current = max(league.season for league in readable)
        readable = [league for league in readable if league.season == current]

    for league in unreadable:
        plan.skipped.append(_row(None, league, reason=SKIP_SPORT))

    taken = set(config.profiles_by_name)
    existing = {
        (profile.provider, profile.sport, profile.league_id, profile.season): profile.name
        for profile in config.profiles()
    }
    for league, base in zip(readable, slugs(readable), strict=True):
        key = (league.provider, league.sport, league.league_id, league.season)
        if key in existing:
            plan.skipped.append(_row(existing[key], league, reason=SKIP_EXISTS))
            continue
        slug, count = base, 1
        while slug in taken:
            count += 1
            slug = f"{base}-{count}"
        taken.add(slug)
        plan.written.append(_row(slug, league))

    # An explicit default is never changed. Without one, the file's target must
    # not move or become ambiguous: a lone existing profile is the implied
    # default, so it is made explicit before a second profile arrives; a lone
    # new league becomes the default only on a file with no leagues at all.
    if config.default is None and plan.written:
        existing_names = config.names()
        if not existing_names and len(plan.written) == 1:
            plan.default = plan.written[0]["slug"]
        elif len(existing_names) == 1:
            plan.default = existing_names[0]
        elif not existing_names:
            first = plan.written[0]["slug"]
            plan.next_step = (
                f'No default is set: add `default = "{first}"` to config.toml, '
                "or pass --league <name> to each command."
            )
    return plan


def _write(plan: _Plan, *, dry_run: bool):
    from fantasy_sports.config.document import read_document, write_atomically
    from fantasy_sports.config.paths import config_file

    path = config_file()
    if dry_run or not plan.written:
        return path
    document = read_document(path)
    leagues = document.get("leagues")
    if not isinstance(leagues, dict):
        leagues = {}
    leagues.update(plan.tables())
    document["leagues"] = leagues
    if plan.default is not None:
        document["default"] = plan.default
    write_atomically(path, document)
    return path


# --------------------------------------------------------------------------- #
# 4. Doctor
# --------------------------------------------------------------------------- #


def _doctor() -> dict[str, Any]:
    from fantasy_sports.commands.doctor import doctor

    report = doctor(live=False).data
    return {
        "status": report["status"],
        "ok": report["ok"],
        "checks": [
            {"name": check["name"], "status": check["status"], "summary": check["summary"]}
            for check in report["checks"]
        ],
    }
