# Commissioner authority is two flags in `mNav`, and `isLeagueManager` is the wrong one to read alone

**Found:** 2026-09-12, researching #14 — by getting it wrong first. **Applies
to:** anything that needs to know whether the session's member is the
commissioner: the ownership guard for writes, the journal's role field (#16),
`doctor`, and the "never act as LM by accident" rule in #18.

`view=mTeam` returns `members[]` with `displayName`, `firstName`, `lastName`,
`id`, `notificationSettings` — nothing about authority. Two views carry it,
and they do not say the same thing:

| View | `members[]` authority keys | John (creator, primary LM) | Team-2 owner (granted LM) |
|---|---|---|---|
| `mNav` | `isLeagueCreator`, `isLeagueManager` | `true`, **`false`** | `false`, `true` |
| `mLeagueManager` | `isLeagueManager` | **`false`** | `true` |

John founded the league and later granted the team-2 owner LM powers. ESPN
records the grant as `isLeagueManager: true` on the grantee and records the
founder as `isLeagueCreator: true` — and **does not mirror the creator's
authority into `isLeagueManager`**. A check that reads only
`isLeagueManager`, which is all `mLeagueManager` offers, reports the actual
commissioner as a plain member. The brief's first draft did exactly that and
concluded the commissioner was not one.

**Commissioner authority = `isLeagueCreator OR isLeagueManager`, from
`mNav`.** `mLeagueManager` alone is insufficient. The account-side view of the
same fact is `fan.api`'s `preferences[type.id == 9].metaData.entry.groups[]
.groupManager`, which reads `true` for John on this league and is the cheaper
check when only the session's own role is wanted; it cannot see other
members' authority.

Neither view is in `03-espn-api-surface.md` §1.4's table, because `espn-api`
never requests them. `mNav` also carries `teams[]` and `settings`, so it is
the cheaper one to fold into an existing multi-view bootstrap.

Two traps on the write side: the request body's `isLeagueManager` was `false`
on every probe and the commissioner's cross-team write executed anyway, so
the session confers the authority, not the flag; and `X-Fantasy-Role` read
`NONE` on every write-host response for the commissioner's own session, so it
is not the indicator it looks like.

Related: [[commissioner-cookie-writes-any-teams-lineup]],
[[espn-api-is-a-shape-reader-not-a-client]]
