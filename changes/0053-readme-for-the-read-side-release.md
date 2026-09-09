# README rewrite for the read-side release (#53)

The README still said "pre-alpha. Nothing works yet," advertised `doctor`
before it shipped, told people to `uv tool install fantasy-sports` with
nothing on PyPI, and used real-looking example commands (`standings
--league dynasty`, `roster --team "Wulff"`). Ten PRs had landed since —
`doctor`, the drift canary, untrusted-text labels, true cache age,
`--no-raw`, `NOT_AVAILABLE`, finished-season caching, ADR-0009,
`SECURITY.md`/`CONTRIBUTING.md`, and release plumbing — and the front page
of a public repo had not caught up with any of it.

## What changed

Rewrote `README.md` end to end against the current registry and docs
rather than editing the old text in place:

- **Status** now says what's actually true: read path complete for ESPN
  (nine commands), writes not started (linked to the
  [v0.1 epic](https://github.com/jwulff/fantasy-sports/issues/1) and
  ADR-0006), package not yet on PyPI.
- **Install** shows the clone + `uv sync` path that works today, with the
  `uv tool install` path marked as available once a release ships
  (`docs/RELEASING.md`).
- **Quick start** uses real commands sourced from `fantasy-sports --help`
  and each subcommand's own `--help`, against a neutral `my-league`
  profile name — never a real league id.
- **One envelope sample** (adapted from `tests/unit/golden/envelope.json`,
  not a live call) shows the freshness fields (`sources[]`,
  `data_as_of`/`data_age_seconds`), `untrusted`, and `raw_omitted` together,
  each explained in a sentence.
- **One error sample** (`AUTH_EXPIRED`, from `tests/unit/golden/error.json`)
  plus the full eight-code exit-status table, cross-checked against
  `src/fantasy_sports/output/errors.py::EXIT_CODES` rather than retyped
  from memory.
- **Credentials** section covers the resolution chain and what's redacted,
  linking `SECURITY.md` for the full picture instead of duplicating it.
- **Health** section explains `doctor` and what a canary `SCHEMA_DRIFT`
  finding means, without restating `scripts/canary/README.md`.
- **Providers** table: ESPN supported (reads), Sleeper/Yahoo designed for
  but not implemented.

## How the examples stay honest going forward

Added `tests/unit/test_readme.py`, which extracts every fenced
`bash`/`console` block, parses each `fantasy-sports ...` line with `shlex`,
and asserts the command (or group + subcommand, e.g. `auth login`) exists
in `fantasy_sports.commands.REGISTRY` and every long option it uses is one
that command's `CommandSpec.cli_params` actually declares. It imports only
the registry — a dict of dataclasses with no provider or HTTP dependency —
so it costs nothing in the offline suite and fails the next time a README
example drifts from the real CLI, the way `standings --league dynasty` and
`roster --team "Wulff"` did.
