# Re-import browser cookies when ESPN refuses the stored ones

## Why

ESPN rotates `espn_s2` while the browser session lives on, so the copy in the
Keychain drifts behind it and every read starts failing with a 401 that says
nothing useful. On the maintainer's machine (2026-10-27, via #171's
`find_espn_cookies`; booleans only) Chrome's `SWID` matched the Keychain copy and
its `espn_s2` did not. The first idea, "on `AUTH_EXPIRED`, re-import and retry",
would have been dead code: ESPN answers a bad cookie, a missing cookie and a
league you are not in with the byte-identical 401, and the adapter reports all
of them as `LEAGUE_NOT_FOUND` (`docs/memory/espn-401-tells-you-nothing.md`). So
the trigger is the one piece of evidence we can get: the browser holds a
*different* pair than the one we sent. Closes #159, part of #155.

## How

- `auth/reimport.py` (new) has two halves. `attempt` decides "is there a newer
  pair worth trying": opt-out and credential-source gates, `find_espn_cookies`,
  a per-cookie `hmac.compare_digest` (both always evaluated). It returns a
  `Reimported` holding `Secret`s, saves nothing, and never raises. `commit`
  stores the pair with the browser's expiry via `save_credentials`.
- The retry lives where the request does, not around the league build (preflight
  review: with the default cache the bootstrap can be served entirely from the
  store, so the stale cookie is first used by a later live view, outside any
  build-time wrapper). `_Transport._live` catches the library's
  `ESPNAccessDenied` (raised only after its alternate-shape probe, so a season
  that merely wanted the other URL never triggers it), asks the provider for
  fresh cookies, points the request at them and sends that one request again.
  It fires only when a complete pair was sent, which is exactly the set that
  `_access_denied` turns into `LEAGUE_NOT_FOUND` or `AUTH_EXPIRED`; the
  classification is untouched. Once per provider, hence once per CLI invocation,
  whether or not the browser had anything. A second refusal adds
  `details.reimported_from_browser`. A transport built before the swap just
  retries with the pair now in force, without another browser read or save.
- **The pair is saved after ESPN accepts it, not before the retry** (a deviation
  from the order the issue lists, found by preflight review). A 401 is ambiguous
  (wrong league id, lost membership), so saving first could overwrite good
  credentials with whichever other account a browser holds. Now a refused retry
  stores nothing and puts the sent pair back in use; a save that fails (or fails
  between the two cookie writes) reads the prior Keychain entries strictly (a locked Keychain means nothing is
  written) and, on failure, restores the Keychain exactly as it was (the real prior entries, or a delete
  for ones it created, so config-sourced credentials do not grow stale Keychain
  entries that outrank the file) and the warning says the pair could not be
  saved.
- **Writes are sent exactly once** (brief section 8 item 1; John's call on review:
  reads-only retry). A write's preflight reads go through the same transport
  retry. If the write host itself answers 401, nothing is resent: the repo has
  only seen that 401 on reversible lineup swaps, never on an irreversible add,
  drop or claim, so "401 means not applied" is unproven where a wrong guess is a
  double-apply. Instead `_post` runs the browser re-import (same SWID only),
  saves a fresh pair immediately (nothing here can prove it, but the next run
  needs it; a failed save restores the Keychain exactly and counts as no
  import) and raises the unchanged `WRITE_REJECTED kind=auth` plus
  `details.reimported_from_browser` and a remediation saying a newer espn_s2 was
  imported, nothing was sent twice, run it again. With no fresh pair the error is
  exactly the old one. The tests assert one POST, the pair saved, and one journal
  entry (status rejected).
- Eligibility is "both cookies resolved from the Keychain or config". Env-sourced,
  mixed, and plain-mapping credentials never read the browser.
- **Deviation from the issue text: a different SWID is not "a different pair".**
  The issue counts either cookie differing, and each is still compared on its
  own, in constant time. But only a profile with *our SWID* and a different
  `espn_s2` (the rotation the live evidence showed) is adopted. `add`, `drop`
  and `claim` derive team ownership from the SWID, so adopting another account's
  pair on an ambiguous 401 could execute an irreversible move as an account nobody
  chose; preflight flagged this as P0 on three successive designs. A browser
  pair for another SWID leaves the original error unchanged, and switching
  account stays an explicit `auth login`. Among same-SWID profiles the latest
  `espn_s2` expiry wins (expired cookies are not candidates; a session cookie
  ranks after any live dated one), and an inactive profile that still holds the stale
  pair does not veto it. `_post` re-checks the SWID as defence in depth.
- Warnings reach every command, not only those using `context.success`: the
  provider also posts to `core/notices.py` and `cli.app._dispatch` folds what was
  posted into the envelope (preflight found `experts --league` and `doctor --live`
  build their own). Drained at the start of a dispatch and on failure, so nothing
  leaks between runs.
- `warnings` is a new always-present envelope key (`[]` by default), the only
  place a collection-shaped `data` could carry it. Additive on the precedent of
  `raw_omitted` (ADR-0004 amendment). `commands.context.success` copies
  `provider.warnings`; `cli.app._dispatch` also prints each to stderr. All doc
  samples and goldens gained `"warnings": []` mechanically.
- `[auth] auto_reimport` lives in `config/auth.py`, reading only its own table.
  A damaged value raises `CONFIG_INVALID` from the reader, but the re-import
  treats damage as "off" so a typo can never replace ESPN's error.
- The retry rebuilds the league with the new cookies and records them as
  Keychain-sourced, so the write path's credential-age block reads the fresh
  `stored_at`.

## Validation

- `tests/unit/test_auth_reimport.py`: per-cookie difference, identical, partial
  and absent pairs, profile choice, env/mixed/unattributed sources, both opt-outs,
  damaged config, failing save/search, and no value in any rendering.
- `tests/unit/test_espn_reimport.py`: a cookie-gated transport (401 for the stale
  `espn_s2`, data for the fresh one). Different pair retries and succeeds with
  one warning and one save; same or no pair raises the original error with no retry; a second
  refusal carries `reimported_from_browser`, stores nothing and never asks again; `AUTH_EXPIRED`
  triggers it too; `fetch_raw` is retried; env/opt-out/mapping never read the
  browser; `add` whose preflight read was retried yields one journal entry and one POST;
  a write 401 with a fresh same-SWID pair yields one POST, the pair saved,
  `WRITE_REJECTED` with `reimported_from_browser` and one journal entry, and with
  no fresh pair the error is the old one;
  `teams` through the CLI prints the warning on stderr and in the envelope.
  Deleting the retry fails ten of them.
- `tests/conftest.py` now installs an in-memory `keyring` backend for every test. CI found the gap: the strict snapshot in `commit` read the real keyring, which bailed on ubuntu (no backend) and would have read the maintainer's login Keychain on a Mac. Verified under `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`, a throwaway HOME and `sys.platform=linux`.
- Nothing touches the real Keychain, browsers or network; `find_espn_cookies`
  and `write_to_keychain` are patched, and no test consults `sys.platform`.
- Full suite, ruff, budgets and startup checks, and the new tests with
  `sys.platform` patched to `linux`: see the PR body.
