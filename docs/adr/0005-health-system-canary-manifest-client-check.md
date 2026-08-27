# ADR 0005: Health system — canary, manifest, and client-side check

**Status:** Accepted
**Date:** 2026-08-26

## Context

ESPN's fantasy API is unofficial and breaks without notice. Every prior attempt
at tooling in this space died the same way:

> ESPN changed something → the maintainer did not notice for months → users hit
> raw stack traces → the repo looked abandoned → people left.

Surveyed 2026-08-26: `KBThree13/mcp_espn_ff` (41★) last pushed 2025-12-19;
`JayMishra-source/Fantasy-Football-AI-CoManager` last pushed 2025-09-11;
`Avanderheyde/espn-fantasy-cli` has one commit and was abandoned.

Detecting breakage is therefore not a nice-to-have. It is the difference between
this project being alive in two years or not.

## Decision

A two-sided health system.

**Server side — the canary.** A scheduled GitHub Action runs a live smoke suite
against a real *public* ESPN league: daily in season, weekly in the offseason.
Unit tests run offline against cassettes; the canary is the only thing that
touches live ESPN. On drift it auto-files an issue (label `auto-error`) and
publishes an updated `health.json`.

**The manifest.** A static `health.json` committed to the repo and served from
`raw.githubusercontent.com` — no hosting, no Pages, CDN-cached. It carries
`latest_version`, `min_supported_version`, `yanked_versions`, and per-provider
status with known issues.

**Client side — the health check.** On error only (`SCHEMA_DRIFT`,
`PROVIDER_UNAVAILABLE`, unexpected exceptions), the CLI fetches the manifest and
folds the result into its output. Six non-negotiable rules:

1. Fail open, always. Offline/timeout/malformed → say nothing extra, surface the
   original error unchanged. The check must never produce an error of its own.
2. 2-second timeout.
3. Cached 6 hours in `~/.cache/fantasy-sports/`.
4. No telemetry — an unauthenticated GET of a public static file. Documented
   explicitly in the README.
5. Opt out via `FANTASY_SPORTS_NO_HEALTH_CHECK=1`.
6. PEP 440 comparison via `packaging.version`, never string compare.

**`fantasy-sports doctor`** runs every check proactively in one call.

## Consequences

**Easier:** Breakage becomes an open issue within 24 hours, automatically, which
the existing Foreman/shepherd fleet can pick up. Humans get "known issue #42,
fixed in 0.1.4, run this." Agents read `upgrade_available` and either self-heal
or stop retrying — the latter matters more, since agents waste enormous effort
re-attempting upstream failures.

**Harder:** Requires maintaining a public test league and real response-shape
validation. The canary itself is a thing that can break.

**Accepted:** The manifest is only as fresh as the last canary run — up to 24h
stale in season. `min_supported_version` and `yanked_versions` ship unused in
v0.1 so a known-bad release can be hard-stopped later without a schema migration.

## Alternatives considered

**Canary only, no client check** — rejected; the maintainer learns about
breakage but users still get stack traces.

**Client telemetry as the primary detector** — rejected as primary; it inverts
the burden onto users, risks flooding, and raises privacy questions. Client-side
error reporting remains under evaluation as a *supplement* (see
`docs/research/01-telemetry-auto-issues.md`).

**A hosted status service** — rejected; a static file in the repo achieves the
same result with zero operational cost.
