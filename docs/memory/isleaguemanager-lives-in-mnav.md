# `isLeagueManager` is not in `mTeam`

**Found:** 2026-09-12, researching #14. **Applies to:** any code that needs
to know whether the session's member is the commissioner — the ownership
guard for writes, `doctor`, and the "never act as LM" rule in #18.

`view=mTeam` returns `members[]` with `displayName`, `firstName`, `lastName`,
`id`, `notificationSettings` — and nothing about authority. The community
belief that `members[].isLeagueManager` comes with `mTeam` is wrong for the
2026 payload. Two views carry it:

| View | `members[]` keys |
|---|---|
| `mNav` | `displayName`, `firstName`, `id`, `isLeagueCreator`, `isLeagueManager`, `lastName` |
| `mLeagueManager` | `displayName`, `id`, `isLeagueManager` |

Neither is in `03-espn-api-surface.md` §1.4's table, because `espn-api` never
requests them. `mNav` also carries `teams[]` and `settings`, so it is the
cheaper one to add to an existing multi-view bootstrap; `mLeagueManager` is the
narrower one if only the flag is wanted.

In the configured default league the commissioner is the owner of another
team, not John — so #18's "John holds commissioner privileges" is a
per-league fact to read, never an assumption to code against.

Every write-host response also carries an `X-Fantasy-Role` header; it read
`NONE` for a plain manager. Whether it reads something else for a commissioner
is unverified, and until it is, `mNav` is the source of truth.

Related: [[espn-does-not-check-team-ownership-on-lineup-writes]],
[[espn-api-is-a-shape-reader-not-a-client]]
