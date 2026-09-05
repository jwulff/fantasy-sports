"""Named league profiles: read, enumerate, resolve (ARCHITECTURE §7).

John runs several leagues, so a single implicit league is useless by week two.
``~/.config/fantasy-sports/config.toml`` names them::

    default = "dynasty"

    [leagues.dynasty]
    provider  = "espn"
    league_id = "123456"
    season    = 2026
    sport     = "football"

    [leagues.redraft]
    provider  = "espn"
    league_id = "789012"
    season    = 2026
    sport     = "football"

Reading is stdlib ``tomllib``; writing is ``tomli-w``, imported inside
:func:`save` so the read path never pays for it.

This module deliberately exposes *functions*, not CLI plumbing. ``--league``
and ``--season`` are global options, but the typer wiring lands with the
command surface (jwulff/fantasy-sports#9) and projects onto
:func:`resolve_league` — nothing here imports typer.

**Error seam.** The taxonomy of ADR-0004 lands in ``core/errors.py`` with the
domain models (jwulff/fantasy-sports#3). Until then :class:`LeagueNotFoundError`
carries its own ``code`` attribute; the follow-up rebases it onto the shared
base class without changing this module's callers.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from fantasy_sports.config import paths

DEFAULT_SPORT = "football"

_REQUIRED_FIELDS = ("provider", "league_id", "season")
_PROFILE_FIELDS = (*_REQUIRED_FIELDS, "sport")


class ConfigError(Exception):
    """The config file exists but cannot be understood.

    ADR-0004's taxonomy has no code for a malformed config, and adding one is
    an API change, so ``code`` is ``None`` here. The output layer
    (jwulff/fantasy-sports#9) decides how an uncoded error is rendered.
    """

    code: ClassVar[str | None] = None


class LeagueNotFoundError(ConfigError):
    """The requested league is not configured. ADR-0004: ``LEAGUE_NOT_FOUND``."""

    code: ClassVar[str | None] = "LEAGUE_NOT_FOUND"


@dataclass(frozen=True)
class LeagueProfile:
    """One configured league. ``season`` is per-league and ``--season``-overridable."""

    name: str
    provider: str
    league_id: str
    season: int
    sport: str = DEFAULT_SPORT

    def with_season(self, season: int) -> LeagueProfile:
        """This profile aimed at a different season, leaving the original alone."""
        return replace(self, season=season)


@dataclass(frozen=True)
class LeagueConfig:
    """A parsed ``config.toml``. Immutable: enumerating it cannot change it."""

    path: Path
    default: str | None = None
    profiles_by_name: Mapping[str, LeagueProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles_by_name", MappingProxyType(dict(self.profiles_by_name)))

    def names(self) -> list[str]:
        """Every configured league name, sorted."""
        return sorted(self.profiles_by_name)

    def profiles(self) -> list[LeagueProfile]:
        """Every configured profile, sorted by name. A fresh list every call."""
        return [self.profiles_by_name[name] for name in self.names()]

    def resolve(self, name: str | None = None, season: int | None = None) -> LeagueProfile:
        """The profile ``name`` targets, or the default; ``season`` overrides its season.

        Raises :class:`LeagueNotFoundError` when the name is unknown, when no
        name was given and no default can be determined, or when nothing is
        configured at all.
        """
        key = name if name is not None else self.default
        if key is None:
            key = self._implicit_default()
        profile = self.profiles_by_name.get(key)
        if profile is None:
            raise LeagueNotFoundError(self._not_found_message(key, explicit=name is not None))
        return profile if season is None else profile.with_season(season)

    def _implicit_default(self) -> str:
        """A lone configured league is its own default; anything else is ambiguous."""
        if len(self.profiles_by_name) == 1:
            return next(iter(self.profiles_by_name))
        raise LeagueNotFoundError(
            f"no league given and no 'default' set in {self.path}; "
            f"{self._available()}. Pass --league <name>."
        )

    def _not_found_message(self, key: str, *, explicit: bool) -> str:
        source = "--league" if explicit else "the 'default' key"
        return f"no league named {key!r} ({source}) in {self.path}; {self._available()}"

    def _available(self) -> str:
        names = self.names()
        return f"configured: {', '.join(names)}" if names else "no leagues are configured"


def load(path: Path | None = None) -> LeagueConfig:
    """Parse ``path`` (default: the XDG config file). A missing file is empty, not an error."""
    path = path or paths.config_file()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LeagueConfig(path=path)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    return _parse(raw, path)


def list_leagues(path: Path | None = None) -> list[LeagueProfile]:
    """Every configured profile, sorted by name. Reads only — never writes."""
    return load(path).profiles()


def resolve_league(
    name: str | None = None,
    season: int | None = None,
    path: Path | None = None,
) -> LeagueProfile:
    """Resolve the ``--league`` / ``--season`` pair against the config file."""
    return load(path).resolve(name, season)


def save(config: LeagueConfig, path: Path | None = None) -> Path:
    """Write ``config`` as TOML, creating the config directory. Returns the path written."""
    import tomli_w

    path = path or config.path
    document: dict[str, Any] = {}
    if config.default is not None:
        document["default"] = config.default
    if config.profiles_by_name:
        document["leagues"] = {
            profile.name: {
                "provider": profile.provider,
                "league_id": profile.league_id,
                "season": profile.season,
                "sport": profile.sport,
            }
            for profile in config.profiles()
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(document), encoding="utf-8")
    return path


def _parse(raw: dict[str, Any], path: Path) -> LeagueConfig:
    # Unknown *top-level* keys are deliberately tolerated. `config.toml` is a
    # shared namespace: ARCHITECTURE §6 puts the credential fallback in this
    # same file, so a future `[auth]` section must not make this parser throw.
    # Inside `[leagues.<name>]` the namespace is ours alone, and a typo there
    # is rejected — that is where typos actually cost something.
    default = raw.get("default")
    if default is not None and not isinstance(default, str):
        raise ConfigError(f"{path}: 'default' must be a league name, got {default!r}")

    tables = raw.get("leagues", {})
    if not isinstance(tables, dict):
        raise ConfigError(f"{path}: 'leagues' must be a table of named profiles")

    profiles = {name: _profile(name, table, path) for name, table in tables.items()}
    return LeagueConfig(path=path, default=default, profiles_by_name=profiles)


def _profile(name: str, table: Any, path: Path) -> LeagueProfile:
    where = f"{path}: [leagues.{name}]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table, got {table!r}")
    _reject_unknown_fields(table, where)

    missing = [key for key in _REQUIRED_FIELDS if key not in table]
    if missing:
        raise ConfigError(f"{where} is missing required field(s): {', '.join(missing)}")

    return LeagueProfile(
        name=name,
        provider=_text(table["provider"], "provider", where),
        league_id=_league_id(table["league_id"], where),
        season=_season(table["season"], where),
        sport=_text(table.get("sport", DEFAULT_SPORT), "sport", where),
    )


def _reject_unknown_fields(table: Mapping[str, Any], where: str) -> None:
    """A typo like ``leage_id`` must fail loudly rather than be silently dropped."""
    unknown = sorted(set(table) - set(_PROFILE_FIELDS))
    if unknown:
        raise ConfigError(
            f"{where}: unrecognized field(s): {', '.join(unknown)}. "
            f"Expected: {', '.join(_PROFILE_FIELDS)}"
        )


def _text(value: Any, field_name: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{field_name}' must be a non-empty string, got {value!r}")
    return value.strip()


def _league_id(value: Any, where: str) -> str:
    """League ids are opaque strings, but TOML lets a user write ``league_id = 123456``."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: 'league_id' must be a string or integer, got {value!r}")
    if isinstance(value, int):
        return str(value)
    return _text(value, "league_id", where)


def _season(value: Any, where: str) -> int:
    """A season is a year. ``bool`` is an ``int`` in Python, so exclude it explicitly."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: 'season' must be a four-digit year, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ConfigError(f"{where}: 'season' must be a four-digit year, got {value!r}")
