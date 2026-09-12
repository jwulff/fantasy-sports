# `auth logout` fails when it could not reach a link (#89)

The first comment on the launch thread (u/kantorcodes1, r/fantasyfootballcoding,
2026-09-12) read `auth/logout.py` and asked the right question: it clears
each link independently, reports an unreachable Keychain or config file as
`unavailable`, and still returns success. If it clears the Keychain but
cannot rewrite `config.toml`, should exit 0 mean best effort, or should the
exit be nonzero while a stored credential may remain?

Nonzero. The reader of this command is a script or an agent branching on
the exit status, and the one thing it must not learn from a logout is
"done" while a leaked value may still be on the machine. A warning inside a
success envelope does not reach a caller that checks `$?`.

## What changed

- **`LogoutReport.unavailable`** names every credential with a stored link
  the call could not reach, and the payload carries it beside `removed` and
  `still_set`.
- **`commands/auth.py::logout`** still clears every reachable link first,
  then raises `ConfigInvalidError(kind="credential_store")` when
  `unavailable` is non-empty, with the whole per-link report under
  `details.report` so the caller sees what did get cleared. The code is the
  taxonomy's "a human must change something; retrying unchanged cannot work"
  (ADR-0009): unlock the Keychain or fix the file, run it again, and the
  second run finishes and exits 0. No eighth code.
- **`still-set` stays exit 0.** The environment is reported, not failed:
  the command named the variables and there is nothing it could have done.
- `SECURITY.md` step 3 and the changelog say what a nonzero exit means.

## Decision worth recording

`kind` gains a third value, `credential_store`, next to `config` and
`argument`. ADR-0009 made `kind` the discriminator precisely so a new cause
would not need a new code; this is the first use of that door.
