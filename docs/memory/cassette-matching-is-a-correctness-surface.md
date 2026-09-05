# A cassette matcher that is right is not enough; it also has to be stable

**Found:** 2026-09-05, building U9 (#12). **Applies to:** anything that records
or replays ESPN traffic, and the box-score fixture (#29) when it lands.

## 1. The default matcher is a silent wrong-answer machine

vcrpy's default `match_on` is `method, scheme, host, port, path, query`. It
**ignores headers entirely**. ESPN scopes free agents, transactions, the
activity feed and box scores by a JSON `x-fantasy-filter` *header* against an
otherwise identical URL, so two such recordings collide and the second read
replays the first one's body. Nothing errors and the filter-gated test passes.

The corpus had a live instance of this before the matcher landed:
`fetch_free_agents(week, position="wr")` and the unfiltered call are the same
URL, and the position-filtered test asserted `assert ...` against a truthy list
that was the *unfiltered* response. For this endpoint specifically that is the
worst available failure mode — ESPN's default player set looks exactly like a
plausible answer to any filter, which is also why the adapter refuses an unknown
position rather than sending it.

Same shape as the cache-key bug that put `x-fantasy-filter` into `cache_key`'s
`extra`. **When a dimension outside the URL changes the response, every keyed
store has to learn about it — the cache and the cassette are two such stores,
and fixing one does not fix the other.**

## 2. The header is not stable across processes, so the matcher cannot be `==`

This is the part that is invisible until it bites. `espn-api` builds the
transactions filter as `{"transactions": {"filterType": {"value": list(types)}}}`
where `types` is a Python **set**, and `str` hashing is randomised per
interpreter. The identical query therefore serialises its `value` array in a
different order on every run:

```
run 1  ["ROSTER", "TRADE_ACCEPT", "WAIVER", "FREEAGENT", "TRADE_UPHOLD", ...]
run 2  ["TRADE_UPHOLD", "TRADE_ACCEPT", "WAIVER", "DRAFT", "WAIVER_ERROR", ...]
run 3  ["WAIVER_ERROR", "ROSTER", "DRAFT", "FREEAGENT", "WAIVER", ...]
```

A literal string matcher would make the committed `mTransactions2` interactions
replay or miss depending on `PYTHONHASHSEED`. **A flaky matcher is worse than
the bug it fixes**, and it would have looked like ESPN flakiness rather than
ours. So `canonical_filter()` compares parsed JSON with object keys sorted and
arrays sorted by their elements' canonical form. Every filter ESPN accepts is a
*set* of values (`filterType`, `filterSlotIds`, `filterIncludeMessageTypeIds`),
so order carries no meaning and sorting loses nothing. Unparseable falls back to
a literal comparison — unparseable is not a licence to treat two headers as one.

The committed `canary_2018.yaml` deliberately still holds a **stale** order:
re-recording rewrites those header lines and nothing else, and that diff was
reverted. Keeping it means the suite is green only because the comparison
canonicalises, which keeps the guarantee visible instead of letting a fresh
recording paper over it.

**The generalisable form: before matching on a value a third-party library
serialises, check whether the library's serialisation is deterministic.** A
`json.dumps` over a `set`, a `dict` built from `**kwargs`, an unordered
container anywhere in the chain — any of them turns an exact-match assertion
into a hash-seed lottery.

## 3. `match_on` is resolved by name, which is the seam that keeps it enforced

`VCR.matchers` is an instance dict populated in `__init__`, and
`get_merged_config` looks each `match_on` entry up in it. A custom matcher
therefore cannot be passed inline — it must be `register_matcher`'d on the
instance. That is a nuisance and also the enforcement: `build_vcr_config()`
names `fantasy_filter` in `match_on`, so `vcr.VCR(**build_vcr_config())` raises
`KeyError` at `use_cassette` time instead of silently matching on less.
`build_vcr()` is the only supported constructor. `pytest-recording` builds its
own `VCR`, so the `pytest_recording_configure` hook registers it there too.

## 4. What a cassette corpus structurally cannot prove

`decode_compressed_response=True` decompresses *before* anything is written, so
**every cassette lands as plain text by construction**. It is not that the
recordings happen not to be gzipped — no recording ever can be. A green corpus
run is therefore not evidence that the scrubber survives a compressed body, even
though "the scrubber works, look at all these fixtures" is exactly what it looks
like.

Same class as the vacuous-scan guard. The answer is the same too: assert the
absence. `test_no_committed_cassette_carries_a_compressed_body` fails if a
compressed or `!!binary` body ever does reach a committed cassette, so the claim
in `docs/testing.md` §2 cannot quietly go stale, and the compressed path is
covered by direct unit tests instead.

The hole `decode_compressed_response` does **not** close: `decode_response`
knows gzip, deflate and brotli, and returns an encoding it does not recognise
untouched. The scrubber is then handed opaque bytes — and refuses, which is the
only honest outcome. No cassette can cover that either; it would require ESPN to
start serving zstd.

## 5. PII is a provenance question, not a content question

The credential scan proves a cassette holds no credential. It cannot tell whose
league the payload came from, and #12 asks for both. A name is not
machine-recognisable; a league id is. So the enforcement is an allowlist of
league ids in committed cassettes (`1234` public, `99` invented), a recording
script that routes every other league to a gitignored directory, and a refusal
when `--out` points back at the committed one.

The part worth stating out loud: #38's SWID pseudonyms use a **public,
deterministic** salt because a cassette must re-record byte-identically. That
makes cassette pseudonyms a confirmable mapping — anyone holding a real SWID can
hash it and test for that member's presence. Accepted for a league that is
already public; not something to extend to nine people who did not choose to be
in this repository.

Related: [[cassette-scrubbing-blind-spots]], [[swid-pseudonyms]],
[[cache-redaction-and-tag-classes]], [[espn-api-is-a-shape-reader-not-a-client]]
