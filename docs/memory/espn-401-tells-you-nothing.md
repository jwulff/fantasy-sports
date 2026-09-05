# ESPN's 401 body is typed, and the type is a constant

**Found:** 2026-09-05, building U7 (#4). **Applies to:** the ESPN adapter, the
canary, `auth status`, and anything tempted to classify a credential from a
response.

ESPN answers an unreadable league with `401` and a body that looks like a gift:

```json
{"messages":["You are not authorized to view this League."],
 "details":[{"message":"You are not authorized to view this League.",
             "shortMessage":"You are not authorized to view this League.",
             "resolution":null,"type":"AUTH_LEAGUE_NOT_VISIBLE","metaData":null}]}
```

A machine-readable `details[].type` reads like the discriminator that finally
resolves the ambiguity ARCHITECTURE §14 item 1 describes. **It is not.** Probed
against a real private league, one variable at a time:

| What was sent | Status | `details[].type` |
|---|---|---|
| no cookies at all | 401 | `AUTH_LEAGUE_NOT_VISIBLE` |
| valid `SWID`, invalid `espn_s2` | 401 | `AUTH_LEAGUE_NOT_VISIBLE` |
| `SWID` alone | 401 | `AUTH_LEAGUE_NOT_VISIBLE` |
| `espn_s2` alone | 401 | `AUTH_LEAGUE_NOT_VISIBLE` |

Byte-identical. ESPN does not distinguish "your cookie is bad" from "this league
is not yours" from "you sent nothing".

**The absence is the finding.** An earlier note on #4 said to record the type an
*expired* cookie produces the first time we saw one. We have now seen a real
invalid `espn_s2` against a real private league and it produced no distinct type
at all. Do not go looking for one on this path.

## What follows for the adapter

- **The alternate-URL-shape double-probe stays mandatory**, not a fallback. It
  is the only thing that separates "wrong current-vs-historical shape for this
  season" from everything else, and it is library-owned — `espn-api`'s
  `checkRequestStatus` swaps `/leagueHistory/` for `/seasons/` and retries
  before raising, so our code never observes a bare 401. Re-implementing it
  would double the request cost against a provider whose throttle behaviour is
  unconfirmed.
- **Read `details[].type` anyway.** It is cheap, it varies on other paths, and a
  value we have not seen before is worth recording. It is read *before* the
  library's retry, because the retry does not surface the body.
- **Never map `AUTH_LEAGUE_NOT_VISIBLE` to `AUTH_EXPIRED`.** Sending a user to
  re-extract cookies that were fine is the exact failure §14 item 1 exists to
  prevent. `providers/espn.py` encodes the asymmetry instead: *nothing was sent*
  is a fact about us and reports `AUTH_MISSING`; everything else reports
  `LEAGUE_NOT_FOUND`, whose agent action — confirm the id and the access — is
  right for all the remaining possibilities. `AUTH_EXPIRED` is reachable only
  from a reason type that positively proves the credential failed, and the
  `AUTH_REASONS` table has no such entry.
- **Half a cookie pair is missing, not rejected.** `espn-api` sends cookies only
  when it has both `espn_s2` and `SWID`, so one alone reaches ESPN as an
  unauthenticated request. Reporting that as a rejection would send a user to
  re-extract a value that was never transmitted.

## The membership probe, and why it is not wired in

`https://fan.api.espn.com/apis/v2/fans/{url-encoded SWID}` returns 200 and
describes the account, including — with `?featureFlags=fantasy` — a
`fantasyData` block. It is the one cheap answer to "is this league even mine?",
which would convert the ambiguity above into evidence.

It is **not** wired into the adapter, for two reasons, and both should be
settled before it is:

1. **It is not a credential check.** It answers 200 with *no cookies at all*,
   for an arbitrary SWID belonging to someone else. It can only ever answer the
   membership question, never the credential one.
2. ~~**The league-listing shape is unverified.**~~ **Resolved 2026-09-05,
   during U8 (#9)** — probed with a SWID that *does* have leagues, so the shape
   is now observed rather than guessed. `fantasyData.totalFantasyLeagues` is
   the count and `fantasyData.leaguesBySportLeague` breaks it down by sport,
   but neither carries an id. The league ids live in **`preferences[]`**: one
   entry per league with `type.id == 9` (`"Fantasy League Manager"`), whose
   `metaData.entry.groups[].groupId` is the league id, alongside `seasonId`,
   `entryId` (the user's own team id), the group name, and a `href` that
   repeats the id as a query parameter. The earlier probe saw nothing because
   an account with no leagues has no such preference entries — an absence of
   rows, not an absent field.

   That removes the "shape nobody has seen" objection. Reason 1 above stands
   unchanged and is still enough on its own: this can answer the *membership*
   question and never the credential one.

**One trap if it is ever wired in:** the SWID travels in the *URL path*,
percent-encoded. The cassette scrubber matches brace-wrapped GUIDs and
`swid`-keyed values; `%7B...%7D` matches neither, and neither does the repo-wide
scan. A cassette of that request would write a real SWID to disk and pass every
check. Either extend the patterns first or never record it.

Related: [[cassette-scrubbing-blind-spots]], [[credential-leak-channels]]
