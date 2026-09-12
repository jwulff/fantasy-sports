# Security Policy

`fantasy-sports` holds your ESPN session cookies so it can read your league on
your behalf. This document explains where those cookies live, what the
project does to keep them off disk and out of logs, how to report a problem
privately, and what to do if you think a cookie has leaked.

## Supported versions

The project is pre-alpha (`0.1.0.dev0`) and has not cut a release yet.
Security fixes land on `main` only. Once versioned releases start, this
section will name which lines still receive fixes.

## Where your credentials are stored

ESPN's fantasy API is unofficial and cookie-authenticated: `espn_s2` and
`SWID` from your browser's cookie jar stand in for a login. How to find and
copy them is in the README's [First run](README.md#first-run) section; this
document covers what happens to them afterwards. The CLI resolves them in
this order, and stops at the first one it finds:

1. **Environment variables**: `FANTASY_SPORTS_ESPN_S2` / `FANTASY_SPORTS_SWID`
   (also accepts the bare `ESPN_S2` / `ESPN_SWID` / `SWID` names some other
   tools use). Checked first because a `launchd` job, a cron run, or a CI
   runner has no unlockable keychain and no terminal to prompt against.
2. **The macOS Keychain**, service name `fantasy-sports`, one entry per
   credential. `fantasy-sports auth login` prompts for both cookies with
   `getpass` (nothing echoed, nothing left in shell history) and writes them
   here; `fantasy-sports auth logout` deletes them again.
3. **`~/.config/fantasy-sports/config.toml`**, a `[credentials]` table. This
   is a plaintext fallback for hosts without a usable Keychain backend
   (headless Linux, some CI images). Prefer the Keychain wherever one exists;
   anyone who can read your home directory can read this file.

Every link in the chain fails soft. A locked Keychain, a missing backend, an
unreadable config file all fall through to the next link rather than raising,
so the end result is `AUTH_MISSING`, not a crash that might render a partial
value.

Cookie values are never accepted as command-line arguments. An argument is
visible to `ps` and to any process listing a co-tenant on the same machine can
read, so the only interactive entry point is the no-echo prompt in
`auth login`.

## What is redacted, and where

**In memory and in errors.** A resolved credential is wrapped in `Secret`,
which returns a fixed redacted string from `repr`, `str`, and `format`. That
matters because Python's own traceback machinery (and pytest's failure
output) reprs every local in every frame of a failing call: a plain `str`
credential sitting in a frame is captured in full, but a `Secret` is not.
Every error message the project raises is scrubbed at construction against
the set of values the process has ever wrapped in a `Secret`, so a future
error that interpolates a cookie into its own message still cannot leak one.

**In recorded test fixtures (cassettes).** Unit tests run offline against
recorded ESPN HTTP traffic (see `docs/testing.md`). Before a cassette is
written, request and response filters strip cookies, `Authorization` headers,
and `espn_s2`/`SWID` query parameters, and rewrite any SWID GUID echoed inline
in a response body: ESPN returns other league members' SWIDs in roster
payloads, not just yours. Those GUIDs are not simply blanked. Each one becomes
a distinct, stable pseudonym (`docs/memory/swid-pseudonyms.md`), because a
shared placeholder would destroy the owner-to-member join the payload
encodes. Recording is only trusted after the finished file is read back off
disk and scanned for the literal values that were actually sent, in every
form they could have been encoded (brace-wrapped, bare, percent-encoded). A
recording that still contains one is deleted rather than committed
(`docs/testing.md` §7).

A response body ESPN sends gzipped is decompressed before any of this runs,
not after. Scrubbing a compressed body is a no-op that still reports success
(`docs/memory/cassette-scrubbing-blind-spots.md`), so this ordering is treated
as a security-relevant invariant, not an optimization, and is held by tests.

Only two leagues' data may ever be committed to a cassette: ESPN's own public
test league, and an invented league that does not exist at ESPN. A committed
fixture from any other league, meaning any real private league including
yours, is not acceptable, scrubbed or not; see `docs/testing.md` §6 for why
redaction alone does not make that safe.

**In the local response cache.** Reads are cached to a local SQLite database
(`~/.cache/fantasy-sports`) to avoid hammering ESPN. The same body scrubbing
applies before a response is written to the cache, using a random per-store
salt rather than the cassette's fixed one, so a cached SWID pseudonym cannot
be correlated across machines the way a committed cassette's can
(`docs/memory/cache-redaction-and-tag-classes.md`). A response body that
cannot be decoded is returned to the caller but not persisted, because bytes
that cannot be read cannot be proven clean.

## Reporting a vulnerability privately

Please do not open a public GitHub issue for a suspected security problem,
in particular anything that could expose a credential, leak another league
member's data, or bypass the redaction described above.

Report privately by emailing **john@johnwulff.com** with a description of the
issue and, if you have one, a way to reproduce it. Please allow a reasonable
window to investigate and land a fix before any public disclosure.

## If a cookie leaks

`espn_s2` and `SWID` are session cookies, not API keys. ESPN does not offer a
way to rotate one in isolation through this tool. If you believe a cookie was
exposed (committed to a public fork, pasted somewhere, printed by a bug):

1. **Log out of ESPN** in your browser (or wherever you originally copied the
   cookies from). That invalidates the session the leaked values belonged to.
2. **Log back in**, which mints a new `espn_s2` and `SWID`.
3. Run `fantasy-sports auth logout`. It deletes both Keychain entries and
   removes `espn_s2` and `SWID` from the `[credentials]` table in
   `config.toml`, leaving every other key in that file alone, and reports
   per link whether it removed a value, found none, or could not reach the
   link (a locked Keychain, an unreadable file). It never prints the values.
   The environment is the one link it cannot clear: a process cannot unset
   its parent's variables, so an exported `FANTASY_SPORTS_ESPN_S2` or
   `FANTASY_SPORTS_SWID` (or one of the bare aliases) is reported as
   `still-set`, by name, and you remove it from your shell profile, `launchd`
   plist, or CI secrets yourself. If it could not reach a link at all (a
   locked Keychain, an unreadable file) it reports that link `unavailable`
   and **exits nonzero** with `CONFIG_INVALID`, because a value may still be
   on the machine; the per-link report is in `error.details.report`. Unlock
   the Keychain or fix the file and run it again. Exit 0 means every stored
   copy it could reach is gone.
4. Re-run `fantasy-sports auth login` to store the new values.
5. If the exposure went through a channel this project controls, such as a
   committed cassette, a log line, or a cache entry, please report it
   privately (above) so the underlying scrub can be fixed, not just your own
   cookie rotated.
