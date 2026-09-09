# `doctor` reports findings; it must never itself raise

**Found:** 2026-09-08/09, building U10/U11 (#10). **Applies to:** every check
inside `commands/doctor.py`, and any future health-system code that calls
`config.leagues` or makes a network request on `doctor`'s behalf.

`doctor`'s whole value proposition is "one call that answers what is wrong
before you start guessing" (ADR-0005 §11.4). That promise is void the moment
`doctor` itself can crash — a broken `config.toml` becoming a *stack trace*
instead of a `fail` finding is a worse experience than the individual command
failures `doctor` exists to preempt. Two traps found while building it, both
caught by tests rather than by review.

## 1. A downstream check can raise on the exact input an earlier check already handled

`_config_check()` catches `ConfigInvalidError` from `config.leagues.load()`
and reports it as a `fail` finding. `_leagues_reachable_check()` originally
called `config.leagues.list_leagues()` **uncaught** — same underlying parse,
same exception type, but nothing there to catch it. A `config.toml` that does
not parse took down the whole `doctor` command instead of being reported once.

The general form: when two checks read the same fallible resource, only the
*first* one being defensive is not enough. Grep for every call site of a
function that can raise, not just the one you are adding.

## 2. A boolean config value has two failure directions, and "fixed" can still be backwards

`health.client.is_opted_out()`'s config-file check reads `health_check` from
`config.toml` and must opt out only when it is explicitly `false`. The first
version:

```python
def _config_disables_health_check(config_path):
    ...
    return value if isinstance(value, bool) else None

# in is_opted_out():
return _config_disables_health_check(config_path) is True
```

`health_check = false` parses to Python `False`, which is a `bool`, so the
helper correctly returns `False` — and then `is_opted_out` checks whether that
return value **is `True`**, which it never is. The opt-out was silently
inverted: `health_check = false` did not opt out, and `health_check = true`
did. Every unit test that asserted the *env var* opt-out passed; only a test
that asserted the *config-file* opt-out specifically caught it
(`tests/unit/test_health_client.py::test_config_toml_health_check_false_opts_out`).

The fix renamed the helper to `_config_health_check_value` (it returns the
raw value, not a verdict) and made the call site read `is False` — the
distinction that matters. **A three-way return (`True` / `False` / `None`)
needs a test for the `False` case specifically**, not just "truthy" and
"absent"; a test suite that only exercises `None` and `True` cannot catch this
class of inversion.

Related: [[config-toml-is-a-shared-namespace]], [[no-code-for-a-bad-argument]]
