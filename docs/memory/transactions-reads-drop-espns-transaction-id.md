# `transactions` cannot find a write by its id

**Found:** 2026-10-15, building `add`/`drop`/`claim` (#18). **Applies to:**
anything that joins a write's `provider_transaction_id` to the read side:
`claims` and `claim cancel` (#109), a pending-claim read-back, reconciling a
`pending` journal row after a timeout.

A write answers with ESPN's transaction UUID (`WriteResult.provider_transaction_id`,
journaled as the join key). The read side never shows it. `espn-api`'s
`Transaction` keeps `type`, `status`, `scoringPeriodId`, `bidAmount`,
`processDate`/`proposedDate`, and the items, and throws away `id`, so
`_transaction()` builds `provider_id` as `f"{type}-{scoring_period}"`.
Every waiver claim in a period has the same `provider_id`.

So `#18`'s claim read-back matches a pending row by **type, team, claimed
player, and `raw.status == "PENDING"`**, reports `matched_by`, and returns
`verified: null` when nothing matches, never `true`. A duplicate claim for the
same player by the same team would satisfy that match; an id join would not.

**For #109:** a `claims` command that lists pending claims *by id*, which is
what `claim cancel` takes, has to read `mTransactions2` itself (its `id` is in
the body; the adapter's `raw` and `last_fetch` hold it) rather than go through
`league.transactions()`. Do that inside the adapter, and normalize the id
onto the row, so the command layer never parses ESPN's body. The `"PENDING"`
status string is inferred, not captured; #108 confirms it.

Related: [[espn-api-is-a-shape-reader-not-a-client]]
