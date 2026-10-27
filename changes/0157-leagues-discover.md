# `leagues discover`: list the account's leagues from fan.api

## Why

Closes #157, part of #155. "First run" step 1 asked the user to fish a
`leagueId=` out of a URL and hand-write `config.toml`. The account's leagues
are listable from the SWID already stored, through an endpoint whose shape
`docs/memory/espn-401-tells-you-nothing.md` documents.

That memory doc also names a trap this PR had to close first: the SWID travels
in the fan.api URL *path*, percent-encoded (`%7B<guid>%7D`), which neither the
brace-GUID pattern nor the `swid`-key pattern matched. A cassette of that
request would have written a real SWID to disk and passed the scrubber and the
repo-wide scan.

## How

- **Scrubber first** (own commit, before any cassette existed).
  `core/redaction.py` gains `_SWID_ENCODED_RE` in `CREDENTIAL_PATTERNS`, so the
  scrubber and the scan share it. It carries the same reserved-sentinel
  lookahead as the brace pattern and is case-insensitive (`%7b`), and it
  rewrites to the same pseudonym the brace form gets, re-encoded, so a member
  spelled both ways still agrees and a second pass is a no-op. `remember_secret`
  also registers the percent-encoded spelling, so an exception that
  interpolates the URL is redacted by the registry too.
- **`providers/espn_fan.py`** holds the HTTP and the normalization; the command
  in `commands/leagues.py` is thin. `requests` is imported inside the call and
  looked up as `requests.get` at call time. No cache: the read is one cheap
  request and a stale membership list is worse than none. Errors never carry the
  URL.
- **Only the SWID is resolved.** `espn_s2` is neither required nor sent;
  fan.api answers 200 without cookies, which is also why this is documented as
  not a credential check.
- **One row per `groups[]` entry** of each type-9 preference (normally exactly
  one), so a multi-group entry is not silently truncated. A type-9 entry that
  lost `groups`, `seasonId`, `entryId` or `groupId` is `SCHEMA_DRIFT`; a body
  with no `preferences` list is `SCHEMA_DRIFT`; zero type-9 entries is an empty
  success. The envelope has no warnings channel (adding one is an API change),
  so the empty-account warning goes to stderr, as `auth login`'s guidance does.
- **`--config-snippet`** adds a `config_snippet` string to every row rather than
  adding an output format: an envelope stays an envelope, and
  `| jq -r '.data[].config_snippet'` prints the blocks. Values are emitted as
  JSON strings (valid TOML basic strings) because `league_id` and `sport` come
  from ESPN. It writes nothing; that is #158. **Only football leagues get a
  profile.** `EspnProvider` ignores `LeagueProfile.sport` and always builds the
  football client against the `nfl` endpoint, so a pasted hockey profile would
  read the wrong resource (Codex preflight P1). Other sports still appear as
  rows, with a TOML comment in place of the block. Making the adapter
  sport-aware is out of scope here.
- Slugs reserve every emitted name, suffixed ones included, so `Foo`, `Foo`,
  `Foo 2` cannot yield `foo-2` twice. A 429 keeps `Retry-After`, parsed by the
  adapter's own `_retry_after_seconds`.
- **The cassette is synthetic** (`scripts/build_fan_cassette.py`): no live call,
  no real credential. The game abbreviation is `metaData.entry.abbrev`, uppercase
  `"FFL"`, verified live 2026-10-27 (`docs/testing.md` §11) and used uppercase
  in the fixture. Fallback spellings stay; an entry with none reports
  `sport: "unknown"`.
- `DiscoveredLeague` is a new model rather than a `League`: the listing has no
  team count, week or slots. It is not in `NORMALIZED_MODELS`.

## Validation

- `uv run pytest -m "not live" --cov --cov-report=term-missing`, `ruff check`,
  `ruff format --check`, `scripts/check_budgets.py`, `scripts/check_startup.py`.
- Tests written first for the scrubber (poisoned fixture caught by the
  repo-wide scan, encoded GUID scrubbed in a cassette URI, idempotence under a
  different salt, lowercase escape).
- The generated doc samples were regenerated offline with
  `render_readme_samples.py --only-experts`; unrelated samples were left as
  committed.
- Not done: a live request. Nothing here reads a real credential.
