# Contributing

`fantasy-sports` is an agent-native CLI for fantasy sports leagues, ESPN
first. The full design and its rationale are in `docs/ARCHITECTURE.md`;
individual decisions are in `docs/adr/`. This file is the practical how-to for
getting a change in.

## Fresh-clone setup

```bash
uv sync                  # installs deps into .venv, including the dev group
uv run fantasy-sports doctor
uv run pytest            # offline, against recorded cassettes: no network, no credentials
```

`uv run fantasy-sports doctor` should report a healthy tool even with no ESPN
credentials configured; it does not need network access. If `uv sync` or
`pytest` fail on a fresh clone, that is itself a bug worth an issue.

## Running the tests

```bash
uv run pytest -m "not live" --cov --cov-report=term-missing   # what CI runs
uv run pytest                                                  # same, plus a local skip of `live`
uv run ruff check .                                             # lint
uv run ruff format --check .                                    # format check
```

Unit tests never touch the network. `pytest-socket` blocks it at the socket
level, so a cassette that does not cover a request fails loudly instead of
quietly falling through to a real ESPN call. `tests/conftest.py`'s
`pytest_collection_modifyitems` also skips anything marked `live` when no
`-m` flag is given, so a bare `uv run pytest` on your machine cannot start
hitting ESPN by accident either.

Coverage is enforced, not advisory: `fail_under = 90` (line), and CI also
checks branch coverage at 85%. Both are hard failures, not warnings.

### Golden files

Some output tests compare against files under `tests/unit/golden/`. The table
golden is rendered by `rich`, which arrives transitively through `typer`
rather than as a pinned direct dependency (ADR-0008's five-dependency
budget), so a `typer`/`rich` version bump can move box-drawing characters or
padding and turn the table golden red with no change to our code. If that
happens (table golden red, JSON and CSV goldens still green), regenerate the
table golden rather than debugging it. JSON is the actual output contract and
the table is a terminal convenience surface, so a JSON golden going red means
a real contract change instead.

## Recording an ESPN cassette

Unit tests run against recorded HTTP traffic instead of live ESPN; see
`docs/testing.md` for the full fixture policy (what each cassette covers, what
it cannot, and how request matching works). If your change needs a new
recorded interaction:

```bash
uv run python scripts/record_espn_cassettes.py                # ESPN's public test league, no credentials needed
uv run python scripts/record_espn_cassettes.py --credentialed # + your own cookies, for a private league
```

**Only two leagues may ever be committed:** ESPN's public test league
(`league_id=1234, year=2018`) and an invented league that does not exist at
ESPN. Never commit a recording of a real private league, yours or anyone
else's, scrubbed or not. `record_espn_cassettes.py` writes anything else to
`tests/cassettes/private/`, which is gitignored, and refuses `--out` pointed
at the committed directory.

**The scrub gate is not optional and is not just a regex over the finished
file.** Before a recording is written, request/response filters strip
cookies and rewrite any SWID GUID an ESPN response echoes inline (see
`SECURITY.md`). After writing, the script re-reads the file off disk, greps
it for the literal credential values this run actually sent in every form
they could have been encoded, and **deletes the recording and exits non-zero
if it finds anything**; the write is not trusted until it has survived that
read-back. If a recording session raises `UnscrubbableResponseError`, the
fix is to find out what encoding ESPN sent, not to catch the exception; see
`docs/testing.md` §8.

Before committing a new or re-recorded cassette, run the full suite.
`tests/unit/test_scrubbing.py` and `tests/unit/test_cassette_harness.py`
independently re-check for credential-shaped values in every committed
fixture, and `test_every_committed_cassette_comes_from_a_public_league`
fails on any request naming a league outside the two above.

## Budgets (ADR-0008): these fail CI

| Metric | Budget | Enforced by |
|---|---|---|
| `--version` / `--help` cold start | < 50 ms | `scripts/check_startup.py` |
| Direct runtime dependencies | ≤ 5 | `scripts/check_budgets.py` |
| Our wheel size | < 150 KB | `scripts/check_budgets.py` |
| Line coverage | ≥ 90% | `[tool.coverage.report]` in `pyproject.toml` |
| Branch coverage | ≥ 85% | CI's `pytest --cov` invocation |
| Mutation score on `core/` and `providers/` | ≥ 80% | scheduled, not on every PR |

Run the enforcement scripts locally before opening a PR that might be close to
a limit:

```bash
uv run python scripts/check_startup.py    # needs `uv sync` first; builds nothing
uv run python scripts/check_budgets.py    # builds a wheel if `dist/` is missing
```

**Lazy imports are mandatory.** Never import `espn_api`, `requests`,
`rich.table`, or `keyring` at module scope. Import them inside the function
that needs them. `--help` must not pay for an HTTP stack it will never use.
`tests/unit/test_imports.py` asserts these are absent from `sys.modules`
after importing the CLI entry point, so an eager import of one of them is
caught before the startup benchmark even runs. Adding a sixth direct runtime
dependency needs a documented justification in the PR body; see the comment
above `dependencies` in `pyproject.toml` for the shape that justification
takes.

## The rule that isn't a budget

**Never log, print, or record a credential.** `espn_s2` and `SWID` are
secrets; see `SECURITY.md` for where they are stored, what redacts them, and
the three channels that leak automatically (a captured-locals traceback, a
caller-formatted error message, a cassette or cache write) if you don't wrap
a new credential-carrying value in `Secret` or route a new error type through
the scrubbing base class.

## Worktrees

This repo uses the nested-worktree layout: `main/` is the admin checkout
(never edit or commit application code there), and work happens in
`main/.claude/worktrees/<name>`, branched from `origin/main`. Before editing,
confirm `git rev-parse --show-toplevel` includes `.claude/worktrees/`.
Branch protection means `main` never takes a direct commit: branch, PR, merge.

## Test-driven

Write the failing test first. For anything touching the ESPN provider, that
means recording or hand-writing a cassette before the implementation, never a
live call inside a unit test.

## Opening a pull request

- **Every PR body includes `Closes #N`** (or `Refs #N` for partial progress)
  linking the issue it addresses. One PR can close several issues; give each
  its own `Closes #N` line rather than listing multiple numbers after one
  keyword, since GitHub only honors the first.
- **Add a `changes/` file** narrating why the change was made and how, for
  anyone reading the repo after it has merged. It is not a changelog entry;
  it is a short piece of institutional memory. `docs/memory/` is for
  cross-PR traps and diagnostics discovered along the way, while `changes/`
  is for the rationale behind one specific merged PR.
- CI runs lint, format check, the offline test suite with coverage, and the
  ADR-0008 budget checks on every PR (`.github/workflows/ci.yml`). All of it
  should be green before requesting review. If a budget check is red, that is
  a design conversation, not something to suppress and move past.
- Live tests (`pytest -m live`) never run in CI and are not required to pass
  before merge; they hit real ESPN and only make sense on demand.
