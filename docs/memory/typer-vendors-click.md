# typer 0.27 vendors click — never `import click`

**Found:** 2026-09-05, building U1 (#2). **Applies to:** every unit that touches
the CLI surface or handles a CLI error.

`typer` 0.27.2 bundles click as the private module `typer._click` and **no
longer declares `click` as a dependency**. In a normal install of this project
`import click` raises `ModuleNotFoundError`.

This matters because nearly every published example of "return an exit code
from typer without `standalone_mode`" — including the pattern in
`docs/research/04-python-cli-packaging.md` §3 — catches `click.exceptions.Exit`
and `click.ClickException`. That code imports cleanly, passes review, and then
fails at runtime *only on an error path*, which is the worst place to discover
it. It cost one red test on its first use.

**Use typer's public surface instead:**

| Situation | Catch |
|---|---|
| Explicit exit | `typer.Exit` (has `.exit_code`; subclasses `RuntimeError`, **not** `TyperException`) |
| Ctrl-C / abort | `typer.Abort` |
| Usage errors, bad parameters | `typer.TyperException` — duck-type `.show()` and `.exit_code` |

Catch `Exit` and `Abort` before `TyperException`; they are unrelated in the
class hierarchy. `typer.BadParameter` is also public and safe.

Two tests hold the line, both in `tests/unit/test_no_click_import.py`: an AST
scan asserting no module under `src/` imports `click` or any `typer._*`
private module, and an assertion that `click` genuinely is not importable in
the environment. If a future dependency drags click back in, the second test
fails and tells you the guarantee changed.

Related: [[import-budget-and-the-fastpath]]
