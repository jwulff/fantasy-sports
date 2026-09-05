# `config.toml` belongs to every layer, not just `leagues`

**Found:** 2026-09-05, building U3 (#7). **Applies to:** any unit that reads
`config.toml` or resolves an XDG path — U4 (auth), U6 (cache), U9 (CLI).

Three things bit, or nearly bit, while writing `config/leagues.py` and
`config/paths.py`. None of them is visible from the plan.

## 1. Do not reject unknown top-level keys

The first version of the parser rejected any top-level key that was not
`default` or `leagues`, on the reasonable theory that `defualt = "dynasty"`
should fail loudly rather than silently mean "no default".

That theory is wrong for this file. ARCHITECTURE §6 puts the credential
fallback — the third link in the `env → Keychain → config file` chain — in this
same `config.toml`. A strict league parser would have thrown on the first
`[auth]` section U4 writes, from a module U4 never touched. The top level is a
**shared namespace across layers**; every layer must read its own keys and
ignore the rest.

Inside `[leagues.<name>]` the namespace is exclusively the league layer's, so a
typo there *is* rejected. That is the line: reject unknown keys where you own
the namespace, tolerate them where you share it. Apply the same rule when the
auth and cache layers add their sections.

## 2. A path test must clear the XDG variables, not just point `HOME` at `tmp_path`

`paths.config_home()` reads `XDG_CONFIG_HOME` first and only falls back to
`~/.config`. A test that monkeypatches `HOME` to `tmp_path` and stops there
still reads the *developer's real* `XDG_CONFIG_HOME` if one is exported —
passing on a CI runner that has none and failing, or silently reading real
config, on a machine that does.

`tests/unit/test_config.py` has an autouse fixture that sets `HOME` **and**
`monkeypatch.delenv`s all three of `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and
`XDG_DATA_HOME`. Copy that fixture rather than reinventing it; the cache layer
needs exactly the same isolation for `XDG_CACHE_HOME`.

Two spec rules are also implemented and easy to miss: an **empty** XDG variable
falls back to the default, and a **relative** path in one is invalid and must be
ignored. `os.environ.get(var, default)` gets both wrong.

## 3. `bool` is an `int`, and TOML has real booleans

`season = true` in TOML parses to Python `True`, and `isinstance(True, int)` is
`True`. Without an explicit `isinstance(value, bool)` rejection ahead of the
`int` branch, that config silently produces `season=1` — a valid-looking
integer year that fails much later, against ESPN, as something else entirely.
Same for `league_id = true` becoming `"True"`.

Any TOML field typed as `int` or coerced from `int` needs the bool guard first.

Related: [[typer-vendors-click]]
