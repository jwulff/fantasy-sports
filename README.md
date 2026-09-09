# fantasy-sports

An agent-native command-line interface for fantasy sports leagues: structured
JSON by default, a rich table when you're at a terminal, and every payload
versioned so a script can parse it without guessing at shape. It's built for
two readers at once — a human running it from a shell, and an agent that
gets piped its stdout and has to decide what to do next from a machine code,
not a stack trace.

## Why

ESPN has no official fantasy API. The unofficial one changes without notice,
and every previous attempt at tooling around it died the same way: ESPN
moved something, nobody noticed for months, the repo went quiet
(`docs/memory/prior-art-graveyard.md`). This project treats that as the
central design problem. A scheduled canary watches the real API and fails
loudly the day it drifts; `doctor` and the client-side health check read
that signal so the CLI can tell you whether a bad response is your
credentials, a known outage, or something new.

## Status

The read path is complete for ESPN: nine commands — `league info`, `teams`,
`standings`, `roster`, `matchups`, `box-scores`, `free-agents`,
`transactions`, and the `raw` escape hatch — all return the versioned
envelope described below, backed by an offline test suite and a daily
drift canary. Writes (lineup changes, waiver claims, trades) have not
started; they're tracked in the [v0.1 epic](https://github.com/jwulff/fantasy-sports/issues/1)
and gated by [ADR-0006](docs/adr/0006-read-only-v01-gated-writes-later.md).
The package is not yet on PyPI (`0.1.0.dev0`; see
[Releasing](docs/RELEASING.md) for what's left).

## Install

Nothing is published yet, so install from a clone:

```bash
git clone https://github.com/jwulff/fantasy-sports.git
cd fantasy-sports
uv sync
uv run fantasy-sports --help
```

Once `v0.1.0` is on PyPI (see [Releasing](docs/RELEASING.md)), this will also
work:

```bash
uv tool install fantasy-sports
fantasy-sports --help
```

## Quick start

Configure a league in `~/.config/fantasy-sports/config.toml` (see
[Credentials](#credentials) below), then, using a neutral league name in
place of your real one:

```bash
fantasy-sports league info --league my-league
fantasy-sports standings --league my-league
fantasy-sports teams --league my-league
fantasy-sports roster --team "My Team" --league my-league
fantasy-sports matchups --week 3 --league my-league
fantasy-sports free-agents --pos RB --limit 10 --league my-league
fantasy-sports raw --view mSettings --league my-league
```

`--output json` (the default whenever stdout isn't a terminal) or
`--output table`/`--output csv` control rendering; pipe any command into
another tool and it'll emit JSON automatically:

```bash
fantasy-sports standings --league my-league | jq '.data[0]'
```

## The output contract

Every successful payload is wrapped and versioned. This example is
illustrative — adapted from the project's own envelope fixture
(`tests/unit/golden/envelope.json`), not captured from a live call — showing
one field from every part of the contract at once:

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-09-08T18:04:11Z",
  "data_as_of": "2026-09-08T17:56:11Z",
  "data_age_seconds": 480,
  "sources": [
    {"name": "mTeam", "fetched_at": "2026-09-08T17:56:11Z", "age_seconds": 480, "cached": true},
    {"name": "mRoster", "fetched_at": "2026-09-08T18:04:11Z", "age_seconds": 0, "cached": false}
  ],
  "untrusted": {"[0].name": "Not A Cheater :)"},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "1",
      "name": "Not A Cheater :)",
      "wins": 8,
      "losses": 3,
      "points_for": 1234.5,
      "raw": {"id": 1, "abbrev": "CHEA"}
    }
  ],
  "error": null
}
```

- **Freshness.** `sources[]` itemizes every upstream fetch this response drew
  on, each with its own `fetched_at` and `age_seconds`; `data_as_of` and
  `data_age_seconds` report the oldest of them, so a consumer that only
  wants one number to gate on doesn't have to walk the list.
- **`untrusted`.** A path-to-string map pointing at any normalized field a
  league member controls, like a team or league name, so an agent that
  reasons over `data` can treat that text as data rather than instructions
  instead of trusting it by default.
- **`raw_omitted`.** Whether `--no-raw` stripped the `raw` passthrough key
  from every normalized object in this response; `false` means nothing was
  suppressed here, not that `raw` is necessarily present.

## Errors

Every failure goes to stderr as JSON, stdout stays empty, and the process
exits with a code stable enough to branch a cron job on without parsing
anything:

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-08-26T18:04:11Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "AUTH_EXPIRED",
    "message": "ESPN rejected the stored cookies.",
    "retryable": false,
    "agent_action": "Ask the human to re-extract their ESPN cookies.",
    "remediation": "Re-extract espn_s2 and SWID from DevTools, then run `auth login`.",
    "details": {"status": 401},
    "health": null
  }
}
```

| Code | Meaning | Exit |
|---|---|---|
| `AUTH_MISSING` | No credentials configured | 3 |
| `AUTH_EXPIRED` | ESPN cookies rejected | 4 |
| `LEAGUE_NOT_FOUND` | Bad league ID or no access | 5 |
| `CONFIG_INVALID` | `config.toml` won't parse, or an argument only the provider can validate came back invalid | 6 |
| `PROVIDER_UNAVAILABLE` | ESPN 5xx or timeout | 7 |
| `RATE_LIMITED` | Throttled | 8 |
| `SCHEMA_DRIFT` | Response shape unrecognized | 9 |
| `NOT_AVAILABLE` | ESPN positively refuses this request; it will never succeed as asked | 10 |

`0` is success, `1` is an unclassified crash, `2` is a usage error. The full
taxonomy, including why each code carries the agent instruction it does, is
in [ADR-0004](docs/adr/0004-versioned-output-contract-and-error-taxonomy.md).

## Credentials

ESPN's fantasy API is unofficial and cookie-authenticated. `fantasy-sports
auth login` prompts for your `espn_s2` and `SWID` cookies without echoing
them and stores them in the macOS Keychain:

```bash
fantasy-sports auth login
fantasy-sports auth status
```

The resolution chain checks an environment variable first (for `launchd`,
cron, or CI, none of which have an unlockable keychain), then the Keychain,
then a plaintext `~/.config/fantasy-sports/config.toml` fallback for hosts
without one. A credential is never accepted as a command-line argument, and
once resolved it's wrapped so it can never appear in a traceback, an error
message, a cache entry, or a recorded test cassette. The full picture —
exactly where cookies live, what's redacted and where, and how to report a
leak privately — is in [SECURITY.md](SECURITY.md).

## Health

```bash
fantasy-sports doctor
```

`doctor` checks your Python and dependency versions, config, credentials,
local cache, and (with `--live`) whether each configured league is actually
reachable, and reports one `ok`/`warn`/`error` status per check. A scheduled
canary (`scripts/canary/`) separately hits ESPN's own public test league
daily during the season and fails loudly the moment a response no longer
matches the shape the provider adapter expects — a `SCHEMA_DRIFT` finding
means ESPN changed something, not that your credentials or your league are
the problem.

## Providers

| Provider | Status |
|---|---|
| ESPN | Supported (reads) |
| Sleeper | Designed for, not implemented |
| Yahoo | Designed for, not implemented |

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — the full design and its rationale
- [ADRs](docs/adr/) — individual decision records
- [Changelog](CHANGELOG.md) — notable changes by release
- [Releasing](docs/RELEASING.md) — how a release actually gets to PyPI
- [Contributing](CONTRIBUTING.md) — fresh-clone setup, running tests, and PR conventions
- [Security Policy](SECURITY.md) — where credentials are stored, what is redacted, and how to report a problem privately

## License

MIT
