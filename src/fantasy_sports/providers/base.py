"""The ``Provider`` Protocol — the contract every adapter satisfies.

One method per normalized concept. Every method returns objects from
``core.models`` carrying ``.raw``; nothing here returns bare provider JSON
except :meth:`Provider.fetch_raw`, which is the explicit escape hatch.

Scoring settings, draft and keeper detail, and playoff bracket structure are
deliberately **not** part of this Protocol (ADR-0002). They have structurally
incompatible shapes across providers and are reachable only through
:meth:`Provider.fetch_raw`.

Validated on paper against Sleeper and Yahoo
-------------------------------------------

v0.1 ships one adapter (ESPN), so the risk this Protocol exists to manage is
encoding ESPN-only assumptions that a second provider then has to fake. Each
method below was checked against the actual client libraries for the two most
likely second providers — ``sleeper-api-wrapper`` and ``yfpy`` — as recorded in
``docs/research/02-provider-data-shapes.md`` §5. The result:

===================== ============ ================================ ==========
Method                ESPN         Sleeper                          Yahoo
===================== ============ ================================ ==========
``credential_specs``  cookie pair  none — returns ``[]``             OAuth2
``fetch_league``      direct       direct                            direct
``fetch_teams``       direct       join ``rosters`` ⋈ ``users``      direct
``fetch_roster``      direct       join against the players dump     direct
``fetch_standings``   client sort  client sort                       server
``fetch_matchups``    week trap    flat week                         flat week
``fetch_transactions`` two surfaces single endpoint                  single
``fetch_free_agents`` server-side  client-computed set difference    league
``fetch_raw``         ``view=``    endpoint path                     resource
===================== ============ ================================ ==========

Every ⚠ cell above is real *implementation* work inside an adapter — a join, a
cached players dump, a tiebreaker cascade — and not a Protocol *design*
problem. That is the test this shape was built to pass. The executable half of
this check lives in ``tests/unit/test_provider_protocol.py``, where stubs
shaped like all three providers satisfy the Protocol.

Four consequences of that check are visible in the signatures:

* Ids are strings everywhere. Yahoo's client stringifies league ids
  specifically to survive leading zeros.
* ``fetch_standings`` returns ``list[Team]`` and each adapter owns its own
  tiebreaker source. There is no shared standings algorithm, because Yahoo's
  server-side ranking would disagree with one.
* ``credential_specs`` may legitimately return ``[]``. Sleeper needs no auth,
  and forcing a credential to exist would be an ESPN assumption.
* ``fetch_raw`` takes provider-specific keyword arguments on purpose. A unified
  query language over three different raw APIs would be a translation layer
  pretending to be a passthrough.

This module imports only the standard library and ``core.models``. It must
never import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from fantasy_sports.core.models import (
    CredentialSpec,
    FreeAgent,
    League,
    Matchup,
    RosterSlot,
    Team,
    Transaction,
)

__all__ = ["PROVIDER_METHODS", "Provider"]

#: The Protocol's method surface, in the order issue #3 enumerates it. Pinned
#: by a test so a method cannot be added or dropped without a deliberate change.
PROVIDER_METHODS: Final[tuple[str, ...]] = (
    "credential_specs",
    "fetch_league",
    "fetch_teams",
    "fetch_standings",
    "fetch_roster",
    "fetch_matchups",
    "fetch_transactions",
    "fetch_free_agents",
    "fetch_raw",
)


@runtime_checkable
class Provider(Protocol):
    """What ``fantasy-sports`` requires of a fantasy provider.

    ``isinstance`` works against this Protocol; ``issubclass`` does not,
    because :attr:`name` is a data member. That is a documented limitation of
    ``runtime_checkable``, not a bug — and ``isinstance`` only checks that the
    members *exist*, so it catches a missing method, never a wrong signature.
    Type checking is what catches the latter.
    """

    name: str
    """``"espn"``, ``"sleeper"``, ``"yahoo"``, ..."""

    # --- auth (ARCHITECTURE.md §6) ------------------------------------------

    def credential_specs(self) -> list[CredentialSpec]:
        """What this provider needs to authenticate, and how staleness shows.

        May be empty: Sleeper requires no credentials at all.
        """
        ...

    # --- core reads ---------------------------------------------------------

    def fetch_league(self, league_id: str, season: int) -> League:
        """The league itself, including its roster-slot configuration (R3a)."""
        ...

    def fetch_teams(self, league_id: str, season: int) -> list[Team]:
        """Every team in the league, unordered."""
        ...

    def fetch_standings(self, league_id: str, season: int) -> list[Team]:
        """Teams ordered by the provider's own rank.

        The adapter owns the ordering *and* its tiebreaker source. ESPN and
        Sleeper have no server-side standings resource at all — both compute it
        client-side, ESPN through a multi-rule cascade selectable per league.
        Yahoo returns one already ranked. Do not assume "sort by wins".
        """
        ...

    def fetch_roster(
        self, league_id: str, season: int, team_id: str, week: int | None = None
    ) -> list[RosterSlot]:
        """A team's roster. ``week=None`` means the current roster.

        A specific week is a historical or in-progress lineup. All three
        providers can serve past weeks, at different cost: ESPN directly,
        Sleeper only after a client-side join against its daily players dump,
        Yahoo through its own week/coverage parameters.
        """
        ...

    def fetch_matchups(self, league_id: str, season: int, week: int) -> list[Matchup]:
        """Head-to-head pairings for the **provider's matchup week**.

        For ESPN this is not 1:1 with the scoring period once playoff formats
        collapse several scoring weeks into one matchup. The adapter resolves
        that internally and records both identifiers on the returned
        :class:`~fantasy_sports.core.models.Matchup` and in its ``raw``.
        """
        ...

    def fetch_transactions(
        self, league_id: str, season: int, since: datetime | None = None
    ) -> list[Transaction]:
        """Roster moves, narrowed to the four normalized types.

        Provider-specific states — ``TRADE_VETO``, ``TRADE_PROPOSAL``,
        ``WAIVER_ERROR`` — survive only in ``raw``. An ESPN adapter must
        reconcile ``mTransactions2`` *and* ``kona_league_communication``
        internally; exposing only one surface would bake ESPN's own internal
        asymmetry into the normalized model.
        """
        ...

    def fetch_free_agents(
        self, league_id: str, season: int, week: int, position: str | None = None
    ) -> list[FreeAgent]:
        """Unrostered players, optionally filtered to one position."""
        ...

    # --- explicit non-normalized escape hatch (ARCHITECTURE.md §3) ----------

    def fetch_raw(self, league_id: str, season: int, **provider_params: Any) -> dict:
        """Direct passthrough to the provider's own API.

        ``provider_params`` are **not** normalized across providers: ESPN takes
        ``view=``, Sleeper an endpoint path, Yahoo a resource/sub-resource
        pair. Using raw access should look like using the provider's own API,
        not like translating through an invented query language.
        """
        ...
