# The taxonomy has no code for "you passed a bad argument" — and, settled, never will

**Found:** 2026-09-05, building U8 (#9). **Settled:** 2026-09-08, #48
(ADR-0004 amended by ADR-0009). **Applies to:** every command that validates
an argument, and any unit tempted to add a taxonomy code for this.

ARCHITECTURE §5's seven codes describe *the world* — credentials, the league,
the config file, the provider, the schema. None of them describes **the
invocation**. That is not an oversight anyone can quietly patch: adding a code
is an API change, and the command layer is the wrong place to make one.

Three of U8's arguments cannot be validated without provider knowledge:

| Argument | What ESPN does with a bad value |
|---|---|
| `--pos QUARTERBACK` | 200, with ESPN's **default** player set — not an error |
| `--filter players.limit=5` | ignored; 200 with the same default subset |
| `raw --view` (absent) | ESPN has no default view; the request is meaningless |

Letting any of them fall through to `classify()` produces
`PROVIDER_UNAVAILABLE`, whose `retryable` is `True` and whose `agent_action` is
"retry with bounded exponential backoff". That is the worst possible answer: it
tells an agent to loop forever on an invocation that can never succeed.

## The line that was drawn

**If the CLI can validate it, it is a usage error and exits 2.** `EXIT_USAGE`
was already reserved for #9 in `output/errors.py`. A bad `--output`, a missing
required `--team`, an unknown command — typer renders its own message and exits
2, which is what every argument parser on the machine does.

**If only the provider knows, it is `CONFIG_INVALID` — and it stays
`CONFIG_INVALID`, for good, not as a stopgap.** #48 asked the question this
memory left open: does the imperfect fit ever justify an eighth code? No.
`retryable=False` and "a human must change something; retrying unchanged
cannot work" is the same instruction for a broken config file and a
provider-rejected argument — that is the only thing an exit code is *for*
(ADR-0004: cron jobs branch on exit codes), and both causes want the same
branch. An `ARGUMENT_INVALID` code would duplicate `CONFIG_INVALID`'s
instruction under a new name. ADR-0009 has the full reasoning, including why
this is not the same shape as the `LEAGUE_NOT_FOUND` near-miss that justified
adding `CONFIG_INVALID` in the first place: reusing `LEAGUE_NOT_FOUND` for a
broken config file was *actively wrong* (its instruction, "retry with a
different `--league`", could not work); reusing `CONFIG_INVALID` for a bad
argument was only *imprecisely worded*, which is what changed instead of the
taxonomy:

- `ConfigInvalidError`'s class-level `agent_action` no longer names a config
  file — it says "fix the input named in the message: a config value or a
  command argument."
- `ConfigInvalidError` gained a `kind` field (`"config"` default, `"argument"`
  where a command passes it), recorded in `details.kind` on every instance.
  It is metadata for a consumer that wants to tell the two causes apart — it
  changes neither `code` nor `retryable` nor the exit status.

**`LEAGUE_NOT_FOUND` stays for things that are genuinely not in the league.**
An unknown `--team`, an ambiguous team name, a non-numeric league id. That is
the adapter's own precedent (`fetch_roster` raises it with "run
`fantasy-sports teams`"), and diverging from it in the layer above would mean
two answers to one question.

`commands/free_agents.py` and `commands/raw.py` are the two call sites — three
raise sites — that pass `kind="argument"`; both say so, and both point at #48
and ADR-0009.

## The related trap: `--no-cache` is a cache *mode*, not a missing cache

The obvious implementation of `--no-cache` is to build the provider with no
`CacheStore` at all. That is wrong, and it fails on the payload rather than on
the flag: the **store is what scrubs the response body**, so a provider without
one hands the adapter unscrubbed bytes, and `--no-cache` becomes the one flag
that changes what gets parsed
(`docs/memory/cache-redaction-and-tag-classes.md` §3).

`commands/context.py` therefore always constructs a store and varies only
`CacheMode`: `DEFAULT`, `FRESH` (refresh *and* update), `BYPASS`. A test asserts
the store exists on the bypass path, because the bug is invisible from the
command's output.

Related: [[output-contract-test-traps]], [[cache-redaction-and-tag-classes]]
