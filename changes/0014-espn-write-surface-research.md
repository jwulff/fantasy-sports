# The ESPN write surface, probed against a real league (#14)

Every write requirement in the epic (R6–R10) was blocked on one fact nobody had
checked: whether `espn-api` could write at all, and if not, what ESPN's write
host actually accepts. The installed library cannot — its only two
`requests.post` calls sit inside a commented-out login method — and no brief in
`docs/research/` covered the mutation side. Community folklore agreed on a host
and a body shape but nobody had published a captured rejection, and "everyone
says so" is not evidence to build a mutation layer on.

This PR adds `docs/research/05-espn-write-surface.md` and the scrubbed captures
behind it. It implements nothing.

## What was done

Twenty-one requests to `lm-api-writes.fantasy.espn.com` against the configured
default league on 2026-09-12, week 1, in the window after the Thursday game and
before any Sunday kickoff. One probe at a time; every executing probe a lineup
slot change immediately reversed and read back through `mRoster`; only lineup
moves ever sent. Ten transactions executed (five swaps and their five
reversals), eleven were rejected without touching state, and the final
read-back of both affected teams is slot-for-slot identical to the snapshot
taken before the first request — through the raw view and through the CLI's own
`roster --no-cache`.

## What was found

- **The host and shape are real.** `POST …/leagues/{id}/transactions/` with a
  `ROSTER` envelope of `LINEUP` items returns `200` / `status: "EXECUTED"`, and
  the read host reflects it on the next read. `GET` on the same URL is a typed
  `405`.
- **One POST is atomic.** A transaction mixing legal moves with a locked
  player's move, or with a no-op `from == to` item, is rejected whole; nothing
  is applied. AE2's "ESPN offers no transaction boundary" is wrong for a
  single request and the brief says how to amend it: diff the target lineup
  against the read, send the diff as one POST, read back for the case where the
  world moved in between.
- **Rejections are `409` with a typed `TRAN_*` reason** — `TRAN_LINEUP_LOCKED`,
  `TRAN_ROSTER_SLOT_LIMIT_EXCEEDED`, `TRAN_ROSTER_SAME_SLOT`,
  `TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT` — and a malformed body is an untyped
  `400`. The brief maps them onto ARCHITECTURE §5 and proposes `WRITE_REJECTED`
  with a `details.kind` discriminator, the same move ADR-0009 made for
  `CONFIG_INVALID`.
- **A lineup write only ever targets the current scoring period.** `--week` is
  not a write parameter; next week and week 18 are both refused.
- **`espn_s2` is the whole credential.** The SWID cookie and the body's
  `memberId` are optional; ESPN derives the member from the session. The write
  layer never has to reveal the SWID.
- **ESPN executed a lineup change on a team the caller does not own.** The
  same swap sent with another manager's `teamId` returned `200 EXECUTED`,
  with the caller's own member id recorded against that team in the league's
  transaction log. The caller is not an owner, co-manager, or commissioner of
  that team by any record ESPN exposes. Reversed on the next request; the
  team read back identical. "Only my team" is therefore a client-side guard,
  and the brief makes it a precondition for #15 and, with more force, #18.
- **`isLeagueManager` is not in `mTeam`.** It is in `mNav` (with
  `isLeagueCreator`) and `mLeagueManager`. In this league the commissioner is
  another member, which corrects an assumption in #18's body.

## What was deliberately not done

No add, drop, waiver claim, bid, trade, or league-setting request; no request
with `isLeagueManager: true`; no probe of a third team after the second one
succeeded; no burst to find the rate limit; no second guess at a
non-executing `executionType` after `VALIDATE` came back `400`. The
budget-exceeded, position-limit, already-dropped and roster-full rejections
could not be observed without an add/drop, and the brief lists a safe way to
capture each in a throwaway league before #18 implements them.

## Evidence handling

Captures live in `docs/research/05-espn-write-surface/` as one JSON per probe
plus the read-back ledger and ESPN's own transaction log for the session.
Codex's review pointed at `docs/testing.md` §6 — a real private league's
recording is not committable even scrubbed — and it was right, so the
committed files were rewritten until they are not one: the cookie line is
redacted, the league id in every URL is the synthetic `99`, the member id is
the non-confirmable `{SWID-REDACTED}` rather than a salted pseudonym,
transaction UUIDs are redacted, and the other manager's roster is withheld
(player ids `0`, snapshots replaced by equality facts). Each file lists its
own redactions. The scrub was checked in-process against the real values,
every member GUID, and `CREDENTIAL_PATTERNS`. The provenance test that used to
read only cassette YAML now scans the text of every committed `.json`/`.yaml`
for a league id in a URL, so the next capture under `docs/` cannot bypass it.
The unredacted captures stay with the author, outside git. No cassette was
recorded for a write, by design.

## Memory

Two notes in `docs/memory/`: the ownership finding and where the commissioner
flag lives, both of which are the kind of thing the next implementer would
otherwise rediscover by sending a request they should not.
