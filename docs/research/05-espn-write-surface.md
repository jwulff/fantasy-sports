# ESPN Fantasy API (v3) — The Write Surface

**Status:** Research brief, 2026-09-12. Answers jwulff/fantasy-sports#14 and unblocks
#15, #16, #18. Feeds `docs/ARCHITECTURE.md` §5, §6, §9.

**Method:** Probed the write host directly against a real private league (the
configured default league, 10 teams, NFL 2026 week 1, Saturday 2026-09-12
18:29–18:35Z — after the Thursday game, before any Sunday kickoff) using the
cookies the auth chain resolves from the Keychain. Every probe was one request,
read, decided, then the next; every executing probe was a lineup slot change on
the probing member's own team, immediately reversed and read back, with the one
deliberate exception in §6. No add, drop, waiver, trade, bid, or league-setting
request was ever sent. Prior art was read, not trusted: the shapes below are
quoted from captured traffic, and every claim is tagged with the capture that
backs it. Community sources are cited where they agreed with or diverged from
what was observed.

**Evidence:** `docs/research/05-espn-write-surface/` — one JSON per probe
(`p<N>-*.json`: method, URL, headers with the cookie line redacted, body,
response status/headers/body), plus `roster-readbacks.json` (the
`{playerId: lineupSlotId}` snapshot before the first probe and after every one)
and `audit-trail-mTransactions2.json` (how ESPN itself recorded the executed
transactions). Cookie values are redacted, every SWID is the repo's per-GUID
cassette pseudonym (`docs/memory/swid-pseudonyms.md`), and member/team names
are replaced. Player ids and NFL player names are ESPN's public identifiers and
are left as captured.

---

## 0. Headline answers, one per acceptance checkbox

| # | Question (issue #14) | Answer | Evidence |
|---|---|---|---|
| 1 | Write host and path, distinct from `lm-api-reads` | `POST https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}/transactions/` — same path family as reads, different host. `GET` on it is `405 HTTP_METHOD_NOT_SUPPORTED`. | p0, p3 |
| 2 | Lineup-set confirmed end to end | Yes. `type: "ROSTER"` envelope with `type: "LINEUP"` items → `200`, body `status: "EXECUTED"`, read-back shows exactly the requested slots changed. | p3, p4, readbacks |
| 3 | One transaction for a full target lineup, or one item per slot change | **One transaction, many items, applied atomically** — but it must contain *only* the slots that change. A from==to item is rejected (`TRAN_ROSTER_SAME_SLOT`) and takes the whole transaction with it. One item alone is also fine. So R6's explicit target state is implementable as *diff, then one POST*. | p3, p6, p7, p9 |
| 4 | Roster-lock rejection on the wire | `409` with `details[0].type == "TRAN_LINEUP_LOCKED"`, message `"Lineup transaction could not be completed, <player> is locked"`. The lock is visible before sending: `playerPoolEntry.lineupLocked` per entry. | p7, p8 |
| 5 | Per-operation rejection vocabulary | Seven typed reasons observed (§5). `budget exceeded`, `already dropped`, `roster full` could **not** be provoked without an add/drop and are listed with a safe procedure for later. | §5 |
| 6 | Do the read cookies authorize writes, including on co-managed teams | **Yes — and on a team the member does not own at all.** `espn_s2` alone is sufficient; the SWID cookie and the body's `memberId` are both optional. The identical swap against another manager's team (not co-managed, not commissioner) returned `200 EXECUTED`. Reversed immediately. | p10, p12, p13–p15 |
| 7 | `--week` on a write: scoring or matchup period | Neither, in practice: a `ROSTER` write is **only accepted for the league's current `scoringPeriodId`** (`TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT` for both next week and week 18). The field is named and enforced as a scoring period. | p11, p11b |
| 8 | Commissioner vs manager authority | `isLeagueManager` is **not** in `mTeam`'s `members[]`; it is in `mNav` (with `isLeagueCreator`) and `mLeagueManager`. In this league the LM is another member — the probing account is a plain manager, which corrects #18's assumption that John holds commissioner privileges here. No LM write was exercised. | §7 |

The result for row 6 is the one that changes the design, so it is stated
again plainly: **ESPN did not enforce team ownership on a lineup transaction.**
"Only my team" has to be a client-side guard in `providers/espn.py`, not a
property inherited from the provider.

---

## 1. The host and the endpoint

### 1.1 Confirmed

```
POST https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/713073439/transactions/
```

- Same `/apis/v3/games/{sport}/seasons/{year}/segments/0/leagues/{id}` prefix as
  the read host, plus `/transactions/` (trailing slash, as the web client sends
  it; a slashless form was not tried).
- `GET` returns `405` with a typed body (`p0`):

  ```json
  {"messages": ["HTTP Method Not Supported."],
   "details": [{"message": "HTTP Method Not Supported.", "shortMessage": "HTTP Method Not Supported.",
                "resolution": null, "type": "HTTP_METHOD_NOT_SUPPORTED", "metaData": null}]}
  ```

  `Access-Control-Allow-Methods: GET,PUT,POST,DELETE,OPTIONS,HEAD` on that
  response is a CORS allowlist, not a statement of what the resource accepts —
  only `POST` was exercised.
- Responses are gzipped (`Content-Encoding: gzip`), served through CloudFront,
  and carry `X-Fantasy-Server-Time` and `X-Fantasy-Role: NONE` on every
  cookie-bearing response. `X-Fantasy-Role` is **[unverified]** as a
  commissioner indicator — it read `NONE` for a plain manager, and no LM
  session was available to compare.
- No `Retry-After`, `X-RateLimit-*`, or any throttling header appeared on any of
  the 21 responses. Rate limiting on the write host remains **[unverified]**
  (§8).

### 1.2 The read/write split is real

Community prior art agrees on this host (every source in §10 that writes at all
uses it), and `DanielTomaro13/sportsdata-mcp`'s plan independently observed the
`405`-on-`GET` behaviour. `cwendt94/espn-api` v0.46.0 has no reference to it: the
only two `requests.post` calls in the installed package are inside the
commented-out `authentication()` method in `requests/espn_requests.py` (the dead
Disney-identity login, removed from the active path in 2022-07 per
`03-espn-api-surface.md` §2.2). `ffscrapr` is read-only too — its `R/espn_api.R`
is `GET`-only. There is no library to lean on; the write layer is ours.

---

## 2. The lineup transaction

### 2.1 Request — captured verbatim (`p3-swap-execute.json`)

```http
POST /apis/v3/games/ffl/seasons/2026/segments/0/leagues/713073439/transactions/ HTTP/1.1
Host: lm-api-writes.fantasy.espn.com
Content-Type: application/json
x-fantasy-source: kona
x-fantasy-platform: kona-PROD
Cookie: espn_s2=<redacted>; SWID=<redacted>

{
  "isLeagueManager": false,
  "teamId": 1,
  "type": "ROSTER",
  "memberId": "{<swid>}",
  "scoringPeriodId": 1,
  "executionType": "EXECUTE",
  "items": [
    {"playerId": 4685472, "type": "LINEUP", "fromLineupSlotId": 4,  "toLineupSlotId": 20},
    {"playerId": 4685278, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 4}
  ]
}
```

Slot ids are ESPN's `lineupSlotId` integers exactly as the read side's `raw`
carries them per roster entry (`4` = WR, `20` = BE, `23` = RB/WR/TE, `2` = RB,
`0` = QB, `6` = TE, `16` = D/ST, `17` = K, `21` = IR in this league). The
league's slot configuration is `settings.rosterSettings.lineupSlotCounts`
(`{"0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1, "23": 1}`
here), and a player's legal targets are `playerPoolEntry.player.eligibleSlots`.

### 2.2 Which parts of that are actually required

Each of these was varied one at a time on an otherwise-identical, legal swap:

| Element | Required? | Evidence |
|---|---|---|
| `Cookie: espn_s2` | **Yes.** Absent or invalid → `401 AUTH_MISSING_CREDENTIALS` (§5). | p1, p13, p14 |
| `Cookie: SWID` | **No.** `espn_s2` alone → `200 EXECUTED`; ESPN fills `memberId` from the session and echoes it. | p15, p15r |
| body `memberId` | **No.** Omitted → `200 EXECUTED`, response carries the session's member id. | p10, p15r |
| `x-fantasy-source` / `x-fantasy-platform` | **No.** `Content-Type: application/json` alone → `200 EXECUTED`. | p4, p15r |
| `Content-Type: application/json` | Not varied; always sent. | — |
| `User-Agent` | `python-requests/2.34.2` accepted throughout. No browser impersonation needed. | all |
| `executionType` | `"EXECUTE"` executes. `"VALIDATE"` → `400 {"messages": ["Invalid Input."]}` with **no** `details[]`. No non-executing mode was found; `"CANCEL"` exists in community constants for withdrawing a pending transaction and was not sent. | p2 |
| `isLeagueManager` | Sent `false` throughout. `true` was **not** sent (rule 7 in the task: no LM write). | — |
| `scoringPeriodId` | Must equal the league's current period (§4). | p11, p11b |

**Design consequence for §6 of ARCHITECTURE:** the write path needs exactly one
secret on the wire, `espn_s2`. The SWID never has to be revealed for a write,
which removes one of the three leak channels in
`docs/memory/credential-leak-channels.md` from the mutation layer entirely —
no body interpolation, so nothing for a caller-formatted message to catch.

### 2.3 Successful response (`p3-swap-execute.json`)

```json
{
  "bidAmount": 0,
  "executionType": "EXECUTE",
  "id": "bfc30cbc-579e-4589-b528-2c4848a30590",
  "isActingAsTeamOwner": false,
  "isLeagueManager": false,
  "isPending": false,
  "items": [
    {"fromLineupSlotId": 4,  "fromTeamId": 0, "isKeeper": false, "overallPickNumber": 0,
     "playerId": 4685472, "toLineupSlotId": 20, "toTeamId": 0, "type": "LINEUP"},
    {"fromLineupSlotId": 20, "fromTeamId": 0, "isKeeper": false, "overallPickNumber": 0,
     "playerId": 4685278, "toLineupSlotId": 4,  "toTeamId": 0, "type": "LINEUP"}
  ],
  "memberId": "{<swid>}",
  "proposedDate": 1789237765019,
  "rating": 0,
  "scoringPeriodId": 1,
  "skipTransactionCounters": false,
  "status": "EXECUTED",
  "subOrder": 0,
  "teamId": 1,
  "type": "ROSTER"
}
```

- `id` is a UUID, unique per transaction. It is the join key into
  `mTransactions2`, where the same record appears (without `proposedDate`
  being renamed — note `03-espn-api-surface.md` §3.4's finding that
  `processDate` is often absent applies here too: the recorded row carries
  `proposedDate` only). `audit-trail-mTransactions2.json` shows all ten executed
  transactions from this session, in order, and none of the rejected ones.
- `status: "EXECUTED"` is the success discriminator. `isPending: false`
  distinguishes it from a queued waiver claim (**[inferred]** from the field
  names; no pending transaction was created).
- `isActingAsTeamOwner: false` was returned even for the write against the
  other manager's team (§6), so this flag does **not** mean "the caller owns
  `teamId`". Its semantics are **[unverified]**; do not use it as an
  authorization signal.
- `proposedDate` is epoch milliseconds, UTC — re-derive, never trust a library
  `datetime` (ARCHITECTURE §5).
- `fromTeamId`/`toTeamId` are `0` on `LINEUP` items whether or not the request
  supplied them. They matter for `ADD`/`DROP` items (community shape, §10),
  which were not exercised.

### 2.4 Latency

Every write answered in 70–110 ms server-side (`elapsed_ms` in each capture);
rejections were no faster than successes. Read-back via `mRoster` on
`lm-api-reads` reflected the change on the first read, ~1.5 s later, every
time — no propagation lag was observed between the write host and the read
host across ten executed transactions.

---

## 3. One transaction or one per slot — the R6 / AE2 decision

Four probes settle it:

| Probe | Items sent | Result | Read-back |
|---|---|---|---|
| p3 | two moves (a swap) | `200 EXECUTED` | both applied |
| p6 | one move (FLEX → BE, leaving FLEX empty) | `200 EXECUTED` | applied; ESPN accepts an empty starting slot |
| p7 | two legal moves **+** two moves involving a locked player | `409 TRAN_LINEUP_LOCKED` | **nothing** applied — the legal pair was not touched |
| p9 | two legal moves **+** two from==to no-op items | `409 TRAN_ROSTER_SAME_SLOT` | **nothing** applied |

So a single `ROSTER` POST is **validated and applied atomically**: any rejected
item rejects the whole transaction, and no item is applied before validation
completes. AE2's premise — "ESPN offers no transaction boundary, so partial
application is reported, not prevented" — is **wrong for one POST** and should
be amended: a single POST cannot half-apply. What *can* diverge is the world
between the read that computed the diff and the write that sends it (another
device, a lock that engaged at kickoff, a waiver that processed), and that is
what read-back verification is for.

**Recommendation for R6 (explicit target state):** read the roster; compute
`items` as one `LINEUP` entry per player whose `lineupSlotId` differs from the
target; refuse to send if that list is empty (there is no no-op transaction —
`TRAN_ROSTER_SAME_SLOT` would reject it); send one POST; re-read; compare
slot-for-slot. A target lineup that differs from the read in `n` slots is `n`
items in one request, never `n` requests. Sending per-slot requests would
*create* the partial-application failure mode AE2 worries about, because
between two of them ESPN would hold an intermediate lineup that was never
asked for.

Two consequences for the item order inside one POST: ESPN did not care whether
the "vacate the slot" move came before or after the "fill the slot" move — p3
listed the outgoing WR first, p4 (the reverse) listed him second, both
executed. Slot-count validation is over the final state, not step by step.
**[Observed on two orderings only]**; an implementer should still emit vacates
before fills so the body reads sensibly in the journal.

---

## 4. `scoringPeriodId` on a write

- Sent `2` (next week, well inside the season): `409 TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT`,
  "Transaction type can only be executed in the current scoring period" (p11b).
- Sent `18` (a real NFL week with no matchup period in this 17-week league):
  the same code (p11).
- Neither touched the current-week roster, and neither touched the
  `mRoster?scoringPeriodId=2|18` views (read back both times).

So for a `ROSTER` transaction the field is a scoring period by name and by
enforcement, and the only accepted value is the league's current one — the
top-level `scoringPeriodId` of any league payload (`1` during this session;
`status.latestScoringPeriod` agreed). This league's `matchupPeriods` are 1:1
(`{"1": [1], …, "17": [17]}`), so scoring-vs-matchup could not be *separated*
here; the enforcement message names the scoring period and the rejection for
`18` — a period that exists as a scoring period and not as a matchup period —
is consistent with that reading and inconsistent with a matchup-period one.

**Recommendation:** `--week` is not a parameter of a lineup write. The write
layer sends the league's current `scoringPeriodId` from the bootstrap payload
it already fetched, and a `--week` that is not current is refused client-side
as `CONFIG_INVALID` (`details.kind: "argument"`, per ADR-0009) before any
request. Future-week lineups — which the ESPN web UI does let a human set —
must use some other mechanism; it was not investigated (§8).

---

## 5. Rejection vocabulary

Every rejection body observed had the same shape as the read host's 401
(`docs/memory/espn-401-tells-you-nothing.md`): `messages[]` of prose plus
`details[]` with a typed `type` — **except the 400**, which had `messages`
only.

### 5.1 Observed

| HTTP | `details[0].type` | Message | Provoked by | Capture |
|---|---|---|---|---|
| 401 | `AUTH_MISSING_CREDENTIALS` | `Unauthorized:  Credentials are missing.` (two spaces, sic) | no cookies; **invalid** `espn_s2` with a valid SWID; SWID only | p1, p13, p14 |
| 405 | `HTTP_METHOD_NOT_SUPPORTED` | `HTTP Method Not Supported.` | `GET` on the write URL | p0 |
| 400 | *(none — no `details[]`)* | `Invalid Input.` | `executionType: "VALIDATE"` (an enum value ESPN does not know) | p2 |
| 409 | `TRAN_ROSTER_SLOT_LIMIT_EXCEEDED` | `Too many players in the WR slot (maximum 2)`; `metaData: {"teamid": "1"}` | a third WR moved into two WR slots | p5 |
| 409 | `TRAN_LINEUP_LOCKED` | `Lineup transaction could not be completed, <player> is locked` | moving a player whose game has been played | p7, p8 |
| 409 | `TRAN_ROSTER_SAME_SLOT` | `<player> is already in the RB slot` | an item with `fromLineupSlotId == toLineupSlotId` | p9 |
| 409 | `TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT` | `Transaction type can only be executed in the current scoring period` | `scoringPeriodId` ≠ current | p11, p11b |

Three things worth noticing:

1. **The write host's 401 is typed and constant, like the read host's** — but
   it is a *different* constant. `AUTH_MISSING_CREDENTIALS` was returned for
   "no cookies", for "a well-formed but invalid `espn_s2`", and for "SWID
   only". It therefore cannot distinguish expired from absent either, and the
   asymmetry in `espn-401-tells-you-nothing.md` carries over: *we sent nothing*
   is `AUTH_MISSING`; *we sent a credential and got this* is the one place a
   positive `AUTH_EXPIRED` classification is defensible, because on the write
   host there is no "wrong URL shape for the season" ambiguity (no
   `leagueHistory` alternate exists for writes) and no "league not visible"
   reading (the read of the same league succeeded seconds earlier with the same
   cookie). Recommend: a write-host 401 after a successful read with the same
   credential set → `AUTH_EXPIRED`; a write-host 401 with nothing sent →
   `AUTH_MISSING`.
2. **The 400 has no type.** It is what a malformed body gets — an enum ESPN
   does not recognise, and presumably a field it does not expect. That is our
   bug or ESPN's shape moving, never something a user fixes, so it maps to
   `SCHEMA_DRIFT` (stop; file an issue), not `CONFIG_INVALID`.
3. **All roster-rule rejections are `409`** with a `TRAN_*` type. `moneypro/
   fantasy_basketball_tools`' logged `409 Client Error` on a `FREEAGENT` add
   (§10) is consistent: `409` is the status family for "the league's rules
   refused this", across transaction types.

### 5.2 Not observed today, and how to provoke each safely later

These require an add/drop-class transaction, which the task forbade. None can be
provoked with a lineup move.

| R9a reason | Expected shape **[inferred]** | Safe procedure |
|---|---|---|
| budget exceeded (FAAB) | `409`, a `TRAN_*` type naming the acquisition budget | In a **throwaway test league** the implementer commissions (ESPN allows creating a private league for free), submit a `WAIVER` claim with `bidAmount` = budget + 1. Never in a real league: even a rejected claim is a transaction row other managers can see. |
| position limit (roster composition, e.g. a 5th QB) | `409`, distinct from `TRAN_ROSTER_SLOT_LIMIT_EXCEEDED`, driven by `rosterSettings.positionLimits` (`{"1": 4, "2": 8, …}` here) | Test league: `FREEAGENT` add of a 5th QB with no drop. |
| already dropped / not on team | `409`, `TRAN_*` | Test league: `DROP` a player id that is a free agent. |
| roster full | `409` — community-attested (`garavitgabriel`'s brief names it `ESPNRosterFull` on a 409) | Test league: `FREEAGENT` add with no drop on a full roster. |
| trade-related | unknown | Test league with two accounts. |

`docs/testing.md`'s live-suite policy already assumes a dedicated league for
anything that mutates; #18 should budget for standing one up before any of
these codes are implemented from a guess.

### 5.3 Mapping onto ARCHITECTURE §5

R9a asks for "a distinct machine code" per provider rejection reason. Two ways
to satisfy it; the second is recommended.

- *One top-level code per reason* (`ROSTER_LOCKED`, `SLOT_LIMIT`, …): every
  reason ESPN adds is an API change and a new exit status, and the seven
  observed here are unlikely to be the whole set.
- **One new code, `WRITE_REJECTED` (exit 11), `retryable: false`, with the
  discriminator in `details`** — the same move ADR-0009 made for
  `CONFIG_INVALID`'s two causes:

  ```json
  {"code": "WRITE_REJECTED", "retryable": false,
   "agent_action": "Do not retry unchanged; read the roster and re-plan",
   "remediation": "<ESPN's message, verbatim>",
   "details": {"kind": "roster_lock", "provider_type": "TRAN_LINEUP_LOCKED",
               "http_status": 409, "team_id": 1, "transaction_applied": false}}
  ```

  with `kind` drawn from a closed vocabulary we own (`roster_lock`,
  `slot_limit`, `same_slot`, `not_current_period`, `budget_exceeded`,
  `position_limit`, `already_dropped`, `roster_full`, `unknown`) and
  `provider_type` carrying ESPN's string verbatim so an unknown `TRAN_*` is
  still reported, not lost. `transaction_applied: false` is always true for a
  409 (§3) and is there so a consumer never has to infer it.

  Read-back divergence (AE2) is a different failure — the write *succeeded* and
  the world moved — and wants its own code (`WRITE_DIVERGED`, exit 12) rather
  than being folded into a rejection.

  The existing codes still apply above the rejection layer: write-host `401` →
  `AUTH_EXPIRED`/`AUTH_MISSING` (§5.1 item 1); `400` without `details` →
  `SCHEMA_DRIFT`; 5xx/timeouts → `PROVIDER_UNAVAILABLE` **with the caveat in
  §8 item 1** (a timed-out write may have applied); a `429` → `RATE_LIMITED`
  once one has ever been seen.

---

## 6. Who the cookies can write for

### 6.1 The other-team probe (`p12`, `p12r`)

After the full own-team flow was proven, the identical two-item swap was sent
with `teamId: 11` — another manager's team, with the same `isLeagueManager:
false` and the probing member's own `memberId`.

- Response: `200`, `status: "EXECUTED"`, `teamId: 11`, `memberId` = the
  probing member, `isActingAsTeamOwner: false`.
- Read-back of team 11's `mRoster`: both slots changed as requested.
- Reversed on the next request (`p12r`, `200 EXECUTED`); read-back identical
  to the pre-probe snapshot slot-for-slot; the CLI's own `roster --team 11
  --no-cache` agreed.
- Both transactions are in `mTransactions2` under the probing member's id
  (`audit-trail-mTransactions2.json`), so the league's activity feed shows the
  other manager's lineup being edited by someone who is not them.

The probing member is not an owner of team 11 by any record ESPN exposes:
`teams[11].owners` lists one SWID and it is not the prober's; `primaryOwner`
is not the prober's; the prober's own `fan.api` profile (`preferences[]` with
`type.id == 9`, the shape `espn-401-tells-you-nothing.md` documents) lists
exactly one entry in this league, team 1. And the prober is not the league
manager (§7). **ESPN executed a lineup change on a team the caller neither
owns, co-manages, nor commissions.**

Whether this is "any league member may set any lineup", or something narrower
(a member of the *same league*, a member with *any* team, an account-level
quirk), was **not** narrowed further: the task permitted one other-team probe
and one was sent. A third team was not touched.

### 6.2 What follows

1. **Team ownership is a client-side invariant.** `providers/espn.py`'s write
   path must resolve "my team" from the session — `fan.api`'s `entryId` for the
   league, or `teams[].owners` ∋ the resolved SWID — and refuse any `teamId`
   outside that set before building a request. `--team` on a write is not
   "which team"; it is "which of *my* teams", and in a one-team league it is
   redundant. This is the same class of guard as the roster-lock check: ESPN
   will not do it for us.
2. **This is not the co-manager case the issue asked about.** The question
   "does the read cookie authorize writes on co-managed teams" is answered a
   fortiori — it authorizes writes on *un*-managed teams — and the co-manager
   case is therefore not a special case worth designing for.
3. **The audit trail is ESPN's, not just ours.** Every executed write, own-team
   or not, is a permanent, member-attributed row in the league's transaction
   log. The journal (#16) should store ESPN's transaction `id` so the two
   ledgers can be joined.
4. **Consider telling ESPN.** A league member editing another member's lineup
   is a real integrity problem for every league on the platform. Whether and
   how to report it is John's call, not this brief's; it is noted so it is not
   lost.

### 6.3 Credential scope, summarised

| Sent | Result |
|---|---|
| `espn_s2` + `SWID` cookies, `memberId` in body | `200` (p3) |
| `espn_s2` + `SWID`, no `memberId` | `200` (p10) |
| `espn_s2` only, `memberId` in body | `200` (p15) |
| `espn_s2` only, no `memberId`, `Content-Type` only | `200` (p15r) |
| `SWID` only | `401 AUTH_MISSING_CREDENTIALS` (p14) |
| invalid `espn_s2` + valid `SWID` | `401 AUTH_MISSING_CREDENTIALS` (p13) |
| nothing | `401 AUTH_MISSING_CREDENTIALS` (p1) |

`espn_s2` is the session; the SWID is a label ESPN already knows from it. This
is consistent with `03-espn-api-surface.md` §2.1's description of `espn_s2` as
the opaque session token, and it means `auth status`'s staleness reporting for
`espn_s2` is the one that matters for writes.

---

## 7. Commissioner vs manager

- **Where the flag lives.** `mTeam`'s `members[]` carries only
  `displayName`, `firstName`, `lastName`, `id`, `notificationSettings` — no
  authority flag. `view=mNav` adds `isLeagueManager` and `isLeagueCreator` per
  member; `view=mLeagueManager` returns `members[]` with `id`, `displayName`,
  `isLeagueManager` only. Neither view is in `03-espn-api-surface.md` §1.4's
  table (which enumerated what `espn-api` sends), so this is a new read the
  adapter will need.
- **This league.** Exactly one member has `isLeagueManager: true`, and it is
  the owner of team 2. The probing account (team 1) has neither flag. **#18's
  note that "John holds commissioner privileges" is not true of the configured
  default league**; it may be true of another league John is in, which is
  exactly why the guard must be per-league and read from `mNav`, not assumed.
- **What was not exercised, by rule.** The request body's `isLeagueManager`
  was always `false`. Whether setting it `true` as a non-LM is rejected, ignored,
  or (worse) honoured is **[unverified]**, and so is every LM-scoped write
  (acting on other teams *as* LM, league-setting changes, trade veto). Given
  §6 — ESPN executed an other-team write with the flag `false` — the flag may
  be largely decorative for lineup transactions. Do not rely on it either way.
- **Recommendation.** The tool never sets `isLeagueManager: true`. Full stop.
  If a future issue wants commissioner tooling it is a separate capability with
  its own sanity gate, and the `mNav` flag is how the tool knows it is even
  talking to a commissioner. `X-Fantasy-Role` (§1.1) may be the cheap tell on
  every response; verify with an LM session before using it.

---

## 8. Open items — what a later implementer must still verify

1. **A write that times out is not a write that failed.** Every response here
   came back in ~100 ms, but a socket timeout after the request was sent leaves
   the transaction in an unknown state. Before mapping a timeout to
   `PROVIDER_UNAVAILABLE` ("retry with backoff"), the write path must read back
   first — a blind retry of a lineup move is harmless (the second would be
   `TRAN_ROSTER_SAME_SLOT`), but a blind retry of a waiver claim is a
   double-spend. Encode this before #18, not after.
2. **Rate limiting.** 21 requests to the write host in six minutes, never closer than ~5 s apart, produced no
   `429` and no throttling header. The write host's behaviour under a burst is
   **[unverified]**, and should stay that way — do not probe it.
3. **Idempotency.** No idempotency key was found in the request or response.
   The `id` is server-assigned. Re-sending an identical lineup POST after it
   executed would fail on `TRAN_ROSTER_SAME_SLOT`, which is a usable, if
   accidental, replay guard for lineups only.
4. **Future-week lineups.** The web UI can set them; the `ROSTER` transaction
   cannot (§4). The mechanism the UI uses is unknown — possibly a different
   `type`, possibly a different endpoint. Not needed for #15.
5. **Two-week playoff matchup periods.** Not observable in this league. Since
   the write is pinned to the *current* scoring period, the question reduces to
   "which scoring period is current during a two-week round", which the read
   side already answers.
6. **`isActingAsTeamOwner` semantics.** Returned `false` for an own-team write
   and for an other-team write alike. Unknown meaning; not an authorization
   signal.
7. **`executionType: "CANCEL"`** and `relatedTransactionId` for withdrawing a
   pending claim — community shape only, never sent.
8. **The add/drop/waiver/trade shapes** in §10 are community-attested and
   internally consistent across four independent implementations, but nothing
   here confirms them. #18 must capture them in a test league first.
9. **IR slot rules.** `21` is in every player's `eligibleSlots` regardless of
   injury status in the raw data; whether ESPN rejects moving a healthy player
   to IR (`TRAN_*`?) was not tried.
10. **`X-Fantasy-Role`** as a commissioner indicator (§7).

---

## 9. What this means for #15 / #16 / #18

**#15 — explicit-state lineup writes.** Implementable exactly as R6 asks, with
one amendment to AE2:

- One `ROSTER` POST per lineup set, items = the diff between the read and the
  target; refuse an empty diff client-side; `scoringPeriodId` = the league's
  current period from the payload already fetched, never from `--week`.
- Pre-flight, from data the read already has: every target slot is in the
  player's `eligibleSlots`; every moved player has `lineupLocked: false`; the
  target does not exceed `lineupSlotCounts`; `teamId` is one the session owns
  (§6.2 item 1). Each of these is something ESPN *will* reject or — for
  ownership — *will not*, and pre-flighting them turns a 409 into a
  `CONFIG_INVALID` with a name, before any request.
- Read-back after `200`, compare slot-for-slot, `WRITE_DIVERGED` on mismatch.
  Drop AE2's "partial application is reported, not prevented" — a single POST
  is atomic (§3); divergence means the world moved, not that ESPN half-applied.
- `--dry-run` reports the exact `items` list that would be sent. There is no
  server-side validate mode (§2.2), so R9's "does not validate provider
  acceptance" stands.
- Error mapping per §5.3: `WRITE_REJECTED` + `details.kind`, plus the
  write-host `401` → `AUTH_EXPIRED` rule in §5.1.
- Cache: a successful write must purge every entry tagged with this league
  and this scoring period (R10) — `mRoster`, `mTeam`, `mMatchup`,
  `mTransactions2` all changed within 1.5 s of each write. The `X-Fantasy-Last-Update-League`
  header ESPN exposes over CORS may be a cheap freshness signal; unverified.
- Transport: `requests.post` with `cookies={"espn_s2": …}` only, `Content-Type`
  only. The `x-fantasy-*` headers are optional; sending them costs nothing and
  keeps the request indistinguishable from the web client, so keep them, but
  do not treat their absence as a failure cause when debugging.

**#16 — journal.** Record ESPN's transaction `id` and `proposedDate` alongside
our prior/applied state so the journal joins to `mTransactions2`. Reversal of a
lineup write is another `ROSTER` POST with each item's `from`/`to` swapped —
p4, p6r, p10r, p12r, p15r are five worked examples — and it fails on a lock
with `TRAN_LINEUP_LOCKED`, which is the "distinguishable code" the issue asks
for. Journal writes must be attributed to the *session's* member id as ESPN
echoes it (the response's `memberId`), not to a `memberId` we sent, because we
should not be sending one.

**#18 — irreversible writes.** Three things this brief changes:

- Stand up a **throwaway test league** before implementing any of §5.2; the
  rejection codes for budget/position/drop cannot be learned in a real league
  without leaving transaction rows other managers see.
- The **ownership guard is load-bearing** for the irreversible class in a way
  it is merely embarrassing for lineups: if ESPN also fails to check ownership
  on `FREEAGENT`/`WAIVER`/`TRADE_PROPOSAL` (unknown — not probed, must not be
  probed in a real league), a `--team` typo drops another manager's player.
  The guard in §6.2 item 1 is a precondition for #18, not a nicety.
- The **timeout rule** in §8 item 1 is a precondition too: a timed-out claim
  needs a read of `mTransactions2` (pending claims appear there,
  `03-espn-api-surface.md` §1.4) before any retry.

**ADR / doc updates this brief implies:** ARCHITECTURE §5 gains
`WRITE_REJECTED` (11) and `WRITE_DIVERGED` (12) and the write-host `401` rule;
§6 notes that writes need `espn_s2` only; §9 and AE2 lose "no transaction
boundary"; #18's body loses "John holds commissioner privileges" in favour of
"read `mNav`". `docs/memory/` gets two notes: the ownership finding, and
`isLeagueManager`-lives-in-`mNav`.

---

## 10. Sources

Captured evidence (primary): `docs/research/05-espn-write-surface/*.json`,
this session, 2026-09-12.

Read-side context from this repo: `docs/research/03-espn-api-surface.md`;
`docs/memory/espn-401-tells-you-nothing.md`; `docs/memory/swid-pseudonyms.md`;
`docs/memory/credential-leak-channels.md`; `src/fantasy_sports/providers/espn.py`.

Installed library, read in full for write capability: `espn-api` 0.46.0
(`.venv/lib/python3.12/site-packages/espn_api/`) — `requests/espn_requests.py`
has two `requests.post` calls, both inside the commented-out
`authentication()`; `constant.py` defines only the reads host and the news
host; no module references `lm-api-writes`, `executionType`, or
`transactions/` as a request path (the string appears only in test fixture
JSON and the baseball `Transaction` model, which parses `mTransactions2`
output).

Community prior art consulted for shapes (none of it authoritative; where it
disagreed with the wire, the wire wins):

- `AbdulsaboorS/fantasybasketballbot` — `espn_lineup.py`, `espn_transactions.py`,
  `CAPTURE_LINEUP.md`: the `ROSTER`/`LINEUP` body with `fromLineupSlotId`/
  `toLineupSlotId` "confirmed from browser capture (HTTP 200, status=EXECUTED)",
  the `FREEAGENT` add/drop item shape (`ADD` + `toTeamId`, `DROP` + `fromTeamId`),
  and the `x-fantasy-platform: espn-fantasy-web` header value.
  <https://github.com/AbdulsaboorS/fantasybasketballbot>
- `mcolen5050/FantasyFootballAutomation_public` — `old_files/testChangeLineup.py`:
  the football (`ffl`) form of the same body, slots `4`↔`20`.
  <https://github.com/mcolen5050/FantasyFootballAutomation_public>
- `RafiKDev00/AIFantasyLineupGenius` — `swapper.py`: one-item-per-POST lineup
  moves with `fromTeamId`/`toTeamId` set to the team id (ESPN echoed `0` for
  ours regardless). <https://github.com/RafiKDev00/AIFantasyLineupGenius>
- `Zinkelburger/Fantasy-Football-Tool` — `engine/weekly/espn_league.py`: the
  claim that ESPN "validates the whole lineup at once" (confirmed, §3), the
  `WAIVER`-with-`bidAmount` shape, `x-fantasy-platform: kona-PROD`.
  <https://github.com/Zinkelburger/Fantasy-Football-Tool>
- `heyitaki/espn-fantasy-football-mcp` — `src/espn/constants.ts`: the enums
  `TRANSACTION_TYPE = {WAIVER, FREEAGENT, TRADE_PROPOSAL, TRADE_ACCEPT, ROSTER,
  DRAFT}`, `EXECUTION_TYPE = {EXECUTE, CANCEL}`, `ITEM_TYPE = {ADD, DROP, LINEUP}`.
  <https://github.com/heyitaki/espn-fantasy-football-mcp>
- `garavitgabriel/espn-fantasy-claude-openclaw` — `docs/writes/00-BRIEF.md`:
  a baseball write plan naming `409` as roster-full / illegal-move, the
  `TRADE_PROPOSAL` and `CANCEL` + `relatedTransactionId` shapes, and the
  observation that trade accept/reject were never captured.
  <https://github.com/garavitgabriel/espn-fantasy-claude-openclaw>
- `DanielTomaro13/sportsdata-mcp` — `docs/ESPN-WRITES-PLAN.md`: independent
  observation of `405` on `GET` and of the typed `AUTH_MISSING_CREDENTIALS`
  401 body; a plan, not a capture. <https://github.com/DanielTomaro13/sportsdata-mcp>
- `moneypro/fantasy_basketball_tools` — `logs/post_free_agent.log`: a `409` on
  a repeated `FREEAGENT` POST, the only logged add/drop rejection found.
  <https://github.com/moneypro/fantasy_basketball_tools>
- `ffverse/ffscrapr` — `R/espn_api.R`: `GET`-only; no write support.
  <https://github.com/ffverse/ffscrapr>
- `cwendt94/espn-api` issues and PRs searched for `lineup`, `write`, `post`,
  `lm-api-writes`, `executionType`: no open or closed PR adds write support;
  the only hits are read-side (`lineupSlotId` on `Player`, #553/#175).
  <https://github.com/cwendt94/espn-api>
