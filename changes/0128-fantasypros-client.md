# FantasyPros client, credential, cache resources, and doctor check (#128)

## Why

FantasyPros is the expert-consensus layer ESPN's own data lacks: consensus
rankings with spread across ~40 experts, weekly and rest-of-season projections
with three point totals, injuries with practice reports, news with an impact
line, and a players table whose `espn_id` joins onto every ESPN read this tool
already makes. John's account is Hall of Fame as of 2026-10-15, so the key
answers `tier: premium`; premium is 1 request/second and 500 requests/day, and
the free tier truncates every list to 10 rows while looking complete. This PR
is the foundation #129 (commands) and #130 (the ESPN join) build on: a client,
its credential, its cache rows, its errors, and a `doctor` check. ADR-0011
records the shape.

## How

- **A `sources/` package, not a `Provider`.** FantasyPros has no leagues, so
  `providers/base.py::Provider` cannot describe it without raising from most
  of the interface. `sources/fantasypros.py` shares the credential chain and
  the HTTP cache and nothing else; the registry and the league commands do not
  know it exists.
- **The credential is optional to the tool, required by the source.** The
  `fantasypros_api_key` spec is `required=False` so `auth status` stays
  `complete` without it (rows now carry `required`), `auth login` takes Enter
  at its third prompt as "skip", and `auth logout` clears it. The client raises
  `AUTH_MISSING` naming the variable when it is absent. The key is a `Secret`
  from resolution, the cassette harness filters `x-api-key` by name and now
  applies `redact` after the patterns, and the repo-wide scan gained an
  `api-key`-keyed pattern that also reads vcrpy's YAML list form.
- **Cache: season-scoped with facet tags, no third class.** Eight `Resource`
  members with TTLs that are budget decisions (players 24 h, the rankings
  family 6 h, injuries 1 h, news 15 min). `RequestContext.facets` adds
  `season:fantasypros:2026:week=6`-style tags so a week is purgeable and a
  league purge still refuses them. ESPN's season resources pass no facets and
  are byte-for-byte unchanged.
- **Validate on the live path, before the fetcher can store.** A free-tier or
  refused-argument body raises inside the fetch callable, so nothing truncated
  reaches SQLite and an upgrade takes effect on the next call, not after the
  TTL. The outer parse runs again on a hit, so a corrupt row is `SCHEMA_DRIFT`.
- **Errors through the existing taxonomy.** Truncation is `CONFIG_INVALID`
  `kind="tier"` (the fix is on the human's side; `kind` already had a third
  value in `credential_store`); 401/403 is `AUTH_EXPIRED` `kind="auth"`
  (`scoring=` on projections is a 403, so no method sends it and a 403 has one
  cause); a body with `message` is `CONFIG_INVALID` `kind="argument"` verbatim
  whether the status is 200 (the issue's capture) or 400 (the same probe an
  hour later, with `parameter` and `valid_format`); everything else lands on
  availability per R12. Known positions and scorings are refused before
  sending so a doomed request does not spend one of the 500.
- **One pacer per process.** The 1 request/second limit belongs to the
  account, so two clients in one command share `shared_pacer()`; the clock and
  sleep resolve at call time (not bound as defaults) so tests swap them.
- **`doctor` never raises, again.** The chain's config link fails hard on a
  damaged `config.toml`, and unlike the cookies this key is rarely in the
  environment, so the walk reaches that link. Caught and reported `skipped`;
  `test_doctor` found it.
- **Docs samples.** The five `auth` samples were regenerated offline (the
  `required` key and the third row); `doctor.json` is `offline: false`, so its
  `fantasypros` entry was inserted by hand in the shape the code emits, as
  #113 did for `notify`.

## Validation

- `uv run pytest -m "not live" --cov`: 2,154 passed, coverage 99 % overall
  and 100 % on the source module (one `pragma: no cover` on an
  `AttributeError` branch a plain mapping cannot reach). `ruff check` and
  `ruff format --check` clean.
  `scripts/check_budgets.py` and `scripts/check_startup.py` pass (5/5
  dependencies, 254 KB wheel, ~20 ms cold start); the module imports no HTTP
  stack at import time and a test holds that.
- `tests/unit/test_fantasypros.py`: every endpoint's shape against the trimmed
  recording, the tier/argument/auth/429/5xx/drift paths, pacing with an
  injected clock, cache hits that send nothing and need no key, facet tags and
  TTLs read back out of SQLite, the key absent from an error payload, from a
  SQLite row (gzipped fixture), and from a cassette recorded through the real
  vcrpy hooks and read back off disk; the committed cassette replays through
  real `requests` write-protected.
- Three live requests with the real key through the finished client
  (`players?player=22947`, `consensus-rankings` QB/PPR week 6, and a bad
  `position=XX` straight at the wire): premium on both reads, and the 400
  shape recorded in `docs/memory/fantasypros-refuses-an-argument-with-a-body-not-a-status.md`.
- Rebased onto main on 2026-10-20 (one conflict, the `docs/memory/MEMORY.md` index, kept both
  lines) and the suite re-run offline: 2278 passed, 19 skipped, 99.22% total coverage
  (`sources/fantasypros.py` 100%).
- Preflight (Codex) caught two defects, both fixed: the position whitelist refused the
  superflex and IDP formats (`OP`, `IDP`, `DL`, `LB`, `DB`) that the premium capture's
  `ecr_experts` lists, and the shared pacer could let two threads send together, so it now
  holds a lock through the wait and the stamp. Each has a test.
- Raised the wheel budget to 384 KB (ADR-0008, `scripts/check_budgets.py`): main was already
  256,994 bytes against 262,144, and this branch measures 271,351.
- Not done, filed as follow-ups: `auth login --only <name>` so the key can be
  added without re-pasting the cookies; generalising
  `AuthExpiredError.agent_action`'s ESPN wording.

Closes #128.
