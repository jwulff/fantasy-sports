# ADR 0011: FantasyPros is an expert *source*, not a `Provider`

**Status:** Accepted
**Date:** 2026-10-16
**Decided on:** jwulff/fantasy-sports#128

## Context

FantasyPros' public API (`https://api.fantasypros.com/public/v2/json`, an
`x-api-key` header) is the expert-consensus layer ESPN's own data lacks:
per-position consensus rankings with best/worst/average/std across ~40 weekly
experts, weekly and rest-of-season projections carrying STD/HALF/PPR points,
injuries with practice-report status and a probability of playing, player news
with a fantasy-impact line, and a players table whose `espn_id` is the join onto
every ESPN read this tool already makes. John's account went to Hall of Fame on
2026-10-15 and the key answers `tier: premium`.

Three facts about the upstream decided the shape of what was built.

1. **It has no leagues.** No teams, no rosters, no standings, no matchups. Every
   method on `providers/base.py::Provider` is about a league, so the only way to
   make FantasyPros a `Provider` would be to raise `NOT_AVAILABLE` from most of
   the interface and pretend the rest.
2. **Premium is 1 request/second and 500 requests/day**, announced in the
   approval email and nowhere in the response headers. The free tier answers
   every list truncated to 10 rows, and says so with `"tier": "free",
   "limit": 10`. A truncated player pool that looks complete is the worst
   available failure for a lineup decision.
3. **The terms are personal and non-commercial**, and FantasyPros has to be
   credited where its data appears.

## Decision

**FantasyPros is a second kind of upstream, a *source*, and lives in a new
package, `src/fantasy_sports/sources/`.** `sources/fantasypros.py` is a client
over `requests` with eight methods, one per endpoint the next issues consume
(players, consensus-rankings, rankings, projections, injuries, news, experts,
player-points). It does not implement `Provider`; nothing about the registry,
the config profiles, or the league commands knows it exists. Commands (#129) and
the ESPN join (#130) sit above it.

**What it shares.** The credential chain and the HTTP cache, and nothing else.

- *The chain.* One `CredentialSpec`, `fantasypros_api_key`, resolves env
  (`FANTASY_SPORTS_FANTASYPROS_API_KEY`) → Keychain (service `fantasy-sports`,
  account `fantasypros_api_key`) → `[credentials]` in `config.toml`, exactly as
  ESPN's two cookies do. It is `required=False` **to this tool**: `auth status`
  reports it with `required: false` and leaves `complete` to the cookies,
  `auth login` prompts for it last and takes Enter as "skip", `auth logout`
  clears it wherever it is stored. The source itself refuses to run without it
  (`AUTH_MISSING`, naming the variable). The key is a `Secret` from the moment
  it resolves, so every error message, cache body and cassette is scrubbed of
  it; the `x-api-key` header is filtered by name in the cassette harness, and
  the repo-wide credential scan gained an `api-key`-keyed pattern.
- *The cache.* Every read goes through `CachingFetcher`/`CacheStore`, the only
  read path. Eight new `Resource` members with TTLs that are budget decisions
  as much as freshness ones: players 24 h, rankings/consensus/projections/
  experts/player-points 6 h, injuries 1 h, news 15 min. A hit costs nothing
  against the 500/day. A FantasyPros payload names no league, so every one of
  these is season-scoped (`cache/tags.py`'s existing class); what is new is
  **facet tags** (`season:fantasypros:2026:week=6`, `...:position=RB`,
  `...:scoring=PPR`), so one week's rankings are purgeable without the season
  and a league purge still cannot touch them. No third tag class.

**Pacing.** One process-wide `Pacer` enforces ≥1 s between live requests
across every client in the process, because the limit is the account's. No
retry on 5xx or timeout beyond what the ESPN transport already allows, which is
none.

**Errors, through the existing taxonomy (ADR-0004/0009/0010), no new code.**

| Upstream answer | Error | Why this one |
|---|---|---|
| `tier != "premium"`, or a `limit` key | `CONFIG_INVALID`, `details.kind = "tier"`, remediation naming the Hall of Fame upgrade | Not retryable, and the fix is on the human's side (the account), which is exactly `CONFIG_INVALID`'s "a human must change something; retrying unchanged cannot work" (ADR-0009). `NOT_AVAILABLE` would tell an agent to look for an alternative request shape, and there is none; `PROVIDER_UNAVAILABLE` would have it retry forever. `kind` has a precedent for a third value (`auth logout`'s `credential_store`). |
| 401 or 403 | `AUTH_EXPIRED`, `details.kind = "auth"` | A key was sent and refused. `scoring=` on projections is also a 403, so no method sends it; a 403 therefore has one cause. The class-level `agent_action` still says "re-extract their ESPN cookies"; the instance `remediation` names the FantasyPros key. Generalising that wording is a follow-up, not a taxonomy change. |
| A body carrying `message` (a 200 with no data on 2026-10-16's capture; a 400 with `parameter` and `valid_format` on the same day's probe) | `CONFIG_INVALID`, `kind = "argument"`, the message verbatim | ADR-0009: an argument only the provider can validate. Known positions and scorings are refused before sending, so a request that can never succeed does not spend one of the 500. |
| 429 | `RATE_LIMITED` with `Retry-After` when sent | A positive throttle signal. |
| 5xx, a transport failure, any other status without a `message` | `PROVIDER_UNAVAILABLE` | R12: unclassifiable is availability, never a guess. |
| Not JSON, no `tier`, no list where one is documented | `SCHEMA_DRIFT`, `provider = "fantasypros"` | The shape moved. |

A truncated or refused answer is raised **on the live path, before the fetcher
can store it**, so an upgrade takes effect on the next call rather than after
the TTL.

**`doctor` gains a `fantasypros` check**, last in the list: absent key →
`skipped` (optional), present → `ok` naming the source, and under `--live` one
one-row request (`/nfl/players?player=22947`, cache bypassed) reporting the
tier: free is `warn`, a rejected key is `fail`, offline is `skipped`. It never
raises, including on a `config.toml` that will not parse, which the chain's
config link fails hard on and the `config` check already reported.

**Terms.** `SourceResponse.as_source()` returns a `DataSource` named
`fantasypros:<endpoint>`, and every envelope that carries FantasyPros data must
list it in `sources[]`. The data is for personal, non-commercial use.

## Consequences

**Easier:** #129 is commands over eight typed methods and a `DataSource` each;
#130 is a local join on `espn_id` over a table cached for a day. A Yahoo or
Sleeper *provider* still has one Protocol to satisfy, untouched. A second
source (an ADP feed, a weather feed) has a package to live in.

**Harder:** `auth status` carries a row that is usually `present: false`, and
`auth login` has a third prompt most users will skip. A user with the cookies
already stored has to paste them again to add the key; `auth login --only` is a
follow-up.

**Accepted:** the 30-day staleness heuristic applies to the key's `stored_at`
like any Keychain entry, and its warning text says "cookie". An API key does not
expire on a timer; the warning is harmless and wrong in one word. The daily
budget is not persisted across processes (#128 out of scope); `requests_made`
counts within one.

## Alternatives considered

**A `Provider` with most methods raising `NOT_AVAILABLE`.** Rejected. It would
need a league profile in `config.toml` for something that has no leagues, and
every league command would have to learn to skip it.

**A third tag class for sources.** Rejected. Season-scoped already says the
right thing (no league may purge it); facets add the grain without a new
`scope_of` branch for the store to get wrong.

**Treat a short list as truncation by `count`.** Rejected. `count` is not a
signal (`players` says 8,564; a one-row `?player=` says 1); `tier` and `limit`
are what FantasyPros actually says.

**Retry 5xx inside the client.** Rejected. The ESPN transport does not, the
budget is 500 a day, and `PROVIDER_UNAVAILABLE` already tells the agent to.
