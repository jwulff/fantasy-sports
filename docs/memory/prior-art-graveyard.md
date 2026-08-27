---
name: prior-art-graveyard
description: Survey of ESPN fantasy tooling as of 2026-08-26 and the common failure mode that killed it
metadata:
  type: project
---

Surveyed 2026-08-26, before writing any code. Every ESPN fantasy tool found was
either a library (not a CLI) or dead, and they died the same way.

| Project | Stars | Last push | State |
|---|---|---|---|
| `cwendt94/espn-api` | 948 | 2026-08-18 | Healthy — a library, not a CLI. Our dependency. |
| `derekrbreese/fantasy-football-mcp-public` | 61 | 2026-08-26 | Healthy — Yahoo only, MCP not CLI |
| `KBThree13/mcp_espn_ff` | 41 | 2025-12-19 | 8 months stale — the most-starred ESPN MCP |
| `gtonic/nfl_mcp` | 15 | 2026-08-24 | NFL news, not league management |
| `jdguggs10/flaim` | 13 | 2026-08-26 | Multi-platform, hosted, read-only |
| `Avanderheyde/espn-fantasy-cli` | 0 | 2026-07-30 | One commit, abandoned, never published |

**The failure mode, which is identical in every case:** ESPN changes something →
the maintainer does not notice for months → users hit raw stack traces → the repo
looks abandoned → people leave.

**Why this matters for us:** star count is *inverted* from maintenance in this
space. The 41-star ESPN MCP is stale while the 13-star multi-platform one ships
daily. Do not read popularity as health here.

This is the entire justification for the health system in
[[0005-health-system-canary-manifest-client-check]] — the canary exists
specifically to break this cycle by making drift visible within 24 hours instead
of months.

PyPI namespace was entirely open at survey time, which is itself evidence that
nobody has seriously shipped here.
