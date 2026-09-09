# ADR 0009: `CONFIG_INVALID` covers a provider-rejected argument too

**Status:** Accepted
**Date:** 2026-09-08
**Amends:** ADR-0004 (the error taxonomy stays at seven codes; `CONFIG_INVALID`'s
class-level `agent_action` is reworded and gains a `kind` detail)

## Context

ARCHITECTURE §5's seven codes describe **the world** — credentials, the league,
the config file, the provider, the schema. None of them describes **the
invocation**. Building U8 (jwulff/fantasy-sports#9) hit this three times, in
cases the CLI cannot validate on its own because only the provider knows what
is legal:

| Argument | What ESPN does with a bad value |
|---|---|
| `free-agents --pos QUARTERBACK` | 200, with ESPN's *default* player set — not an error |
| `raw --filter players.limit=5` | ignored; 200 with the same default subset |
| `raw` with no `--view` | ESPN has no default view; the request is meaningless |

Letting any of them fall through to `output.errors.classify()` produces
`PROVIDER_UNAVAILABLE`, which is `retryable: true` with an `agent_action` of
"retry with bounded exponential backoff" — the worst possible answer, because it
tells an agent to loop forever on an invocation that can never succeed.

U8 shipped all three as `CONFIG_INVALID`, the only existing code whose
semantics are "a human must change something; retrying unchanged cannot work",
and left a written note (`docs/memory/no-code-for-a-bad-argument.md`) flagging
that its `agent_action` — "fix the config file named in the message" — does
not fit an argument, not a file. jwulff/fantasy-sports#48 asked this ADR to
settle whether that imperfect fit is worth an eighth taxonomy code.

## Decision

**No eighth code. `CONFIG_INVALID` covers both causes**, and two things about
it change to make that an honest fit rather than a borrowed one:

1. **The class-level `agent_action` no longer names a config file.** It reads
   "Ask the human to fix the input named in the message — a config value or a
   command argument. Retrying unchanged cannot work." — true for both causes,
   where the old wording was only true for one.
2. **`ConfigInvalidError` gains a `kind` field**, `"config"` or `"argument"`,
   recorded in `details.kind` on every instance. It defaults to `"config"`
   because every pre-existing raise site — a `config.toml` that will not
   parse, an unknown `provider` name, a malformed credentials file
   (`config/leagues.py`, `config/credentials.py`, `commands/context.py`) — is
   that cause, and none of them had to change. The two `commands/raw.py` sites
   and the one `commands/free_agents.py` site pass `kind="argument"`.

`kind` is metadata, not a second taxonomy surface: it does not change `code`,
`retryable`, or the exit status. A consumer that wants to log or branch on
which cause it was can read `details.kind`; a consumer that only wants "stop
and ask a human" — the only thing that was ever load-bearing — needs nothing
new.

The three cited call sites also get sharper hint text, since "the best
possible message under the existing codes" was the alternative this ADR was
asked to deliver if it did not add a code:

- `--pos`: the raised message already lists ESPN's valid position values
  (unchanged); the remediation now points back at them explicitly rather than
  just saying "recognises".
- `--filter`: the remediation keeps its worked JSON example; `details.filter`
  now carries the raw value that failed to parse, for a human or agent
  reading the payload without re-running the command.
- `raw` with no `--view`: unchanged wording, now carrying `kind="argument"`.

## Why not an eighth code

The three options on the table were: add `ARGUMENT_INVALID` with its own exit
status; reuse `CONFIG_INVALID` with a `details.kind` discriminator; or change
nothing. The deciding question is whether a bad argument and a broken config
file ever want an agent to *do something different* — because that is the only
thing an exit code is for (ADR-0004: "cron jobs can branch on exit codes").

They don't. Both are `retryable: false`. Both mean "a human must change what
they gave us; nothing the agent retries on its own will fix it." That is a
different answer from `PROVIDER_UNAVAILABLE`'s "retry with backoff" and from
`LEAGUE_NOT_FOUND`'s "retry with a different `--league`" — which is exactly why
`CONFIG_INVALID` was worth adding in the first place, on jwulff/fantasy-sports#6
(ADR-0004's first amendment). It is not a different answer from itself. An
`ARGUMENT_INVALID` code would duplicate `CONFIG_INVALID`'s instruction under a
new name, buying agent consumers nothing they do not already have from
`retryable` and the exit status, at the cost of a ninth exit number, a ninth
ARCHITECTURE §5 row, and a permanent taxonomy-change bar (CLAUDE.md rule 4) for
every future command that hits the same shape.

The `LEAGUE_NOT_FOUND` near-miss that justified `CONFIG_INVALID` in the first
place is the test this ADR applies here, and it comes out the other way: reusing
`LEAGUE_NOT_FOUND` for a broken config file was **actively wrong** because its
instruction — retry with a different `--league` — could not possibly work.
Reusing `CONFIG_INVALID` for a bad argument is not wrong in that sense; at worst
its wording was imprecise, which is a docstring fix, not a taxonomy gap.

"Do nothing" was rejected on its own: leaving `agent_action` naming a config
file for a failure that has none costs nothing to fix and the fix is free at
this stage — the package is unpublished and no consumer parses exact prose
today, the same argument ADR-0004's first amendment made for adding
`CONFIG_INVALID` at all.

## Consequences

**Easier for agent consumers:** nothing changes for a consumer that already
branches on exit code or `retryable` — code 6 covered "ask a human, don't
retry" before this decision and still does. No consumer has to learn a new
code, and none of the six other codes or their exit statuses move.

**A little harder:** a consumer that wants to *display* the cause ("your
config file is broken" vs. "that argument isn't valid") now reads
`details.kind` instead of trusting `agent_action`'s wording to say which; that
field was never part of the stable contract's promised content before, so this
is new capability, not a removed one.

**Accepted:** the class-level `agent_action` string for `ConfigInvalidError`
changed. `agent_action` was never pinned to an exact string by any test or
documented as a literal-value contract — `retryable`, `code`, and the exit
status are what ADR-0004 calls stable — so this is not a taxonomy change under
CLAUDE.md rule 4, and needed no version bump.

**Accepted:** every `ConfigInvalidError` payload's `details` is now non-null
(it always carries at least `{"kind": "config"}`), where a handful of
pre-existing raise sites used to render `details: null`. `details` is
documented as a general-purpose, always-present, extensible bag; adding a key
to it is the kind of change that bag exists to absorb without a version bump.

## Alternatives considered

**`ARGUMENT_INVALID`, an eighth code with its own exit status (10).** Rejected
above — duplicates `CONFIG_INVALID`'s instruction to an agent under a new name,
for no behavioural gain, at the cost of permanent taxonomy surface.

**Change nothing; leave `agent_action` naming a config file.** Rejected — the
mismatch was already written down as a known rough edge
(`docs/memory/no-code-for-a-bad-argument.md`) and costs nothing to fix now,
before any consumer depends on the exact wording.

**Rename `CONFIG_INVALID` to something cause-neutral (e.g. `INPUT_INVALID`).**
Not seriously considered: renaming a code is exactly as much an API change as
adding one (ADR-0004), and every existing raise site — six of them, across
`config/leagues.py`, `config/credentials.py`, and `commands/context.py` — would
have to move for a purely cosmetic gain over reusing the code as-is.
