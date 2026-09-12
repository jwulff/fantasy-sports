# `leagues.save()` keeps the tables it does not own (#85)

`config/leagues.py::save()` rebuilt `config.toml` from `default` and the
league profiles and nothing else. On a host without a Keychain — cron, CI,
headless Linux, the path the README's first-run section now documents — the
ESPN cookies live in a `[credentials]` table in that same file, and one
`save()` would have thrown them away without a word. Nothing called
`save()` on such a file yet, which is the only reason it had not bitten;
the `auth logout` agent noticed it while writing `remove_credentials`, which
does the round-trip correctly.

## What changed

- **`save()` edits two keys of a document instead of writing a document.**
  It reads the file back (a missing file is an empty document), drops its
  own `default` and `leagues` keys, writes the new values in, and leaves
  every other top-level table exactly as it found it — `[credentials]`, and
  any `[future]` table a later version adds. An unset `default` or an empty
  profile set *removes* the key, so a stale value from last week does not
  outlive the config that cleared it.
- **One atomic writer, in `config/document.py`.** `remove_credentials`
  already had the right helper — temp file in the same directory, `replace`,
  original mode kept — and the issue asked that `save()` share it rather
  than grow a second copy. It moved out of `credentials.py` into a module
  both can import without `leagues` reaching into `credentials`. One
  addition: a target that does not exist yet is created rather than
  `stat`-failed, and it starts `0600` (mkstemp's own default), since the
  next `auth login` may put cookies in it. The previous `write_text` left a
  new config at the umask, typically world-readable.
- **A file `save()` cannot parse is refused, not overwritten.** The other
  tables cannot be carried through a document that cannot be read, and
  replacing a broken file with a partial one would turn a syntax error the
  user can fix in seconds into data loss. It raises the same
  `ConfigInvalidError` `load()` does, through the same `_read_document`
  helper `load()` now uses.

- **The writer resolves a symlink before it replaces anything.** Codex's
  review caught that `temp.replace(link)` swaps a symlinked `config.toml`
  for a plain file and leaves the canonical copy in a dotfiles checkout
  stale — a regression from `write_text`, which followed the link, and a
  latent defect in `remove_credentials`, which never did. The writer now
  targets the referent, so both callers write through the link and the
  link survives.

## What did not change

Comments and hand formatting are still lost on a rewrite — `tomli-w`
cannot keep them, and the trade is already documented on
`remove_credentials`. Every existing `save()` test passes unchanged.

## Tests

Six new tests in `tests/unit/test_config.py`: `[credentials]` and an
unknown `[future]` table survive a save while the old profile is replaced
(the issue's own acceptance test), unset keys are removed, a `0600` file
stays `0600` with no temp file left behind, a new file is created private,
an unparseable file is left byte-for-byte alone, and a symlinked config is
written through by both `save()` and `remove_credentials`. `config/document.py`,
`leagues.py`, and `credentials.py` are each at 100% line and branch
coverage.
