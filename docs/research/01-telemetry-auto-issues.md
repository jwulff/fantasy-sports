# Client-side error telemetry that auto-files GitHub issues

*Research brief, 2026-08-26. Informs `docs/ARCHITECTURE.md` §5 (error taxonomy)
and §11 (health system). Where this brief and the architecture doc disagree,
the architecture doc is being updated to match, not the other way around.*

## Question

`fantasy-sports` talks to ESPN's unofficial, undocumented API, which breaks
without notice. §11 already ships a **server-side canary** that detects drift
within 24h and auto-files an issue. This brief asks: should the **client**
also report failures, and if so, how — without flooding the tracker, leaking
secrets, or requiring a GitHub write token inside a public CLI?

## 1. John's existing pattern

**There is a real, mature, three-times-shipped pattern for this — but it is
server-side every single time.** `fantasy-sports` would be the first place
John has needed the client-side half of this problem, because it's the first
public, credential-bearing CLI he's shipped. That gap is the load-bearing fact
for the recommendation below.

### 1.1 The canonical port chain: lucentbrief → olfantic → (cotrugli, adjacent)

**Source of truth: `~/Development/lucentbrief/main`.** Two capabilities,
documented as ported verbatim into olfantic in
`olfantic/main/docs/brainstorms/2026-05-17-auto-issue-reporting-requirements.md`
("Direct port of the mechanism from lucentbrief… adapted to olfantic's stack").

**Epic A — auto error → GitHub issue.** `Rails.error.report` fans out to
`GitHubIssueErrorSubscriber#report`
(`lucentbrief/main/app/services/git_hub_issue_error_subscriber.rb`, 200+
lines):

```ruby
def report(error, handled:, severity:, context:, source:)
  return if handled
  return unless github_token.present? && github_repo.present?
  return if self.class.from_unreviewable_origin?(error)

  fingerprint = self.class.fingerprint(error)
  existing_record = ErrorIssueFingerprint.find_by(fingerprint: fingerprint)
  if existing_record
    add_occurrence_comment_by_number(existing_record.github_issue_number, error, context)
    return
  end

  with_fingerprint_lock(fingerprint) do
    # re-check cache under an advisory lock, then GitHub Search, then create
  end
rescue => e
  Rails.logger.error("[GitHubIssueErrorSubscriber] Failed to report error: #{e.message}")
end

def self.fingerprint(error)
  location = error.backtrace&.first&.gsub(/:\d+/, "") || "unknown"
  Digest::SHA256.hexdigest("#{error.class}:#{location}")[0, 12]
end
```

That single method is the whole anti-flood contract in miniature:

- **Fingerprint = `SHA256("#{class}:#{file_path_no_line}")[0,12]`.** Line
  numbers stripped (a fix one line above the throw site shouldn't split one
  logical bug into two issues); file path kept (the same exception class
  raised from two call sites is legitimately two different bugs).
- **Local DB cache first** (`ErrorIssueFingerprint`, a 4-line model over a
  `fingerprint UNIQUE, github_issue_number` table) — the fast path, avoids
  hitting GitHub for the overwhelmingly common "seen this before" case.
- **Postgres advisory lock keyed on the fingerprint** around the
  cache-miss path, so two concurrent workers racing the same new error don't
  both pass the `find_by` and the GitHub Search and both POST a new issue.
  Documented rationale inline, with a cross-reference to the sister fix in
  olfantic (`jwulff/olfantic#102`).
- **GitHub Search before create**
  (`repo:#{github_repo} is:issue is:open label:auto-error #{fingerprint} in:body`)
  — catches issues created by another process/deploy that never made it into
  the local cache.
- **`rescue => e; log; end`** wraps the entire method. A broken reporter must
  never break the thing it's reporting on.
- **Origin filtering** (`from_unreviewable_origin?`) skips ad-hoc
  `rails runner` one-liners and interactive `irb`/`eval` frames — a documented
  response to four real noisy-issue incidents (#1587, #1607, #1608, #1983).
  The lesson generalizes: **filter by "is this actionable," not just by
  volume.**

Companion files, all tiny and all worth porting verbatim in spirit:
- `git_hub_api_client.rb` (44 lines) — a bare `Net::HTTP` wrapper, no gem
  dependency, 5s connect / 10s read timeouts, reads
  `ENV["GITHUB_ERROR_ISSUES_TOKEN"]` / `ENV["GITHUB_ERROR_ISSUES_REPO"]`.
- `error_issue_fingerprint.rb` (4 lines) + migration — `fingerprint` unique,
  `limit: 12`. Olfantic later hardened this with a DB-level
  `CHECK (char_length(fingerprint) = 12)` constraint after recognizing the
  Postgres `varchar(12)` limit only enforces a *maximum*, not equality — a
  bulk-import or `insert_all` bypassing model validations could otherwise
  plant a corrupt cache row.
- `config/initializers/error_subscriber.rb` — subscribes **only in
  `Rails.env.production?`**, so dev/test never hits GitHub's API by accident.
  The subscriber *also* no-ops cleanly when the token/repo env vars are
  absent — belt-and-suspenders so a fresh deploy without secrets configured
  fails safe instead of crashing on boot.

**Epic B — user feedback → GitHub issue**
(`lucentbrief/main/app/services/user_feedback_issue_creator.rb`). Same
`GitHubApiClient`, same repo, different label (`user-feedback`), and a
retryable/terminal failure split (`429/500/502/503/504` → raise → Solid Queue
retries 3× at 30s backoff; anything else → `status: "failed"`, logged, not
retried). Not directly relevant to `fantasy-sports` (no authenticated user
surface, no admin dashboard) but worth knowing it exists — same shared
`GitHubApiClient` concern serves both.

### 1.2 Operational hardening only shows up after production experience

Two follow-on artifacts matter more than the original design, because they
capture failure modes John actually hit:

- **`olfantic/main/docs/runbooks/auto-error-triage.md`** — the human
  playbook: how to triage, when to mark `wontfix`, how to merge duplicate
  fingerprints (same root cause raised from two call sites), and — most
  important — **the local cache going stale is a first-class documented
  failure mode**. Closing an `auto-error` issue without reconciling the local
  `ErrorIssueFingerprint` row means the next occurrence silently comments on
  a closed, invisible-to-triage issue forever. The fix
  (`olfantic/main/lib/tasks/error_fingerprints.rake`, task
  `error_fingerprints:prune`) looks up each cached issue's live `state` and
  deletes rows pointing at anything no longer `open` — **run offline by the
  operator, adds zero per-hit GET to the subscriber's hot path.**
- **Noisy-fingerprint risk was scoped and explicitly deferred, not solved.**
  Olfantic's requirements doc names the failure mode outright ("a single bad
  deploy throwing hundreds of distinct exceptions in minutes… will open
  hundreds of issues") and picks **"do nothing in P0; a sweeping `wontfix`
  close takes seconds"** over a rate cap, because a cap "hides real signal."
  That's a deliberate, reasoned choice — not an oversight — and it matters for
  the recommendation below because `fantasy-sports` does **not** get to make
  the same choice: it has no admin who owns the repo full-time watching for a
  storm, and a public CLI's install base can be far larger and less
  coordinated than an internal Rails app's request volume.

### 1.3 The adjacent, more relevant precedent: `cotrugli`'s alarm→issue bridge

**`cotrugli/packages/alarm-github-bridge/handler.py`** (583 lines, issue
#488) is architecturally the *closer* analogue for a public CLI, because it's
the one place John already holds a GitHub write credential **off the thing
that's reporting** — a Lambda subscribed to an SNS topic, not the alarm
source itself:

```
CloudWatch alarm ──► SNS topic ──► bridge Lambda ──► GitHub Issues API
                                        │
                              (label: auto-alarm, dedup by alarm name)
```

Details worth carrying forward if `fantasy-sports` ever builds a relay:

- **Dedup key is the alarm name in the issue title**
  (`is:open label:auto-alarm "{alarm_name}" in:title`), searched **open
  first as its own query**, because a single state-agnostic search sorted by
  `updated` can return a more-recently-touched *closed* duplicate and hide an
  older still-open issue — a bug class the code comments call out explicitly.
- **Flap handling (issue #2844).** Auto-close-on-recovery plus open-only
  dedup composed into a real incident: an hourly-flapping alarm produced
  ~330 issues in 2.5 days. The fix: a recurrence within `_REUSE_WINDOW_DAYS`
  **reopens** the closed issue instead of creating a new one; a recurrence
  within `_FLAP_WINDOW_HOURS` of auto-close gets a `flapping` label, which
  suppresses further auto-close and throttles comments to one per
  `_FLAP_HEARTBEAT_HOURS` (a *second* incident, #2073, had accrued 605
  comments on one issue before that throttle existed).
- **An independent rate backstop (issue #3024)**: every fresh-issue creation
  emits a CloudWatch metric on a namespace the bridge itself is **explicitly
  not subscribed to** — "routing rate-limit alerts about the bridge through
  the bridge itself would let a broken bridge suppress the signal."
- **Injection-safe rendering.** Alarm name/reason/description are
  AWS/user-controlled strings rendered into the issue body as **4-space
  indented code blocks**, specifically because indented blocks have no fence
  terminator a hostile input can close — `@mentions`, `#refs`, and triple
  backticks can't escape. The docstring calls this "same defense as the Rails
  subscriber," i.e. it's a deliberate, repeated convention, not one-off.
  **This directly matters for `fantasy-sports`**: ESPN response bodies are
  untrusted third-party content that could contain `@mentions` or markdown
  that breaks out of a naively-interpolated issue body.
- **Dormant-safe bootstrapping.** The Lambda resolves its PAT from SSM at
  cold start; if the parameter doesn't exist yet, it logs-and-skips rather
  than raising, so the infra can be applied before the secret lands and
  activates the moment it does.

### 1.4 What does **not** exist anywhere in John's repos

Grepped `lucentbrief`, `cotrugli`, `glucagent`, `edgelab`, `olfantic`, and
`dotfiles` for `public CLI`, `embed.*token`, `client-side telemetry`,
`opt-in telemetry`, `crash report` — **no hits beyond unrelated matches**
(OAuth client registration, git-credential helpers). **John has never before
shipped a public, credential-bearing CLI that needs to report on itself
without a server holding the write token.** Every existing pattern —
lucentbrief, olfantic, the cotrugli bridge — puts the GitHub PAT in a process
John operates (a Rails app, a Lambda), never in something distributed to
strangers. That's the one structural difference `fantasy-sports` must design
around, and it's why this brief doesn't recommend a straight port.

## 2. Options

| Option | Flood risk | Secret-leak risk | Token exposure | User friction | Maintenance cost | Works headless (cron/agent) |
|---|---|---|---|---|---|---|
| **(a) Prefilled URL — CLI prints/opens a URL, human clicks Submit** | Low (human reviews before submitting; no auto-fire) | Low — human sees the exact body before it leaves the machine | **None.** No token anywhere. | One click, but requires a browser + a human present | Near zero — no infra, no server, no secret rotation | **No**, not on its own — needs a fallback (see §3, recommendation) |
| **(b) Serverless relay holding a token (Cloudflare Worker / Lambda, à la `alarm-github-bridge`)** | Depends entirely on the rate-limit/dedup you build — real risk if under-built | Medium — payload crosses a network hop you control but don't inspect in real time | Token lives server-side, never shipped; standard-and-proven pattern in this codebase | Zero — fully automatic | Real ongoing cost: infra to run, a token to rotate, abuse/rate-limit logic to maintain | Yes |
| **(c) GitHub App instead of a PAT** | Same as (b) | Same as (b) | Marginal improvement over a PAT (installable, scoped, revocable, auto-rotating) but still lives server-side — doesn't change the client story at all | Zero | Highest of any option — App registration, installation flow, webhook handling for a capability this project doesn't otherwise need | Yes |
| **(d) Canary-only — no client telemetry at all** | None | None | None | None | Lowest possible | N/A |
| **(e) Hybrid: local `gh` CLI shell-out when authenticated, prefilled URL fallback otherwise** | Low — dedup via GitHub Search runs under the *user's own* token, exactly like lucentbrief's search-before-create | Low — same local-review property as (a) when falling back; when shelling to `gh`, body is still assembled by allowlist (§5) before it ever leaves the process | **None held by the project.** Uses the operator's/agent's own already-authenticated `gh` credential — the project never custodies a write token at all | Zero when `gh` is present and authenticated (which is the CLI's actual audience — developers and coding agents); one click otherwise | Low — no relay to run; only client code | **Yes** — this is the option built specifically to satisfy the headless requirement without infra |

Olfantic's own scoping decision is directly on point here: *"GitHub App is
overkill for one bot… fine-grained PAT, annual rotation."* That reasoning
generalizes even further against (c) for `fantasy-sports`, which doesn't
have "one bot" — it would need a fleet-facing relay serving every installed
copy of a public CLI.

## 3. Recommendation

**Ship (e) — hybrid, `gh`-CLI-first with a prefilled-URL fallback — in v0.1.
Defer (b)/(c) indefinitely; build only if v0.1's actual issue volume proves
the fallback path is missing real reports.**

Why this beats a straight port of the lucentbrief/olfantic pattern:

1. **The project's own architecture doc has already done the hard part of
   this decision.** §11 states client telemetry is *supplementary* to the
   canary, not the primary detector — "catching what the canary misses...
   NOT being the primary detector." A supplementary signal doesn't need
   the always-on automatic reliability that justified a relay in the
   internal-app cases. It needs to *exist* and to *not be worse than
   nothing*.
2. **This CLI's actual audience already holds a GitHub credential.** The
   architecture doc frames this as "agent-native" throughout — the primary
   users are developers and coding agents running in a dev environment or
   cron, most of whom already have `gh auth login` done for other reasons.
   That flips the token problem: instead of the *project* needing to hold a
   write credential safely, the *operator* already has one, scoped to
   whatever they've authorized (their own account, their own rate limits,
   their own audit trail in `gh auth status`). This is a genuinely different
   trust boundary than lucentbrief/olfantic, where end users have no GitHub
   relationship to the repo at all — there, a project-held token was the
   only option. Here, it's the worse option.
3. **Zero infra to build or maintain**, matching v0.1's own stated ambition
   ("prove the read path survives real ESPN behavior for a few weeks before
   touching mutations" — the same minimalism applies to a brand-new
   observability surface).
4. **The redaction story is strictly better than a relay's.** Path (a)/(e)'s
   fallback shows the human the *exact* body before anything leaves the
   machine — a stronger backstop than any server-side scrubber, and it's
   free.
5. **Headless is solved without a token**, which was the one gap a pure
   prefilled-URL design (a) can't close on its own.

### What ships in v0.1 vs. what's deferred

**v0.1 (build now):**
- Fingerprinting (§4), local "already reported" cache, health.json
  cross-check before ever offering to report.
- `gh`-CLI path: detect `gh` on `PATH` + `gh auth status` succeeding →
  search-then-create-or-comment, mirroring lucentbrief's dedup logic exactly,
  but executed as subprocess calls against the *user's* token.
- Prefilled-URL fallback when `gh` is absent/unauthenticated, with a
  `--show-report` / log-only mode to preview the body without sending
  anything (borrowed from `gh`'s own `GH_TELEMETRY=log`, §6).
- Consent gate (§6), redaction (§5), and the `auto-error`-style label on
  `jwulff/fantasy-sports` (seed via `apply-labels.sh` extension, matching
  olfantic's convention of adding project-specific labels beyond the core
  palette).

**Explicitly deferred, revisit only if triggered:**
- A serverless relay (option b) or GitHub App (option c) — build **only** if
  `health.json`/issue volume after a season of real usage shows the
  `gh`-CLI-and-fallback path is systematically missing reports (e.g., most
  users never have `gh` authenticated, and issue volume from private-league
  breakage stays suspiciously at zero despite user complaints elsewhere). If
  that trigger fires, port `alarm-github-bridge`'s shape almost verbatim: a
  small relay function, PAT in a secrets manager (or GitHub App), the exact
  same dedup-by-fingerprint + reopen-not-duplicate + flap-throttle logic
  already proven in that codebase.
- A rate cap on issue creation. Olfantic's P0 reasoning applies unchanged:
  volume is unknown, and a cap hides real signal. Revisit if a storm
  actually happens — closing a sweeping burst as `wontfix` takes seconds.
- Auto-close-on-fix-release. Manual triage, matching both lucentbrief and
  olfantic's posture (`docs/runbooks/auto-error-triage.md`'s whole triage
  flow assumes a human closes the loop).

## 4. Anti-flood design

**The single strongest anti-flood mechanism is one this project already has
and the internal-app precedents didn't: `health.json`.** Before ever
fingerprinting or offering to report, check the already-cached health
manifest from §11.2. If `known_issue` exists for this error code + provider +
version range, **do not offer to file anything** — surface the existing issue
URL instead, exactly like the human-readable error output §11.3 already
specifies (`This looks like a known issue, fixed in 0.1.4: #42`). This means
the overwhelmingly common flood scenario — one ESPN outage hitting every
installed copy simultaneously — **never reaches the reporting path at all**,
because the canary will have already published `known_issue` within 24h and
every client after that point short-circuits to "here's the tracked issue."
The exposure window is only the gap between an individual user hitting
`SCHEMA_DRIFT` and the *next* canary run — bounded, not unbounded.

Layered under that:

1. **Trigger scope, reusing §11.3's existing table exactly.** Only
   `SCHEMA_DRIFT` and genuinely unexpected/unhandled exceptions are
   report-eligible — not `AUTH_EXPIRED` (local cause, not ESPN's fault), not
   `RATE_LIMITED` (retry, not a bug), not `PROVIDER_UNAVAILABLE` on its own
   (transient; only escalate if it recurs past a small local counter, since a
   single 5xx is noise and a sustained one is signal).
2. **Fingerprint = `sha256(f"{error_code}:{provider}:{endpoint_or_view}:{response_shape_summary}")[:12]`.**
   Adapted from lucentbrief's `SHA256(class:file_no_line)` — the CLI has no
   equivalent of "our own file that threw," since the interesting failure is
   ESPN's response shape, not our code. `response_shape_summary` is a small
   derived descriptor (e.g. sorted top-level JSON keys actually present, or
   "missing key X") — never the raw response body, which is where secrets
   and league PII could hide.
3. **Local "already reported" cache**, the same shape as
   `ErrorIssueFingerprint` but a flat file:
   `~/.cache/fantasy-sports/reported_fingerprints.json` mapping
   `fingerprint → {issue_url, reported_at, cli_version}`. Once a fingerprint
   is reported once from this machine, subsequent occurrences show "already
   reported: `<url>`" and don't prompt again — mirrors the fast-path local
   DB check in lucentbrief, minus the advisory lock (no concurrent-worker
   problem in a single CLI invocation; a simple file lock is enough if two
   `fantasy-sports` processes race, which is rare and low-stakes here).
4. **Server-side dedup on the `gh`-CLI path**: `gh issue list --repo
   jwulff/fantasy-sports --search "in:body <fingerprint>" --state open`
   before creating — directly reusing lucentbrief's GitHub-Search-before-file
   query shape, just invoked as the user's own `gh`, not an HTTP call under a
   project-held token. On a hit, `gh issue comment <n> --body "🤖 Occurred
   again..."` instead of creating.
5. **No rate cap in v0.1**, per §3 — deliberately matching olfantic's P0
   reasoning, and lower-risk here than it was for olfantic because (2)+(3)
   already bound the worst case to "one report per fingerprint per machine,"
   and (1) already bounds it to "one canary cycle's worth of exposure."

## 5. Redaction design

**Follow John's own established doctrine exactly: default-deny allowlist
assembly, not blocklist regex scrubbing of free text.** This is the same
principle underlying `olfantic/main/lib/privacy/safe_rails_subscribers.rb`
(`Privacy::LoggingPolicy.request_fields` — an allowlisted field extractor
that Rails' default subscribers get wrapped in specifically because the
*defaults* leak params, exception messages, and backtraces). The issue body
is built field-by-field from known-safe values; it is never
`str(exception)` or `traceback.format_exc()` dropped in wholesale.

What must never appear in an issue body, and why:

| Item | Why it's dangerous | How it's kept out |
|---|---|---|
| `espn_s2` / `SWID` cookie values | Session credentials; leaking them hands over the victim's ESPN account | Never captured into the report context in the first place — the provider layer's error wrapper extracts only `{status_code, endpoint, response_shape_summary}` from any `espn-api` exception before it's allowed near the reporter. `str(exc)` and `exc.args` are discarded, not scrubbed. |
| League IDs | Numeric, but combined with a real name they're enough to identify a specific private group of people — PII-adjacent, not public | Redacted by default (`league_id: "<redacted>"` in the body); an explicit `--include-league-id` opt-in for a user who wants their specific league's config visible to a maintainer debugging it |
| Team / owner / member display names | Real names of people who never consented to appear in a public issue tracker | Never sourced — the reporter only ever touches error-path metadata (error code, provider, endpoint, version), not any parsed roster/team/matchup payload |
| Raw ESPN response bodies | Could contain any of the above, plus unpredictable future PII ESPN adds to their (undocumented, unversioned) API | Never included. `response_shape_summary` is *shape only* — key names present/absent, never values |
| File paths | `/Users/jsmith/...` leaks the reporter's OS username | Any path in the (allowlisted, internal-only) traceback frames is rewritten relative to the package root before inclusion; `$HOME`/`~` and username segments stripped |
| Full traceback | Local variable reprs in a traceback can echo anything in scope, including cookies passed as function arguments | Only `file:line` for frames **inside the `fantasy_sports` package** are included (mirrors lucentbrief's "application backtrace" framing in the runbook — "top 20 **allowlisted application** file:line locations", never third-party or stdlib frames, never variable values) |

**Backstop, not the primary mechanism**: run a regex pass over the final
assembled body for known secret *shapes* (`espn_s2=`, `SWID={`, `Authorization:`,
`Cookie:`, long hex/base64 blobs) before it's ever printed or transmitted,
and refuse to proceed (fail closed, show an error) if a match is found rather
than silently redacting — a silent redaction that gets it wrong is worse than
a loud failure that makes the user re-run with `--show-report` to see why.
This is defense-in-depth under the allowlist, exactly as cotrugli's bridge
treats its `MAX_INTERPOLATED_LENGTH` truncation as a backstop under its own
indent-as-code-block escaping, not a substitute for it.

**Injection safety**: any third-party string that does get included
(ESPN's own error message text, if short and genuinely useful) is rendered
as an indented code block — "no fence terminator a hostile input can close,"
verbatim the technique in `alarm-github-bridge/handler.py`'s
`_indent_as_code_block`. ESPN's API is unofficial and unversioned; treat
anything it returns as untrusted input, not merely "third-party but
friendly."

## 6. Consent / privacy UX

**Default: opt-in, ask-per-occurrence, fail-silent-and-print when
non-interactive.** This is a stricter bar than Homebrew's or `gh`'s own
opt-out-with-notice model, deliberately: those transmit aggregate,
low-sensitivity usage counters; this transmits the specifics of one user's
actual failure, which — even after redaction — is closer to lucentbrief's
"user feedback" surface (explicit content a person is choosing to share)
than to `gh`'s "which commands do people run" telemetry.

**Mechanism:**

- Config key, `~/.config/fantasy-sports/config.toml`:
  ```toml
  [telemetry]
  report_errors = "ask"   # "ask" | "always" | "never" — default "ask"
  ```
- Env override (checked first, same precedence rule as the rest of the
  project's auth resolution chain in §6 of the architecture doc):
  ```
  FANTASY_SPORTS_REPORT_ERRORS=always|never|ask|log
  ```
- **Ecosystem convention**: also honor `DO_NOT_TRACK=1`
  (https://consoledonottrack.com — the informal cross-tool CLI standard) as
  an unconditional `never`, same as GitHub CLI itself does per its 2026-04-22
  telemetry launch (`DO_NOT_TRACK=true` short-circuits `gh`'s own opt-out
  logic). One env var a user sets once covers every well-behaved CLI on
  their machine, `fantasy-sports` included.
- **Interactive prompt** (TTY only), on a report-eligible error, after the
  human-readable error + health-check output from §11.3:
  ```
  This looks like a new issue — no existing report found.

    Report it to the maintainer? Redacted preview: fantasy-sports doctor --show-report
    [y] yes, once   [a] yes, always   [n] no   [N] no, never ask again

  >
  ```
  `n`/no answer defaults to not sending. `N` writes `report_errors = "never"`
  back to config. `a` writes `"always"`.
- **Non-interactive (`report_errors` unresolved and no TTY — cron, CI, an
  agent's subprocess call)**: never prompt, never silently send. Print the
  fully-redacted report body plus either the `gh` command or the prefilled
  URL to **stderr**, and fold the same into the JSON error envelope under
  `error.report` (mirroring the existing `error.health` shape from §11.3) so
  an agent reading structured output can decide for itself whether to run
  `gh issue create` — the "documented non-interactive fallback" the
  architecture doc's headless requirement calls for. This is also exactly
  the `--show-report` / `log` mode: `FANTASY_SPORTS_REPORT_ERRORS=log` prints
  the payload and takes no other action, borrowed directly from `gh`'s own
  `GH_TELEMETRY=log` — "output the JSON payload to stderr without sending
  it," so a user can audit before ever trusting `always`.
- **First use of the `gh`-CLI auto-file path always announces the identity
  it's acting as** — `Filing this issue as @<gh auth status login> using your
  local gh credentials.` — even under `always`, at least once per session.
  Silently using someone's personal GitHub identity to open issues is a
  meaningfully different trust event than a project-held bot account doing
  it, and the user should never be surprised by it.
- **README states plainly** what does and doesn't happen, matching the
  existing health-check framing in §11.3 rule 4 ("Say this explicitly...
  reads as tracking to many people, and here it genuinely is not") — except
  here the honest statement is the mirror image: *this feature does
  transmit specifics about your error, only with your explicit per-report or
  standing consent, and only after client-side redaction you can preview
  first.*

## 7. Implementation sketch

```python
# fantasy_sports/telemetry/reporter.py
"""
Report an eligible error as a GitHub issue on jwulff/fantasy-sports.

Never holds a GitHub token. Two paths, tried in order:
  1. Local `gh` CLI, if present and authenticated — full dedup via
     `gh issue list --search`, create-or-comment, under the OPERATOR's
     own credential.
  2. Prefilled github.com/.../issues/new URL — printed or opened in a
     browser, human reviews and submits (or doesn't).

Both paths are gated by the same consent/redaction pipeline so neither
can transmit anything the other wouldn't.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import textwrap
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

REPO = "jwulff/fantasy-sports"
REPORTABLE_CODES = {"SCHEMA_DRIFT", "UNEXPECTED_EXCEPTION"}
CACHE_PATH = Path.home() / ".cache" / "fantasy-sports" / "reported_fingerprints.json"

# Defense-in-depth backstop only — the primary defense is that these
# values are never captured into ReportContext in the first place (§5).
_SECRET_SHAPE = re.compile(
    r"espn_s2=|SWID=\{|Authorization:\s*Bearer|Cookie:\s|[A-Fa-f0-9]{32,}"
)


@dataclass(frozen=True)
class ReportContext:
    """Allowlisted fields only. Never constructed from str(exception)
    or traceback.format_exc() — see docs/research/01-telemetry-auto-issues.md §5."""
    error_code: str
    provider: str
    endpoint: str
    response_shape_summary: str
    cli_version: str
    python_version: str
    os_name: str
    app_frames: list[str]  # "fantasy_sports/providers/espn.py:142", internal-only


def fingerprint(ctx: ReportContext) -> str:
    key = f"{ctx.error_code}:{ctx.provider}:{ctx.endpoint}:{ctx.response_shape_summary}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def build_body(ctx: ReportContext, fp: str) -> str:
    body = textwrap.dedent(f"""\
        ## Error
        - **Code:** `{ctx.error_code}`
        - **Provider:** `{ctx.provider}`
        - **Endpoint/view:** `{ctx.endpoint}`
        - **Fingerprint:** `{fp}`
        - **CLI version:** `{ctx.cli_version}`
        - **Python:** `{ctx.python_version}` · **OS:** `{ctx.os_name}`

        ## Response shape (keys only, no values)
        ```
        {ctx.response_shape_summary}
        ```

        ## Application backtrace (internal frames only)
        ```
        {chr(10).join(ctx.app_frames[:20])}
        ```
        """)
    if _SECRET_SHAPE.search(body):
        raise RuntimeError(
            "Refusing to report: possible secret shape detected in assembled "
            "body. This should be unreachable (see §5's allowlist assembly) — "
            "please file a bug with `--show-report` output attached, and "
            "manually redact before doing so."
        )
    return body


def already_reported(fp: str) -> str | None:
    if not CACHE_PATH.exists():
        return None
    cache = json.loads(CACHE_PATH.read_text())
    entry = cache.get(fp)
    return entry["issue_url"] if entry else None


def record_reported(fp: str, issue_url: str, cli_version: str) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    cache[fp] = {"issue_url": issue_url, "cli_version": cli_version}
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def gh_available_and_authenticated() -> str | None:
    """Returns the authenticated login, or None if gh is missing/unauthenticated."""
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=3
        )
        if result.returncode != 0:
            return None
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def file_via_gh_cli(ctx: ReportContext, fp: str, body: str) -> str | None:
    search = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open",
         "--search", f"in:body {fp}", "--json", "number,url"],
        capture_output=True, text=True, timeout=10,
    )
    existing = json.loads(search.stdout or "[]")
    if existing:
        number, url = existing[0]["number"], existing[0]["url"]
        subprocess.run(
            ["gh", "issue", "comment", str(number), "--repo", REPO,
             "--body", f"🤖 Occurred again.\n\n{body}"],
            capture_output=True, timeout=10,
        )
        return url

    created = subprocess.run(
        ["gh", "issue", "create", "--repo", REPO,
         "--title", f"[Auto] {ctx.error_code}: {ctx.endpoint} [{fp}]",
         "--body", f"🤖 Auto-filed by fantasy-sports client.\n\n{body}",
         "--label", "bug,auto-error"],
        capture_output=True, text=True, timeout=10,
    )
    return created.stdout.strip() or None


def prefilled_issue_url(ctx: ReportContext, fp: str, body: str) -> str:
    title = f"[Auto] {ctx.error_code}: {ctx.endpoint} [{fp}]"
    # GitHub / most browsers cap usable URL length well under 8KB; truncate
    # defensively and tell the user in the body itself.
    truncated_body = body[:6000]
    params = urllib.parse.urlencode(
        {"title": title, "body": truncated_body, "labels": "bug,auto-error"}
    )
    return f"https://github.com/{REPO}/issues/new?{params}"
```

Corresponding label seed (extends the shared palette from
`~/Development/dotfiles/scripts/apply-labels.sh`, the same way olfantic's
requirements doc records "add `auto-error` to `apply-labels.sh` defaults so
future repos inherit it" — worth actually doing that generalization now that
a third project wants it):

```bash
gh label create auto-error --repo jwulff/fantasy-sports \
  --color "d93f0b" --description "Auto-filed from a client or canary error report"
```

## 8. Risks and failure modes

- **`gh` absent or unauthenticated is the common case, not the edge case,
  for a chunk of the real audience.** A user who installed via `uv tool
  install fantasy-sports` specifically to avoid touching `gh` at all falls
  straight to the prefilled-URL path every time. That's fine — it's the
  documented fallback — but it means path (e)'s "zero friction" framing in
  §3 only holds for the subset of users (likely large, given the
  agent-native framing, but not universal) who already have `gh` set up.
- **Prefilled-URL length limits.** GitHub's own issue-URL query params and
  most browsers cap usable URL length well under 8KB (mirroring cotrugli's
  own `MAX_INTERPOLATED_LENGTH = 4000` truncation discipline). A body that
  overflows silently truncates mid-thought unless explicitly bounded and
  flagged — the sketch above truncates to 6000 chars and should also note
  "truncated, full detail in `--show-report`" inline in the body itself.
- **No `$BROWSER` in the environment that hit the error** (SSH session, a
  cron job, a Docker container). "Open a URL" can't literally open anything.
  The CLI must always *print* the URL/command to stdout/stderr in addition
  to attempting to open it — never rely on `webbrowser.open()` succeeding.
- **Using the operator's own `gh` token means the project has zero control
  over abuse**, by design — but that also means a mis-set `always` in a
  shared or looping-agent context could open real spam under a real human's
  GitHub identity, which is a *worse* consequence for that human than the
  same spam under a disposable bot account. The local fingerprint cache
  (§4.3) plus the "always announces the acting identity" rule (§6) are the
  mitigations; there is no way to fully eliminate this risk without
  reintroducing a project-held token, which trades this risk for the
  token-custody risk the whole design exists to avoid.
- **The allowlist can still miss something.** New ESPN response fields, a
  new exception type whose `__str__` embeds request context in a way nobody
  anticipated — the regex backstop (§5) catches known *shapes*, not unknown
  ones. Treat any confirmed leak as a P0: rotate nothing (there's no
  project-held token to rotate), but immediately patch the allowlist,
  release, and — if a leak did reach a real issue — delete/redact that issue
  content directly, the same "stop rollout, don't copy matched content into
  GitHub or chat" discipline `olfantic/main/docs/runbooks/pii-canary-logging.md`
  already documents for a structurally similar canary-leak scenario.
- **Health.json cross-check assumes the canary is running and fresh.** If
  the canary itself breaks (its own CI creds expire, its own PAT rotates
  out from under it — the exact PAT-expiry failure mode
  `olfantic/main/docs/runbooks/auto-error-triage.md` documents for the
  *human-facing* subscriber applies equally to the *canary's* write path),
  `health.json` goes stale and client reports lose their strongest
  anti-flood backstop silently. Worth a `doctor`-visible staleness check on
  `health.json`'s own `updated_at` (already numerically available in the
  manifest shape from §11.2) as a cheap add: if it's more than ~2 canary
  cycles old, say so in `doctor` output rather than trusting a possibly-dead
  signal.
- **No storm has ever actually happened to calibrate against.** Every
  "don't build a rate cap yet" decision in this brief (and in olfantic's own
  precedent) is a bet that a manual `wontfix` sweep is cheap enough when it's
  needed. That bet has held for olfantic so far; it hasn't been tested here.
  Revisit immediately if a real storm occurs rather than waiting for a
  scheduled review.
