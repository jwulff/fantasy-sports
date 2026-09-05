"""The normalized domain model — ADR-0002 as amended, origin R2/R3.

**Normalize shape, not semantics.** Teams, rosters, standings, matchups,
transactions, and free agents get a shared shape. Scoring settings, draft
logic, playoff formats, and cross-provider player identity explicitly do not —
they are reachable only through ``raw``. Every object here carries
``provider``, ``provider_id``, and ``raw`` so nothing an adapter saw is ever
lost.

ADR-0002's amendment matters for reading this file: normalization is selected
for **legibility per provider**, not for the cross-provider intersection. A
field may exist because ESPN exposes it usefully even where Sleeper would
leave it empty. Every such field is optional, and *absence is never drift*.

Three shapes here exist because of specific, documented traps
(``docs/research/02-provider-data-shapes.md`` §§4, 6):

* :attr:`Team.owner_names` is plural. ESPN's ``owners`` is a list from day one
  and Yahoo's ``managers`` carries an ``is_comanager`` flag; a single
  ``owner_name`` silently loses a manager in every co-managed league.
* :class:`Matchup` is symmetric (``team_a``/``team_b``), not home/away. Only
  ESPN has that distinction, and the other two would have to fake it.
* :class:`Matchup` surfaces *both* period identifiers. ESPN's
  ``matchupPeriodId`` and ``scoringPeriodId`` diverge during playoff weeks, and
  an adapter that keeps only one has already lost the information. The
  untouched provider values stay in ``raw`` as well.

Slot eligibility **is** modelled, on :class:`Player`. ARCHITECTURE §14's
finding 13 excluded it; origin R3 voids that finding, because an agent cannot
construct a legal lineup from position alone. Providers that do not publish
eligibility as player data (Sleeper computes it client-side; Yahoo reports
only ``is_flex`` for the current slot) leave the tuple empty rather than
synthesising conventions their API never states.

Nothing here imports anything beyond the standard library, and nothing here is
allowed to import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar, Final, Self

from fantasy_sports.core.errors import SchemaDriftError

__all__ = [
    "NORMALIZED_MODELS",
    "TRANSACTION_TYPES",
    "CredentialSpec",
    "FreeAgent",
    "League",
    "Matchup",
    "Player",
    "ProviderObject",
    "RosterSlot",
    "Team",
    "Transaction",
]

#: The narrowed, lossy, honest transaction vocabulary (research §4). ESPN's
#: ``TRADE_VETO`` / ``TRADE_PROPOSAL`` / ``WAIVER_ERROR`` states have no
#: normalized equivalent and are readable only via ``raw``.
TRANSACTION_TYPES: Final[tuple[str, ...]] = ("add", "drop", "trade", "waiver_claim")


class ProviderObject:
    """Mixin giving every normalized object its construction contract.

    Subclasses are frozen dataclasses. A field with no default is *required*;
    a field with a default is optional and its absence is ordinary provider
    data, not drift.

    :meth:`from_payload` takes a mapping keyed by **our** field names — the
    adapter has already translated the provider's own key names — so this
    layer stays free of ESPN vocabulary while still refusing to let a
    ``KeyError`` escape when an expected value stops arriving.
    """

    #: field name -> nested model class, coerced from a mapping on construction
    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {}
    #: field names normalized to a tuple so a frozen object is really immutable
    _SEQUENCES: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        for name in self._SEQUENCES:
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Build a model from a normalized payload mapping.

        Raises :class:`~fantasy_sports.core.errors.SchemaDriftError` — never a
        bare ``KeyError`` — when a required field is absent. Unknown keys are
        ignored: an upstream *addition* is not drift, and failing on one would
        break the tool every time ESPN ships a new field.
        """
        if not isinstance(payload, Mapping):
            raise SchemaDriftError(
                f"{cls.__name__} payload is {type(payload).__name__}, not a mapping",
                path=[cls.__name__],
            )

        values: dict[str, Any] = {}
        missing: list[str] = []
        for f in fields(cls):  # type: ignore[arg-type]  # always a dataclass subclass
            if f.name in payload:
                values[f.name] = cls._coerce(f.name, payload[f.name])
            elif f.default is MISSING and f.default_factory is MISSING:
                missing.append(f.name)

        if missing:
            provider = payload.get("provider")
            raise SchemaDriftError(
                f"{cls.__name__} payload is missing required field(s): {', '.join(missing)}",
                path=[f"{cls.__name__}.{name}" for name in missing],
                provider=provider if isinstance(provider, str) else None,
            )
        return cls(**values)

    @classmethod
    def _coerce(cls, name: str, value: Any) -> Any:
        nested = cls._NESTED.get(name)
        if nested is not None and isinstance(value, Mapping):
            return nested.from_payload(value)
        return value

    def to_dict(self) -> dict[str, Any]:
        """A plain-Python view for the output layer.

        Nested models expand, tuples become lists, and ``raw`` is passed
        through by reference — it is the provider's payload, and copying it
        would be both wasteful and a chance to alter it.
        """
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]  # always a dataclass subclass
            value = getattr(self, f.name)
            if isinstance(value, ProviderObject):
                value = value.to_dict()
            elif isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return out


@dataclass(frozen=True)
class League(ProviderObject):
    """A league as every provider describes it."""

    provider: str
    provider_id: str
    """The provider's native league id, always a string — Yahoo's own client
    stringifies it to survive leading zeros, which ESPN's integer ids never
    have."""
    name: str
    season: int
    sport: str
    team_count: int
    current_week: int
    raw: Mapping[str, Any]
    roster_slots: Mapping[str, int] = field(default_factory=dict)
    """Slot name -> count, e.g. ``{"QB": 1, "RB": 2, "RB/WR/TE": 1, "BE": 7}``.

    R3a: a legal target lineup must be constructible from normalized output
    alone. ESPN publishes slot counts, Sleeper a repeated ``roster_positions``
    array, Yahoo position/count pairs — all three flatten to this mapping.
    Empty when a provider does not expose it."""


@dataclass(frozen=True)
class Team(ProviderObject):
    """A team and its record."""

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"owner_names"})

    provider: str
    provider_id: str
    name: str
    owner_names: tuple[str, ...]
    """Plural, always. Stored as a tuple because the object is frozen — a list
    field would be mutable in place and would undercut the guarantee."""
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    raw: Mapping[str, Any]
    standing: int | None = None
    """The provider's own rank where it has one. ESPN and Sleeper compute
    standings client-side through a multi-rule tiebreaker cascade; Yahoo
    returns one server-side. Each adapter owns its own tiebreaker source —
    there is deliberately no shared standings algorithm in ``core/``."""


@dataclass(frozen=True)
class Player(ProviderObject):
    """A player, with the context R3 requires to make a lineup decision.

    ``provider_id`` is the provider's own player id and is **not** portable
    across providers, or even across sports within ESPN. Cross-provider player
    identity is explicitly out of scope (ADR-0002); a crosswalk is deferred to
    a future ADR.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"eligible_slots"})

    provider: str
    provider_id: str
    name: str
    position: str
    """The provider's own position string, not normalized further. ESPN's
    ``RB/WR``, Sleeper's ``FLEX`` and Yahoo's flex boolean genuinely disagree."""
    raw: Mapping[str, Any]
    eligible_slots: tuple[str, ...] = ()
    """Slots this player may fill, in the provider's own slot vocabulary (R3).
    Empty where the provider does not publish eligibility as player data."""
    status: str | None = None
    injury_status: str | None = None
    pro_team: str | None = None
    opponent: str | None = None
    projected_points: float | None = None
    kickoff: datetime | None = None
    """Timezone-aware, always. ``espn-api`` hands back naive host-local
    datetimes; an adapter must re-derive this from the raw epoch value."""


@dataclass(frozen=True)
class RosterSlot(ProviderObject):
    """A player *in a roster context* — occupancy, not a bare player."""

    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {"player": Player}

    provider: str
    provider_id: str
    """The player's provider id; a slot has no id of its own."""
    player: Player
    slot: str
    """The lineup slot this player currently occupies."""
    is_starter: bool
    """Every provider gives some way to derive this: ESPN via a lineup slot
    that is not ``BE``/``IR``, Sleeper via membership of ``starters``, Yahoo
    via a selected position that is not ``BN``."""
    raw: Mapping[str, Any]
    is_locked: bool | None = None
    """Whether the slot can still be changed (R3). ``None`` where the provider
    does not report lock state."""

    @property
    def player_provider_id(self) -> str:
        """Alias for :attr:`provider_id`, for adapters reading research §3."""
        return self.provider_id


@dataclass(frozen=True)
class Matchup(ProviderObject):
    """One head-to-head pairing, symmetric by design."""

    provider: str
    provider_id: str
    """Synthesised by the adapter where the provider has no matchup id."""
    week: int
    """The **provider's matchup week**, not necessarily the NFL week."""
    team_a_provider_id: str
    team_a_score: float
    team_b_provider_id: str
    team_b_score: float
    is_playoff: bool
    raw: Mapping[str, Any]
    scoring_period_id: int | None = None
    """ESPN's scoring period. Diverges from :attr:`matchup_period_id` when a
    playoff matchup spans more than one scoring week."""
    matchup_period_id: int | None = None
    """ESPN's matchup period. Equal to :attr:`scoring_period_id` in the regular
    season, which is exactly why the split is easy to miss."""


@dataclass(frozen=True)
class Transaction(ProviderObject):
    """A roster move, narrowed to a four-value vocabulary.

    The narrowing is lossy and honest: filtering on ``type == "trade"`` will
    not show a proposed-then-declined trade, because ESPN's ``TRADE_PROPOSAL``
    and ``TRADE_DECLINE`` have no normalized equivalent. Read ``raw`` for
    those. An ESPN adapter must reconcile ``mTransactions2`` *and*
    ``kona_league_communication`` before building these.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"players_in", "players_out"})

    provider: str
    provider_id: str
    type: str
    raw: Mapping[str, Any]
    team_provider_id: str | None = None
    players_in: tuple[str, ...] = ()
    """Player provider ids gained by :attr:`team_provider_id`."""
    players_out: tuple[str, ...] = ()
    faab_spent: int | None = None
    timestamp: datetime | None = None
    """When the provider processed the move. ``None`` when the payload carries
    neither a processed nor a proposed date — that is ordinary ESPN data and
    must not be reported as drift."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.type not in TRANSACTION_TYPES:
            raise SchemaDriftError(
                f"Transaction type {self.type!r} is outside the normalized vocabulary "
                f"{TRANSACTION_TYPES}; provider-specific states belong in `raw`",
                path=["Transaction.type"],
                provider=self.provider,
            )


@dataclass(frozen=True)
class FreeAgent(ProviderObject):
    """An unrostered player, plus ownership where the provider tracks it."""

    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {"player": Player}

    provider: str
    provider_id: str
    player: Player
    raw: Mapping[str, Any]
    percent_owned: float | None = None
    """Present on ESPN and Yahoo; ``None`` on Sleeper, which does not track
    per-player ownership the same way."""


@dataclass(frozen=True)
class CredentialSpec:
    """What a provider needs to authenticate, and how staleness is detected.

    Returned by ``Provider.credential_specs()``. A provider needing no
    credentials at all — Sleeper — returns an empty list, which is itself a
    useful signal about that provider's operational risk profile.

    :attr:`staleness` describes *how expiry becomes visible*, not a predicted
    lifetime. No ESPN documentation, library source, or community post states
    a concrete cookie lifetime, so a fabricated one would either cry wolf on a
    live cookie or stay silent past a dead one.
    """

    name: str
    label: str
    secret: bool = True
    required: bool = True
    staleness: str | None = None


#: Every normalized provider object. ``CredentialSpec`` is deliberately absent:
#: it describes a provider, not a thing a provider returned.
NORMALIZED_MODELS: Final[tuple[type[ProviderObject], ...]] = (
    League,
    Team,
    Player,
    RosterSlot,
    Matchup,
    Transaction,
    FreeAgent,
)
