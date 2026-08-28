# ADR 0002: Normalize shape, not semantics

**Status:** Amended 2026-08-27 by `docs/brainstorms/2026-08-26-agent-managed-fantasy-leagues-requirements.md`
**Date:** 2026-08-26

> **Amendment.** The core decision — normalize a subset, passthrough the rest —
> stands. What changed is the *selection rule*. This ADR defined the normalized
> layer as the structural **intersection** across providers, so portability
> constrained what could be included. Normalization is now selected for
> **legibility per provider**: ESPN's normalized output may carry fields no other
> provider exposes, and cross-provider equivalence is not a goal. Raw ships
> alongside normalized in every response rather than as the fallback for
> non-intersecting fields.
>
> Consequence for §14 of `docs/ARCHITECTURE.md`: findings grounded in ESPN's own
> model remain binding (plural owners, two transaction surfaces, both period
> identifiers in raw, per-adapter standings tiebreakers). Finding 13 — keeping
> slot eligibility out of the normalized model — is **void**; R3 now requires it,
> because an agent cannot construct a legal lineup without it.

## Context

The project is provider-agnostic by design, but only ESPN ships in v0.1. The
question is how much of a unified domain model to commit to now.

Fantasy providers diverge more than they appear to:

- **Player identity is per-provider.** There is no universal NFL player ID shared
  across ESPN, Yahoo, and Sleeper.
- **Scoring settings** have structurally incompatible shapes.
- **Roster slot eligibility** ("can this player fill FLEX?") is expressed
  differently by each provider.
- **Transaction vocabularies** do not map cleanly.
- **Playoff formats and week semantics** differ, including what "week 1" means.

Attempting one true unified `League` model means a year of impedance mismatch
and no shipped product. Attempting no normalization at all means the CLI is a
thin shim with no reason to be provider-agnostic.

## Decision

**Normalize the subset that is structurally identical across providers. Refuse
to normalize the rest. Always ship a raw passthrough.**

- **Normalized:** teams, rosters, standings, matchups, transactions, free agents.
- **Explicitly not normalized:** scoring settings, draft logic, playoff formats,
  cross-provider player identity.
- Every normalized object carries `provider`, `provider_id`, and a `raw` dict.
- `fantasy-sports raw --view <view>` reaches anything the normalized layer does
  not cover.

Cross-provider player-ID mapping is deferred to a future ADR. `nflverse`
publishes crosswalks if it becomes necessary.

## Consequences

**Easier:** v0.1 ships. A second provider is a contained adapter rather than a
renegotiation of the domain model. Nothing is ever unreachable, because `raw`
always exists.

**Harder:** Users wanting scoring-settings parity across providers must handle it
themselves. Some commands will be provider-conditional.

**Accepted:** The normalized model is deliberately smaller than what any single
provider offers. That is the point — it is the intersection, not the union.

## Alternatives considered

**Full unified model** — rejected; this is the failure mode that kills
multi-provider tools before they ship.

**No normalization, pure passthrough** — rejected; removes any reason for the
abstraction to exist and pushes all complexity onto users.
