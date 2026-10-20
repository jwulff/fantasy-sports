# A private league's `mPendingTransactions` omits the queue when it is empty

**Found:** 2026-10-16 (#144, filed from a `claims` run on a real league) and
confirmed 2026-10-18 across all three of John's leagues. **Applies to:**
`claims`, `cancel-claim`, `trades`, `trade cancel`, and the pending read-back
of `claim` and `trade propose`; anything that reads `view=mPendingTransactions`.

**What ESPN does.** With nothing queued, a private league answers the view with
the league envelope only: `id`, `seasonId`, `scoringPeriodId`, `segmentId`,
`gameId`, `draftDetail`, `status`, and **no `pendingTransactions` key**
(`docs/research/06-espn-add-drop-surface/pending-transactions-omitted.json`).
That held for a league read as its creator, one read as a manager, and one
read as a plain member, and no request variant brought the key back:
`scoringPeriodId`, `forTeamId`, a `x-fantasy-filter` on type, or a second
`view=` beside it. The throwaway public league answered the same state with
`pendingTransactions: []` fifty minutes before #144 was filed, so this is a
per-league (or per-league-age) difference, not a change ESPN rolled out
between two reads. `mTransactions2` with a type filter answers an empty set
as `transactions: []`, so ESPN does not omit every empty list in this family.

**What is not known.** Whether a private league's view *lists* a live claim.
Every observation of the key present with a row in it is from the throwaway
league, read by the member who queued the row. John's own claim in
dragon-slayers was cancelled four hours before #144's read, and no `claims`
run is recorded against a league while a claim of John's was live. Until one
is, the view may be "omitted when empty" or "omitted for this member
regardless"; the adapter is built so both are safe.

**What the adapter does** (`EspnProvider._corroborate_empty_queue`). The
parser still refuses a body without the key. The adapter, when the body is
this league's envelope (`id` and `seasonId` match, `status` present) with the
key missing, reads `mTransactions2` for the transaction scoring period, the
period before it, and the current scoring period, with the type filter for
the queue kind, folds every `PENDING` row out that any row's
`relatedTransactionId` names (a `CANCEL` or a `PROCESS` row), and returns an
empty queue only when nothing live remains for the requested team. A live
row the queue did not list is `SCHEMA_DRIFT` with `details.unlisted`, never
an empty list: that is the one case "nothing queued" would send an agent
into a duplicate claim or a blind cancel. The fold is team-scoped on
purpose: the view was only ever captured listing the reader's own claims,
so another team's live claim is not evidence this team's read is wrong. For
proposals the team test is either side of the offer, the window reaches back
three periods (an offer may stay open two weeks), and a row whose
`expirationDate` has passed is not live, because what ESPN logs when an offer
lapses, is accepted, or is declined is uncaptured (#142).

**Traps.** A completed period's log caches forever (`completed_through`),
and the day after a rollover the transaction period itself is a completed
week, so a corroborating read served from the store could never show a claim
queued after the body was cached, and would corroborate an empty queue over
a live claim. The corroborating reads therefore bypass the cache
(`CacheMode.BYPASS`, as the write path's live roster read does) and cost a
few live GETs per read of an omitted queue. The queue body must be live too:
it keeps a 15-minute cache, and the log periods are read off it, so a cached
omitted body from before a rollover, or before a claim queued in the ESPN
app, brackets the wrong weeks. A cached omitted body is re-read live first.
And the log drops a processed
claim's own row after a while while keeping its `PROCESS` row, so the log
can prove a claim live, never that the queue is empty: the corroboration is
one-directional by construction. For the same reason the fold never treats
an unfamiliar status as settled: only `EXECUTED`, `CANCELED`, and `FAILED_*`
have been seen, and a relevant row with anything else, or no status, is drift.

**Next evidence to gather.** The first time a claim of John's is live, run
`fantasy-sports raw --view mPendingTransactions --fresh` and record whether
the key appears with the row. If it does, the view is "omitted when empty"
and the corroboration is belt-and-braces; if it does not, `claims` needs the
log fold as its source, not only as its check.

Related: [[a-cancelled-claim-still-reads-pending]],
[[transactions-reads-drop-espns-transaction-id]],
[[espn-api-is-a-shape-reader-not-a-client]]
