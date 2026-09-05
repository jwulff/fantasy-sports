"""Credential age and staleness reporting — what `auth status` renders.

ARCHITECTURE §6 names this the differentiator: ESPN cookies expire **silently**
after weeks or months, with no refresh path, and it is the top operational
failure in this space precisely because no tool surfaces it.

**This module reports age. It does not predict expiry.** No ESPN
documentation, no `espn-api` source, and no community post reviewed for
`docs/research/03-espn-api-surface.md` states a concrete cookie lifetime. A
fabricated lifetime fails in both directions: too short and it cries wolf on a
live cookie until the warning is ignored, too long and it stays silent past a
dead one — which is the exact failure it was added to prevent. So the threshold
below is a **heuristic, labelled unverified in the payload itself** and
overridable by the user, and the number the report leads with is the measured
age, not a prediction.

Age has a second honesty constraint. It is only knowable for a credential this
tool saved, because that is the only moment we observed. A value handed to us
through the environment or hand-written into a config file has no knowable
age, and the report says so rather than reusing a `stored_at` that may belong
to a completely different value. ``last_success_at`` is the useful signal in
that case: it records the last time a credential under that name actually
worked against ESPN, which is what "is it still alive?" really asks.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from fantasy_sports.auth.chain import (
    CredentialSet,
    CredentialSource,
    ResolvedCredential,
    config_dir,
)

__all__ = [
    "DEFAULT_STALE_AFTER",
    "HEURISTIC_NOTE",
    "STALE_AFTER_ENV",
    "AuthStatus",
    "AuthState",
    "CredentialEvents",
    "CredentialStatus",
    "Freshness",
    "ThresholdSource",
    "auth_state_path",
    "build_auth_status",
    "load_auth_state",
    "record_stored",
    "record_success",
    "resolve_threshold",
    "save_auth_state",
]

DEFAULT_STALE_AFTER = timedelta(days=30)
"""Unverified heuristic. See :data:`HEURISTIC_NOTE` before changing it."""

STALE_AFTER_ENV = "FANTASY_SPORTS_STALE_AFTER_DAYS"

HEURISTIC_NOTE = (
    "Unverified heuristic, not a known ESPN cookie lifetime. ESPN publishes no "
    "expiry, and no library source or community report reviewed states a "
    f"concrete one. Override with {STALE_AFTER_ENV}."
)

STATE_FILENAME = "auth-state.json"
_STATE_VERSION = 1


class Freshness(StrEnum):
    """What `auth status` can honestly say about one credential."""

    MISSING = "missing"
    """Nothing resolved anywhere in the chain."""

    UNKNOWN = "unknown"
    """Present, but its age is not knowable — see the module docstring."""

    FRESH = "fresh"
    """Present and younger than the (unverified) staleness threshold."""

    STALE = "stale"
    """Present and older than the threshold. A warning, not a verdict."""


class ThresholdSource(StrEnum):
    DEFAULT = "default"
    ENVIRONMENT = "environment"
    ARGUMENT = "argument"


# ---------------------------------------------------------------------------
# State: when we stored a credential, and when one last worked
# ---------------------------------------------------------------------------


def auth_state_path() -> Path:
    """Where credential *metadata* lives. Never credential values.

    Sits beside ``config.toml`` rather than in the cache directory: a cache is
    something the tool may evict at will, and losing the record of when a
    cookie was stored would silently downgrade every later ``auth status`` to
    "age unknown".
    """
    return config_dir() / STATE_FILENAME


@dataclass(frozen=True)
class CredentialEvents:
    """Timestamps observed for one credential name. No value, ever."""

    stored_at: datetime | None = None
    last_success_at: datetime | None = None

    def to_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.stored_at is not None:
            payload["stored_at"] = _isoformat(self.stored_at)
        if self.last_success_at is not None:
            payload["last_success_at"] = _isoformat(self.last_success_at)
        return payload


@dataclass(frozen=True)
class AuthState:
    """The whole state file: credential name → :class:`CredentialEvents`."""

    entries: Mapping[str, CredentialEvents]

    def for_name(self, name: str) -> CredentialEvents:
        return self.entries.get(name, CredentialEvents())

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _STATE_VERSION,
            "credentials": {name: events.to_payload() for name, events in self.entries.items()},
        }


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # ARCHITECTURE §14 item 8: never let a naive datetime through — it would
    # be silently reinterpreted as host-local and skew every reported age.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def load_auth_state(path: Path | None = None) -> AuthState:
    """Read the state file. A missing or corrupt file is an empty state.

    Never raises. This is diagnostic metadata; a tool that refuses to report
    credential status because its own bookkeeping file got truncated has the
    failure mode backwards.
    """
    target = auth_state_path() if path is None else path
    try:
        document = json.loads(target.read_text())
    except (OSError, ValueError):
        return AuthState(entries={})
    credentials = document.get("credentials") if isinstance(document, dict) else None
    if not isinstance(credentials, dict):
        return AuthState(entries={})
    entries = {
        name: CredentialEvents(
            stored_at=_parse(events.get("stored_at")),
            last_success_at=_parse(events.get("last_success_at")),
        )
        for name, events in credentials.items()
        if isinstance(events, dict)
    }
    return AuthState(entries=entries)


def save_auth_state(state: AuthState, path: Path | None = None) -> Path:
    """Write the state file atomically, owner-readable only."""
    target = auth_state_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".auth-state-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(state.to_payload(), stream, indent=2, sort_keys=True)
            stream.write("\n")
        temp.chmod(0o600)
        temp.replace(target)
    except BaseException:  # pragma: no cover - defensive cleanup
        temp.unlink(missing_ok=True)
        raise
    return target


def _touch(
    names: Iterable[str],
    *,
    field: str,
    now: datetime | None,
    path: Path | None,
) -> AuthState:
    moment = now or datetime.now(UTC)
    state = load_auth_state(path)
    entries = dict(state.entries)
    for name in names:
        existing = entries.get(name, CredentialEvents())
        entries[name] = (
            CredentialEvents(stored_at=moment, last_success_at=existing.last_success_at)
            if field == "stored_at"
            else CredentialEvents(stored_at=existing.stored_at, last_success_at=moment)
        )
    updated = AuthState(entries=entries)
    save_auth_state(updated, path)
    return updated


def record_stored(
    names: Iterable[str], *, now: datetime | None = None, path: Path | None = None
) -> AuthState:
    """Record that these credentials were saved now. Called by ``auth login``."""
    return _touch(names, field="stored_at", now=now, path=path)


def record_success(
    names: Iterable[str], *, now: datetime | None = None, path: Path | None = None
) -> AuthState:
    """Record that these credentials just authenticated successfully.

    The provider adapter calls this after any authenticated call that came
    back 200. It is the only evidence a cookie is *actually* still alive, and
    it is the signal ``auth status`` leads with when age is unknowable.
    """
    return _touch(names, field="last_success_at", now=now, path=path)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def resolve_threshold(
    threshold: timedelta | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[timedelta, ThresholdSource]:
    """Pick the staleness threshold and say where it came from.

    Argument beats environment beats the default heuristic. An unparseable or
    non-positive override falls back to the default rather than raising: a bad
    number in a shell profile should not stop `auth status` from reporting the
    presence and age it already knows.
    """
    if threshold is not None:
        return threshold, ThresholdSource.ARGUMENT
    env = os.environ if environ is None else environ
    raw = env.get(STALE_AFTER_ENV)
    if raw:
        try:
            days = float(raw)
        except ValueError:
            days = 0.0
        if days > 0:
            return timedelta(days=days), ThresholdSource.ENVIRONMENT
    return DEFAULT_STALE_AFTER, ThresholdSource.DEFAULT


@dataclass(frozen=True)
class CredentialStatus:
    """One credential's line in `auth status`. Carries no value."""

    name: str
    present: bool
    source: CredentialSource | None
    age: timedelta | None
    age_basis: str
    stored_at: datetime | None
    last_success_at: datetime | None
    freshness: Freshness

    @property
    def age_days(self) -> float | None:
        return None if self.age is None else round(self.age.total_seconds() / 86400, 2)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "present": self.present,
            "source": self.source.value if self.source else None,
            "age_days": self.age_days,
            "age_basis": self.age_basis,
            "stored_at": _isoformat(self.stored_at) if self.stored_at else None,
            "last_success_at": (_isoformat(self.last_success_at) if self.last_success_at else None),
            "freshness": self.freshness.value,
        }


@dataclass(frozen=True)
class AuthStatus:
    """The whole `auth status` payload."""

    credentials: tuple[CredentialStatus, ...]
    threshold: timedelta
    threshold_source: ThresholdSource
    generated_at: datetime

    @property
    def threshold_verified(self) -> bool:
        """Always ``False``. There is no verified ESPN cookie lifetime."""
        return False

    @property
    def complete(self) -> bool:
        return all(status.present for status in self.credentials)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Human-readable warnings, in the order a user should act on them."""
        notes: list[str] = []
        for status in self.credentials:
            if not status.present:
                notes.append(f"{status.name}: not configured. Run `fantasy-sports auth login`.")
            elif status.freshness is Freshness.STALE:
                notes.append(
                    f"{status.name}: stored {status.age_days} days ago, past the "
                    f"{self.threshold.days}-day staleness heuristic. "
                    "If reads start failing, re-extract the cookie. " + HEURISTIC_NOTE
                )
        return tuple(notes)

    def to_payload(self) -> dict[str, object]:
        return {
            "generated_at": _isoformat(self.generated_at),
            "complete": self.complete,
            "credentials": [status.to_payload() for status in self.credentials],
            "staleness_threshold": {
                "days": round(self.threshold.total_seconds() / 86400, 2),
                "source": self.threshold_source.value,
                "verified": self.threshold_verified,
                "note": HEURISTIC_NOTE,
            },
            "warnings": list(self.warnings),
        }


def build_auth_status(
    credentials: CredentialSet,
    *,
    state: AuthState | None = None,
    state_path: Path | None = None,
    now: datetime | None = None,
    threshold: timedelta | None = None,
    environ: Mapping[str, str] | None = None,
) -> AuthStatus:
    """Assemble the `auth status` report for an already-resolved credential set.

    Takes a resolved :class:`~fantasy_sports.auth.chain.CredentialSet` rather
    than resolving one itself, so the report and the thing it reports on can
    never disagree, and so a caller can render status for credentials it has
    already loaded without a second Keychain round-trip.
    """
    moment = now or datetime.now(UTC)
    loaded = load_auth_state(state_path) if state is None else state
    limit, limit_source = resolve_threshold(threshold, environ)

    names = list(credentials.resolved) + list(credentials.missing)
    rows: list[CredentialStatus] = []
    for name in sorted(names):
        resolved = credentials.get(name)
        events = loaded.for_name(name)
        rows.append(_status_for(name, resolved, events, moment, limit))
    return AuthStatus(
        credentials=tuple(rows),
        threshold=limit,
        threshold_source=limit_source,
        generated_at=moment,
    )


def _status_for(
    name: str,
    resolved: ResolvedCredential | None,
    events: CredentialEvents,
    now: datetime,
    limit: timedelta,
) -> CredentialStatus:
    if resolved is None:
        return CredentialStatus(
            name=name,
            present=False,
            source=None,
            age=None,
            age_basis="absent",
            stored_at=events.stored_at,
            last_success_at=events.last_success_at,
            freshness=Freshness.MISSING,
        )

    source = resolved.source
    # ``stored_at`` describes the value *this tool wrote to the Keychain*. If
    # the chain resolved from the environment or a config file, that value may
    # be a completely different cookie, and attributing our timestamp to it
    # would report a confident, wrong age — worse than reporting none.
    knowable = source is CredentialSource.KEYCHAIN and events.stored_at is not None
    if knowable:
        assert events.stored_at is not None  # narrowed by `knowable`
        age = now - events.stored_at
        freshness = Freshness.STALE if age > limit else Freshness.FRESH
        basis = "stored_at"
    else:
        age = None
        freshness = Freshness.UNKNOWN
        basis = "unknown" if source is CredentialSource.KEYCHAIN else f"not-tracked:{source.value}"
    return CredentialStatus(
        name=name,
        present=True,
        source=source,
        age=age,
        age_basis=basis,
        stored_at=events.stored_at,
        last_success_at=events.last_success_at,
        freshness=freshness,
    )
