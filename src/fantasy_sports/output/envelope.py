"""The versioned output envelope — ``docs/ARCHITECTURE.md`` §5, ADR-0004.

``CLAUDE.md`` rule 4 calls this contract the product, and it means it: the first
consumer is a weekly newspaper generator (``jwulff/league-gazette``) that shells
out to this CLI, reads stdout, and parses what it finds. Everything here is
designed for a program, not for a person.

**One shape, success or failure.** Both carry the identical key set; ``data``
and ``error`` are the discriminator, and exactly one of them is non-null. A
consumer that has to branch on which *keys exist* before it can branch on what
happened is a consumer that will get it wrong once.

**The untrusted container is reserved now, empty.** Team names, trade notes and
message-board text are attacker-influenceable — any league member sets them, and
they reach an agent that can write. jwulff/fantasy-sports#17 labels them.
Reserving the container here rather than adding it there is the difference
between #17 being a provider change and #17 being a schema-version bump on the
one contract every consumer parses.

**Timestamps are UTC or they are refused.** ``espn-api`` builds its datetimes
with ``datetime.fromtimestamp()`` and no ``tz=``, so they are naive and
host-local: the same league renders a different kickoff time on a laptop in
Seattle and a CI runner in UTC, with nothing anywhere reporting an error.
:func:`utc_timestamp` therefore raises on a naive datetime instead of guessing,
and :func:`from_epoch_millis` is the re-derivation an adapter is expected to
use. Refusing is the only version of this rule that cannot be forgotten.

Nothing here imports anything beyond the standard library and ``core/``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fantasy_sports.core.errors import FantasySportsError
from fantasy_sports.core.models import ProviderObject

__all__ = [
    "SCHEMA",
    "DataSource",
    "Envelope",
    "NaiveDatetimeError",
    "from_epoch_millis",
    "from_epoch_seconds",
    "utc_now",
    "utc_timestamp",
]

SCHEMA = "fantasy-sports/v1"
"""The contract version. Every payload carries it; changing it is an API change."""

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""Second precision with a literal ``Z``, exactly as ADR-0004 documents it.

Sub-second precision would buy a consumer nothing — data ages are reported in
whole seconds — and would make every golden file and every fixture comparison
depend on a clock's resolution.
"""


class NaiveDatetimeError(ValueError):
    """A datetime with no timezone reached the output contract.

    Not a taxonomy code: this is a programming error in an adapter, not a
    failure a caller can act on. It is loud on purpose — the alternative is a
    timestamp that is silently wrong by the host's UTC offset.
    """


def utc_now() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


def from_epoch_millis(value: int | float) -> datetime:
    """Re-derive a UTC datetime from epoch **milliseconds**.

    This is the conversion an adapter must use for every ESPN date. ESPN sends
    epoch milliseconds; ``espn-api`` converts them with
    ``datetime.fromtimestamp(ms / 1000)`` and no ``tz=``, which bakes in the
    host's timezone. Passing that result through would make the envelope read
    differently on every machine.
    """
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def from_epoch_seconds(value: int | float) -> datetime:
    """Re-derive a UTC datetime from epoch **seconds**."""
    return datetime.fromtimestamp(value, tz=UTC)


def _require_aware(value: datetime) -> datetime:
    """The single naive-datetime gate. Every path into the contract goes here.

    One function rather than a check at each use site, because the guarantee
    must not depend on which field a renderer happens to evaluate first.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            "Refusing to render a naive datetime: it carries no timezone, so it "
            "would be wrong by the host's UTC offset on every machine but one. "
            "Re-derive it from the raw epoch value with "
            "fantasy_sports.output.envelope.from_epoch_millis()."
        )
    return value


def utc_timestamp(value: datetime) -> str:
    """Render ``value`` as ``YYYY-MM-DDTHH:MM:SSZ``, converting to UTC.

    :raises NaiveDatetimeError: if ``value`` has no ``tzinfo``. There is no
        correct guess: assuming UTC corrupts a host-local value silently, and
        assuming local time makes the output host-dependent.
    """
    return _require_aware(value).astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _age_seconds(fetched_at: datetime, now: datetime) -> int:
    """Whole seconds between ``fetched_at`` and ``now``, floored at zero.

    Clamped because a clock skew between a cache write and a render must not
    hand a consumer a negative age it has to write a guard for.
    """
    return max(0, int((now - _require_aware(fetched_at)).total_seconds()))


def _plain(value: Any) -> Any:
    """Coerce ``value`` into something JSON, a table, and CSV can all carry.

    Refuses rather than falls back to ``str()``. A stringified object is a
    value a consumer cannot parse and cannot detect, which is worse than a
    loud failure at render time.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, datetime):
        return utc_timestamp(value)
    if isinstance(value, ProviderObject):
        return _plain(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    raise TypeError(
        f"The output contract cannot represent a {type(value).__name__}. "
        "Normalize it in the provider adapter, or put it in `raw` as plain JSON."
    )


@dataclass(frozen=True)
class DataSource:
    """One upstream fetch that contributed to a payload (origin R4).

    ``name`` is the request descriptor — an ESPN view name such as ``mTeam``,
    not a URL. A URL can carry an ``espn_s2`` value in a query parameter, and
    the envelope is written to stdout where a consumer may log it.
    """

    name: str
    fetched_at: datetime
    cached: bool = False

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "name": self.name,
            "fetched_at": utc_timestamp(self.fetched_at),
            "age_seconds": _age_seconds(self.fetched_at, now),
            "cached": self.cached,
        }


@dataclass(frozen=True)
class Envelope:
    """The wrapper around every payload and every failure.

    Build one with :meth:`success` or :meth:`failure` rather than by hand; the
    two constructors are what keep ``data`` and ``error`` mutually exclusive.

    ``data`` is a list for a collection command and a mapping for a
    single-object one. Both render; the renderers do not guess which they were
    given, and no renderer reaches inside ``data`` looking for the "real" rows.
    """

    provider: str | None = None
    league_id: str | None = None
    season: int | None = None
    data: Any = None
    error: FantasySportsError | None = None
    sources: tuple[DataSource, ...] = ()
    generated_at: datetime = field(default_factory=utc_now)
    untrusted: Mapping[str, str] = field(default_factory=dict)
    """Path -> attacker-influenceable string. Empty until #17 populates it."""

    @classmethod
    def success(
        cls,
        *,
        provider: str | None = None,
        data: Any = None,
        league_id: str | None = None,
        season: int | None = None,
        sources: Sequence[DataSource] = (),
        generated_at: datetime | None = None,
        untrusted: Mapping[str, str] | None = None,
    ) -> Envelope:
        """Wrap a payload."""
        return cls(
            provider=provider,
            league_id=league_id,
            season=season,
            data=data,
            error=None,
            sources=tuple(sources),
            generated_at=generated_at or utc_now(),
            untrusted=dict(untrusted or {}),
        )

    @classmethod
    def failure(
        cls,
        error: FantasySportsError,
        *,
        provider: str | None = None,
        league_id: str | None = None,
        season: int | None = None,
        generated_at: datetime | None = None,
        untrusted: Mapping[str, str] | None = None,
    ) -> Envelope:
        """Wrap a failure.

        No ``sources``: there is no payload, so there is nothing whose age
        could honestly be reported. ``provider`` stays optional because a
        ``CONFIG_INVALID`` failure happens before a provider is chosen.
        """
        return cls(
            provider=provider,
            league_id=league_id,
            season=season,
            data=None,
            error=error,
            sources=(),
            generated_at=generated_at or utc_now(),
            untrusted=dict(untrusted or {}),
        )

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        """The exact JSON object a consumer parses. Key order is part of the contract."""
        now = self.generated_at
        sources = [source.to_dict(now) for source in self.sources]
        oldest = min(self.sources, key=lambda s: s.fetched_at, default=None)
        return {
            "schema": SCHEMA,
            "provider": self.provider,
            "league_id": self.league_id,
            "season": self.season,
            "generated_at": utc_timestamp(now),
            "data_as_of": utc_timestamp(oldest.fetched_at) if oldest else None,
            "data_age_seconds": _age_seconds(oldest.fetched_at, now) if oldest else None,
            "sources": sources,
            "untrusted": {str(key): str(value) for key, value in self.untrusted.items()},
            "data": _plain(self.data),
            "error": self.error.to_dict() if self.error is not None else None,
        }
