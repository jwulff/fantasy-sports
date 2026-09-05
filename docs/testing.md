# Testing against an API that breaks

This project's whole thesis is that ESPN changes without notice
(`docs/memory/prior-art-graveyard.md`: every previous ESPN fantasy tool died the
same way). That makes the fixture policy a design decision rather than a test
detail, so it is written down here.

Three layers, and each one covers what the others cannot.

| Layer | Runs | Answers |
|---|---|---|
| Recorded cassettes | every CI run, offline | does the adapter read the shapes ESPN **actually sent**? |
| Hand-authored cassettes | every CI run, offline | does it read the shapes no public league can produce? |
| `live`-marked tests | on demand, never in CI | does ESPN **still** send those shapes? |

Unit tests never touch the network. `pytest-socket` is enabled through
`addopts`, so a cassette miss fails loudly instead of quietly reaching ESPN.

---

## 1. Recorded: `tests/cassettes/espn/canary_2018.yaml`

A real recording of ESPN's public test league — `league_id=1234, year=2018`, the
league `espn-api`'s own integration test has hit daily and unattended for years
(ARCHITECTURE §14 item 6). No credentials are needed and none are sent.

Re-record with:

```bash
uv run python scripts/record_espn_cassettes.py
```

It drives the real `EspnProvider`, so what it records is exactly the request set
the adapter makes — including the *combinations* of `view=` parameters, which
matters because ESPN's server-side view composition is not the union of the
individual responses (research §7.7, issue #596).

**What it covers:** `fetch_league`, `fetch_teams`, `fetch_standings`,
`fetch_roster` (current and a past week), `fetch_matchups`,
`fetch_transactions` (including the full-season `since` sweep), and
`fetch_raw`.

**What it cannot cover, and why.** All three are library refusals keyed on the
*year*, so no request is made and there is nothing to record:

- **Box scores.** `League(1234, 2018).box_scores(1)` raises `Cant use box score
  before 2019`, and league 1234 does not exist for 2019, 2021, 2023 or 2025.
  The canary can never supply one.
- **Free agents.** Same refusal, same reason.
- **The activity feed.** Same refusal, plus ESPN stopped serving
  `kona_league_communication` for historical seasons entirely (issue #546, open
  since 2024).

Two further gaps that are about the league's *data* rather than the library:

- **The playoff week split.** Every one of this league's matchup periods maps
  1:1 to a scoring period, so `scoringPeriodId` vs `matchupPeriodId` — the one
  thing an in-season ESPN-only test never exercises — is not observable here.
- **Owner display names.** The canary's `members[]` carry ids and no names at
  all. Team-to-person mapping must not be built on ESPN member names.

---

## 2. Hand-authored: `tests/cassettes/espn/synthetic_2026.yaml`

Covers exactly the five gaps above (minus box scores, see below). Built by:

```bash
uv run python scripts/build_synthetic_cassette.py
```

**Be honest about what a synthetic fixture proves.** It shows the adapter reads
the shape it was told about. It does not show ESPN still sends that shape — only
the recording and the live tests do that. A green run against a hand-built
payload is weaker evidence than it looks, and the same caveat applies to the
scrubbing: a hand-built body is never gzipped, so a cassette corpus assembled
this way does not exercise the compressed-body path at all. That path is covered
by direct unit tests in `tests/unit/test_cache.py` and
`tests/unit/test_scrubbing.py`, not by the corpus.

Two deliberate choices in the data, both load-bearing:

- **Owner ids are brace-wrapped but not GUIDs** (`{OWNER-ALPHA}`). A real SWID
  would be rewritten to a single `{SWID-REDACTED}` placeholder by the scrub
  hook, collapsing every member to one identity and silently destroying the
  `teams[].owners` → `members[].id` join the fixture exists to exercise.
- **Every timestamp is a round epoch-millisecond value**, so a test can assert
  the exact UTC instant a kickoff renders as and catch a naive, host-local
  datetime leaking through from `espn-api`.

---

## 3. Not yet built: a box-score fixture

Box scores are not part of the Provider Protocol and are not read by any v0.1
command, so U7 does not need one. The League Gazette does (#29), and the recipe
is proven — recorded here so whoever lands it does not rediscover it:

`cwendt94/espn-api` is **MIT licensed** (Copyright (c) 2019 Christian Wendt) and
its test corpus contains a real, unredacted ESPN box-score payload at
`tests/football/unit/data/league_boxscore_2018.json` — a genuine `schedule[]`
response, unused upstream because the library refuses box scores before 2019.

- The raw file is 43,593,721 bytes. Slicing to one matchup and its two teams
  still leaves 1.9 MB, because every player carries a full-season `stats` array.
- Dropping every stat row except the requested scoring period's actual
  (`statSourceId: 0`) and projected (`statSourceId: 1`), plus
  `draftRanksByRankType`, `rankings`, `outlooks`, `seasonOutlook` and
  `ownership`, gives **129,370 bytes** pretty-printed.
- The 2019 guard is enforced on `League(year=...)`, not on the payload, so
  rewriting `seasonId` to 2019 in the fixture is sufficient.
- Shape is
  `schedule[].home.rosterForCurrentScoringPeriod.entries[].playerPoolEntry.player.stats[]`,
  with `entries[]` carrying `lineupSlotId`, `playerId`, `injuryStatus` and
  `acquisitionType`.

**MIT attribution is required, not optional.** The copyright notice must travel
with the derived file: put Christian Wendt's notice and a link to the source
file in a header next to the fixture, and record here that it is a trimmed
derivative rather than something we recorded.

---

## 4. What is *not* a cassette, on purpose

HTTP failures — 401, 404, 429, a body that will not parse — are tested by
injecting a transport (`EspnProvider(http=...)`), not by recording. Recording a
429 would mean provoking one from ESPN, and a cassette of a 401 records the
*absence* of a credential, which is indistinguishable from a recording where the
scrub ran. Neither is worth a fixture nobody can trust.

The one library behaviour that cannot be reached through that seam is the
alternate-URL-shape retry inside `checkRequestStatus`, which issues its own
`requests.get`. `tests/unit/test_espn_provider.py` stubs the module's `requests`
for those two tests specifically, and asserts the probe *ran* — the probe being
mandatory rather than optional is the whole of ARCHITECTURE §14 item 1.

---

## 5. Live tests

```bash
uv run pytest -m live                     # canary only; no credentials needed
ESPN_S2=... ESPN_SWID='{...}' FANTASY_SPORTS_ESPN_LEAGUE=... uv run pytest -m live
```

`tests/live/test_espn_live.py` splits into a canary group that needs nothing and
a private-league group that skips without credentials. When the canary group
goes red while the unit tests stay green, ESPN changed something — that is the
signal the whole health system exists to produce, and a red canary is a genuine
upstream change far more often than it is a flake.
