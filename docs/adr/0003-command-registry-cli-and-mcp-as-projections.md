# ADR 0003: Commands are a typed registry; CLI and MCP are projections

**Status:** Accepted
**Date:** 2026-08-26

## Context

The tool needs to be usable three ways: by a human at a terminal, by a cron job,
and by an AI agent. Agents reach it two different ways — through a shell (which
Claude Code already has) or through MCP (which Claude Desktop and mobile need).

The naive structure puts logic inside CLI framework callbacks. That makes an MCP
adapter a rewrite, because the logic is entangled with argument parsing.

## Decision

**Command implementations are plain typed Python functions in a registry.
The typer CLI and the future MCP server are both thin projections over that
registry. No business logic lives in a typer callback.**

A second boundary: **`core/` and `providers/` have zero LLM dependency.** Only
`reports/` may call a model, and it routes through `model-route` rather than
defaulting to Claude.

## Consequences

**Easier:** `fantasy-sports mcp serve` becomes roughly 100 lines of `fastmcp`
in v0.4 instead of a rewrite. Commands are directly unit-testable without a CLI
runner. Type annotations generate MCP tool schemas.

**Harder:** Slightly more indirection than putting logic in callbacks. Requires
discipline that is easy to erode under time pressure — hence its presence in
`CLAUDE.md` as a named rule.

**Accepted:** We are paying a small structural cost now for an option we may not
exercise until v0.4.

## Alternatives considered

**Logic in typer callbacks** — simpler today, rewrite later. Rejected.

**MCP-first, CLI as a client** — rejected outright. An MCP server is a bad thing
to shell out to, which would lock us out of cron and out of Claude Code's native
Bash access. The CLI is the primitive; MCP is a presentation of it.
