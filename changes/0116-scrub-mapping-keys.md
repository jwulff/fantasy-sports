# Redact mapping keys, not only mapping values (#116)

## Why

`FantasySportsError` scrubs `details` at construction, which is the guarantee
that lets raise sites forget about credentials entirely
(`docs/memory/credential-leak-channels.md`, channel 2). `core.redaction.scrub`
walked that structure's *values* but not its keys.

Keys are caller-supplied strings just as much as values are, and a key is
precisely where an id goes when the value is the thing it points at.
`WriteDivergedError.expected` / `observed` are keyed by player id, so #106 hit
this and patched it — by redacting the keys inside `WriteDivergedError.__init__`
and recording the gap in ADR-0010. That protected one error type. The other
nine, plus any future raise site keying a detail map by a SWID, still carried
the key straight into `to_dict()` and out to stderr.

## How

- `scrub()` redacts string mapping keys as well as values, recursively. It
  already recursed into values, so this is one comprehension.
- A key that is not a string is returned untouched rather than coerced. `scrub`
  preserves shape, and turning `4046` into `"4046"` would be a change every
  caller inherits for no security reason — a non-string key is not a
  credential.
- `FantasySportsError.__init__` now routes `details` through `scrub(dict(...))`
  instead of re-implementing the walk with `{key: scrub(value) for ...}`. The
  hand-rolled comprehension was the bug: it looked like it covered the whole
  mapping while covering half of it.
- `_record_detail` scrubs its key too, since it is the other way a key reaches
  `details` after construction.
- `WriteDivergedError.__init__` drops its hand-rolled key redaction in favour of
  `scrub`. It keeps the local `str` coercion, because those maps are declared
  `Mapping[str, Any]` and land in a JSON payload, so a caller that keys one with
  an int still gets the string form it always did.

## Validation

- `tests/unit/test_redaction.py`: `scrub` replaces a registered value used as a
  key at two depths, leaves an ordinary key and a non-string key alone, and a
  control test asserts an *unregistered* key survives — so the first test cannot
  pass by erasing keys wholesale.
- `tests/unit/test_errors.py`: parametrised over every `ERROR_TYPES` member, a
  registered `espn_s2` value and a `{GUID}` are placed as keys at two depths in
  `details` and neither reaches `to_dict()`. Parametrising over the whole
  taxonomy is the point — a single-class test is how the gap survived #106.
- Both new tests were confirmed to fail against the unpatched `scrub` (12
  failures: all ten error types, `_record_detail`, and the `scrub` unit test).
- `uv run pytest -m "not live"`, `uv run ruff check .`, and
  `uv run ruff format --check .` all pass.

Closes #116.
