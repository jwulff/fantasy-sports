# Changelog

All notable changes to `fantasy-sports` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

The v0.1 read-only milestone: an agent-native CLI over the ESPN fantasy
football API, distributed via `uv tool install fantasy-sports`.

### Added

- **Command surface** — `doctor`, `auth status` / `auth login`, `standings`,
  `roster`, `matchups`, `box-scores`, `free-agents`, `transactions`, and a
  `raw` escape hatch for any ESPN view not yet normalized. Every command is a
  plain typed function in a registry; the `typer` CLI is a thin projection
  over it (ADR-0003), which is what keeps a future MCP adapter a small wrapper
  instead of a rewrite.
- **ESPN provider adapter** with honest error classification — distinguishes
  an expired credential, a private league you're not a member of, and a
  genuine upstream outage, instead of collapsing all three into one 401.
- **Versioned output contract** (`"schema": "fantasy-sports/v1"`) with three
  renderers (JSON, table, CSV) and a stable machine-readable error taxonomy on
  stderr, so a cron job or an agent can branch on failure without parsing
  prose (ADR-0004).
- **Credential resolution chain** — environment variable, then macOS
  Keychain via `keyring`, then config file — with staleness reporting so
  `doctor` can tell you your session cookie is about to expire before ESPN
  does.
- **HTTP-layer cache** with two classes of cache tag, and a scrub-before-write
  hook plus a structural credential scanner so no recorded test cassette can
  ever carry a live `SWID` or `espn_s2` value.
- **Health system** — a scheduled canary against real ESPN that files an
  `auto-error` issue the day the unofficial API drifts, a health manifest, and
  a client-side check so `doctor` can tell you whether a failure is your
  credentials, a known outage, or something new.
- **Performance and quality budgets, enforced in CI** (ADR-0008): sub-50ms
  cold start, five-dependency ceiling, sub-256KB wheel, 90%/85% line/branch
  coverage, 80% mutation score on `core/` and `providers/` — see
  `docs/adr/0008-performance-and-quality-budgets.md`.
- **Trusted publishing to PyPI** via GitHub Actions OIDC — no long-lived API
  token stored anywhere in this repo (#13).
- **`pro_team` on every lineup entry** of `box-scores`, so a consumer can
  place a player in a game without deriving his club from the opponent (#72).

### Fixed

- **`box-scores` lost every `pro_opponent`** for a league whose
  `mPositionalRatings` view ESPN served without its `positionAgainstOpponent`
  key — two of three leagues on opening night 2026. `espn-api` gates the
  opponent on the ratings; the adapter now reads it from
  `proTeamSchedules_wl` directly, and the library's `"None"` sentinel no
  longer reaches the output as a club (#72).

### Out of scope for v0.1

Writes (lineup changes, trades, waiver claims) are gated behind a later
milestone per ADR-0006. Sleeper and Yahoo providers are designed for but not
implemented.

[Unreleased]: https://github.com/jwulff/fantasy-sports/compare/main...HEAD
