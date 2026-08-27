# fantasy-sports — hub

This directory is the worktree hub. The repository is `main/`.

- **`main/`** — the git checkout. Admin-only: create worktrees, merge, run
  repo-wide operations. Never edit or commit application code here.
- **`main/.claude/worktrees/<name>`** — where actual work happens, branched
  from `origin/main`.

Project instructions: `main/CLAUDE.md`. Design: `main/docs/ARCHITECTURE.md`.
