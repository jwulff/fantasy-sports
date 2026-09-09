# ADR 0004: Versioned output contract and machine-readable error taxonomy

**Status:** Accepted
**Date:** 2026-08-26
**Amended:** 2026-09-05 — `CONFIG_INVALID` added to the taxonomy
(jwulff/fantasy-sports#35, decided on #6); the envelope's full key set, the
`remediation` key, and the exit-status table fixed by the output layer
(jwulff/fantasy-sports#6). **Amended:** 2026-09-08 by
[ADR-0009](0009-config-invalid-covers-a-bad-argument-too.md) —
`CONFIG_INVALID` also covers an argument only the provider can validate (no
eighth code); its `agent_action` no longer names a config file specifically,
and it gains a `details.kind` (`"config"` or `"argument"`) discriminator
(jwulff/fantasy-sports#48).

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
| `CONFIG_INVALID` | `config.toml` will not parse | Ask the human to fix the file |
| `PROVIDER_UNAVAILABLE` | Upstream 5xx / timeout | Retry with backoff |
| `RATE_LIMITED` | Throttled | Retry after `retry_after` |
| `SCHEMA_DRIFT` | Response shape unrecognized | Stop; file an issue |

Adding a code is an API change and requires a version consideration.

### Amendment, 2026-09-05: `CONFIG_INVALID`

A malformed config file had no code, and the config layer shipped with
`ConfigError.code = None` as a deliberate seam. Uncoded is the wrong resting
place: the taxonomy exists so an agent knows what to do next, and an error
rendered generically invites the retry loop the taxonomy was built to prevent.

`LEAGUE_NOT_FOUND` is the near-miss and is actively wrong here. It tells an
agent to retry with a different `--league`, which cannot possibly succeed when
the file itself will not parse. `CONFIG_INVALID` is the one instruction no
existing code gives: *user-fixable, not retryable, and no amount of agent
retrying changes it.*

Adding a code is an API change, which is an argument for doing it now rather
than later — the package is `0.1.0.dev0`, nothing is published, and no consumer
parses the taxonomy yet. This is the cheapest moment the change will ever have.

### Amendment, 2026-09-05: the full envelope, `remediation`, and exit statuses

Three things the original decision left to the implementing unit, fixed here by
jwulff/fantasy-sports#6 because a consumer now exists that parses them.

**1. The envelope's complete key set.** The shape above was illustrative; this
is the contract. Success and failure carry the *identical* keys, and `data` and
`error` are the discriminator — exactly one is non-null. A consumer that has to
branch on which keys *exist* before it can branch on what happened is a consumer
that will get it wrong once.

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-08-26T18:04:11Z",
  "data_as_of": "2026-08-26T17:56:11Z",
  "data_age_seconds": 480,
  "sources": [
    {"name": "mTeam", "fetched_at": "2026-08-26T17:56:11Z", "age_seconds": 480, "cached": true}
  ],
  "untrusted": {},
  "data": [],
  "error": null
}
```

`data_as_of` / `data_age_seconds` report the **oldest** contributing upstream
fetch and `sources` itemizes each one, which is origin R4. `untrusted` is a
path-to-string map reserved **now, empty**, for attacker-influenceable text;
jwulff/fantasy-sports#17 populates it. Reserving it here rather than adding it
there is the difference between #17 being a provider change and #17 being a
schema-version bump on the one contract every consumer parses.

Timestamps are UTC with a literal `Z`, always. `espn-api` builds its datetimes
with `datetime.fromtimestamp()` and no `tz=`, so they are naive and host-local;
the envelope **refuses** a naive datetime rather than guessing, and adapters
re-derive from raw epoch milliseconds.

**2. `remediation` is a first-class error key, not an entry in `details`.**
#36 put it in `details` deliberately, on the grounds that promoting it is a
contract change and a refactor is the wrong place to make one quietly. It is
promoted here. Remediation is the part a caller *acts on*; leaving it in a
general-purpose bag makes reading it optional, which means some consumers will
not, and the whole point of the taxonomy is that a failure tells its caller what
to do next. `agent_action` is the class-level instruction ("ask the human to
re-auth"); `remediation` is this instance's concrete next step, naming the
environment variable or the file that would actually fix *this* failure.

Every error key is now always present, `null` when empty — `details` included.
A key that disappears when it is empty is a key some consumers will never look
for, which is the same failure mode one level down.

```json
{
  "code": "AUTH_EXPIRED",
  "message": "ESPN rejected the stored cookies.",
  "retryable": false,
  "agent_action": "Ask the human to re-extract their ESPN cookies.",
  "remediation": "Re-extract espn_s2 and SWID from DevTools, then run `auth login`.",
  "details": {"status": 401}
}
```

**3. Exit statuses, one per code.** "Cron jobs can branch on exit codes" is only
true if the codes are distinct, so nothing collapses onto `1`.

| Code | Exit |
|---|---|
| *success* | `0` |
| *unclassified crash* | `1` |
| *usage error* | `2` |
| `AUTH_MISSING` | `3` |
| `AUTH_EXPIRED` | `4` |
| `LEAGUE_NOT_FOUND` | `5` |
| `CONFIG_INVALID` | `6` |
| `PROVIDER_UNAVAILABLE` | `7` |
| `RATE_LIMITED` | `8` |
| `SCHEMA_DRIFT` | `9` |

All inside the portable range: `126`, `127` and `128+n` are claimed by the shell
for "not executable", "not found" and "killed by signal N". Changing a number is
an API change, exactly like renaming a code.

**Two rules the renderers enforce**, both about a program reading stdout:
**stdout stays byte-empty on failure** — a consumer piping it into a parser must
never receive half a payload followed by an error — and **a failure is always
JSON**, whatever `--output` asked for, because a table-formatted error is prose
again.

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
