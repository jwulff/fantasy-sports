# An empty waiver queue a private league serves without the key (#144)

## Why

Since 2026-10-16, `claims --team 3` on dragon-slayers exited 9 with
`SCHEMA_DRIFT: ESPN's mPendingTransactions response has no
pendingTransactions`, and by 2026-10-18 all three of John's leagues did the
same, so the queue could not be verified before kickoff. #109 had built the
reader on the throwaway league's answer to an empty queue,
`pendingTransactions: []`, and recorded that a body without the key must be
drift because an empty queue invites a duplicate claim. The record was
half-wrong: the add/drop probe read the key with `.get()`, and a private
league answers the same state with the league envelope and no key at all.

## How

- **What was established read-only first.** All three leagues (read as a
  creator, a manager, and a plain member) omit the key under every request
  variant tried; the throwaway league sent `[]` fifty minutes before #144 was
  filed, so this is per league, not a rollout. The transaction log, filtered
  to `WAIVER`, shows no live claim in any of them: every `PENDING` row is
  named by a later `CANCEL` or `PROCESS` row's `relatedTransactionId`. That
  last part is new: a processed claim's own row keeps reading `PENDING`, the
  way a cancelled one does, and most processed claims' own rows then drop out
  of the log while their `PROCESS` rows stay. The log can prove a claim live;
  it cannot prove the queue empty. Whether a private league's view lists a
  live claim is still unobserved (John's claim was cancelled four hours
  before #144's read), so the fix assumes neither answer.
- **The parser is unchanged; the adapter decides.** `_pending_claims` and
  `_pending_trades` still refuse a body without the key. `fetch_pending_claims`
  and `fetch_pending_trades` first ask `_queue_omitted`: a JSON object with no
  `pendingTransactions`, whose `id` and `seasonId` are the league and season
  asked for, with the `status` block. Anything else without the key stays
  drift as before.
- **Absence is corroborated, never assumed.** `_corroborate_empty_queue`
  reads `mTransactions2` for the transaction scoring period, the period
  before it (a claim queued before the Tuesday rollover processes after it),
  and the current scoring period, with the type filter for the queue kind
  (`WAIVER`; the whole trade family for proposals, since what retires one is
  uncaptured). All periods' rows are folded together, once per id, because
  ESPN clamps a future period to the current one and serves the same rows
  twice. A `PENDING` row of the kind that nothing retires, for the requested
  team, is `SCHEMA_DRIFT` with `details.unlisted` and a remediation that says
  not to resubmit or cancel blind. Only an empty fold returns `[]`.
- **Team-scoped on purpose.** The view was only ever captured listing the
  reading member's own claims, so another team's live claim is not evidence
  that this team's read is wrong; raising on it would fail `claims` every
  weekday of the season. For proposals the team test is either side of the
  offer, as `trades` itself reads them.
- **Preflight's two findings, both taken.** The trade fold matched only the
  proposer, so an incoming offer the queue omitted would have read as `[]`;
  it now uses the same either-side test as `_pending_trades`. And the
  one-period lookback could miss a proposal still open from two weeks ago,
  while a lapsed one inside the window would have raised on every read: the
  trade read now looks back three periods (a test pins that to
  `MAX_EXPIRES_HOURS`), and a row whose `expirationDate` has passed is not
  live. What ESPN logs for an accepted, declined, or lapsed offer is still
  uncaptured (#142).
- **The second round's finding, also taken.** A completed period's log
  caches forever, and the day after a rollover the transaction period is one,
  so a corroborating read answered from the store could never show a claim
  queued after it was cached and would have corroborated `[]` over a live
  claim. The corroborating reads now bypass the cache (`CacheMode.BYPASS`,
  as the write path's live roster read does); a test counts the live reads.
  The non-object log body now reports the same `details.path` as the other
  malformed-log cases.
- **The cross-model review's findings, taken.** Codex and Claude, reviewing
  independently, both found that the queue body itself kept its 15-minute
  cache while the log check was live. A cached omitted body could predate a
  claim queued in the ESPN app, an offer made to this team, or a weekly
  rollover. Because the log periods come from that body, a rollover could
  bracket the wrong weeks and corroborate `[]` over a live claim. A cached
  omitted body is now read again live before the log is consulted, and a
  live one is not read twice. Codex also found that a pending log row with
  no readable `teamId` was skipped by a team-scoped read; such a row now
  fails the check instead of vanishing.
- **The cloud review's finding, taken.** The fold read only `PENDING` as
  live and skipped every other status, so a row whose status was missing or
  renamed vanished and the queue read as empty. Only `EXECUTED`, `CANCELED`,
  and `FAILED_*` have been seen on a settled transaction, so only those are
  skipped now. A row of the folded type with any other status, that this
  team could own and that nothing retired or expired, is `SCHEMA_DRIFT` with
  the ids and statuses in `details.unrecognised`. A retiring row is still
  skipped whatever its status, since it describes the cancel or the run. If
  ESPN logs a declined offer under a new status (#142), trade reads on an
  omitted queue exit 9 until the offer lapses: loud, never a false empty.
- **Safety of the writes is untouched.** `cancel-claim` and `trade cancel`
  still need the id in the queue they read, so an omitted queue refuses them
  as `CONFIG_INVALID` with nothing sent. A `claim` whose queue omits the claim
  ESPN just accepted reads back `verified: false, error: SCHEMA_DRIFT` with
  the existing "could not be read back" warning, journaled `executed`.
- **Fixture and harness.** `pending-transactions-omitted.json` is the live
  2026-10-18 body with the league id rewritten to 99; nothing else in it
  names a member or player. `WritableEspn(omit_empty_queue=True)` serves an
  empty queue that way, and `process()` plays a waiver run as the logs showed
  it. Memory and docs record the corrected fact, the new one about processed
  claims, and the observation still missing.

## Validation

- `uv run pytest -m 'not live'`: 2169 passed. New tests cover the captured
  envelope, the period bracketing, every retiring-row shape seen live, a live
  claim the queue omitted (drift, with ids), a log that cannot corroborate,
  a body without the key that is not this league's envelope, `claims` and
  `trades` returning `[]`, a refused cancel, and the accepted-but-omitted
  claim read-back, a cached omitted body re-read live across a rollover, and
  a pending log row that names no team, and a logged row whose status is
  absent or one no capture has shown.
- `ruff check`, `ruff format --check`, `scripts/check_startup.py`, and
  `scripts/check_budgets.py` pass.
- Live, read-only, from the worktree: `claims --fresh` on dragon-slayers,
  big-mac, and supper-club, and `trades --fresh` on dragon-slayers, all exit
  0 with `[]`, with `mPendingTransactions` and two `mTransactions2` reads in
  `sources`. No claim was submitted or cancelled and no roster was changed.

Closes #144.
