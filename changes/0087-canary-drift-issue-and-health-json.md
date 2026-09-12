# Canary drift auto-files an issue and publishes `health.json` (#64)

#11 shipped the ESPN canary's three-way classification but deferred the half
of its own acceptance criteria that turns `SCHEMA_DRIFT` into something
other than a red GitHub Actions run: auto-filing an issue and publishing the
ADR-0005 §11.2 public `health.json`. This is that follow-up — the last piece
of `docs/ARCHITECTURE.md` §11.5's closed loop, and what unblocks #10's
`doctor`/client-side health check, which had nothing to read from
`raw.githubusercontent.com` until now.

## What changed

- **Two-job workflow, not one job with elevated scope.** `.github/workflows/canary.yml`
  splits into `canary` (unchanged detection, `permissions: contents: read`)
  and a new `publish-drift` job (`permissions: contents: write, issues:
  write`) that runs only when `canary`'s output says `classification ==
  'schema_drift'`. This makes AC5 ("the workflow needs write access only for
  this path") a property GitHub Actions enforces at the token level rather
  than something `run.py`'s own code has to be trusted to respect — the
  detection job's token *cannot* file an issue or push a commit no matter
  what bug might exist in its Python, because it was never granted the
  scope. `run.py` gained one flag (`--report-json`) to hand its structured
  `CheckReport` to the second job without either re-fetching ESPN or
  round-tripping through `render_summary()`'s prose; `CheckReport.to_dict()`/
  `report_from_dict()` in `shapes.py` are the (de)serializers.
- **`scripts/canary/issue_filer.py`** files or updates the `auto-error`
  issue. Dedup follows `docs/research/01-telemetry-auto-issues.md` §4's
  anti-flood design, adapted from its client-telemetry proposal to this
  canary's simpler one-provider/one-endpoint case: `drift_signature()`
  fingerprints the drift from its own content — missing paths, `(field,
  value)` enum-gap pairs, and removed signature keys — explicitly excluding
  `CheckReport.detail`'s prose (rewording the summary shouldn't mint a new
  issue) and `EnumGap.context` (*which player* carried a gap is roster
  noise; the unmapped id is the signal). `find_open_issue()` searches `gh
  issue list --search "<signature> in:body"` before `file_drift_issue()`
  creates anything, using the workflow's own `gh` rather than a project-held
  token, exactly as the research doc's point 4 recommends.
- **`scripts/canary/health_manifest.py`** folds the outcome into
  `health.json`. `apply_drift()` is the pure merge (testable without a
  filesystem): it upserts the `known_issues` entry for the provider keyed by
  GitHub issue number — the same identity the dedup search above already
  ties to a signature — so a recurring run against the same open issue
  refreshes its entry in place, while a genuinely distinct drift (a
  different open issue) appends alongside it rather than replacing it.
  `known_issues[].code` is `fantasy_sports.core.errors.ErrorCode.SCHEMA_DRIFT.value`
  ("SCHEMA_DRIFT"), imported rather than retyped, because
  `fantasy_sports.health.client.ProviderHealth.issues_for` (#10) matches a
  known issue's `code` against exactly that value — the canary's own
  `Classification.SCHEMA_DRIFT` enum uses a different (lowercase) value and
  would have matched nothing. `load_health_manifest()` reads the file that
  already exists in the repo (or a fresh skeleton if it's absent or
  malformed) before merging, so `latest_version`/`min_supported_version`/
  `yanked_versions` and every other provider's entry pass through untouched
  — this canary has no opinion about PyPI releases or a second provider.
- **`scripts/canary/publish_drift.py`** is job 2's entry point. It refuses
  (no-ops, exit 0) for any classification other than `SCHEMA_DRIFT` even
  though the workflow already gates on it — belt and suspenders for AC4,
  since a wiring mistake in the workflow should not be the only thing
  standing between `BUILD_ERROR`/`CANARY_INFRA` noise and a filed issue.

## Why not give job 1 elevated permissions and call the filer directly?

That was the first design considered, and it is simpler code — one job, no
artifact hand-off, no JSON round-trip. It was rejected because it makes AC5
a matter of code review rather than infrastructure: nothing would stop a
future change to `run.py` from filing an issue on `BUILD_ERROR` except
someone noticing in review. Splitting into two jobs, gated on job 1's own
output, means the write-scope boundary is enforced by GitHub Actions itself
— the wrong job literally does not have the token to do the wrong thing.

## Why recovery to `OK` is not handled

A later canary run that classifies `OK` never calls `publish_drift.py` at
all. A provider `health.json` marks `"degraded"` stays that way until a
human edits the file — typically when they close the corresponding issue.

This was a real design question, not an oversight (ADR-0005's own
"Consequences" section calls the manifest "only as fresh as the last canary
run," which reads as an argument for refreshing it on every run, not only on
drift). Three considerations decided it:

1. The canary has no way to know whether an `OK` run means "ESPN reverted"
   or "a new release shipped the fix" — the manifest's `known_issues[].fixed_in`
   is a promise about a specific release, and this script cannot honestly
   fill that in from a live payload alone.
2. Auto-clearing `known_issues` without also closing the corresponding GitHub
   issue risks the two data stores disagreeing in the *worse* direction:
   `health.json` says healthy while the issue (and the actual bug, if it was
   never really fixed and ESPN just happened to serve a conforming payload
   once) is still open.
3. #64's acceptance criteria never asked for auto-recovery or auto-closing —
   only for filing/updating on drift. Building it anyway would be exactly
   the kind of scope creep `~/.claude/CLAUDE.md`'s issue workflow exists to
   prevent; it is called out here so a follow-up issue can pick it up
   deliberately if the "degraded forever until a human notices" gap turns
   out to matter in practice.

## Tests

`tests/unit/test_canary_report_serialization.py` (round-trips every
`CheckReport` field through JSON), `tests/unit/test_canary_issue_filer.py`
(signature stability/independence, dedup search, create-vs-comment,
refusing non-drift reports, the cross-taxonomy `code` check),
`tests/unit/test_canary_health_manifest.py` (the pure merge, upsert-by-issue,
preserving fields this canary doesn't own, round-tripping through the real
`fantasy_sports.health.manifest.parse_manifest`), `tests/unit/test_canary_publish_drift.py`
(the CLI entry point no-ops on every non-drift classification, writes
`health.json` and calls `gh` correctly on drift), and
`tests/unit/test_canary_run_report_json.py` (`run.py`'s new flag, exercised
through `main()` against a monkeypatched `EspnProvider` for both `OK` and
`SCHEMA_DRIFT`, plus a backward-compatibility check that omitting the flag
changes nothing). Every `gh` interaction is a fake `runner` — nothing here
calls the real CLI or touches the network; `pytest-socket` would not even
catch a real `gh` subprocess, since it execs rather than opening a socket,
which is exactly why the injection point matters.
