# `auth logout` clears every stored copy of the ESPN cookies (#62)

`SECURITY.md` (#54) had to tell a user whose cookie leaked to log out of
ESPN and then *overwrite* the stored pair with `auth login`, because nothing
could delete the Keychain entry or the plaintext `[credentials]` fallback in
`config.toml`. A remediation that leaves the leaked value on disk until it
happens to be overwritten is not a remediation. The agent writing
`SECURITY.md` filed #62; this is that command.

## What changed

- **`auth/logout.py`** — the removal walk, the mirror image of the chain in
  `chain.py`. Resolution stops at the first link that answers; removal
  stops at none of them, because clearing the Keychain and leaving the
  config copy behind clears nothing. Each credential gets one outcome per
  link: `removed`, `absent`, `still-set` (environment only), or
  `unavailable`. Fail-soft is decided *here*, per link: a locked Keychain
  must not stop the config file from being cleaned, and vice versa — the
  same "the guarantee lives at the layer that promises it" rule
  `docs/memory/credential-leak-channels.md` records for the chain.
- **`chain.delete_from_keychain`** reads before it deletes, because the macOS
  backend raises the same `PasswordDeleteError` for "no such entry" as for
  "could not delete", and those two have to be reported differently — one
  is *absent*, the other means the leaked value is still on the machine. It
  **raises** on a backend failure rather than returning `False`, since
  `False` would be indistinguishable from "nothing was stored", which is the
  exact answer a remediation must not get wrong. The `get_password` result is
  compared in place and never bound to a local, so no frame ever holds the
  value for a traceback to repr.
- **`config.credentials.remove_credentials`** is the write half of the
  config layer. It edits only the `[credentials]` table, carries every other
  table through untouched (wiping the user's league profiles during a leak
  cleanup would be its own incident), drops the table if it empties, writes
  atomically, and keeps the original file mode. It writes **nothing** when
  no named key is present — a logout on a host that never used the fallback
  must not create, rewrite, or even touch the file. It raises `OSError` for
  an unreadable or unwritable file rather than swallowing it the way
  `load_credentials` does, because a removal that silently did nothing
  would report a removal that did not happen; `logout.py` classifies that as
  `unavailable` with a warning.
- **`staleness.forget_stored`** drops `stored_at` for a credential whose
  Keychain entry is now gone (or was found absent). That timestamp described
  the value *this tool wrote*; leaving it behind would let a later entry
  written by some other tool inherit a confident, wrong age. `last_success_at`
  survives — it is evidence about the credential *name* and is what
  `auth status` leads with when age is unknowable.
- **The environment is reported, never changed.** A process cannot unset a
  variable in its parent's shell, a `launchd` plist, or a CI secret store.
  Every set variable is named, aliases included — `read_from_env` stops at
  the first hit, which is right for resolution and wrong here, because the
  second variable resurfaces the moment the first is unset.

## What it does not do

- Rotate anything on ESPN's side. Out of scope on the issue; the
  `SECURITY.md` steps still start with logging out of ESPN in the browser.
- Preserve comments or hand formatting in `config.toml` when it does rewrite
  it. TOML writers do not, and a credential left behind is worse than a
  comment lost. It is only rewritten when a key was actually removed.

## Testing

`tests/unit/test_auth_logout.py` runs every test against a fake `keyring`
module installed in `sys.modules` by an **autouse** fixture, because the
developer's real macOS Keychain holds real cookies under the exact service
name this command deletes by. That fixture is the safety rail for any test
added later to the file, not just the ones written now. Cases: both links
stored and cleared, the rest of the config file surviving, nothing stored
(exit 0, file untouched, no state file invented), env set (reported by
variable name, unchanged, Keychain still cleared), every alias named, no
backend, a backend that reads but will not delete, an unreadable config
file, a damaged one (`CONFIG_INVALID`, as `load_credentials` would), file
mode preserved, idempotency, and the staleness bookkeeping.
