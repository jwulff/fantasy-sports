# Import ESPN cookies from local browsers

## Why

Getting `espn_s2` and `SWID` meant DevTools, Application, Cookies, and two
careful copy-pastes into `auth login`. The cookies already sit in the user's
browser profile, and reading them also yields the cookie's real expiry, which
`auth status` could only guess at (`age_basis: "unknown"` on the maintainer's
machine). Closes #156, part of #155.

## How

- `auth/browser.py` finds the pair in Chrome, Chromium, Brave, Edge and Arc
  (macOS, every profile, `Cookies` or `Network/Cookies`) and Firefox (macOS and
  Linux). The SQLite file is copied to a private temp dir first and the copy is
  always deleted. Linux/Windows Chrome-family stores and `v11` blobs are
  reported under `skipped`, not crashed on.
- `auth/_aes.py` is a pure-Python AES-128 *decrypt* (no sixth dependency, and
  no `openssl -K` because that puts the key in argv). Pinned to FIPS-197 C.1
  and SP 800-38A F.2.2; the S-box is derived rather than typed. The encrypt half
  that fixtures need lives in `tests/unit/_aes_encrypt.py`, so shipping code is
  not a general cipher.
- The Safe Storage password is read from `security`'s stdout (argv holds only
  the service name) and is a `Secret` the instant it leaves the function. The
  functions that hold plaintext convert any failure to a data-free
  `CookieDecryptError` *after* their `except` block, with the plaintext only ever
  inside `Secret`; raising inside the block would leave the failed frame as
  `__context__` for a captured-locals traceback to repr
  (`docs/memory/credential-leak-channels.md`). Plaintext (unencrypted) values are
  passed as `Secret` too, because a `str` argument is a frame local.
- `auth login --from-browser [NAME] [--profile DIR]` is a thin projection;
  logic is in `commands/auth.py`. Several complete profiles resolve to the latest
  `espn_s2` expiry and `profiles_seen` names the rest. Values go through the
  existing `save_credentials`, which now takes `expires_at` and hands it to
  `record_stored`.
- **typer cannot express an optional-value option** (0.27 warns that
  `is_flag`/`flag_value` are unsupported and still demands an argument), so a
  bare `--from-browser` would have been a usage error. `Param.flag_value`
  declares the default in the registry and `cli.app._fill_optional_values`
  inserts it into argv before typer parses. Passing `flag_value` through to typer
  was tried first and does nothing.
- `staleness`: `CredentialEvents.expires_at` is persisted; a known expiry replaces
  the age heuristic for a Keychain-sourced credential. New `Freshness.EXPIRED`;
  inside 7 days of expiry is `STALE` (so `doctor` already warns) with its own
  warning text. A fresh `record_stored` without an expiry clears the previous
  one, and `forget_stored` drops it, since an expiry belongs to one value.
  Environment-sourced credentials ignore it, same rule as `stored_at`.

## Validation

- `uv run pytest -m "not live" --cov`: 2543 passed, 98.99% coverage.
- Fixture Chrome stores at schema 24 and 23 (the hash prefix), Network/Cookies,
  duplicate hosts, session cookies, `v11`, wrong password, denied/missing
  Keychain, and Firefox (macOS, Linux, `-wal` sidecar), all built in `tmp_path`.
  `subprocess.run` is replaced by a failing stub in that file, so a test cannot
  reach the real `security`.
- Redaction tests render the envelope, a captured-locals traceback raised
  mid-decrypt (with a control showing a plain `str` local *does* leak), and
  `str(exc)` of each error path, against sentinel cookie and password values.
- `ruff check`, `ruff format --check`, `check_budgets.py` and `check_startup.py`
  pass (cold start 19 ms). `sqlite3`/`subprocess` are import-time absent
  (`tests/unit/test_imports.py`).
- Not verified: a real Keychain prompt against a real Chrome profile. By design
  no test touches either; the first live run is on the maintainer's machine.
- The `doctor` doc sample is machine-dependent and was not regenerated, so its
  embedded credential rows lack the new `expires_at` key.
