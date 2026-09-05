# Project Memories — fantasy-sports

Persistent context for this project. Scan this index on session start; open
individual files when their topic is relevant.

| Memory | Hook |
|---|---|
| [prior-art-graveyard.md](prior-art-graveyard.md) | Why every previous ESPN fantasy tool died, with dates — the reason the health system exists |
| [typer-vendors-click.md](typer-vendors-click.md) | typer 0.27 bundles click privately and drops the dependency — `import click` fails at runtime, on an error path |
| [ruff-format-rewrites-markdown.md](ruff-format-rewrites-markdown.md) | `ruff format .` silently reformats Python inside `docs/*.md` — why `extend-exclude = ["docs"]` must stay |
| [import-budget-and-the-fastpath.md](import-budget-and-the-fastpath.md) | Importing typer costs 44 ms against a 50 ms budget — why the argparse front door exists and how the two help surfaces stay in sync |
| [cassette-scrubbing-blind-spots.md](cassette-scrubbing-blind-spots.md) | A gzipped or `!!binary` cassette body defeats the SWID scrubber and a grep-based scan — why `decode_compressed_response` and the structural scan pass exist |

Nothing here should duplicate what the code, git history, `CLAUDE.md`, or
`docs/ARCHITECTURE.md` already record.
