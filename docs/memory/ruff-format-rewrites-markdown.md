# `ruff format .` rewrites code blocks inside Markdown

**Found:** 2026-09-05, building U1 (#2).

Running `ruff format .` at the repo root silently reformatted Python code
blocks inside three `docs/research/*.md` briefs — re-wrapping comments and
collapsing alignment. It showed up only as unexpected `M` entries in
`git status` during the commit for an unrelated change.

That matters more here than in most repos. The research briefs and ADRs quote
upstream source and real config **as read at a point in time**; they are
evidence, not code we own. A formatter rewriting them destroys the provenance
that makes them worth keeping, and does it invisibly.

`pyproject.toml` now carries `extend-exclude = ["docs"]` under `[tool.ruff]`.
Do not remove it, and do not "fix" the formatting inside a research brief or an
ADR to match our style.

The general lesson: when a formatter reaches a directory of source *documents*
rather than source *code*, exclude the directory rather than accepting the
diff. Check `git status` before staging, not after.
