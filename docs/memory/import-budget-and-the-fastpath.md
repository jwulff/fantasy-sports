# Why `cli/fastpath.py` exists

**Found:** 2026-09-05, building U1 (#2).

ADR-0008 budgets `--version` / `--help` cold start at **50 ms**. Importing
`typer` alone costs **~44 ms** on John's M-series Mac, before interpreter
startup and before a single line of our own code. An eager `import typer` at
module scope cannot meet the budget on any machine, so the budget is not a
tuning target — it is a structural constraint on where typer may be imported.

`fantasy_sports/cli/fastpath.py` answers `--version`, `--help`, and the no-args
case from the command registry, importing nothing beyond the standard library.
Anything else falls through to `cli/app.py`, which imports typer inside
functions. Measured after this split: **12.5 ms** for `--version`, **17.5 ms**
for `--help`.

The trap this creates: **two help surfaces that can drift.** Both are generated
from `fantasy_sports.commands.REGISTRY`, and `tests/unit/test_cli.py` asserts
the fast-path help lists exactly what is registered. A command becomes visible
by being registered and in no other way. Do not hand-write a command into
either surface.

`scripts/check_startup.py` measures the real console script (min of 15 runs)
and fails CI over the budget. The plan named `hyperfine`; a subprocess loop is
used instead so the gate needs no Rust toolchain in CI and behaves identically
on a laptop.

Related: [[typer-vendors-click]]
