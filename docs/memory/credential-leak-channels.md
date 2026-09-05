# The three channels a credential leaks through, and none of them is `print()`

**Found:** 2026-09-05, building U4 (#5). **Applies to:** every unit that holds,
passes, or fails while holding an `espn_s2` / `SWID` value — the provider
adapter (#6), the output layer (#4), the commands (#9), and the canary.

`CLAUDE.md` rule 5 reads as "don't print secrets", which is the easy half. The
three channels that actually leak in this codebase are all *automatic* — no
line of code asks for them.

**1. Any `repr` of a frame's locals.** `traceback.TracebackException(...,
capture_locals=True)` reprs every local in every frame, and so does **pytest's
own failure output**: a red test prints the arguments and locals of the failing
call. A raw `str` credential sitting in a frame therefore lands in the CI log
of a failing build, where it is durable and public. That is the whole reason
credentials are carried in `auth.chain.Secret` rather than as `str` — the
wrapper redacts in `repr`, `str`, and `format`, so it survives all three
renderers. `tests/unit/test_auth.py` holds this with a matched pair: one test
asserts a `Secret` in a captured-locals traceback is redacted, and a control
test asserts a plain `str` in the same position *does* leak, so the first test
can never quietly stop measuring anything.

**2. A message a caller formatted.** `AuthError` scrubs its message at
construction through `auth.chain.redact()`, against a process-global set of
every value ever wrapped in a `Secret`. That looks over-built until you notice
the leak is usually written by someone else — a future `raise SchemaDriftError(f"GET {url}")`
where `url` carries `?espn_s2=…`. Scrub in the error base class, not at each
raise site; raise sites are where this gets forgotten. **If #3's
`core/errors.py` becomes the shared base, that base must scrub too**, or this
protection is lost for every error type except `AuthError`.

**3. Cassettes and telemetry.** Already covered by `conftest.py` scrubbing, but
the same `redact()` is available and is the cheapest way to be sure.

## The other trap: fail-soft belongs to the chain, not to the reader

`read_from_keychain` swallows backend errors, so it looked like the
locked-keychain fallback was covered. It was not: `resolve_credential` accepts
an injected reader, and an injected one that raised propagated straight out and
killed the chain. Caught by the parametrised locked-keychain test, fixed by
wrapping the reader call in `resolve_credential` too. **A soft-failure
guarantee has to live at the layer that promises it**, not in whichever
implementation happens to be plugged in today.

## And: an age you cannot attribute is not an age

`auth status` reports `stored_at` only when the resolved credential actually
came from the Keychain — the place we wrote it. If the chain resolved from the
environment or a config file, that value may be an entirely different cookie,
and reporting our timestamp against it would be a confident wrong answer, which
is worse than `age_basis: "unknown"`. Same discipline as the refusal to predict
expiry: report what was observed, label what was not.

Related: [[typer-vendors-click]]
