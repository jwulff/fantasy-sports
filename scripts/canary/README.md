# The ESPN canary

ADR-0005 server side. Detection only — see "Scope" below for why.

```bash
uv run python scripts/canary/run.py            # respects the season cadence gate
uv run python scripts/canary/run.py --force     # always runs (used by workflow_dispatch)
```

Scheduled by `.github/workflows/canary.yml`. Uses no credentials — it reads
ESPN's public test league (`1234`, `2018`), the same league `espn-api`'s own
CI has hit daily and unattended for years and the only league this repo may
commit a fixture from (`docs/testing.md` §6).

## Why this exists

Every prior ESPN fantasy tool died the same way: ESPN changed something, the
maintainer didn't notice for months, users hit raw stack traces, the repo
looked abandoned (`docs/memory/prior-art-graveyard.md`). Detecting breakage
before an agent or a user hits it live is `docs/ARCHITECTURE.md` §11's
single highest-leverage decision.

## Scope

jwulff/fantasy-sports#11's original acceptance criteria included auto-filing
a GitHub issue on drift and publishing/updating a public `health.json` in
the repo. The [2026-08-28 review
comment](https://github.com/jwulff/fantasy-sports/issues/11#issuecomment-5448649774)
reclassified that: John's own agents run unattended against a live season on
an API mid-overhaul, which is the exposure detection exists to close —
*before* a decision gets made against broken data. **Detection is core
scope. The public-facing health manifest and auto-filed issues stay
deferred**, split into a follow-up issue (linked from #11).

So this canary:

- **Does** run on a schedule, hit the public league with no credentials,
  assert on the response shape, and fail loudly and distinguishably when
  something's wrong. A failed scheduled workflow run is itself the
  detection signal — GitHub's own Actions failure notification is what
  reaches a human today, same as any other broken workflow.
- **Does not** open, update, or deduplicate a GitHub issue.
- **Does not** publish or update anything in the repo. `shape_manifest.json`
  next to this file is committed by a human (or an agent, reviewed like any
  other change) when the shape genuinely and legitimately changes — see
  "Updating the manifest" below — never by the workflow itself.

`shape_manifest.json` is **not** the ADR-0005 §11.2 public `health.json`.
That file carries `latest_version`/`known_issues` for end users reading it
from `raw.githubusercontent.com`, and doesn't exist yet — it's part of the
deferred follow-up. This one is a private structural fingerprint the canary
diffs itself against, run to run.

## Classification

Three-way, because a live-league smoke test can go red for reasons that have
nothing to do with ESPN. The concrete case study
(`docs/research/03-espn-api-surface.md` §4.2): `espn-api`'s own live daily
canary went red for **12 consecutive days** (2026-08-07 → 2026-08-18) from a
transitive `idna` release breaking *import* — a `TypeError` at module load,
before any HTTP request was ever made — not from ESPN changing anything. A
canary that cries wolf on its own build breakage trains everyone to ignore
it, which is the exact failure this design exists to prevent.

| Classification | Means | Built by |
|---|---|---|
| `BUILD_ERROR` | An exception before any HTTP request was attempted — an import failure, a broken dependency resolution. | `run.py`, directly (nothing in `shapes.py` was reachable yet) |
| `CANARY_INFRA` | A request was attempted and failed before a parseable JSON payload came back — connection error, 401/404/429, anything the app's own error taxonomy maps to something other than success. | `run.py`, directly |
| `SCHEMA_DRIFT` | A real payload came back and `shapes.py`'s own assertions found something wrong with it. | `shapes.classify()` |
| `OK` | None of the above. | `shapes.classify()` |

**Only `SCHEMA_DRIFT` is evidence ESPN changed something.** `BUILD_ERROR` and
`CANARY_INFRA` still fail the workflow run — both are worth a human's
attention — but the run's summary makes which one happened impossible to
miss, which is the whole point: today nothing distinguishes them at all.

`uv sync --locked` in both the workflow and local dev is the primary defense
against a repeat of the `idna` incident: it pins as tightly as the runtime
(same lockfile, never a loose resolve) and refuses to silently pick up a
newer transitive dependency.

## What gets checked, against a real payload

Reusing `EspnProvider._bootstrap_payload`'s exact combined `view=` request
(`mTeam+mRoster+mMatchup+mSettings+mStandings` — `espn_api/requests/espn_requests.py`'s
`get_league()`), not separate per-view calls: `docs/research/03-espn-api-surface.md`
§7.7, "two views combined != two views called separately."

1. **Required paths** (`shapes.REQUIRED_PATHS`) — the exact dotted paths
   `docs/research/03-espn-api-surface.md` §4.4 names as the ones every model
   constructor in `providers/espn.py` does unguarded dict access on. Any
   missing → `SCHEMA_DRIFT`.
2. **Enum coverage** — every `defaultPositionId`/`eligibleSlots`/`proTeamId`
   seen against `espn_api`'s own `POSITION_MAP`/`PRO_TEAM_MAP`. An unmapped
   id degrades silently (`POSITION_MAP.get(x, '')`) rather than raising —
   §4.4 calls this "the cheapest, highest-signal canary assertion
   available," and it's exactly how #662's new hockey position id was
   found: manually, by a user, after the fact. Any gap → `SCHEMA_DRIFT`.
3. **Manifest signature diff** — a shallow structural fingerprint (sorted
   key sets at the load-bearing object shapes: `status`, `settings`,
   `settings.rosterSettings`, `teams[]`, `teams[].record.overall`,
   `teams[].roster.entries[]`, and the nested player object) diffed against
   `shape_manifest.json`. A key that **disappeared** → `SCHEMA_DRIFT`. A key
   that's **new** is reported (visibility matters — it's the general form
   of the #662 case) but does not on its own fail the run; ESPN adding a
   field is common and isn't a break.

Everything here is offline-testable and tested in
`tests/unit/test_canary_shapes.py` against hand-built fixtures, not a live
recording — proving ESPN still sends the documented shape is this script's
job; proving the diff/classification logic reacts correctly to each way a
shape can fail to match is the test suite's.

## What is deliberately not checked

`docs/research/03-espn-api-surface.md` §4.4 also recommends tracking
run-to-run access-policy regressions (a call that returned 200 yesterday
returning 401 today, for the same unchanged league/year/view) and a
stat-ID coverage check against `PLAYER_STATS_MAP` from a real box score.
Neither is implemented here:

- **Access-policy-regression tracking** needs state persisted across
  scheduled runs (GitHub Actions runners are stateless). Doing that without
  either committing state to the repo (which is the deferred public
  manifest's job, not this one's) or standing up the GitHub Actions
  artifact/cache API for it was judged out of size for this issue. Left as
  a note on the follow-up issue.
- **Stat-ID coverage** needs a real box score, and league `1234` can't
  produce one for season `2018` — `espn-api` refuses box scores before
  2019, and `1234` only exists for 2018 (`docs/testing.md` §1,
  `tests/live/test_espn_live.py::test_the_canary_still_cannot_produce_a_box_score`).
  Not checkable from this canary league at all.

## Updating the manifest

When ESPN legitimately and intentionally changes shape — a new field that
should be adopted, not treated as drift forever — regenerate the manifest
from a real live fetch and commit it like any other change:

```python
import sys, json
from datetime import datetime, timezone

sys.path.insert(0, "src")
sys.path.insert(0, ".")
from fantasy_sports.providers.espn import EspnProvider
from scripts.canary.shapes import extract_signature

payload = EspnProvider().fetch_raw(
    "1234", 2018, view=["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]
)
manifest = {
    "schema": "fantasy-sports-canary-shapes/v1",
    "league": "1234",
    "season": 2018,
    "views": ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"],
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "generated_by": "<why -- e.g. jwulff/fantasy-sports#NN>",
    "signature": extract_signature(payload),
}
with open("scripts/canary/shape_manifest.json", "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
```

Review the diff like any manifest change: a shrinking `signature` is a
regression in what's being checked, not an improvement.

## Season cadence

Daily in season, weekly in the offseason (ADR-0005). `run.should_run_today()`
treats September–December, plus the first 13 days of January, as in-season —
a heuristic (NFL fantasy playoffs typically wrap by early-to-mid January),
not something derived from ESPN's own schedule, because deriving it from
ESPN would require a successful request just to decide whether to make one.
Outside that window the canary runs only on Mondays. `--force`
(`workflow_dispatch`) always runs regardless — a manually-fired canary
should never no-op silently.
