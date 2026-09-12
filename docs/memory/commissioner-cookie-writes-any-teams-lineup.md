# The commissioner's cookie writes any team's lineup without being asked to

**Found:** 2026-09-12, researching #14; corrected the same day against
John's ground truth. **Applies to:** every write command (#15, #16, #18), the
provider's write transport, and any test that assumes ESPN will refuse an
out-of-scope `teamId`.

A `ROSTER` transaction with `LINEUP` items sent to
`lm-api-writes.fantasy.espn.com` with another manager's `teamId` — same
league, caller not in that team's `owners`, `isLeagueManager: false` in the
body — returned `200` / `status: "EXECUTED"`, moved that team's players, and
was recorded in `mTransactions2` under the *caller's* member id. Reversed on
the next request; the team read back identical. Evidence:
`docs/research/05-espn-write-surface/p12-*.json`; the brief's §6.

The first reading of this was "any member can edit any lineup". That was
wrong, and the reason it was wrong is the other memory,
[[commissioner-authority-is-two-flags-in-mnav]]: the session belongs to the
league's **creator and primary commissioner**, whose authority is carried by
`isLeagueCreator`, not by the `isLeagueManager` flag the first reading had
checked. The cross-team write is consistent with commissioner authority, and
it says nothing about what an ordinary member's cookie can do — that is
unverified and could not be tested in this league.

What follows, and it is stronger than the mistaken version:

**1. Team ownership is our invariant, not ESPN's — because the cookie is
allowed to do more than the tool should.** ESPN did not need the body to say
"act as LM"; the session was enough. So a `--team` typo on a commissioner's
cookie is *honoured*. Resolve "my teams" from the session — `fan.api`'s
`entryId` for the league, or `teams[].owners` containing the resolved SWID —
and refuse any other `teamId` before building a request, regardless of role.
For lineups a slip is embarrassing; for the irreversible class in #18 a slip
drops another manager's player, and whether ESPN honours LM authority on
`FREEAGENT`/`WAIVER`/`TRADE_*` is unknown and must not be probed in a real
league.

**2. Anything LM-scoped is a separately named, separately gated capability**,
opt-in per invocation, routed through the sanity gate, and it must *require*
the `mNav` flag rather than discover its authority by trying. The tool never
sets `isLeagueManager: true`.

**3. Do not write a test that "proves" ESPN rejects a cross-team write.** A
hand-authored cassette asserting a `403` would encode a behaviour the
provider does not have for this session, and the guard it was meant to cover
would be the only thing standing. The guard's test is a unit test of the
guard.

**To settle the non-LM question later:** in a league the tester belongs to
but does not manage (`mNav`: both flags `false`; `fan.api`:
`groups[].groupManager: false`), run the same swap-and-reverse against
another team's two unlocked, mutually-eligible players. Expect a `409
TRAN_*`; if it executes, reverse on the next request and treat it as an
integrity finding.

Related: [[commissioner-authority-is-two-flags-in-mnav]],
[[espn-401-tells-you-nothing]], [[credential-leak-channels]]
