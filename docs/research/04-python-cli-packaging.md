# Python CLI Packaging, Testing & Distribution — 2026 Best Practice

**Status:** Research complete
**Date:** 2026-08-26
**Scope:** Validates the toolchain choices in `ARCHITECTURE.md` §2 and produces
copy-pasteable scaffolding for `fantasy-sports`.

---

## 0. Verdict up front

| Decision in ARCHITECTURE.md | Verdict | Notes |
|---|---|---|
| `uv` + `hatchling` | **Confirmed** | Correct pairing; see §1, §2 |
| `typer` | **Confirmed** | Registry pattern works cleanly; see §3 |
| `rich` | **Confirmed** | No change |
| TOML config via `tomllib` | **Confirmed**, read-only | Need `tomli-w` or hand-rolled writer for `auth login` / config writes — `tomllib` is read-only stdlib |
| `pytest` + `vcrpy` | **Confirmed, and stronger than it looks** | `espn-api` itself is built on `requests`, which is vcrpy's best-supported target — see §4 |
| `PEP 735 dependency-groups` vs `optional-dependencies` | **Use dependency-groups** | Wasn't explicitly decided; now is — see §1 |
| `httpx` or `requests` (open in doc) | **Use `requests`** | Matches `espn-api`'s own transport, avoids mixing two HTTP stacks under vcrpy, no async need in a synchronous CLI — see §1, §4 |
| `keyring` for macOS Keychain | **Confirmed with a documented footgun** | Locked-keychain-under-cron is real; env-var-first resolution (already decided) is the correct mitigation — see §5 |
| `platformdirs` | **Confirmed, but override the macOS default** | Default macOS paths (`~/Library/Application Support`, `~/Library/Caches`) are wrong for this project's stated paths (`~/.config`, `~/.cache`) — see §6, this is a real decision, not a rubber stamp |
| "uv build --standalone" (open question in doc) | **Does not exist — architecture doc is wrong** | No such flag. `uv build` produces sdist + wheel only. See §2 |
| PyPI trusted publishing via OIDC | **Confirmed** | See §7 |
| Type checker: unspecified | **Recommend `pyright` in CI now, watch `ty`** | See §7 |

---

## 1. `pyproject.toml`

### PEP 735 vs `[project.optional-dependencies]`

**Use `[dependency-groups]` (PEP 735) for dev/test/lint tooling. Reserve
`[project.optional-dependencies]` for user-facing feature extras.**

The distinction that matters: `optional-dependencies` are published in the
package's PyPI metadata and installable by end users (`pip install
fantasy-sports[dev]`) — anyone inspecting the wheel sees them. `dependency-groups`
are declared in `pyproject.toml` but **never published and never installed** by
`pip install fantasy-sports`; they exist purely for people working on the repo.
For `fantasy-sports`, `pytest`/`vcrpy`/`ruff`/`pyright` are pure contributor
tooling — they don't belong in the published metadata of a tool a user is
installing via `uv tool install`. `uv` (0.4.27+) and `pip` (25.1+) both support
`[dependency-groups]` natively; `uv sync` and `uv sync --group dev` are already
the intended `uv` commands.

There are no current user-facing feature extras for `fantasy-sports` (single
provider, no optional heavy dependency), so `[project.optional-dependencies]`
is omitted from the scaffold below entirely. Add it only if, e.g., an optional
`report` extra needs a heavy dependency users shouldn't be forced to install.

### The file

```toml
[project]
name = "fantasy-sports"
version = "0.1.0"
description = "Agent-native CLI for fantasy sports leagues — ESPN first, provider-agnostic by construction."
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
license-files = ["LICENSE"]
authors = [
    { name = "John Wulff", email = "john@johnwulff.com" },
]
keywords = ["fantasy-football", "espn", "fantasy-sports", "cli", "agent"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: End Users/Desktop",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Topic :: Games/Entertainment",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "Typing :: Typed",
]
dependencies = [
    "espn-api>=0.46.0",
    "typer>=0.15.1",
    "rich>=13.9.4",
    "platformdirs>=4.3.6",
    "packaging>=24.2",
    "requests>=2.32.3",
    "keyring>=25.5.0",
]

[project.urls]
Homepage = "https://github.com/jwulff/fantasy-sports"
Repository = "https://github.com/jwulff/fantasy-sports"
Issues = "https://github.com/jwulff/fantasy-sports/issues"
Changelog = "https://github.com/jwulff/fantasy-sports/releases"

[project.scripts]
fantasy-sports = "fantasy_sports.cli.app:main"
fantasy = "fantasy_sports.cli.app:main"

[dependency-groups]
dev = [
    "pytest>=8.3.4",
    "pytest-cov>=6.0.0",
    "vcrpy>=6.0.2",
    "pytest-recording>=0.13.2",
    "ruff>=0.8.4",
    "pyright>=1.1.390",
    "requests-mock>=1.12.1",  # for unit tests that don't want a cassette at all
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fantasy_sports"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "C4"]

[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "standard"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "canary: live-ESPN tests, never run in normal CI (see tests/canary/)",
]

[tool.coverage.run]
source = ["src/fantasy_sports"]
omit = ["*/canary/*"]
```

Notes on choices baked into this file:

- **`src/` layout** (`packages = ["src/fantasy_sports"]`): standard modern
  practice — prevents accidentally importing the uninstalled local package
  instead of the installed one during tests, which is exactly the kind of bug
  that hides ESPN API drift behind a false-green test run.
- **`license = "MIT"` + `license-files`** uses the PEP 639 string form (not the
  old `{text = "MIT"}` table), which is what current `hatchling` and PyPI's
  metadata validator expect going into 2026.
- **Both console scripts point at the same `main`** — `typer`'s app object is
  the single entry point; `fantasy` is a pure alias, not a second app.
- **`keyring` is a hard dependency**, not optional — the architecture's
  resolution chain (env → Keychain → config file) needs it unconditionally on
  step 2, and making it optional just adds an import-error code path nobody
  will test.
- **No upper-bound pins** on runtime dependencies. This is an application, not
  a library other packages depend on — upper bounds only cause "resolution
  couldn't find a version" support tickets. Let `uv.lock` pin exact versions
  for reproducibility; `uv lock --upgrade` on a schedule (or `uv add
  --dev` for dependabot-equivalent) keeps them current. Run `uv lock` once
  this file lands to generate the committed lockfile.

**Sources:**
[PEP 735](https://peps.python.org/pep-0735/) ·
[Understanding dependency groups in uv](https://pydevtools.com/handbook/explanation/understanding-dependency-groups-in-uv/) ·
[uv: Managing dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/) ·
[Using Dependency Groups with uv](https://blog.bront.rodeo/using-dependency-groups-with-uv/)

---

## 2. Distribution

### The three end-user installers, compared

| | `uv tool install` | `pipx` | `pip install --user` |
|---|---|---|---|
| Isolation | Own venv, managed automatically | Own venv, managed automatically | **None** — pollutes user site-packages, real breakage risk |
| Speed | Fastest (Rust, content-addressed cache) | Slower — shells to pip/virtualenv | N/A |
| Python version handling | **Downloads a matching Python automatically** if missing | Requires a pre-existing Python | Requires a pre-existing Python |
| Upgrade | `uv tool upgrade fantasy-sports` | `pipx upgrade fantasy-sports` | `pip install --user -U fantasy-sports` (easy to forget `-U`) |
| Ubiquity | Requires `uv` installed | Pre-installed on more systems / distro-blessed | Always available (anywhere Python is) |
| Ecosystem fit here | **You already have `uv` 0.11.3 on this machine**; the project already standardized on it for everything else | Fine, but a second tool doing what `uv` already does | **Actively wrong** — no isolation, will eventually break |

**Recommendation for the README:** lead with `uv tool install fantasy-sports`,
mention `uvx fantasy-sports doctor` for zero-install one-shot use (huge for
"try it in a cron job before committing to install"), and give `pipx install
fantasy-sports` as the fallback for anyone without `uv`. **Do not tell anyone
to `pip install --user`** — it's the one option here with a real footgun
(unisolated global-ish install), and it buys nothing `uv`/`pipx` don't already
give better.

```markdown
## Install

    uv tool install fantasy-sports

No `uv`? `pipx install fantasy-sports` works identically. Want to try one
command without installing anything: `uvx fantasy-sports doctor`.
```

`uv` is genuinely the 2026 consolidation point — it subsumes pip, pipx,
virtualenv, pyenv, and twine into one Rust binary with a content-addressed
cache, and it downloads a managed Python automatically if the target machine
doesn't have a matching one. That last property matters specifically for the
cron use case this project cares about: a fresh Linux box running
`uv tool install fantasy-sports` doesn't need a pre-installed Python at all.

### Standalone binary (PyInstaller / Nuitka / "uv build --standalone")

**The architecture doc's phrasing — `uv build --standalone` — refers to a
feature that does not exist.** Checked against current `uv` docs (Aug 2026):
`uv build` produces exactly two artifacts, an sdist and a wheel. There is no
`uv`-native path to a single-file executable. That's a factual correction to
make in ARCHITECTURE.md §2, not just a recommendation call.

The real choices are **PyInstaller** and **Nuitka**:

- PyInstaller freezes the interpreter + bytecode + deps into a bundle.
  Startup overhead is real (~1.8s cold start for a `--onefile` build in 2026
  benchmarks) because it's unpacking a bundle every launch.
- Nuitka compiles Python to C and produces a genuinely native binary.
  Startup is dramatically faster (~0.08s in the same benchmark class) —
  actually competitive with a real compiled CLI.

**Recommendation: skip both for v0.1.** The stated cron/CI motivation — "zero
dependency install" — is already solved by `uv tool install` / `uvx`, because
`uv` itself is a static Rust binary with no Python prerequisite, and it
provisions a managed Python transparently. A standalone binary buys nothing
that `uvx fantasy-sports` doesn't already give a cron job, and it adds real
cost: a second build pipeline, per-platform binaries to build and test (macOS
arm64/x86_64, Linux x86_64/arm64, maybe Windows), and Nuitka/PyInstaller's own
failure modes around dynamic imports (`espn-api`'s internals, `typer`'s
introspection-heavy Click layer) that would need dedicated smoke tests. Revisit
only if a real user surfaces who can't get `uv` or `pipx` onto their machine at
all — that hasn't happened in the ecosystem research for this space (§0 of
ARCHITECTURE.md), and it's not the audience this tool is for.

**Sources:**
[uv tool vs pipx](https://pydevtools.com/handbook/explanation/how-do-uv-tool-and-pipx-compare/) ·
[pipx comparisons](https://pipx.pypa.io/latest/explanation/comparisons.html) ·
[uv build backend docs](https://docs.astral.sh/uv/concepts/build-backend/) ·
[uv building distributions](https://docs.astral.sh/uv/concepts/projects/build/) ·
[Nuitka vs PyInstaller vs cx_Freeze 2026](https://blog.thoughtparameters.com/post/nuitka_vs_pyinstaller_python_packaging/)

---

## 3. Typer patterns for an agent-native CLI

### Registry-first structure (Rule 1 from ARCHITECTURE.md §4)

The load-bearing constraint: **typer callbacks contain zero logic.** Every
command is a plain, fully-typed, importable function in `commands/`. The
typer app is a thin adapter that parses argv and calls the function; nothing
in `commands/` imports `typer`.

```
src/fantasy_sports/
  cli/
    app.py           # typer App wiring only
    context.py       # GlobalOptions dataclass + typer.Context helpers
    render.py         # TTY-detection, JSON/table dispatch
  commands/
    __init__.py      # REGISTRY: dict[str, CommandSpec]
    league.py         # info(), standings(), teams() — plain functions
    roster.py
    matchups.py
    auth.py           # status(), login()
    doctor.py
  core/
    models.py
  providers/
    base.py
    espn.py
  auth/
  cache/
  output/
```

**`commands/league.py`** — plain, typer-free, directly unit-testable:

```python
# commands/league.py
from dataclasses import dataclass

from fantasy_sports.core.models import LeagueInfo, StandingsRow
from fantasy_sports.providers.base import Provider


@dataclass(frozen=True)
class LeagueInfoResult:
    league: LeagueInfo


def info(provider: Provider, *, no_cache: bool = False) -> LeagueInfoResult:
    """Fetch normalized league metadata for the active league."""
    league = provider.fetch_league(use_cache=not no_cache)
    return LeagueInfoResult(league=league)


def standings(provider: Provider, *, no_cache: bool = False) -> list[StandingsRow]:
    """Fetch current standings, normalized across providers."""
    return provider.fetch_standings(use_cache=not no_cache)
```

These functions take a resolved `Provider` and plain kwargs — no `typer.Context`,
no `click`, nothing CLI-shaped. That's what makes the MCP adapter in v0.4 "~100
lines," per the architecture doc: `fastmcp` wraps these same functions directly.

**`cli/app.py`** — the typer projection, argument parsing only:

```python
# cli/app.py
import typer
from rich.console import Console

from fantasy_sports import commands
from fantasy_sports.cli.context import GlobalOptions, resolve_provider
from fantasy_sports.cli.render import render, is_tty
from fantasy_sports.errors import CliError, error_envelope

app = typer.Typer(
    name="fantasy-sports",
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # we own error formatting; see errors.py
)
league_app = typer.Typer(help="League metadata and standings.")
auth_app = typer.Typer(help="Credential management.")
app.add_typer(league_app, name="league")
app.add_typer(auth_app, name="auth")


@app.callback()
def main(
    ctx: typer.Context,
    league: str = typer.Option(None, "--league", "-l", envvar="FANTASY_SPORTS_LEAGUE"),
    output: str = typer.Option(None, "--output", "-o", help="json|table|csv"),
    no_cache: bool = typer.Option(False, "--no-cache"),
) -> None:
    """fantasy-sports: agent-native CLI for fantasy sports leagues."""
    ctx.obj = GlobalOptions(
        league=league,
        output=output or ("json" if not is_tty() else "table"),
        no_cache=no_cache,
    )


@league_app.command("info")
def league_info(ctx: typer.Context) -> None:
    """Show league metadata."""
    opts: GlobalOptions = ctx.obj
    provider = resolve_provider(opts)
    try:
        result = commands.league.info(provider, no_cache=opts.no_cache)
    except CliError as exc:
        render(error_envelope(exc), output=opts.output, err=True)
        raise typer.Exit(code=exc.exit_code)
    render(result, output=opts.output)


def main_entry() -> None:
    app()
```

`main` is the console-script target in `[project.scripts]`; `pyproject.toml`
above points at `fantasy_sports.cli.app:main`, so adjust to `main_entry` or
rename — keep the two consistent.

### Sub-command groups

`app.add_typer(auth_app, name="auth")` is the whole mechanism — confirmed
current in typer's own docs. It nests arbitrarily (`auth_app.add_typer(...)`
for a third level if ever needed). `fantasy-sports auth status` and
`fantasy-sports auth login` both live in `cli/auth.py` as thin wrappers over
`commands/auth.py`.

### Global options + `typer.Context`

Every `typer.Typer()` app gets an implicit `Context`. Declaring a callback
with `ctx: typer.Context` and setting `ctx.obj` there is the standard pattern
for options that must be visible to every subcommand (`--league`, `--output`,
`--no-cache`) without re-declaring them on each command. `ctx.obj` is typed as
a project-owned `GlobalOptions` dataclass, not a raw dict — keeps
`resolve_provider(opts)` type-checkable.

### TTY vs pipe detection

```python
# cli/render.py
import sys
from rich.console import Console


def is_tty() -> bool:
    return sys.stdout.isatty()


def render(payload, *, output: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    if output == "json":
        import json

        print(json.dumps(payload, default=str), file=stream)
    elif output == "csv":
        _render_csv(payload, stream)
    else:
        Console(file=stream).print(_as_table(payload))
```

The default resolution (`json` when not a TTY, `table` when it is) is set once
in the top-level callback, per ARCHITECTURE.md §5 — `--output` always wins
when passed explicitly.

### Exit codes and structured stderr errors

```python
# errors.py
from dataclasses import dataclass, field
from datetime import datetime, timezone

EXIT_CODES = {
    "AUTH_MISSING": 2,
    "AUTH_EXPIRED": 2,
    "LEAGUE_NOT_FOUND": 3,
    "PROVIDER_UNAVAILABLE": 4,
    "RATE_LIMITED": 4,
    "SCHEMA_DRIFT": 5,
}


@dataclass
class CliError(Exception):
    code: str
    message: str
    health: dict | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)


def error_envelope(exc: CliError) -> dict:
    return {
        "schema": "fantasy-sports/v1",
        "error": {
            "code": exc.code,
            "message": exc.message,
            **({"health": exc.health} if exc.health else {}),
        },
    }
```

`app.py` catches `CliError` at the command layer (as shown above), writes the
JSON envelope to stderr, and calls `raise typer.Exit(code=exc.exit_code)` —
`typer.Exit` is the correct way to set a real process exit code without typer
printing its own traceback. `pretty_exceptions_enable=False` on the `Typer()`
constructor stops typer/rich from rendering a pretty traceback for *unhandled*
exceptions too, which would otherwise leak Python internals to stderr instead
of the JSON envelope contract agents rely on.

### Can `fastmcp` genuinely reuse the same typed functions?

**Mostly yes, with one real gap.** `fastmcp` generates a tool's JSON Schema
from a plain function's type hints via Pydantic's `TypeAdapter` — it wants an
undecorated, importable function, not a typer/click command object. Since
Rule 1 already forces `commands/*.py` to be typer-free, those functions are
directly what `fastmcp` wants:

```python
# mcp/server.py — the "~100 lines" from ARCHITECTURE.md §4
from fastmcp import FastMCP
from fantasy_sports import commands
from fantasy_sports.cli.context import resolve_provider_from_env

mcp = FastMCP("fantasy-sports")

mcp.add_tool(commands.league.info)
mcp.add_tool(commands.league.standings)
mcp.add_tool(commands.roster.roster)
# ...
```

The gap: `fastmcp`'s docs (checked Aug 2026) do **not** document decorating an
already-typer/click-wrapped function — and per Rule 1 that never happens here,
since the typer layer only ever wraps the plain function, never the other way
around. The only real friction point is **provider resolution**: CLI commands
take a `Provider` object resolved from `--league`/config/env by
`cli/context.py`; an MCP tool call has no argv to parse that from. Give
`commands/*.py` functions a `Provider` parameter with no default (as shown
above) and have each adapter (`cli/app.py`, `mcp/server.py`) own its own
resolution path into that parameter — `resolve_provider(opts)` for CLI,
`resolve_provider_from_env()` (or an MCP-level `--league` equivalent) for MCP.
That keeps the registry functions honestly parameterized rather than reaching
into ambient global state, and it's the only design change this research
surfaces beyond what ARCHITECTURE.md already specifies.

**Sources:**
[Typer: Using the Context](https://typer.tiangolo.com/tutorial/commands/context/) ·
[Typer: Context and callback options](https://typer.tiangolo.com/tutorial/options/callback-and-context/) ·
[FastMCP: Tools](https://gofastmcp.com/servers/tools)

---

## 4. Testing an unofficial, unstable, credentialed API

This is the section where getting it wrong costs the most — a leaked
`espn_s2`/`SWID` cookie in a committed cassette is a real credential leak, not
a style nit.

### Is `vcrpy` still right in 2026?

**Yes — and more specifically right than the architecture doc's rationale
states.** `vcrpy` hooks HTTP client libraries directly (`requests`, `urllib3`,
`http.client`, `httpx` as of recent 6.x releases with caveats on async).
`espn-api` (confirmed by reading its source, `espn_api/requests/espn_requests.py`)
is built on **`requests`**, which is vcrpy's most mature, best-tested target.
That's the actual argument for `vcrpy` here: it isn't a generic choice, it's
the correct choice *because of what the dependency we don't control uses.*

The pytest integration layer has moved: **use `pytest-recording`**, not raw
`vcrpy` decorators. It wraps vcrpy with a `@pytest.mark.vcr` marker, supports
combining multiple cassettes, and — critically for this project — defaults to
`--record-mode=none`, meaning a test hitting an un-cassetted request **fails
instead of silently making a real network call**. That default is exactly the
CI-safety property the architecture doc wants ("Unit tests run against
recorded cassettes: fast, offline, deterministic").

`respx` and `responses` are the other 2026-relevant options, but they're
scoped differently: `respx` mocks `httpx` specifically, `responses` mocks
`requests` at the mock-registration level (you declare expected requests/
responses in code, not from a recorded fixture). Neither replaces vcrpy's
core value here — recording *real* ESPN traffic once and replaying it
verbatim is what catches genuine response-shape drift; a hand-written mock
would happily keep passing after ESPN changes a field name, which is the
exact failure mode ARCHITECTURE.md §11 is designed to prevent. Recommendation:
**`vcrpy` + `pytest-recording`, not `respx`/`responses`.**

### Cassette scrubbing — real config

`vcrpy` does **not** scrub anything by default — every credential ends up in
the cassette file verbatim unless you configure filtering. This has to be
correct before the first cassette is ever recorded, not retrofitted.

```python
# tests/conftest.py
import re
import pytest


def scrub_string(string: str, replacement: str = "REDACTED") -> callable:
    def before_record_response(response):
        return response

    return before_record_response


def _redact_set_cookie(response: dict) -> dict:
    """Strip Set-Cookie headers ESPN sends back (rare, but be safe)."""
    headers = response.get("headers", {})
    for key in list(headers):
        if key.lower() == "set-cookie":
            headers[key] = ["REDACTED"]
    return response


def _redact_swid_in_body(response: dict) -> dict:
    """ESPN sometimes echoes SWID inside JSON payloads (e.g. roster owner
    fields). Defense in depth beyond header/cookie filtering."""
    body = response.get("body", {}).get("string")
    if body:
        text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body
        text = re.sub(r"\{[0-9A-F-]{36}\}", "{SWID-REDACTED}", text, flags=re.IGNORECASE)
        response["body"]["string"] = text.encode("utf-8") if isinstance(body, bytes) else text
    return response


def before_record_response(response: dict) -> dict:
    response = _redact_set_cookie(response)
    response = _redact_swid_in_body(response)
    return response


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "filter_headers": [
            "authorization",
            "cookie",  # covers the espn_s2 + SWID request cookie header
            "set-cookie",
        ],
        "filter_query_parameters": ["espn_s2", "SWID"],
        "before_record_response": before_record_response,
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "cassette_library_dir": "tests/cassettes",
    }
```

Notes:

- `filter_headers: ["cookie"]` is the load-bearing line — the ESPN request
  auth *is* the `Cookie: espn_s2=...; SWID={...}` header. Filtering it
  replaces the value with a placeholder (`"cookie": "REDACTED"`) in the
  cassette rather than the live credential.
- `filter_query_parameters` covers the (less common but real) case where
  `espn_s2`/`SWID` appear as query params rather than cookie header.
- The body-scrub function is defense in depth — ESPN's roster JSON sometimes
  echoes a team owner's SWID inline. Header filtering alone won't catch that.
- **Pre-commit gate:** add a grep-based pre-commit hook (or a dedicated pytest
  test) that fails if any committed cassette contains a raw `{` GUID pattern
  or the literal string `espn_s2=` followed by anything but `REDACTED` —
  belt-and-suspenders against a future contributor recording a cassette
  without running it through this fixture.
- Recording real cassettes: run once locally with real credentials
  (`FANTASY_SPORTS_ESPN_S2=... FANTASY_SPORTS_SWID=... uv run pytest
  --record-mode=once`), inspect the resulting YAML in `tests/cassettes/` by
  hand before `git add`, then commit. `pytest-recording`'s `--record-mode=none`
  default means CI can never accidentally attempt a live re-record.

### Live canary suite, separate from CI

```python
# tests/canary/test_live_espn.py
import os
import pytest

pytestmark = pytest.mark.canary

PUBLIC_LEAGUE_ID = os.environ["FANTASY_SPORTS_CANARY_LEAGUE_ID"]


def test_public_league_standings_shape(live_provider):
    """Runs against real ESPN. Never collected in normal `pytest`."""
    standings = live_provider.fetch_standings(use_cache=False)
    assert standings
    for row in standings:
        assert row.team_id
        assert isinstance(row.wins, int)
```

Isolate it structurally, not just by marker:

- `tests/canary/` is a separate directory `pytest.ini`/`pyproject.toml`
  excludes from the default collection root (`testpaths = ["tests"]` in the
  scaffold above **includes** `tests/canary` by directory walk unless
  explicitly excluded — add `--ignore=tests/canary` to the default `pytest`
  invocation used in CI, and run canary only via `pytest tests/canary -m
  canary` in its own workflow).
- The canary workflow (see §7) is the only CI job with `secrets.ESPN_S2` /
  `secrets.ESPN_SWID` in scope — the main test/lint/type-check workflow never
  sees live credentials at all, which is the strongest guarantee against
  accidental credential exposure in a PR from an external contributor (their
  fork's CI run gets zero secrets by GitHub's default behavior anyway, but
  keeping canary structurally separate means it's true by design, not by
  GitHub's fork-PR secret policy alone).

### Testing the typer CLI itself

```python
# tests/test_cli_standings.py
import json
from typer.testing import CliRunner

from fantasy_sports.cli.app import app

runner = CliRunner()


def test_standings_json_output(vcr_cassette, monkeypatch):
    monkeypatch.setenv("FANTASY_SPORTS_LEAGUE", "dynasty")
    result = runner.invoke(app, ["--output", "json", "league", "standings"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "fantasy-sports/v1"
    assert payload["data"]


def test_auth_missing_exits_2(monkeypatch):
    monkeypatch.delenv("FANTASY_SPORTS_ESPN_S2", raising=False)
    result = runner.invoke(app, ["--output", "json", "league", "standings"])
    assert result.exit_code == 2
    err = json.loads(result.stderr) if result.stderr else json.loads(result.output)
    assert err["error"]["code"] == "AUTH_MISSING"
```

`typer.testing.CliRunner` is `click`'s test runner under a typer-flavored
import (typer is built on click) — `runner.invoke(app, [...])` captures
stdout/stderr and exit code without spawning a subprocess, so it composes
cleanly with the `vcr_cassette`/`vcr_config` fixtures above for full
CLI-through-provider-through-HTTP integration tests, not just unit tests of
`commands/*.py` in isolation.

**Sources:**
[vcrpy docs](https://vcrpy.readthedocs.io/en/latest/) ·
[Redacting secrets and PII from VCR.py cassettes](https://imoskvin.com/blog/redacting-vcrpy-cassettes/) ·
[pytest-recording](https://github.com/kiwicom/pytest-recording) ·
[espn-api source (`requests` dependency)](https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/espn_requests.py) ·
[Typer testing docs](https://typer.tiangolo.com/tutorial/testing/)

---

## 5. Secrets: macOS Keychain and CI

### Is `keyring` still standard? Yes — with one real footgun

`keyring` (jaraco/keyring) remains the de facto standard cross-platform
Python secrets API in 2026: `keyring.set_password(service, username,
password)` / `keyring.get_password(service, username)`, auto-selecting the
native backend (macOS Keychain, Windows Credential Locker, Secret
Service/kwallet on Linux).

**The documented footgun, and it's exactly the one that matters for this
project's cron use case:** the macOS Keychain backend requires the keychain to
be *unlocked*, and a `launchd`/cron-invoked process running outside an
interactive login session frequently cannot unlock it — there's no TTY to
prompt against, and unattended unlock via `security unlock-keychain -p
<password>` means storing the unlock password somewhere, which defeats the
point. This is a known, longstanding class of failure for any headless macOS
Keychain access, not something specific to a bug in `keyring`.

**This is exactly why ARCHITECTURE.md's resolution chain is ordered `env →
Keychain → config file` and not `Keychain → env → config file`.** Env-first
means a cron job or CI run that exports `FANTASY_SPORTS_ESPN_S2` never
touches Keychain at all — the locked-keychain failure mode simply doesn't
trigger for the automated path. Keychain is for **interactive** use
(`fantasy-sports auth login` from a Terminal session), where the keychain is
already unlocked because the user is logged in.

### Real code

```python
# auth/keychain.py
import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "fantasy-sports"


def store_credential(name: str, value: str) -> None:
    """name e.g. 'espn_s2', 'SWID'. Called only from `auth login` (interactive)."""
    keyring.set_password(SERVICE, name, value)


def read_credential(name: str) -> str | None:
    """Never called on the cron/CI path — see auth/resolve.py's env-first chain.
    Returns None on any keyring failure (locked keychain, no backend, etc.)
    rather than raising: this is the *second* link in a resolution chain that
    must fail soft, exactly like the ARCHITECTURE.md §11.3 health check does."""
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def delete_credential(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass
```

```python
# auth/resolve.py
import os

from fantasy_sports.auth.keychain import read_credential
from fantasy_sports.config import load_config

ENV_PREFIX = "FANTASY_SPORTS_"


def resolve_credential(name: str) -> str | None:
    """env -> macOS Keychain -> config file, per ARCHITECTURE.md §6.
    Order matters: env-first means cron/CI never depends on an unlockable
    keychain."""
    env_value = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
    if env_value:
        return env_value

    keychain_value = read_credential(name)
    if keychain_value:
        return keychain_value

    config = load_config()
    return config.get("credentials", {}).get(name)
```

### CI supplies credentials via env — no Keychain involved

```yaml
# .github/workflows/canary.yml (excerpt — see §7 for full file)
env:
  FANTASY_SPORTS_ESPN_S2: ${{ secrets.ESPN_S2 }}
  FANTASY_SPORTS_SWID: ${{ secrets.ESPN_SWID }}
```

Because `resolve_credential` checks env first, CI runners (which have no
Keychain, no GUI session, nothing) work identically to a developer's Mac —
the only difference is *which* link in the chain resolves. No CI-specific
code path is needed, which is itself a small but real validation that the
env-first ordering in ARCHITECTURE.md §6 was the right call.

**Sources:**
[keyring (jaraco/keyring)](https://github.com/jaraco/keyring) ·
[keyring docs](https://keyring.readthedocs.io/)

---

## 6. Config and cache file locations

### `platformdirs` on macOS is wrong for this project's stated paths — and that's worth flagging explicitly

`platformdirs.user_config_dir("fantasy-sports")` on macOS returns
`~/Library/Application Support/fantasy-sports` by default, and
`user_cache_dir("fantasy-sports")` returns `~/Library/Caches/fantasy-sports`.
That's correct, native-macOS behavior for a GUI-adjacent app — but
ARCHITECTURE.md §7 and §8 explicitly specify `~/.config/fantasy-sports/config.toml`
and `~/.cache/fantasy-sports/`, i.e. the **XDG-style** paths, on macOS too.
This is a real conflict between the design doc's stated paths and
`platformdirs`'s macOS default, not a hypothetical one.

**Two ways to resolve it, and the recommendation:**

1. **Set `XDG_CONFIG_HOME`/`XDG_CACHE_HOME` awareness explicitly** —
   `platformdirs` *does* respect XDG environment variables on macOS when
   they're set, overriding the native default. But if the user hasn't set
   them (the common case), you're back to the `~/Library/...` default.
2. **Force XDG-style paths unconditionally**, independent of whether the
   user has XDG env vars set. This is what a CLI tool aimed at
   developers/power users (this audience — cron jobs, agents, `--output json`)
   conventionally does, and it's what ARCHITECTURE.md already committed to in
   writing.

**Recommendation: option 2 — force it.** This is a CLI tool for technical
users who expect `~/.config` and `~/.cache` the way every other modern CLI
(ripgrep, gh, docker, uv itself) behaves on macOS, not a GUI app that should
follow Apple HIG. Don't rely on `platformdirs`'s macOS-native default at all;
either call `platformdirs.PlatformDirs(..., appname="fantasy-sports")` **and
explicitly set `XDG_CONFIG_HOME`/`XDG_CACHE_HOME` in-process before
constructing it if unset**, or bypass `platformdirs`'s macOS branch entirely
with a small wrapper:

```python
# config/paths.py
import os
from pathlib import Path

APP_NAME = "fantasy-sports"


def _xdg_or_default(env_var: str, fallback: str) -> Path:
    value = os.environ.get(env_var)
    if value:
        return Path(value) / APP_NAME
    return Path.home() / fallback / APP_NAME


def config_dir() -> Path:
    return _xdg_or_default("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    return _xdg_or_default("XDG_CACHE_HOME", ".cache")


def config_file() -> Path:
    return config_dir() / "config.toml"


def cache_db() -> Path:
    return cache_dir() / "cache.sqlite3"


def health_cache_file() -> Path:
    return cache_dir() / "health.json"
```

This still honors `XDG_CONFIG_HOME`/`XDG_CACHE_HOME` when a user has set them
(same as `platformdirs` would), but **defaults to `~/.config` / `~/.cache` on
every platform including macOS**, matching what the architecture doc already
promises in its example paths and `config.toml` listing. It's ~15 lines and
drops the `platformdirs` runtime dependency entirely for this specific need —
worth it here because the one thing `platformdirs` is for (getting the
*platform-native* answer) is precisely the behavior being deliberately
overridden. Keep `platformdirs` in `pyproject.toml` only if something else in
the codebase genuinely wants native per-OS paths (e.g. a future Windows port
that should *not* use XDG-style paths); otherwise drop it from dependencies
and delete this line from the earlier pyproject scaffold.

**Linux** needs no special-casing either way — `~/.config`/`~/.cache` (or the
XDG env vars) are already the Linux-native answer, so the same function is
correct there unmodified.

**Sources:**
[platformdirs How-to guides](https://platformdirs.readthedocs.io/en/latest/howto.html) ·
[platformdirs API](https://platformdirs.readthedocs.io/en/latest/api.html) ·
[Understanding platformdirs](https://platformdirs.readthedocs.io/en/latest/explanation.html)

---

## 7. CI/CD

### Type checker: `pyright` in CI now, watch `ty`

- **`ruff` does not type-check** — it's lint + format only; a separate tool is
  required regardless.
- **`pyright`** (Microsoft): ~98% typing-spec conformance, 2–5x faster than
  `mypy`, the de facto standard for new projects entering 2026, integrates with
  VS Code/Pylance for the same feedback loop as CI. **Recommended for this
  project's CI.**
  Comparisons in 2026 also surface **Pyrefly** (Meta, Rust, hit stable 1.0 in
  May 2026, dramatically faster than both `mypy` and `pyright` on large
  codebases) as a legitimate emerging option, and **`ty`** (Astral, Rust,
  still beta/alpha as of Aug 2026, not yet at `mypy`-level conformance) as
  one to track but not adopt in CI yet — pairs naturally with `uv`/`ruff` once
  it stabilizes, worth revisiting in 6–12 months.
- `mypy` remains the plugin-ecosystem-richest option but is the slowest of the
  group; no plugin need here justifies its cost over `pyright`.

**Decision: `pyright` for CI gating, no editor-specific tie-in required
(works the same via `pyright` CLI in CI and Pylance locally).**

### Full CI workflow

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
        with:
          enable-cache: true
      - run: uv run ruff check .
      - run: uv run ruff format --check .

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
        with:
          enable-cache: true
      - run: uv sync --group dev
      - run: uv run pyright

  test:
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.12", "3.13", "3.14"]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - run: uv sync --group dev
      - run: uv run pytest --ignore=tests/canary --cov --cov-report=xml
      - uses: codecov/codecov-action@v5
        if: matrix.python-version == '3.12'
        with:
          files: coverage.xml

  build:
    needs: [lint, typecheck, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv build
      - uses: actions/upload-artifact@v6
        with:
          name: dist
          path: dist/
```

### Trusted publishing (OIDC) — no API token, ever

```yaml
# .github/workflows/publish.yml
name: Publish to PyPI

on:
  release:
    types: [published]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv build
      - uses: actions/upload-artifact@v6
        with:
          name: dist
          path: dist/

  publish:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/p/fantasy-sports
    permissions:
      id-token: write   # required — this is the whole trusted-publishing mechanism
    steps:
      - uses: actions/download-artifact@v6
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          attestations: true
```

Setup is one-time and happens on PyPI's side, not in the repo: create the
project on PyPI (or use "pending publisher" to register before the first
release), go to
`https://pypi.org/manage/project/fantasy-sports/settings/publishing/`, and
register this exact repo + workflow filename (`publish.yml`) + environment
name (`pypi`) as a trusted publisher. After that, `id-token: write` is the
entire authentication story — no `PYPI_API_TOKEN` secret ever exists in this
repo. `attestations: true` additionally has the action generate Sigstore
attestations binding the published wheel/sdist to this exact GitHub Actions
run, which `pip install` can (increasingly) verify.

### Scheduled canary — separate workflow, live ESPN, files an issue on drift

```yaml
# .github/workflows/canary.yml
name: ESPN Canary

on:
  schedule:
    # Daily at 06:00 UTC in season; weekly cadence can be a second cron
    # entry commented in/out by month, or handled by a step that no-ops
    # outside the NFL season window.
    - cron: "0 6 * * *"
  workflow_dispatch: {}

permissions:
  contents: write   # to commit health.json
  issues: write      # to auto-file on drift

jobs:
  canary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv sync --group dev
      - name: Run live canary suite
        id: canary
        env:
          FANTASY_SPORTS_ESPN_S2: ${{ secrets.ESPN_S2 }}
          FANTASY_SPORTS_SWID: ${{ secrets.ESPN_SWID }}
          FANTASY_SPORTS_CANARY_LEAGUE_ID: ${{ vars.CANARY_LEAGUE_ID }}
        run: uv run pytest tests/canary -m canary --json-report --json-report-file=canary-report.json
        continue-on-error: true
      - name: Publish health.json
        run: uv run python scripts/publish_health_manifest.py --canary-result canary-report.json
      - name: Commit health.json
        run: |
          git config user.name "fantasy-sports-canary"
          git config user.email "actions@github.com"
          git add health.json
          git diff --cached --quiet || git commit -m "canary: update health.json [skip ci]"
          git push
      - name: File issue on drift
        if: steps.canary.outcome == 'failure'
        run: uv run python scripts/file_drift_issue.py --canary-result canary-report.json
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

`scripts/publish_health_manifest.py` and `scripts/file_drift_issue.py` are
project-specific glue (parse the pytest JSON report, diff against the
previous `health.json`, decide `SCHEMA_DRIFT` vs `PROVIDER_UNAVAILABLE`,
`gh issue create` with the templated body ARCHITECTURE.md §11.2 shows) —
out of scope for this packaging research, called out here only so the
workflow shape is concrete and the two scripts have named homes.

**Sources:**
[gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish) ·
[PyPI trusted publishers docs](https://docs.pypi.org/trusted-publishers/using-a-publisher/) ·
[GitHub OIDC in PyPI](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-pypi) ·
[setup-uv GitHub Action](https://github.com/astral-sh/setup-uv) ·
[Using uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/) ·
[mypy vs Pyright vs ty (2026)](https://www.danilchenko.dev/posts/ty-vs-mypy-vs-pyright/) ·
[Pyrefly vs mypy vs ty (2026)](https://www.danilchenko.dev/posts/pyrefly-vs-mypy-vs-ty/)

---

## 8. Versioning and release

**Recommendation: `uv version` for the mechanics, GitHub Releases'
auto-generated notes for the changelog, no `release-please` and no
`towncrier`.**

Reasoning, given this is a solo-maintainer project:

- **`release-please`** shines when a team is disciplined about Conventional
  Commits and wants fully automated version-bump PRs from commit history.
  Overhead (commit-message linting, a bot-maintained release PR to review and
  merge) isn't worth it for one maintainer who can run one command.
- **`towncrier`** shines when contributors are expected to drop a news
  fragment file per PR, assembled into a curated changelog at release time.
  That workflow assumes a PR-per-change cadence with multiple contributors
  reviewing fragments — real overhead for a solo maintainer, and
  ARCHITECTURE.md doesn't describe a contributor-heavy workflow for v0.1.
- **`uv version`** (built into `uv` itself, no extra dependency) reads and
  bumps the static `version` field in `pyproject.toml` directly:
  `uv version --bump patch` / `--bump minor` / `--bump major`, or
  `uv version 0.2.0` to set exactly. Since the project already standardized on
  `uv` for everything else, this is zero new tooling.
- **GitHub's own `--generate-notes`** (`gh release create vX.Y.Z
  --generate-notes`) builds release notes from merged PR titles since the
  last tag automatically — genuinely good output with zero configuration,
  and it's what triggers `publish.yml` above (`on: release: types:
  [published]`).

**The release mechanism, concretely:**

```bash
# release.sh — run by hand when ready to ship
set -euo pipefail
uv version --bump "${1:-patch}"          # edits pyproject.toml
NEW_VERSION=$(uv version --short)
git commit -am "Release v${NEW_VERSION}"
git tag "v${NEW_VERSION}"
git push && git push --tags
gh release create "v${NEW_VERSION}" --generate-notes
# → publish.yml fires on release:published, builds, and does the OIDC publish
```

This is intentionally the simplest mechanism that satisfies "automated" for a
solo maintainer: one script, one command, zero bot PRs to babysit. If the
project later grows real outside contributors and PR volume, revisit
`release-please` then — the cost/benefit flips with contributor count, not
with project maturity per se.

**Sources:**
[uv CHANGELOG](https://github.com/astral-sh/uv/blob/main/CHANGELOG.md) ·
[uv-dynamic-versioning](https://pypi.org/project/uv-dynamic-versioning/) (evaluated, not recommended — solves VCS-tag-driven versioning for cases wanting the git tag itself to be the source of truth, which adds indirection this project doesn't need over a plain `uv version --bump`) ·
[Towncrier docs](https://towncrier.readthedocs.io/en/stable/release.html)

---

## Appendix: files this research produced, ready to copy into the repo

- `pyproject.toml` — §1 (complete)
- `tests/conftest.py` — §4 `vcr_config` fixture + scrubbing functions
- `src/fantasy_sports/cli/app.py`, `cli/render.py`, `commands/league.py`,
  `errors.py` — §3 (skeleton, extend per command)
- `src/fantasy_sports/auth/keychain.py`, `auth/resolve.py` — §5 (complete)
- `src/fantasy_sports/config/paths.py` — §6 (complete)
- `.github/workflows/ci.yml`, `publish.yml`, `canary.yml` — §7 (complete except
  the two canary glue scripts, which are project logic, not packaging)
- `release.sh` — §8 (complete)

**One correction to make in `ARCHITECTURE.md` itself:** §2's toolchain table
references `uv build --standalone`, which does not exist as a `uv` feature
(confirmed against current Astral docs, Aug 2026) — see §2 above for the real
options and the recommendation to skip a standalone binary for v0.1 entirely.
