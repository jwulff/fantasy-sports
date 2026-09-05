# The taxonomy has no code for "you passed a bad argument"

**Found:** 2026-09-05, building U8 (#9). **Applies to:** every command that
validates an argument, and any unit tempted to add a taxonomy code.

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

**If only the provider knows, it is `CONFIG_INVALID`.** Of the seven codes it
is the only one whose semantics are "a human must change something; retrying
unchanged cannot work" — `retryable=False`, user-fixable, and explicitly *not*
"try a different `--league`". Its `agent_action` mentions a config file, which
is the part that fits imperfectly and is why this is written down.

**`LEAGUE_NOT_FOUND` stays for things that are genuinely not in the league.**
An unknown `--team`, an ambiguous team name, a non-numeric league id. That is
the adapter's own precedent (`fetch_roster` raises it with "run
`fantasy-sports teams`"), and diverging from it in the layer above would mean
two answers to one question.

If a future unit adds an eighth code for this, `commands/free_agents.py` and
`commands/raw.py` are the two call sites to move, and both say so.

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
