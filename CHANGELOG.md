# Changelog

All notable changes to `fantasy-sports` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

The v0.1 read-only milestone: an agent-native CLI over the ESPN fantasy
football API, distributed via `uv tool install fantasy-sports`.

### Added

- **`auth logout`** — removes `espn_s2` and `SWID` from the Keychain and from
  `config.toml`'s `[credentials]` table (every other key survives), reports
  each link as removed / absent / still-set / unavailable without ever
  printing a value, and names any environment variable it cannot unset. This
  is the leak remediation `SECURITY.md` could not describe before (#62).
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

- **`auth logout` exits nonzero when it could not reach a link.** A locked
  Keychain or an unwritable `config.toml` used to warn inside a success
  envelope; now it is `CONFIG_INVALID` (`kind: credential_store`) with the
  per-link report in `details.report`, because a leaked value may still be on
  the machine. Raised by u/kantorcodes1 on the launch thread (#89).

- **`box-scores` lost every `pro_opponent`** for a league whose
  `mPositionalRatings` view ESPN served without its `positionAgainstOpponent`
  key — two of three leagues on opening night 2026. `espn-api` gates the
  opponent on the ratings; the adapter now reads it from
  `proTeamSchedules_wl` directly, and the library's `"None"` sentinel no
  longer reaches the output as a club (#72).
- **`roster` and `free-agents` returned `opponent: null`** for every player
  while `box-scores` resolved it. The shared player path never read the
  schedule at all; it now takes its opponent from the same
  `proTeamSchedules_wl` map the box-score path uses, a bye is `null`, and a
  player with no club (`"None"`) has neither a team nor an opponent (#86).
- **`leagues.save()` dropped every table it did not own** — on a host using
  the plaintext `[credentials]` fallback, any rewrite of `config.toml` would
  have silently discarded the stored cookies. It now reads the document
  back, replaces only `default` and `[leagues]`, and writes atomically
  through the same mode-preserving helper `auth logout` uses; a file it
  cannot parse is refused rather than overwritten (#85).

### Out of scope for v0.1

Writes (lineup changes, trades, waiver claims) are gated behind a later
milestone per ADR-0006. Sleeper and Yahoo providers are designed for but not
implemented.

[Unreleased]: https://github.com/jwulff/fantasy-sports/compare/main...HEAD
