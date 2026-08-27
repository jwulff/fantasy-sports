# ADR 0001: Python as the implementation language

**Status:** Accepted
**Date:** 2026-08-26

## Context

A CLI that talks to fantasy sports providers must handle ESPN's unofficial,
undocumented v3 API — and eventually Yahoo's OAuth2 API and Sleeper's public API.

The hard part of this problem is not HTTP. It is the accumulated knowledge of an
undocumented API: view parameters, scoring-setting shapes, position eligibility
maps, ID mappings, and the 2024 base-URL migration. That knowledge exists, in
working form, in exactly one place.

| Provider | Library | Version | Last release |
|---|---|---|---|
| ESPN | `espn-api` (cwendt94) | 0.46.0 | 2026-03-23 |
| Yahoo | `yfpy` | 17.0.0 | 2025-09-14 |
| Sleeper | `sleeper-api-wrapper` | 1.2.1 | 2025-11-02 |

All three are Python. There is no comparable ecosystem in Go, Rust, or TypeScript.

## Decision

**Python 3.12+**, with `uv` + `hatchling` for packaging, `typer` for the CLI
surface, and `rich` for terminal rendering.

Distribution is `uv tool install fantasy-sports`.

## Consequences

**Easier:** ESPN support is a wrapper rather than a reverse-engineering project.
Adding Yahoo and Sleeper later is likewise a wrapper each. Type annotations on
command functions do double duty as MCP tool schemas later.

**Harder:** Distribution is worse than a static binary. A cron user needs a
Python environment. `uv tool install` closes most but not all of this gap.

**Accepted risk:** We inherit a dependency on `espn-api`, which has effectively
one maintainer. Mitigation: the provider interface (ADR-0002) keeps `espn-api`
behind our own Protocol, so replacing it with direct HTTP calls is a contained
change rather than a rewrite.

## Alternatives considered

**Go** — genuinely better distribution (single static binary, ideal for cron).
Rejected because choosing Go means reimplementing `espn-api` from scratch, which
is the entire project, and then doing it twice more for Yahoo and Sleeper.

**TypeScript** — `npx` distribution is acceptable and one competing project
(`flaim`) uses it. Rejected for the same reason: no mature provider libraries.

**Rust** — same objection as Go, more so.
