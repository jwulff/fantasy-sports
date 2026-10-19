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
  weekday of the season.
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

- `uv run pytest -m 'not live'`: 2154 passed. New tests cover the captured
  envelope, the period bracketing, every retiring-row shape seen live, a live
  claim the queue omitted (drift, with ids), a log that cannot corroborate,
  a body without the key that is not this league's envelope, `claims` and
  `trades` returning `[]`, a refused cancel, and the accepted-but-omitted
  claim read-back.
- `ruff check`, `ruff format --check`, `scripts/check_startup.py`, and
  `scripts/check_budgets.py` pass.
- Live, read-only, from the worktree: `claims --fresh` on dragon-slayers,
  big-mac, and supper-club, and `trades --fresh` on dragon-slayers, all exit
  0 with `[]`, with `mPendingTransactions` and two `mTransactions2` reads in
  `sources`. No claim was submitted or cancelled and no roster was changed.

Closes #144.
