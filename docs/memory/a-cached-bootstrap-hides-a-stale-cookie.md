# A cached bootstrap hides a stale cookie, so a retry must live at the request

**Found:** 2026-10-27, preflight review of #159. **Applies to:** anything that
reacts to ESPN refusing the cookies we hold (the browser re-import, a future
`doctor` probe) and anything that assumes "the league built, so the cookies work".

Cache keys are URL plus params plus the filter header. They do **not** include
the cookie, and the bootstrap views have long TTLs, so a command can build the
whole `League` from the store without a single request ever carrying the stored
`espn_s2`. The first request that does carry it is whichever live view comes
next (`mNav` in `session_role`, a roster or matchup view) or, for a write, the
POST. A first draft wrapped the league *build* in the retry and passed every
test, because the tests used a provider with no `CacheStore`; the default
configuration would have skipped the re-import and failed one step later.

So the retry is in `_Transport._live` (any GET). The write host's 401 gets no
retry at all: a write is sent exactly once, because that 401 has only been seen
on reversible lineup swaps, never on an irreversible add, drop or claim. `_post`
instead saves a fresh same-SWID browser pair for the next invocation and raises
the unchanged `WRITE_REJECTED` with `details.reimported_from_browser`. Corollaries:

- **A swap must be provisional.** The browser pair is used for the retry and
  saved only after ESPN accepts it; otherwise an ambiguous 401 (wrong league id,
  lost membership) overwrites good credentials with whatever account a browser
  holds. A refused retry puts the sent pair back.
- **Only the same account, ever.** Ownership comes from the SWID, so a browser
  pair with another SWID is not adopted at all, not even for the preflight read:
  the write that follows would execute as that account. The issue's wording
  ("either value differs") was narrowed to "same SWID, different `espn_s2`".

Related: [[espn-401-tells-you-nothing]], [[credential-leak-channels]], [[cache-redaction-and-tag-classes]]
