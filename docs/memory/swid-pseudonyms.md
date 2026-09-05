# A redaction that preserves a join is a confirmable mapping, and that is the trade

**Found:** 2026-09-05, fixing #38. **Applies to:** the ESPN adapter (#4/#6),
anything that reads `teams[].owners`, and anything that adds a credential shape
to `CREDENTIAL_PATTERNS`.

## The bug: a placeholder is not a redaction when the value is a join key

ESPN uses the SWID GUID as a **join key inside a single payload**.
`teams[].owners` is a list of member SWIDs; `members[].id` is the member SWID.
Replacing every one of them with a shared `{SWID-REDACTED}` turns ten teams and
ten members into a ten-by-ten ambiguity, and `Team.owner_names` cannot be
derived at all.

Two things made that a correctness bug in the read path rather than a caching
wrinkle:

1. A cache hit and a cache miss **deliberately return the same bytes**
   (`cache-redaction-and-tag-classes.md` §3), so the adapter never sees the real
   GUID whether the store is warm or cold. There is no "just read it before the
   cache" escape.
2. The probe on #29 found the canary league's `members[]` carries **no display
   names at all** — only the GUID. The join is not one path from a team to a
   person; it is the only one.

The fix is a per-GUID pseudonym rather than one shared token:
`{00000000-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`, 24 hex digits of a BLAKE2b digest
keyed with a salt, over the upper-cased GUID with its braces stripped. Distinct
in, distinct out; same in, same out; no inverse.

**The generalisable form: before flattening a value, ask what else in the
payload points at it.** A scrubber that is correct on one field can still
destroy the only relationship the payload encodes.

## Four traps, and the first two are silent

**1. A well-formed synthetic GUID matches the SWID regex.** So a second
scrubbing pass re-scrubs a pseudonym into a *different* value — which the cache
does for real, on every `--fresh` refresh — and every already-scrubbed cassette
becomes a finding for the repo-wide credential scan. Both are fixed by one
edit, because `scrub_credential_patterns` and `scan_file` share
`CREDENTIAL_PATTERNS`: the first group is reserved as `00000000` and the
pattern carries `(?!\{00000000-)`. Neither failure announces itself, so the
tests are the only alarm — `test_scrubbing_a_pseudonym_shaped_body_is_idempotent`
(including a pass under a *different* salt) and
`test_a_pseudonym_is_not_a_credential_scan_finding`. Delete the lookahead and
eleven tests go red; that is the intended blast radius.

The residual: a *real* SWID whose first group happened to be `00000000` would
pass through unscrubbed. One in 2**32, and unavoidable for a sentinel that must
stay hex to stay GUID-shaped. Accepted, not overlooked.

**2. Share the function, not the salt.** This is the one place "one definition,
not two" does not hold, and the reason is a security property rather than a
style preference:

| | Cassette | Cache |
|---|---|---|
| Committed? | yes | never |
| Must reproduce byte-identically? | yes | no |
| Salt | deterministic, **public** (`CASSETTE_SWID_SALT`) | random per store (`new_swid_salt()`) |
| Confirmable mapping? | **yes** | no |

A cassette that re-records to different bytes every time is a fixture nobody can
review, so its salt cannot be random and cannot be secret. A cache has no such
constraint and gets the strictly stronger property for free. One function, salt
injected by the caller.

**3. The cache's salt lives in the same SQLite file as its entries**
(`store_meta`), so a discarded-and-rebuilt store loses both together.
Rotating it independently would make older and newer entries disagree about the
same member — the precise failure #38 exists to prevent, and one a
single-session test cannot see. The test that holds it writes an entry, closes
the store, reopens it, writes a second, and compares the two **rows**, not the
two salts.

**4. Encounter-order counters were rejected.** "First GUID seen becomes
`{...0001}`" is stable within one payload and wrong between two recorded in
different request orders, so the join breaks *across* fixtures instead of
within one — later, and somewhere else.

## The security trade, stated plainly

A stable pseudonym is by construction a **confirmable mapping**. Anyone holding
a real SWID can hash it under a known salt and test whether that member appears
in a committed cassette. That is presence, not value: the GUID itself still
never reaches disk, and there is no inverse. But it is a real, if modest,
regression from the genuinely one-way scrubber that shipped in #12, and it was
made deliberately, in exchange for a join the product cannot work without.

It is confined to the place that has no alternative. Cassettes pay it because
determinism is forced on them; the cache does not pay it at all. If a future
change wants a public salt somewhere new, that is a security decision to argue
for, not an implementation detail to inherit from whatever was convenient.

Related: [[cache-redaction-and-tag-classes]], [[cassette-scrubbing-blind-spots]],
[[credential-leak-channels]]
