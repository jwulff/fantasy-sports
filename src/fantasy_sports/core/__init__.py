"""core layer. See docs/ARCHITECTURE.md.

The normalized domain model and the error taxonomy, both free of any provider,
CLI, or LLM dependency. Re-exported here so adapters can write
``from fantasy_sports.core import League, Team`` as
``docs/research/02-provider-data-shapes.md`` §5 assumes.
"""

from __future__ import annotations

from fantasy_sports.core.errors import (
    ERROR_TYPES,
    AuthExpiredError,
    AuthMissingError,
    ErrorCode,
    FantasySportsError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    error_type_for,
)
from fantasy_sports.core.models import (
    NORMALIZED_MODELS,
    TRANSACTION_TYPES,
    CredentialSpec,
    FreeAgent,
    League,
    Matchup,
    Player,
    ProviderObject,
    RosterSlot,
    Team,
    Transaction,
)

__all__ = [
    "ERROR_TYPES",
    "NORMALIZED_MODELS",
    "TRANSACTION_TYPES",
    "AuthExpiredError",
    "AuthMissingError",
    "CredentialSpec",
    "ErrorCode",
    "FantasySportsError",
    "FreeAgent",
    "League",
    "LeagueNotFoundError",
    "Matchup",
    "Player",
    "ProviderObject",
    "ProviderUnavailableError",
    "RateLimitedError",
    "RosterSlot",
    "SchemaDriftError",
    "Team",
    "Transaction",
    "error_type_for",
]
