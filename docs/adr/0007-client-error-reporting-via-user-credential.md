# ADR 0007: Client error reporting uses the operator's own `gh` credential

**Status:** Accepted — the `gh`-first approach was independently proposed by John on 2026-08-26, converging with the research recommendation
**Date:** 2026-08-26

## Context

The canary (ADR-0005) detects breakage against a *public* test league. It cannot
see failures that only occur in private leagues, unusual configurations, or rare
edge cases. Client-side error reporting would cover that gap.

Three problems make naive auto-filing dangerous:

1. **Flooding.** One ESPN outage becomes hundreds of identical issues.
2. **Secret leakage.** Errors may carry `espn_s2`/`SWID` cookies, league IDs, and
   the real names of league members.
3. **Token custody.** A write credential cannot be shipped inside a public CLI.

John has solved (1) and (2) three times — `lucentbrief`'s
`GitHubIssueErrorSubscriber`, its port into `olfantic`, and `cotrugli`'s
`alarm-github-bridge`. **All three are server-side.** In every case the GitHub
PAT lives in a process John operates: a Rails app or a Lambda.

`fantasy-sports` would be the first public, credential-bearing CLI he has
shipped, and that is precisely the case none of the existing patterns cover.
See `docs/research/01-telemetry-auto-issues.md`.

## Decision

**Do not custody a write token. Use the operator's own.**

- **Primary path:** when `gh` is on `PATH` and `gh auth status` succeeds, shell
  out to it. Search-then-create-or-comment, mirroring `lucentbrief`'s dedup logic
  exactly, but executed against the *user's* credential — their account, their
  rate limits, their audit trail.
- **Fallback:** when `gh` is absent or unauthenticated, print/open a **prefilled
  GitHub issue URL**. The human sees the exact body before anything leaves the
  machine.
- **Preview mode:** `--show-report` renders the body without sending anything.
- Reporting is **consent-gated** and never fires without the user having agreed.

**The error output carries agent-actionable instructions, not just an offer.**
This is the piece that makes the design agent-native rather than merely
human-friendly. When an error is reportable, the JSON envelope includes a
`report` block telling an agent exactly how to file it:

```json
"report": {
  "reportable": true,
  "already_reported": false,
  "fingerprint": "a3f9c21e4b07",
  "gh_available": true,
  "instructions": "No matching issue exists. File it with the command below, which uses your own gh credential. Review the body first — it is pre-redacted but you are the last check.",
  "command": "gh issue create -R jwulff/fantasy-sports --label auto-error --title '...' --body-file /tmp/fantasy-sports-report-a3f9c21e.md",
  "body_file": "/tmp/fantasy-sports-report-a3f9c21e.md",
  "fallback_url": "https://github.com/jwulff/fantasy-sports/issues/new?title=...&body=..."
}
```

An agent reads this and can file the issue in one step, with a human-reviewable
body already on disk. A human at a TTY gets the same thing as prose plus the
command to copy. Neither path requires the project to hold a credential, and
both put a reviewable artifact in front of someone before anything is sent.

`already_reported` is set from the local fingerprint cache and a search of open
issues, so an agent in a retry loop does not file the same thing twice.

This inverts the trust boundary. The audience for an agent-native CLI is
developers and coding agents who already have `gh auth login` done for other
reasons — so the operator already holds the credential, and the project never
needs to.

## Consequences

**Easier:** No relay to build, no token to rotate, no abuse surface, no infra.
The redaction story is strictly stronger than a relay's, because the fallback
path shows the human the exact payload first. Headless works without the project
holding a secret.

**Harder:** Coverage is incomplete — users without `gh` who decline the browser
fallback report nothing. Accepted, because ADR-0005 makes this signal
*supplementary*: the canary is the primary detector.

**Carried forward from the existing patterns:**

- **Fingerprint** = `SHA256("{error_class}:{file_path_without_line}")[0:12]`.
  Line numbers stripped so a one-line fix does not split one bug into two;
  file path kept so the same exception from two call sites stays two bugs.
- **Search before create**, then comment on the existing issue rather than
  filing a duplicate.
- **The reporter can never break the thing it reports on** — the entire path is
  wrapped and failure-swallowing.
- **Filter for actionability, not just volume.** Origin filtering in lucentbrief
  came out of four real noisy-issue incidents.
- **Render untrusted content in 4-space-indented code blocks, never fenced.**
  Indented blocks have no terminator a hostile input can close, so `@mentions`,
  `#refs`, and backticks cannot escape. **This matters here specifically: ESPN
  response bodies are untrusted third-party content going into an issue body.**
- **Flap protection.** `cotrugli` issue #2844: an hourly-flapping alarm produced
  ~330 issues in 2.5 days; a separate incident accrued 605 comments on one issue
  before throttling existed. Reuse-window reopen instead of re-create, and
  throttle repeat comments.

**Deferred:** a serverless relay or GitHub App. Build only if a season of real
usage shows the `gh`-plus-fallback path systematically misses reports. If that
trigger fires, port `alarm-github-bridge`'s shape nearly verbatim.

## Alternatives considered

| Option | Why not |
|---|---|
| Prefilled URL only | Cannot work headless — no cron or agent path |
| Serverless relay with a PAT | Real infra and a rotating secret for a supplementary signal |
| GitHub App | Highest maintenance; does not change the client story at all |
| Canary only, no client reporting | Leaves private-league-only breakage invisible |
