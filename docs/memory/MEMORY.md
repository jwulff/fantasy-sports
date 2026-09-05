# Project Memories — fantasy-sports

Persistent context for this project. Scan this index on session start; open
individual files when their topic is relevant.

| Memory | Hook |
|---|---|
| [prior-art-graveyard.md](prior-art-graveyard.md) | Why every previous ESPN fantasy tool died, with dates — the reason the health system exists |
| [typer-vendors-click.md](typer-vendors-click.md) | typer 0.27 bundles click privately and drops the dependency — `import click` fails at runtime, on an error path |
| [ruff-format-rewrites-markdown.md](ruff-format-rewrites-markdown.md) | `ruff format .` silently reformats Python inside `docs/*.md` — why `extend-exclude = ["docs"]` must stay |
| [import-budget-and-the-fastpath.md](import-budget-and-the-fastpath.md) | Importing typer costs 44 ms against a 50 ms budget — why the argparse front door exists and how the two help surfaces stay in sync |
| [runtime-checkable-proves-less-than-it-looks.md](runtime-checkable-proves-less-than-it-looks.md) | `isinstance(x, Provider)` checks method *names* only, and `issubclass` raises — why an adapter's conformance test must call every method |
| [credential-leak-channels.md](credential-leak-channels.md) | The three automatic channels a credential leaks through — captured-locals reprs, caller-formatted messages, cassettes — and why fail-soft belongs to the chain, not the reader |

Nothing here should duplicate what the code, git history, `CLAUDE.md`, or
`docs/ARCHITECTURE.md` already record.
