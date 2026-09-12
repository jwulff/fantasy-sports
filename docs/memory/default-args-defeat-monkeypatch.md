# A default parameter value is bound once; `monkeypatch.setattr` on the module can't reach it

**Found:** 2026-09-12, building #64 (`scripts/canary/issue_filer.py`,
`scripts/canary/publish_drift.py`). **Applies to:** any function whose
default argument is a callable meant to be swappable for tests — the exact
shape of every `runner=`/injectable-client parameter in this codebase.

The first draft of `find_open_issue`/`file_drift_issue` used the obvious
pattern:

```python
def find_open_issue(signature, *, repo, runner: GhRunner = _default_gh_runner):
    ...
```

That reads as "swap `_default_gh_runner` for a fake when you need to," and
it works for every caller that passes `runner=fake` explicitly — which is
every test in `tests/unit/test_canary_issue_filer.py`. It quietly breaks for
a caller that relies on the *default* to already be a test double, which is
exactly `scripts/canary/publish_drift.py::main()`'s situation: it calls
`file_drift_issue(report, repo=..., league=..., season=...)` with no
`runner=` at all, so a test for `main()` has nothing to pass through —
the only lever left is `monkeypatch.setattr("scripts.canary.issue_filer._default_gh_runner", fake)`.

That monkeypatch **does nothing** to `find_open_issue`/`file_drift_issue`.
Python evaluates a default argument expression exactly once, at function
*definition* time (import time here), and stores the resulting object on the
function itself (`__defaults__`). Reassigning the module-level name
`_default_gh_runner` afterward does not touch the object already captured in
`__defaults__` — the function keeps calling the original real `gh`
subprocess wrapper. The test would have looked like it passed (no assertion
failed) or hung/failed opaquely, depending on whether `gh` happened to be on
`PATH` and authenticated, which is a much worse failure mode than a clean
`AttributeError`.

**Fix:** default the parameter to `None` and resolve it *inside the function
body*, where a bare name lookup happens at call time against the current
module globals:

```python
def find_open_issue(signature, *, repo, runner: GhRunner | None = None) -> int | None:
    runner = runner or _default_gh_runner
    ...
```

Now `monkeypatch.setattr(module, "_default_gh_runner", fake)` works for
every caller, whether or not it passes `runner=` explicitly, because the
lookup of `_default_gh_runner` happens fresh on each call rather than once
at import.

The general form: **any default value that is itself a name meant to be
monkeypatched must be re-resolved inside the function, not bound as the
parameter's default.** This is not specific to test doubles for subprocess
calls — the same trap applies to a default logger, a default clock
(`now=None` resolving to `time.time()` inside the body is the correct
pattern this codebase already uses elsewhere), or any other "swap this
module-level default for a test" seam.

Related: [[cassette-matching-is-a-correctness-surface]] (a different flavor
of "the obvious test setup silently tests the wrong thing").
