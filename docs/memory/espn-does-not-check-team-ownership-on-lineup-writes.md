# ESPN executes a lineup write on a team the caller does not own

**Found:** 2026-09-12, researching #14. **Applies to:** every write command
(#15, #16, #18), the provider's write transport, and any test that assumes ESPN
will refuse an out-of-scope `teamId`.

A `ROSTER` transaction with `LINEUP` items sent to
`lm-api-writes.fantasy.espn.com` with another manager's `teamId` — same
league, caller not an owner, not a co-manager, not the league manager,
`isLeagueManager: false` in the body — returned `200` with `status:
"EXECUTED"`, moved that team's players, and was recorded in `mTransactions2`
under the *caller's* member id. It was reversed on the next request and the
team read back identical. Evidence: `docs/research/05-espn-write-surface/p12-*.json`
and `audit-trail-mTransactions2.json`; the brief's §6.

Two things follow, and the second is the reason this is a memory rather than a
line in the brief.

**1. Team ownership is our invariant, not ESPN's.** Resolve "my teams" from the
session — `fan.api`'s `preferences[]` entry for the league (`entryId` is the
team id) or `teams[].owners` containing the resolved SWID — and refuse any
`teamId` outside that set *before building a request*. For lineups a slip is
embarrassing; for the irreversible class in #18 a slip drops another manager's
player, and whether ESPN checks ownership on `FREEAGENT`/`WAIVER`/`TRADE_*` is
unknown and must not be probed in a real league.

**2. Do not write a test that "proves" ESPN rejects it.** A hand-authored
cassette asserting a `403` on an other-team write would encode a behaviour the
provider does not have, and the guard it was meant to cover would be the only
thing standing. The guard's test is a unit test of the guard.

Scope of the observation: one league, one other team, one probe, on the
football (`ffl`) game. Whether the laxity is "any league member", "any account
with any team", or narrower was deliberately not narrowed. Treat it as the
default assumption until a probe in a throwaway league says otherwise.

Related: [[espn-401-tells-you-nothing]], [[credential-leak-channels]],
[[isleaguemanager-lives-in-mnav]]
