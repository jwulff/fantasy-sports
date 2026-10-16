# Trades: `trade propose`, `trade cancel`, and `trades` (#112)

## Why

On 2026-10-15 agents managing Supper Club, Big Mac and Dragon Slayers
recommended three trade offers, and each one had to be made by hand in the
ESPN app because the tool could not propose a trade. #112 was split on
2026-10-16: proposing, cancelling, and listing need only John's account, so
they ship here; accepting and declining need an offer made *to* one of his
teams, so a second account, and moved to #142. Part of epic #134; each
command is atomic in the sense ADR-0011 (#135) records.

## How

- **Captured before built.** `scripts/probe_espn_trades.py` sent four writes
  to the throwaway test league on 2026-10-16, with the guards of the #108
  probe adapted to a transaction that names a second team (every `TRADE` item
  must move a player between team 7 and the one `--with` team). One
  bench-for-bench proposal, cancelled 7 seconds later; a re-cancel
  (`TRAN_NOT_FOUND`); and an offer of a player team 7 does not hold
  (`TRAN_PLAYER_NOT_ON_TEAM`). The redacted captures and what they show are
  brief 07. The community shape held on every point.
- **A proposal is irreversible.** The other manager can accept the moment it
  lands, and an accepted trade enters the league's review window. So
  `trade propose` sits in the same class as `add`/`drop`/`claim`:
  `--acknowledge-irreversible`, journal kind `trade` with `reversible: false`,
  notify hook. A cancel follows `cancel-claim`: no flag, kind `trade_cancel`,
  not announced.
- **Both rosters are checked before sending.** ESPN's answer to a player on
  the wrong roster is `TRAN_PLAYER_NOT_ON_TEAM`, which the shared rejection
  vocabulary maps to `already_dropped` because it was learned on drops. Rather
  than add a trade-specific kind (an API change under ADR-0010), the command
  reads both rosters and refuses by name, so an agent never sees the
  misleading kind. Only `--team` is ownership-guarded; the counterparty's
  players are ESPN's to check.
- **The queue, not the log.** A cancelled proposal stays `PENDING` in
  `mTransactions2`, exactly like a cancelled claim, so `trades`, the
  propose read-back, and `trade cancel` all read `mPendingTransactions`. The
  row validation is shared with `fetch_pending_claims` (`_pending_rows`).
- **Sides restated from the reader.** The same proposal reads differently
  from each team, so `trades` adds `direction`, `other_team_id`,
  `giving`, and `receiving` relative to `--team` beside the proposer-relative
  `PendingTrade` fields. The comment is free text another manager wrote and
  is labeled untrusted (#17); ESPN's queue row omits it.
- **`trade` group, not `trade-propose`.** `cancel-claim` exists because a
  `claim` group would shadow the `claim` command. There is no top-level
  `trade` command and the list is the plural `trades`, so a group is safe.
- **Expiry.** `--expires-hours`, default 48 (what community clients send),
  bounded 1 to 336. ESPN takes it as a `…T…:….000Z` string and echoes epoch
  milliseconds.

## Review fixes (preflight: Codex and Claude, both P2)

- The queue parser read any item as a trade leg and accepted a leg missing a
  team id (Codex). It now reads only `TRADE` items as legs, leaving others in
  `raw`, and requires numeric, distinct, non-zero team ids on the proposer's
  side, or it is drift.
- One malformed proposal between two *other* teams would have made `trades`,
  `trade cancel`, and the propose read-back fail for everyone, because drift
  was raised before the team filter (Claude). Rows that do not name the
  reading team are now skipped before they are validated.
- `WRITING_PROVIDER_METHODS` now lists the three trade methods, and both tests
  that pin it were updated (Codex).

Second preflight round (P3s, fixed): `trades` first wrote the reading-side
team into `counterparty_team_id`, so an incoming row named the proposer
twice; the reading-side value is now `other_team_id` and the model's field
keeps its meaning (Claude). The README's irreversible-write section and notify
section now include `trade propose`, with a trade lifecycle example (Codex).
The docs and the `comment` docstring say plainly that ESPN's queue never
returns the comment (Claude). A third round noted that listing incoming
offers is assumed, not captured: every queue read so far held only the
reading team's rows. That is now stated in brief 07 §3, the `trades`
reference, and the README, and #142's capture settles it.

## Validation

- `tests/unit/test_trades.py` (53 tests): the queue parser against brief 07's
  captures, including drift cases; the exact bodies the adapter sends against
  `t1-propose` / `t2-cancel`; dry run, acknowledgement gate, ownership, wrong
  roster, own team, ambiguous names, expiry bounds; journal, read-back and
  notify on success; diverged writes, ESPN rejections replayed from the
  captures, an unreadable queue, a cancel ESPN ignored, and an incoming offer
  that cannot be cancelled. `commands/trades.py` is at 99% branch coverage.
- The test harness's write host queues and withdraws `TRADE_PROPOSAL` rows the
  way brief 07 captured them; `WritingFake` gained the three methods.
- `uv run pytest -m 'not live'` passes in full; ruff check and format,
  `scripts/check_startup.py`, and `scripts/check_budgets.py` pass.
- Samples for all three commands are generated by
  `scripts/render_readme_samples.py` and spliced into `docs/commands.md`;
  unrelated samples were left untouched.
- Live, in the test league only: one proposal and its cancel through the
  probe script, plus a `trade propose --dry-run` and `trade cancel` refusal
  through the CLI. No write touched Supper Club, Big Mac, or Dragon Slayers.
