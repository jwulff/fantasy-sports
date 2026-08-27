# ADR 0008: Performance and quality budgets, enforced in CI

**Status:** Accepted
**Date:** 2026-08-26

## Context

"Fast, efficient, lightweight, heavily tested" is a stated requirement, not a
preference. Requirements that live only in prose erode — the second dependency
looks free, the tenth is a 300ms startup penalty nobody chose.

Measured on an M-series Mac, Python 3.12, best of 7 wall-clock runs including
interpreter startup:

| Import chain | Cold start |
|---|---|
| Bare interpreter | 21 ms |
| `+ typer` | 40 ms |
| `+ rich.table` | 36 ms |
| `+ requests` | 59 ms |
| `+ keyring` | 48 ms |
| `+ espn_api.football` | 61 ms |
| **Naive: all of the above at module scope** | **75 ms** |
| **Lazy: `typer` only** | **39 ms** |

For a CLI a human runs interactively and an agent may invoke dozens of times in
a loop, startup time *is* perceived performance. It is also the one cost that is
entirely within our control — network latency is ESPN's, import time is ours.

## Decision

Budgets, enforced by CI, failing the build on regression.

### Speed

| Metric | Budget |
|---|---|
| `--version` / `--help` cold start | **< 50 ms** |
| Any read command, cache hit | **< 150 ms** |
| `doctor`, no network | **< 200 ms** |

**Lazy imports are mandatory.** Nothing heavy at module scope — not `espn_api`,
`requests`, `rich.table`, or `keyring`. They are imported inside the function
that needs them. A `--help` invocation must not pay for an HTTP stack it will
never use. Enforced by a test that imports the CLI entry point and asserts the
heavy modules are absent from `sys.modules`.

Benchmarks run in CI via `hyperfine`, compared against committed baselines.

### Lightweight

| Metric | Budget |
|---|---|
| Direct runtime dependencies | **≤ 5** |
| Our own wheel size | **< 150 KB** |

**`platformdirs` is dropped.** We force XDG-style paths unconditionally (§7), so
it buys nothing but an import. Roughly 30 lines of our own code replaces it.

Remaining direct deps: `espn-api`, `typer`, `rich`, `tomli-w`, `keyring`.
`requests` arrives transitively via `espn-api`, so it is free. Adding a sixth
direct dependency requires a documented justification in the PR.

### Heavily tested

Coverage measures whether a line ran. It does not measure whether an assertion
would catch a bug. Both are budgeted.

| Metric | Budget |
|---|---|
| Line coverage | **≥ 90%**, hard CI fail |
| Branch coverage | **≥ 85%**, hard CI fail |
| Mutation score on `core/` and `providers/` | **≥ 80%** |

Plus these, each of which closes a specific hole:

- **`pytest-socket` blocks all network in unit tests.** Without it a cassette
  miss silently makes a real ESPN call — the suite stops being offline, CI
  becomes dependent on ESPN being up, and nobody notices until it flakes. A miss
  must fail loudly.
- **Every error code in the ADR-0004 taxonomy has a test that produces it.**
  A taxonomy nothing exercises is documentation, not behaviour.
- **Property-based tests (`hypothesis`) on the normalization layer.** That is
  where subtle shape bugs live, and example-based tests systematically miss them.
- **Golden-file tests per renderer** — JSON, table, CSV.
- **A test asserting no committed cassette matches a credential pattern.** This
  is a security control, not hygiene.
- **Lazy-import assertion test** (see Speed above).

Mutation testing is slow, so it runs on a schedule and pre-release rather than
on every PR, scoped to changed files where possible.

## Consequences

**Easier:** The tool stays fast by construction rather than by remembering to.
Regressions surface at PR time with a number attached. "Is this dependency worth
it?" becomes a budgeted question with a real answer.

**Harder:** Lazy imports are less ergonomic than top-level ones and are easy to
undo accidentally — hence the automated assertion rather than a code-review
convention. The mutation-score gate will occasionally demand real test work to
clear.

**Accepted:** The 50ms startup budget is achievable but not generous — measured
`typer`-only startup is already 39ms, leaving 11ms of headroom. If typer itself
regresses, the budget forces the conversation rather than absorbing it silently.

## Alternatives considered

**Budgets as documentation only** — rejected; this is the failure mode the ADR
exists to prevent.

**Coverage alone, no mutation testing** — rejected. High coverage with weak
assertions is the most common way a "heavily tested" codebase is not.

**Dropping `rich` to save startup** — rejected. It measured 15ms over baseline,
it is a soft dependency of typer already, and lazy-loading confines the cost to
commands that actually render a table.
