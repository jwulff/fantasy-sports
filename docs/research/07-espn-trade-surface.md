# ESPN Fantasy API (v3) — The Trade-Proposal Surface

**Status:** Research brief, 2026-10-16. Answers the capture criterion of
jwulff/fantasy-sports#112 (propose, cancel, list). Accepting and declining an
offer need a second account and are #142. Builds on `05-espn-write-surface.md`
(host, envelope, status mapping) and `06-espn-add-drop-surface.md` (the queue
view and the cancel shape), and does not repeat them.

**Method:** Probed through `scripts/probe_espn_trades.py` against the same
throwaway public league as brief 06 (`[leagues.test]`; 10 teams, NFL 2026,
scoring period 6), where the session's member owns team 7 and is a plain
member. Friday 2026-10-16, 06:46–06:48Z. The script refuses any `--league` but
`test`, asserts before every POST that the URL names the test league and no
other configured league, that the body's `teamId` is the one team `mNav` says
the session owns, and that every `TRADE` item moves a player between that team
and the one `--with` team. It keeps at least 5 s between writes, one write per
invocation, and never re-sends a probe name. The propose and cancel bodies are
built by `EspnProvider.propose_trade` / `cancel_trade`, so the shape under
test is the shape the code sends.

The league's trade settings (`mSettings.settings.tradeSettings`) at capture:
`deadlineDate` 2026-12-02, `revisionHours: 24`, `vetoVotesRequired: 4`,
`allowOutOfUniverse: false`, `max: -1`.

**Writes sent:** 4. One proposal (team 7's bench WR for another team's bench
WR, with a comment telling that manager it was a test), cancelled **7 seconds**
later; one re-cancel of the same proposal; one proposal naming a player who is
not on team 7. The two rejections created nothing. Nothing executed: the
roster and the queue read back as they started. The other manager may have
been notified of the offer during its 7 seconds.

**Evidence:** `docs/research/07-espn-trade-surface/`, one JSON per probe or
read. Redactions: league id `99`; the member's SWID `{SWID-REDACTED}`; team
7's player `9000101`; the counterparty team is written as `2` (its number is
withheld) and its players as `0` and `1`; the one player name ESPN
interpolates into a message is `<counterparty player 1>`; transaction ids are
`<uuid-N>` in order of first appearance, so the cancel still joins to the
proposal it withdrew. Request headers are dropped. The unredacted captures are
under `tests/cassettes/private/112/` (gitignored) and are not in git.

---

## 1. Headline answers

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Proposal envelope | `type: "TRADE_PROPOSAL"`, `executionType: "EXECUTE"`, plus the shared fields (`isLeagueManager: false`, `teamId`, `scoringPeriodId`). `expirationDate` is sent as a `YYYY-MM-DDTHH:MM:SS.000Z` string; `comment` is optional free text. No `memberId`. | t1-propose |
| 2 | Proposal items | One `TRADE` item per player, each naming **both** teams: a given player `fromTeamId` = proposer, `toTeamId` = counterparty; a requested one the reverse. Matches the community shape (brief 05 §10). | t1-propose |
| 3 | Proposal answer | `200`, `status: "PENDING"`, `isPending: true`, a new transaction `id`, `teamActions: {"<proposer>": "ACCEPTED"}`, `expirationDate` echoed in **epoch milliseconds**, `comment` echoed, each item echoed with `fromLineupSlotId` (the player's current slot) and `toLineupSlotId: -1`. | t1-propose |
| 4 | Where an open proposal is listed | `view=mPendingTransactions`, as a `TRADE_PROPOSAL` / `PENDING` row beside any waiver claims. The row carries `expirationDate`, `teamActions`, and the items with `acquisitionBudget: 0`, but **not** `comment`. | t1-pending |
| 5 | Cancel shape | `type: "TRADE_PROPOSAL"`, `executionType: "CANCEL"`, `relatedTransactionId` naming the proposal, no items. The waiver cancel's shape (06 §2) with the proposal's type. | t2-cancel |
| 6 | Cancel answer | `200`, a **new** id, `status: "CANCELED"`, `isPending: false`, the proposal's items echoed, `relatedTransactionId`. The proposal left `mPendingTransactions` at once. | t2-cancel, t2-pending |
| 7 | The transaction log after a cancel | `mTransactions2` keeps the proposal's own row at `PENDING` and adds the `CANCELED` row naming it, exactly as for a waiver claim (06 §2, `docs/memory/a-cancelled-claim-still-reads-pending.md`). Read the queue, not the log. | private t2-log |
| 8 | Cancelling a proposal that is no longer open | `409 TRAN_NOT_FOUND`, "Transaction with ID … could not be found." | t4-recancel |
| 9 | Offering a player you do not hold | `409 TRAN_PLAYER_NOT_ON_TEAM`, "<player> is not on team 7". The shared rejection vocabulary maps that type to `already_dropped` (it was learned on drops), which misreads a trade; the command refuses the case itself before sending. | t3-give-not-mine |

## 2. What this changes in the code

- `EspnProvider.propose_trade` and `cancel_trade` build the shapes above;
  `_post` passes `expirationDate` and `comment` through.
- `fetch_pending_trades` parses the `TRADE_PROPOSAL` rows of the queue into
  `PendingTrade`, sharing the row validation with `fetch_pending_claims`. Only
  `TRADE` items are read as legs. A proposal whose legs do not move players
  between the proposer and exactly one other team is `SCHEMA_DRIFT`, but rows
  that do not name the reading team are skipped first, so a bystander's odd
  proposal cannot break another team's reads.
- `trade propose` checks both rosters before sending, because ESPN's answer to
  a player on the wrong roster (row 9) carries a misleading kind.
- `TRAN_NOT_FOUND` is left `unknown` in `_TRAN_KINDS`: `trade cancel` reads
  the queue first, so ESPN only says it after a race, and the verbatim
  `provider_type` is enough to act on.

## 3. Not probed

- **Accept and decline** (#142): need an offer made *to* a team this session
  owns, so a second account.
- **Whether the queue shows a member offers made to it** (or other teams'
  rows at all). Every queue read here and in brief 06 held only team 7's own
  rows, so `trades`' `incoming` direction rests on the assumption that it
  does. The same second-account capture (#142) settles it.
- **What a processed trade looks like** in either view, and how the 24-hour
  review window (`revisionHours`) shows up.
- **Proposals past the trade deadline**, a locked player, or a trade that
  would break a roster limit on either side. ESPN's answers will come back as
  `WRITE_REJECTED` with the type verbatim.
- **A proposal with no `expirationDate`** — whether ESPN defaults one.
- **Uneven trades** (2-for-1) and one-sided offers. The command requires at
  least one player each way; ESPN's own rule is unknown.

## 4. Sources

Captured evidence (primary): `docs/research/07-espn-trade-surface/*.json`,
this session. Community prior art for the shape:
`garavitgabriel/espn-fantasy-claude-openclaw` — `espn_api/baseball/league.py`
`propose_trade` / `cancel_trade` and `docs/writes/wt-c-trades.md`, which
described the item direction and the two-day `expirationDate` and noted that
only a single leg had been captured live. The wire agreed with it on every
point it covered.
