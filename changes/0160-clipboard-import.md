# Import cookies from the clipboard via a bookmarklet

## Why

`auth login --from-browser` (#156) covers Chrome-family and Firefox on macOS.
Safari, Windows, Linux with a keyring and remote hosts are left to DevTools and
two careful pastes. Neither ESPN cookie is HttpOnly, so page JavaScript on
fantasy.espn.com can read both: a bookmarklet can put them on the clipboard in
any browser. Closes #160, part of #155.

## How

- `auth/clipboard.py` holds the static `BOOKMARKLET`, the per-platform clipboard
  tools (`pbpaste`/`pbcopy`; `wl-paste`/`wl-copy --clear` falling back to
  `xclip`; PowerShell `Get-Clipboard`/`Set-Clipboard`), and a never-raising
  `parse_cookies` that takes `espn_s2` and `SWID` in either order and ignores
  the rest. Platform and runner are resolved at call time (not bound defaults),
  so the unit tests drive all three platforms on a Linux runner.
- The clipboard text is a `Secret` from the instant it leaves the subprocess.
  `_capture` never binds the `CompletedProcess` to a name, because its repr
  includes stdout.
- Validity is checked by `cookies_are_valid` *before* `save_credentials`. The
  captured-locals test caught that `save_credentials` rejecting a bad SWID
  leaves the plain-string `values` mapping in the raising frame, so the
  clipboard's bytes would have rendered in a traceback. The check turns the
  failure into a flag outside the `except` block and the command raises a
  data-free `AUTH_MISSING` of its own. The same shape exists in the
  `--from-browser` path; not touched here (out of scope), noted for a follow-up.
- The clipboard is cleared only after a successful store, never on a failure
  (it may hold something that is not ours to erase). Clearing is best effort and
  surfaces as `clipboard_cleared` in `data`.
- `--from-clipboard` and `--from-browser` together raise `CONFIG_INVALID` with
  `kind: "argument"` (ADR-0009). `--from-clipboard` is a plain bool flag, so it
  needs none of the `flag_value` machinery from #156.
- `auth bookmarklet` is a new registered command returning the URL and one line
  of instructions. All subprocess imports are inside functions, so `--help`
  stays inside budget; the fast-path help picks up the command from the registry.
- `render_readme_samples.py --new-auth` adds only the not-yet-committed auth
  samples offline (stubbing the clipboard tool), so no live sample is rewritten.
- README manual-setup gains the bookmarklet path; SECURITY.md gains "Importing
  cookies from the clipboard" (the clipboard briefly holds credentials, clipboard
  history may keep a copy, the command clears it).

## Validation

- `uv run pytest -m "not live" --cov`: 2598 passed, 99.21% coverage;
  `auth/clipboard.py` and `commands/auth.py` at 100% line and branch.
- Redaction tests use a sentinel clipboard and render `str`, `repr`, message,
  remediation, details and `capture_locals` tracebacks; a control test proves a
  plain `str` would leak.
- `ruff check`, `ruff format --check`, `check_budgets.py` (cold start 19.8 ms),
  `check_startup.py`, and `render_readme_samples.py --check` all pass.
- Not exercised: the bookmarklet in a real browser and the real clipboard tools
  (tests must never touch the real clipboard).
