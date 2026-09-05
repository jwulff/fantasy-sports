# You cannot register a secret you have never seen, and a cache is not vcrpy

**Found:** 2026-09-05, building U6 (#8). **Applies to:** U7 (the ESPN adapter),
anything that persists a provider response, and anything that purges by tag.

Three things about the HTTP cache that are not visible from the plan.

## 1. `core/redaction.py` now holds two mechanisms, and the cache needs the second

The registry — `remember_secret()` / `redact()` — blanks values this process
was *handed*. It came out of #5 so `FantasySportsError` could scrub its own
message, and it is the right tool for our own `espn_s2` and `SWID`, which
arrive through the auth chain.

It is structurally unable to protect the cache. ESPN echoes **other league
members'** SWID GUIDs inline in roster payloads. Those values were never
wrapped in a `Secret`, were never registered, and `redact()` will happily write
every one of them to disk. You cannot register a secret you have never seen.

What covers them is the *pattern* scrubber that landed with #12 — regexes
matching the credential shape rather than its value. It lived in
`tests/conftest.py`, which production code cannot import, so it was promoted to
`core/redaction.py` alongside the registry and `tests/conftest.py` now imports
it. Both mechanisms stay; neither replaces the other. The repo-wide fixture
*scanner* (`scan_file`, `iter_fixture_paths`) stayed in tests, because it
audits committed files rather than describing runtime behaviour.

The generalisable form: **"do we hold this value?" decides which scrubber you
need.** Registry for ours, patterns for everyone else's.

## 2. The cache has no vcrpy decompressing on its behalf

`decode_compressed_response=True` is what makes the cassette scrubber work —
vcrpy composes its `decode_response` *before* the scrub hook, so the regex sees
plain text. Nothing does that for a cache. `requests` sends
`Accept-Encoding: gzip, deflate` and ESPN answers gzipped, so a store that
scrubs the body it was handed scrubs a gzip stream, matches nothing, reports
success, and writes the credential to SQLite — where a completed week lives
**forever**.

`scrub_body()` therefore decodes first and scrubs second, and the test that
holds it starts from a *gzipped* fixture and reads the row back **out of
SQLite**. Both halves matter: a test written against an uncompressed fixture
passes with the order reversed, and a test that inspects the returned value
passes against a getter that scrubs on read while the disk holds the
credential. Reversing the two lines in `scrub_body` is the cheapest way to
check the test still measures something.

A body that will not decode is handed back to the caller but **not stored**.
Bytes that cannot be read cannot be proven clean, and an unreadable body is
worth much less than that guarantee.

## 3. A cache hit and a cache miss must return the same bytes

The stored body is redacted, so the miss returns the redacted body too. The
tempting alternative — live body on a miss, redacted body on a hit — makes the
adapter's normalized output depend on cache state, and it only misbehaves on
the *second* run, which is the worst possible place for it to show up.

The cost lands on U7 and is worth knowing before you write it: `teams[].owners`
and `members[].id` are both brace-wrapped SWID GUIDs, and both are replaced
with the same `{SWID-REDACTED}` placeholder. **The owner-to-member join cannot
use the SWID across the cache boundary.** Filed as follow-up work rather than
solved here, because the fix (a stable per-GUID pseudonym instead of one shared
placeholder) changes the shared scrubber that #12 owns.

## 4. Two tag classes, because one loses either the hit or the data

`players_wl` and `proTeamSchedules_wl` come from a **season** endpoint carrying
no league id, and both are re-fetched by `free_agents()` and `box_scores()` in
every league. Tagging them league-scoped breaks one of two ways: put the league
in the tag only, and a purge after a write to league A evicts league B's copy
of the whole-season player map; put it in the key as well, and every league
keeps its own copy of the same multi-megabyte payload and the cross-league hit
is gone.

So they carry a season-scoped, league-independent tag and no league tag at all.
`scope_of()` recovers the class from the tag string, and
`purge_by_league_tag()` *raises* on a season tag rather than running — a purge
that cannot classify its argument must not proceed, because the failure mode is
silent deletion of shared data.

Related: [[cassette-scrubbing-blind-spots]], [[credential-leak-channels]],
[[parallel-wave-seams]]
