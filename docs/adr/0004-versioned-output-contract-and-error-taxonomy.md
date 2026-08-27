# ADR 0004: Versioned output contract and machine-readable error taxonomy

**Status:** Accepted
**Date:** 2026-08-26

## Context

The moment a cron job or an AI agent parses this tool's output, that output is an
API. Most CLIs discover this late and cannot change shape without breaking
consumers.

Separately: agents handle failure badly when errors are prose. An agent that
cannot distinguish "your credentials expired" from "ESPN is down" will retry a
permanent failure until it exhausts its budget.

## Decision

**Every successful payload is wrapped and versioned:**

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-08-26T18:04:11Z",
  "data": {}
}
```

Output format is JSON when stdout is not a TTY, a rich table when it is, and CSV
on request.

**Every error goes to stderr as JSON with a nonzero exit and a stable machine
code:**

| Code | Meaning | Correct agent response |
|---|---|---|
| `AUTH_MISSING` | No credentials configured | Ask the human |
| `AUTH_EXPIRED` | Credentials rejected | Ask the human to re-auth |
| `LEAGUE_NOT_FOUND` | Bad ID or no access | Ask the human |
| `PROVIDER_UNAVAILABLE` | Upstream 5xx / timeout | Retry with backoff |
| `RATE_LIMITED` | Throttled | Retry after `retry_after` |
| `SCHEMA_DRIFT` | Response shape unrecognized | Stop; file an issue |

Adding a code is an API change and requires a version consideration.

## Consequences

**Easier:** Agents can act correctly on failure without parsing English. Cron
jobs can branch on exit codes. Schema evolution has a defined path.

**Harder:** Every command must produce the envelope. Every error path must map to
a code. Slightly more ceremony per command.

**Accepted:** `SCHEMA_DRIFT` requires us to actually validate response shapes
rather than letting `KeyError` propagate. That is real work, and it is what makes
ADR-0005's canary actionable.

## Alternatives considered

**Unversioned JSON** — rejected; retrofitting a version after consumers exist is
miserable and this costs nothing now.

**Prose errors only** — rejected; defeats the agent-native premise.
