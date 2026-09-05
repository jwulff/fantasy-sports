# ESPN Fantasy API (v3) — Surface, Auth, and Breakage History

**Status:** Research brief, 2026-08-26. Feeds `docs/ARCHITECTURE.md` §5, §6, §8, §11.

**Method:** Cloned `cwendt94/espn-api` at `v0.46.0` (commit as of 2026-08-26) and read
every line of `espn_api/`, walked its git history and GitHub Actions run history,
read all 218 issues (open + closed, all-time), and cross-checked against
community sources (ffscrapr, stmorse.github.io, GitHub discussions). Every
`view=` name, URL, field name, and error class below is either quoted directly
from library source, from real fixture JSON in the library's own test suite, or
labeled **[inferred]** / **[unverified]** where I could not confirm it directly.
No field name below is invented.

---

## 1. The endpoint and view surface

### 1.1 Base URLs — confirmed from source + git history

```python
# espn_api/requests/constant.py (current, v0.46.0)
FANTASY_BASE_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/'
NEWS_BASE_ENDPOINT    = 'https://site.api.espn.com/apis/fantasy/v3/games/'
FANTASY_SPORTS = {'nfl': 'ffl', 'nba': 'fba', 'nhl': 'fhl', 'mlb': 'flb', 'wnba': 'wfba'}
```

**The 2024 migration, confirmed to the exact commit:**

```
commit 0e39576  Author: Christian Wendt  Date: 2024-04-25 15:27:21 +0000
    Update ESPN API Base Endpoint
-   FANTASY_BASE_ENDPOINT = 'https://fantasy.espn.com/apis/v3/games/'
+   FANTASY_BASE_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/'
```

So: **2024-04-25** is the exact date, not "sometime in 2024." Old host
(`fantasy.espn.com/apis/v3/games/`) still resolves for some read paths per
community reports but is not what any current client should target.

A second, separate base exists for player news only, added 2025-02-02:
`NEWS_BASE_ENDPOINT = 'https://site.api.espn.com/apis/fantasy/v3/games/{sport}/news/players'`
— unauthenticated, different host family entirely (`site.api.espn.com`, ESPN's
general sports-content API, not the fantasy-platform API). Used by
`get_player_news(playerId)`.

### 1.2 Current-season vs. historical-season endpoint shape — CONFIRMED, and it is messier than a clean cutoff

```python
# espn_api/requests/espn_requests.py __init__
self.LEAGUE_ENDPOINT = FANTASY_BASE_ENDPOINT + FANTASY_SPORTS[sport]
if year < 2018:
    self.LEAGUE_ENDPOINT += "/leagueHistory/" + str(league_id) + "?seasonId=" + str(year)
else:
    self.LEAGUE_ENDPOINT += "/seasons/" + str(year) + "/segments/0/leagues/" + str(league_id)
```

- **Current-season shape:** `.../ffl/seasons/{year}/segments/0/leagues/{league_id}`
- **Historical shape (year < 2018):** `.../ffl/leagueHistory/{league_id}?seasonId={year}`
- The historical shape's response is a **JSON list**, not an object —
  `league_get()` unwraps it: `return response[0] if isinstance(response, list) else response`.
- **The `< 2018` cutoff is a heuristic, not a guarantee.** The library's own
  `checkRequestStatus` treats **any 401** as "wrong endpoint shape for this
  year" and automatically retries the *other* shape before giving up — see
  §4. That fallback exists because the cutoff is empirically unreliable at
  the boundary, and because ESPN's own private-league gating interacts with
  it (§1.3).

### 1.3 Public vs. private league access

- No separate endpoint — same URL, same `view` params. Difference is
  entirely in whether `espn_s2`/`SWID` cookies are sent and whether ESPN's
  server-side authz permits the request.
- Public league, no cookies: 200, full data.
  Private league, no cookies: **401**.
  Private league, correct cookies: 200.
  Private league, wrong/expired cookies: **401**, same as no cookies —
  **ESPN does not distinguish "no credentials" from "bad credentials" in the
  status code.** Both are 401.
- **Confirmed 2025-08 community finding (issue #650):** pre-2018 league data
  now appears to require `espn_s2`/`SWID` even for what used to be public
  historical data — several users hit `League {id} does not exist` (404, not
  401 — see §2.3) for years before 2018 that worked earlier in 2026. One
  commenter successfully fetched 2005–2017 data using cookies; unauthenticated
  requests to the same league/year got `{"messages":["Not Found"],...}`.
  ESPN's own website shows the same reduced history to logged-out users. This
  reads as ESPN tightening/relocating historical data behind auth sometime in
  2025, not a data purge (a maintainer initially suspected deletion; a later
  commenter demonstrated the data is still there behind cookies). **This is
  exactly the kind of drift our canary needs to catch for the public test
  league** — see §4.4.

### 1.4 Full view surface — every `view=` value the library sends, verified by exact grep across every sport module

| View | Used for | Called from (v0.46.0) |
|---|---|---|
| `mTeam`, `mRoster`, `mMatchup`, `mSettings`, `mStandings` (combined, one call) | Full league bootstrap: teams, rosters, schedule/scores, settings, standings | `get_league()` — `base_league.py::_fetch_league` |
| `mRoster` (alone, with `scoringPeriodId`) | Roster **as of a specific week** (not just current) | `League.load_roster_week(week)` |
| `mMatchupScore` | Weekly matchup scores (`League.scoreboard(week)`) | `football/league.py::scoreboard` |
| `mMatchupScore` + `mScoreboard` (combined, one call, with `x-fantasy-filter` scoping to one `matchupPeriodId`) | Full box scores incl. per-player lineup/points (`League.box_scores(week)`) | `football/league.py::box_scores` |
| `mDraftDetail` | Draft picks | `get_league_draft()` |
| `mTransactions2` | Transactions, waiver offers/bids, pending claims (filtered by `filterType` — `WAIVER`, `FREEAGENT`, `TRADE_*`, etc.) | `League.transactions()`, `League.offers_report()` |
| `mPositionalRatings` | Positional strength-of-schedule ranks, used internally to annotate box score players' matchup difficulty | `League._get_positional_ratings(week)` |
| `kona_player_info` | Free agent / waiver player pool search (this is the "free agents" endpoint) | `League.free_agents()` |
| `kona_playercard` | Single/multi player detail card incl. season-long stats by scoring period | `get_player_card()` — `League.player_info()` |
| `kona_league_communication` | League activity feed (adds/drops/trades) — filtered via `x-fantasy-filter` on `topicsByType`/message-type IDs | `League.recent_activity()` |
| `kona_league_messageboard` | League message board threads | `get_league_message_board()` |
| `players_wl` | Full pro-player master list (id → name mapping for the whole player pool) — hit against `/players` sub-path, filtered to `filterActive:true` | `_fetch_players()` — populates `player_map` |
| `proTeamSchedules_wl` | NFL/pro team schedule (game dates, home/away) | `_get_pro_schedule()` |
| — (no `view` param; separate base URL `site.api.espn.com`) | Player news | `get_player_news()` |

**Not used by the library, despite appearing in some community docs:**
`mBoxscore` is **not** a view name the library sends. What most people call
"box scores" is actually `mMatchupScore` + `mScoreboard` combined, plus
positional-ratings and pro-schedule side calls, stitched client-side into a
`BoxScore` object. If our team's earlier assumption was that `mBoxscore` is a
real, separate ESPN view — **that assumption should be dropped or explicitly
labeled unverified.** Likewise there is no dedicated `mPendingTransactions`
view; pending waiver claims come back from `mTransactions2` with a
`WAIVER`/`WAIVER_ERROR` filter and `PENDING`/`FAILED_*` statuses on individual
offer items (see §7.1 for the exact status vocabulary).

### 1.5 v0.1 command → view mapping

| Our v0.1 command | View(s) needed | Extra calls required |
|---|---|---|
| `league info` | `mSettings` (part of the combined bootstrap call) | none |
| `teams` | `mTeam` (bootstrap call) | none |
| `standings` | `mTeam` + `mStandings` (bootstrap call); pure client-side sort, no separate request | none |
| `roster --team` | `mRoster` (bootstrap call, or `mRoster` alone with `scoringPeriodId` for a specific past week) | `players_wl` (player name map) already loaded at bootstrap |
| `matchups --week N` | `mMatchupScore` (+ `mScoreboard` if we want live/projected box-score detail, not just final score) | `proTeamSchedules_wl` + `mPositionalRatings` if projected points are wanted |
| `transactions --limit N` | `mTransactions2` | `players_wl` for name resolution |
| `free-agents --pos WR --limit N` | `kona_player_info` (requires `x-fantasy-filter` header, see §7.1) | `proTeamSchedules_wl` + `mPositionalRatings` (library always fetches these for `free_agents()`) |
| `raw --view X` | passthrough, any view above | n/a by design |

### 1.6 Example, copy-pasteable `curl`

```bash
# Public league bootstrap (current season) — no auth
curl -s "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/1234" \
  --data-urlencode "view=mTeam" \
  --data-urlencode "view=mRoster" \
  --data-urlencode "view=mMatchup" \
  --data-urlencode "view=mSettings" \
  --data-urlencode "view=mStandings" -G | jq '.status'

# Private league — cookies required; SWID keeps its braces, unencoded
curl -s "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/YOUR_LEAGUE_ID" \
  --data-urlencode "view=mTeam" -G \
  -H "Cookie: espn_s2=YOUR_ESPN_S2; SWID={YOUR-SWID-GUID}"

# Historical season (pre-2018 shape)
curl -s "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/1234?seasonId=2015" \
  --data-urlencode "view=mTeam" -G

# Free agents — the x-fantasy-filter header is not optional; ESPN silently
# returns its default (probably top-owned) player set without it, not an error
curl -s "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/YOUR_LEAGUE_ID" \
  --data-urlencode "view=kona_player_info" \
  --data-urlencode "scoringPeriodId=1" -G \
  -H 'x-fantasy-filter: {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},"filterSlotIds":{"value":[4]},"limit":50,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}' \
  -H "Cookie: espn_s2=YOUR_ESPN_S2; SWID={YOUR-SWID-GUID}"
```

---

## 2. Authentication mechanics

### 2.1 What the cookies are and where they live

- **`espn_s2`**: an opaque, URL-encoded session token, typically 250+
  characters, containing literal `%2F`-style percent-encoding — **do not
  URL-decode it**, pass it exactly as copied.
- **`SWID`**: ESPN's internal user GUID, ~38 characters **including the
  curly braces**: `{A1B2C3D4-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`. The braces are
  part of the value ESPN expects back as a cookie — **stripping them is a
  documented, common mistake** across every community guide I found
  (ffscrapr, GameDayBot, the Chrome "ESPN Cookie Finder" extension's own
  docs). Confirms directly what `docs/ARCHITECTURE.md` should call out in
  `auth login`'s guided extraction: warn explicitly if the pasted SWID lacks
  braces, and add them back rather than rejecting.
- Both live as ordinary browser cookies scoped to `fantasy.espn.com` (some
  guides say `.espn.com`), set after a normal logged-in session. Extraction
  is DevTools → Application/Storage → Cookies → `fantasy.espn.com` → copy
  `espn_s2` and `SWID` values. A third-party "ESPN Cookie Finder" Chrome
  extension automates this (open source, unaffiliated with ESPN or
  `espn-api`) — **[unverified]** whether it's still current; not something
  we should depend on or recommend without vetting.

### 2.2 No programmatic auth, and ESPN closed the one path that existed

`espn-api` used to ship a `username`/`password` → `espn_s2`/`SWID` exchange
against ESPN's Disney-identity login API
(`registerdisney.go.com/jgc/v5/client/ESPN-FANTASYLM-PROD/...`). It is now
**dead code, commented out in the source**, with this note left by the
maintainer:

```python
# Username and password no longer works using their API without using google
# recaptcha. Possibly revisit in future if anything changes
```

Removed from the active code path in commit `bda5ad7` (2022-07-31, "Remove
username and password"). ESPN added reCAPTCHA to the login flow specifically
to close this off. **Confirms `docs/ARCHITECTURE.md` §6's "Refresh: None" is
correct and current** — there is no supported, non-browser-automation path to
mint or refresh these cookies. The only way to get fresh ones is a real
browser session (or a Selenium-style automated browser login, which a
maintainer explicitly said is out of scope for the library and which we
should treat as out of scope for v0.1 too — it's a fragile, ToS-adjacent
approach that a read-only CLI doesn't need).

### 2.3 What rejected credentials actually look like — this is the important part for `AUTH_EXPIRED` mapping

**Status code: `401`.** Confirmed directly in source
(`espn_requests.py::checkRequestStatus`) — this is the only branch that
raises the library's `ESPNAccessDenied` exception. But the mapping is
**not** "401 → auth expired." Read the actual logic:

```python
def checkRequestStatus(self, status, extend="", params=None, headers=None):
    if status == 401:
        # Try the alternate endpoint format (current ⇄ historical shape),
        # but save the original in case it fails
        original_endpoint = self.LEAGUE_ENDPOINT
        # ... swap /leagueHistory/ ⇄ /seasons/{year}/segments/0/leagues/ ...
        r = requests.get(self.LEAGUE_ENDPOINT + extend, ...)
        if r.status_code == 200:
            return r.json()          # it wasn't an auth problem — wrong URL shape
        self.LEAGUE_ENDPOINT = original_endpoint   # restore; don't corrupt future calls
        if not self.cookies or 'espn_s2' not in self.cookies or 'SWID' not in self.cookies:
            raise ESPNAccessDenied("espn_s2 and swid are required")
        raise ESPNAccessDenied(f"League {self.league_id} cannot be accessed with the provided credentials")
    elif status == 404:
        raise ESPNInvalidLeague(f"League {self.league_id} does not exist")
    elif status != 200:
        raise ESPNUnknownError(f"ESPN returned an HTTP {status}")
```

**This is the single most load-bearing fact for our error taxonomy.** A raw
401 from ESPN means "either your cookies are missing/wrong, *or* you hit the
league with the wrong current-vs-historical URL shape for that season." The
library resolves the ambiguity by **retrying the other shape before deciding
it's an auth failure.** If we map "401 → `AUTH_EXPIRED`" naively without
this same double-probe, we will misclassify a season-boundary URL-shape miss
as a credentials problem and tell the user to re-extract cookies when the
real bug is our own year-based endpoint selection.

Separately:

- **`404` → `ESPNInvalidLeague`** — "League does not exist." This is what a
  bad league ID *and* an inaccessible-without-cookies historical league both
  return (see §1.3 — the pre-2018 auth-gating issue manifests as 404, not
  401). Our `LEAGUE_NOT_FOUND` code should account for "actually needs
  cookies" as one cause of a 404, not just "typo'd the ID."
- **Anything else non-200 → `ESPNUnknownError`** with the literal HTTP
  status in the message. **429 is not special-cased anywhere in the library**
  — a rate-limit response is indistinguishable from a 500 to `espn-api`; both
  raise the same generic `ESPNUnknownError`. We have to add our own 429
  detection in `providers/espn.py` if we want `RATE_LIMITED` to actually
  fire (§5).
- **No custom exception exists for "response parsed as JSON but the shape is
  wrong."** A field ESPN renamed or removed surfaces as a raw Python
  `KeyError`/`TypeError`/`IndexError` from deep inside `Team.__init__`,
  `Player.__init__`, etc. (all of which do direct `data['record']['overall']['wins']`-style
  access with no `.get()` guards in the object constructors, versus the
  request layer which does use `.get()` more defensively). **`SCHEMA_DRIFT`
  is entirely our responsibility to synthesize** — `espn-api` gives us zero
  help distinguishing it from "I called the wrong method." See §4.5.
- **Secrets-in-error-message bug, now fixed:** until commit `78c239a`
  (2026-02-15), the access-denied error message literally interpolated
  `self.cookies.get('espn_s2')` into the exception text — i.e. **the raw
  session cookie could end up in a stack trace or log line.** Fixed to a
  generic "cannot be accessed with the provided credentials" message. Good
  confirmation that `docs/ARCHITECTURE.md` §6's "redacted in all error paths
  including tracebacks" is the right call — this is a real bug class, not a
  hypothetical one, and it shipped in a widely-used library for over a year.

### 2.4 Cookie lifetime

**[unverified, no authoritative source found].** No ESPN documentation, no
`espn-api` source, and no community post I found states a concrete
expiration window in days/weeks. `docs/ARCHITECTURE.md`'s "Weeks–months,
silently" is consistent with the *volume* of recurring "auth stopped
working" issues filed against this library since 2019 (issues #36, #99,
#100, #148, #164, #218, #245, #272, #549 — a new one roughly every year,
frequently correlated with the start of a new NFL season when users return
after an off-season gap) but I cannot cite a hard TTL number. Treat the
architecture doc's phrasing as the right *qualitative* claim (silent,
long-but-unpredictable) without a verified quantitative bound. **This is
exactly why `auth status` reporting cookie *age* (not a predicted expiry) is
the right design** — there's no reliable expiry to predict against.

### 2.5 Has ESPN changed auth mechanics recently?

Only indirectly: the two 2025-08-21 commits "Throw access denied error even
if no cookies" / error-verbiage cleanups were maintainer-side robustness
fixes, not a reaction to an ESPN-side auth change. I found no evidence of
ESPN changing the cookie *mechanism* itself (still plain session cookies,
still `espn_s2` + `SWID`, still browser-extraction-only) in the period
covered by this repo's history. The historical-data auth-gating tightening
in §1.3/2.3 (2025-08) is the closest thing to an auth-adjacent ESPN change,
and it's about *what requires* cookies, not *how* cookies work.

---

## 3. What `espn-api` covers vs. does not

### 3.1 Public object model — real attribute names, football module

**`League`** (`espn_api/football/league.py`, extends `BaseLeague`):
`league_id`, `year`, `teams: List[Team]`, `members`, `draft: List[BasePick]`,
`player_map` (bidirectional id↔name dict), `currentMatchupPeriod`,
`scoringPeriodId`, `firstScoringPeriod`, `finalScoringPeriod`,
`previousSeasons`, `current_week`, `nfl_week`, `settings: Settings`.
Methods: `standings()`, `standings_weekly(week)`, `scoreboard(week)`,
`box_scores(week)`, `free_agents(week, size, position, position_id)`,
`player_info(name|playerId)`, `transactions(scoring_period, types)`,
`recent_activity(size, msg_type, offset)`, `offers_report(week)`,
`power_rankings(week)`, `message_board(msg_types)`, `refresh()`,
`refresh_draft()`, `load_roster_week(week)`, `get_team_data(team_id)`.

**`Team`**: `team_id`, `team_abbrev`, `team_name`, `division_id`,
`division_name`, `wins`/`losses`/`ties`, `points_for`, `points_against`,
`acquisitions`/`acquisition_budget_spent`/`drops`/`trades`/`move_to_ir`,
`playoff_pct`, `draft_projected_rank`, `streak_length`/`streak_type`,
`standing` (current playoff seed), `final_standing`, `waiver_rank`,
`logo_url`, `roster: List[Player]`, `schedule: List[Team]` (opponent per
week, self on bye), `scores: List[float]`, `outcomes: List[str]` (`'W'/'L'/'T'/'U'`),
`mov` (margin of victory per week), `owners`, `stats` (dict keyed by
human-readable stat name via `PLAYER_STATS_MAP`).

**`Player`**: `name`, `playerId`, `posRank`, `eligibleSlots` (list of
position-name strings), `acquisitionType`, `proTeam` (NFL team abbrev),
`jersey`, `injuryStatus`, `injured` (bool), `onTeamId`, `lineupSlot`,
`position`, `stats` (dict keyed by `scoringPeriodId` →
`{points, breakdown, points_breakdown, avg_points, projected_points,
projected_breakdown, projected_points_breakdown, projected_avg_points}`),
`schedule` (dict keyed by scoring period → `{team, date}`), `percent_owned`,
`percent_started`, `active_status` (`'bye'|'active'|'inactive'`),
`total_points`, `projected_total_points`, `avg_points`,
`projected_avg_points`.

**`BoxPlayer(Player)`** (adds, per-matchup): `slot_position`, `pro_opponent`,
`pro_pos_rank`, `game_played` (0/100), `on_bye_week`, `points`, `breakdown`,
`points_breakdown`, `projected_points`, `projected_breakdown`,
`projected_points_breakdown`. Note the constructor comment: **ESPN's
top-level `proTeamId` on a player is their *current* team, not their team at
the time of a given game** — the library deliberately prefers the per-week
`stats[].proTeamId` from the actual scoring-period stat entry, with a
same-run cache to paper over bye weeks after a mid-season trade. This is a
real, documented (in-code) ESPN data quirk, not a library bug: **if we ever
build anything from raw `kona_player_info`/`mRoster` payloads ourselves
(`raw` command output, or future normalization), we must not trust the
top-level `proTeamId` for historical/per-week attribution.**

**`Matchup`**: `matchup_type`, `is_playoff`, `home_team`/`away_team` (`Team`,
resolved post-hoc), `home_score`/`away_score`.

**`BoxScore`**: `matchup_type`, `is_playoff`, `home_team`/`away_team` (raw
team *id* until resolved), `home_score`/`away_score`,
`home_projected`/`away_projected`, `home_lineup`/`away_lineup`
(`List[BoxPlayer]`).

**`Settings`** (extends `BaseSettings`): league name, `reg_season_count`,
`matchup_periods` (dict: matchup period id → list of scoring period ids —
**this is the literal `scoringPeriodId` vs `matchupPeriodId` mapping table**,
see §6), `veto_votes_required`, `team_count`, `playoff_team_count`,
`keeper_count`, `trade_deadline`, `division_map`, `tie_rule`,
`playoff_tie_rule`, `playoff_matchup_period_length`, `playoff_seed_tie_rule`,
`scoring_type`, `median_scoring` (bool), `faab` (bool),
`acquisition_budget`/`acquisition_limit`, `waiver_process_days`/`waiver_process_hour`,
`trade_revision_hours`, `scoring_format` (list of `{abbr, label, id, points}`
per scoring stat — football-specific), `position_slot_counts`.

**`Transaction`**: `team`, `type`, `status`, `scoring_period`, `date`
(falls back from `processDate` to `proposedDate`), `bid_amount`,
`items: List[TransactionItem {type, playerId, player}]`.

**`Activity`**: `date`, `actions: List[Tuple[Team, action_str, Player, bid_amount]]`
— trades emit two rows (`TRADE_SENT`/`TRADE_RECEIVED`), everything else one
row (`FA ADDED`, `WAIVER ADDED`, `DROPPED`).

**`Offer`** (waiver/FA auction bid): `id`, `dateTime`, `result` (human string:
`Processed`/`Outbid`/`Budget Exceeded`/`Position Limit Exceeded`/`Failed Due
to Roster Lock`/`Player already dropped`/`Canceled`/raw status), `amount`,
`teamId`, `player`, `droppedPlayer`.

**`BasePick`**: `team`, `playerId`, `playerName`, `round_num`, `round_pick`,
`bid_amount`, `keeper_status`, `nominatingTeam`.

### 3.2 What's directly served vs. what needs raw calls, per our v0.1 command list

| Command | Direct from `espn-api`? |
|---|---|
| `league info` | Yes — `league.settings`, `league.status`-derived attrs |
| `teams` | Yes — `league.teams` |
| `standings` | Yes — `league.standings()` / `standings_weekly(week)` |
| `roster --team` | Yes — `team.roster`; `load_roster_week(week)` for a past week |
| `matchups --week N` | Yes — `scoreboard(week)` for scores only, `box_scores(week)` for full lineup/projection detail |
| `transactions --limit N` | Partial — `transactions()` takes a `scoring_period`, not a rolling `--limit`; we'd page/filter client-side, or use `recent_activity(size)` for a human activity feed instead (different shape, see §3.1) |
| `free-agents --pos WR --limit N` | Yes — `free_agents(position=, size=)`, `position` values are the same string keys as `POSITION_MAP` (`'QB'`, `'RB'`, `'WR'`, `'TE'`, `'D/ST'`, `'K'`, `'FLEX'`, `'DT'`, `'DE'`, `'LB'`, `'DL'`, `'CB'`, `'S'`, `'DB'`, `'DP'`, `'HC'`) |
| `raw --view X` | By design, bypasses the object model — call `league.espn_request.league_get(params={'view': X})` directly |

### 3.3 Where it papers over ESPN quirks (valuable) vs. where it's thin (risky)

**Papers over, genuinely valuable, hard to redo cheaply:**
- Current/historical URL-shape fallback on 401, with restore-on-double-failure
  (§2.3) — this is fiddly and easy to get subtly wrong (the library itself
  shipped the "corrupts endpoint for all subsequent calls" bug for over a
  year before `78c239a` fixed it).
- Per-week `proTeamId` resolution for traded/bye-week players (§3.1,
  `BoxPlayer`).
- The full `PLAYER_STATS_MAP` (234 stat IDs → names) and
  `SETTINGS_SCORING_FORMAT_MAP` (equally large) — reverse-engineered over
  years of contributions; redoing this from scratch would be a multi-week
  side quest with no ESPN documentation to check against.
- Full standings tiebreaker hierarchy (`TOTAL_POINTS_SCORED`/`H2H_RECORD`/
  `INTRA_DIVISION_RECORD`) implemented recursively in `helper.py` — matches
  what ESPN's UI actually computes, including edge cases like bye-week
  self-matchups incorrectly inflating divisional records (fixed
  `78c239a`, 2026-02-15).
- 2018-format response unwrapping (list vs. object).

**Thin / risky — exactly where our `SCHEMA_DRIFT` detection needs to live,
because the library won't catch it for us:**
- **Zero defensive parsing in object constructors.** `Team.__init__` does
  `data['record']['overall']['wins']` — a straight `KeyError` if ESPN
  renames/removes/restructures `record`. Every model class (`Team`,
  `Player`, `BoxPlayer`, `Matchup`, `BoxScore`, `Settings`) is written the
  same way: happy-path dict access, not `.get()`-with-defaults for
  structurally load-bearing fields. Only a handful of *optional* fields use
  `.get()` (e.g. `data.get('logo')`, `data.get('name', 'Unknown')`).
- **No `SCHEMA_DRIFT`-equivalent exception class exists.** The only custom
  exceptions are `ESPNAccessDenied`, `ESPNInvalidLeague`, `ESPNUnknownError`
  — all about HTTP status, none about payload shape. A drift in field names
  surfaces as a raw `KeyError`/`TypeError`/`IndexError` from somewhere deep
  in a constructor call stack; the traceback tells you *where* it broke, not
  *that ESPN changed something* vs. *we called it wrong*.
- **No retry/backoff, no rate-limit handling at all** (§2.3, §5) — every
  non-200/401/404 status collapses into one generic `ESPNUnknownError`.
- **`recent_activity()` explicitly documented (in a code comment, not just
  behaviorally) as broken for anything before 2019**, and per issue #546
  (open since 2024-05-26, still open) **effectively broken/removed for
  *historical* seasons generally** — ESPN appears to have stopped serving
  multi-year activity history through this view. Do not build our
  `transactions` command's historical mode on `recent_activity()`.
- **No offset/pagination support for `free_agents()`** beyond a flat
  `limit`/`size` param passed straight through in the filter — if a league
  has more free agents than `size`, there's no documented `offset` handling
  in the library for this specific call (unlike `recent_activity`, which
  does take an `offset`). **[unverified]** whether ESPN's `kona_player_info`
  filter itself supports an offset key we could add ourselves via `raw`.

### 3.4 Known open issues worth flagging (from all 218 issues, all-time)

Filtered to issues that are either currently open or reveal a durable ESPN
quirk (not "I had two Python versions installed" noise):

- **#650 "League Not Found" (open since 2025-07-01)** — pre-2018 league
  history now requires cookies / may be effectively gone for
  unauthenticated requests; the maintainer and reporters converged on "ESPN
  tightened access, not deleted data," but it remains unresolved/unexplained.
  Directly relevant to our `--season` historical-query feature.
- **#658 "Dates not populating on Transactions that are not WAIVER type"
  (2025-09-05, fixed via unmerged-at-time-of-report PR, may or may not be in
  0.46.0)** — non-waiver transactions can have `None` `processDate`; the
  library falls back to `proposedDate` but that's also sometimes absent.
  Confirms `Transaction.date` can legitimately be `None` — our `transactions`
  command output schema must treat `date` as optional, not assume it.
- **#662 "New Position missing from Constant.py" (open, 2025-09-09, hockey)**
  — a numeric position ID (`10`) started appearing on player eligibility
  lists with no corresponding entry in the sport's `POSITION_MAP`.
  **This exact failure mode — an enum-like ID map silently going stale — is
  a first-class canary signature for us; see §4.5.**
- **#596 "Every Player's lineupSlotId shows as 0 or PG" (open, 2024-10-17,
  basketball)** — a `view` combination issue where a particular query
  pattern returns `lineupSlotId: 0` for everyone. Confirms the
  stmorse.github.io finding (§1, "requesting two views produces a different
  set of information than concatenating them independently") — **combining
  `view` params is not always equivalent to the union of calling them
  separately; ESPN's server-side view composition has non-obvious
  interactions.** Worth a cassette test per view-combination we actually use,
  not just per view.
- **#605 "box_scores with matchup_total=False does not work"** and **#617
  "players .stats only returning current week + past week + season total,
  not the rest of the weeks"** — both point at the same underlying limit:
  ESPN's stat payloads are scoped to a small window around "now," not a full
  season history, unless you specifically request historical scoring
  periods. Relevant if we ever build a "full season stats" report.
- **#547 "HTTP 403 for all leagues" (open since 2024-06-07)** — on
  investigation, every resolved case in the thread traced back to a stale
  `espn-api` version or a stale Python environment (old venv/Docker layer
  with a pinned old version), **not** a live ESPN-side 403 policy. Good
  confirmation that "upgrade and it goes away" really is the common root
  cause behind a chunk of these reports — exactly the failure mode our
  `health.json` + `doctor` design is built to short-circuit (§11 of the
  architecture doc).

---

## 4. Breakage history — timeline and canary signatures

### 4.1 Confirmed timeline of ESPN-side changes (dated to source/commit where possible)

| Date | Event | Source |
|---|---|---|
| ~2018 | ESPN did "a pretty major overhaul" of the fantasy API/site; pre-2018 data uses a structurally different endpoint shape and, per some fixture filenames, different JSON conventions | issue #650 comment (maintainer recollection); `year < 2018` branch in source |
| **2024-04-25** | Base URL migrated `fantasy.espn.com/apis/v3/games/` → `lm-api-reads.fantasy.espn.com/apis/v3/games/` | commit `0e39576`, confirmed exact |
| 2024-05/06 | Wave of "HTTP 403 for all leagues" reports (#547) — resolved case-by-case as stale client versions/environments, not a real ESPN-side block, but the timing right after the base-URL move likely amplified confusion | issue #547 |
| 2024-05-26 | `recent_activity()` stops returning historical-season data; still broken as of the most recent comment (2025-11-14) — works for current-ish years, fails for older ones | issue #546, open |
| 2024-11-28 | Library adds `check_league_endpoint`/`checkRequestStatus` 401-fallback logic (try the other URL shape before giving up) — a direct maintainer response to the ambiguity a plain 401 carries | commit `59f57ef` |
| 2025-02-02 | Separate `NEWS_BASE_ENDPOINT` (`site.api.espn.com`) added for player news, distinct host from the fantasy-platform API | commit `f0637bd` |
| **2025-08 (ongoing)** | Community reports pre-2018 league history newly requires `espn_s2`/`SWID`, or is otherwise gated/reduced vs. earlier in 2026 — unresolved, read as an access-policy tightening | issue #650, active discussion through 2025-08-25 |
| 2025-09-05 | Non-waiver transaction dates found to be frequently absent (`processDate`/`proposedDate` both missing) | issue #658 |
| 2025-09-09 | New, unmapped position ID (`10`) appears on hockey player eligibility — enum drift | issue #662, open |
| 2026-02-15 | Batch bug-fix pass (`78c239a`) fixing: 401-fallback endpoint corruption, a secrets-in-error-message leak, several other constructor-level bugs — evidence the library itself still had latent correctness bugs from earlier "quick fixes" to real ESPN changes | commit `78c239a` |
| **2026-08-07 → 2026-08-18** | espn-api's own live daily CI canary (`Espn API Integration Test`, runs against real public league `id=1234`) went **red for 12 consecutive days** | GitHub Actions run history, confirmed via `gh api .../actions/runs` |
| 2026-08-18 | Canary fixed | commit `985e043` |

### 4.2 The 2026-08 canary failure — a live case study, and an important negative result

I pulled the actual CI logs for the failure window (run `32084798448`,
2026-08-18) and the fix commit. **This was not an ESPN API break at all.**
The failure was:

```
File ".../idna/intranges.py", line 11, in <module>
    def intranges_from_list(list_: list[int]) -> tuple[int, ...]:
TypeError: 'type' object is not subscriptable
```

`idna` shipped a release using PEP 585 generic-type syntax (`list[int]`)
that requires Python ≥3.9; the CI job pins Python 3.8, and `setup.py`'s
`install_requires` didn't pin `idna` tightly enough, so `pip`/`easy_install`
resolved a too-new `idna` and broke import at the dependency-resolution
level, before any HTTP request was ever made. Fixed by pinning
`idna>=3.12,<3.13` (and re-pinning `requests`/`urllib3`) in commit
`985e043` (2026-08-18).

**This is directly relevant to the design of our own canary (architecture
doc §11.1):** a live-league smoke test can go red for reasons that have
*nothing to do with ESPN* — a transitive dependency release, a CI runner
image change, a Python version EOL. If our canary auto-files an issue on any
red run without first isolating "did the *shape of ESPN's response* change"
from "did our own build break," we will file false-positive `SCHEMA_DRIFT`
issues and burn the credibility of the whole health-check system fast.
**Concretely: the canary job should pin its own dependencies as tightly as
the runtime does (same lockfile, not a loose `pip install`), and its
failure-classification logic should distinguish an import/environment
exception (`ImportError`, `TypeError` during module load, anything before
the first HTTP call) from an exception raised *after* a real response came
back from ESPN.** Only the latter is `SCHEMA_DRIFT` material.

### 4.3 What ESPN actually breaks, categorized

From the confirmed timeline plus the issue corpus, ESPN-side changes cluster
into three kinds, each with a different detection strategy:

1. **Infrastructure moves** (base URL/host changes). Rare (one confirmed
   instance in 6+ years: 2024-04-25), but total-outage-shaped when they
   happen — every call fails identically. **Canary signature: connection
   error / DNS failure / 404-on-everything from the old host,** not a
   payload-shape issue.
2. **Access-policy changes** (what requires auth, what's still served at
   all) — the pre-2018-history gating (§1.3) is the clearest example.
   **Canary signature: a call that used to return 200 now returns 401 or
   404 for the *same* league/year/cookie combination it worked for
   yesterday.** This needs the canary to track "did this specific
   league/year/view combo regress" over time, not just "is the API up."
3. **Payload/schema drift** (renamed/removed/added fields, new enum values,
   changed nesting). The rarest kind in the confirmed record (I found no
   dated instance of ESPN silently renaming a core field in this repo's
   history — the #662 new-position-ID case is the closest confirmed
   example, and it's additive, not a rename) but the kind our architecture
   doc's `SCHEMA_DRIFT` code is specifically built for, and the kind a
   library with zero defensive parsing (§3.3) is most exposed to.

### 4.4 Concrete assertions for the canary — what to check on every run

Given §4.3, the canary (docs/ARCHITECTURE.md §11.1) should assert, against
a real public league on every run:

- **Connectivity / infra:** the bootstrap call to
  `lm-api-reads.fantasy.espn.com` returns *some* 200 within a normal latency
  budget (see §5). A DNS failure or connection refusal on the current host
  is `PROVIDER_UNAVAILABLE`, not `SCHEMA_DRIFT`.
- **Access-policy regression:** re-run the exact same league/year/view
  combination that succeeded on the *previous* canary run. A flip from 200
  to 401/404 on a previously-working, unchanged call is the signature of an
  access-policy tightening (§1.3) — worth its own status distinct from
  generic schema drift, since the fix (tell users cookies are now required)
  is different from a code fix.
- **Required top-level keys present** on each view's response — at minimum:
  `status.currentMatchupPeriod`, `status.finalScoringPeriod`,
  `settings.scoringSettings`, `settings.rosterSettings.lineupSlotCounts`,
  `teams[].record.overall.{wins,losses,ties,pointsFor,pointsAgainst}`,
  `teams[].roster.entries[].playerPoolEntry.player.{id,fullName,defaultPositionId,eligibleSlots}`.
  These are the exact paths every constructor in §3.1 does unguarded direct
  access on — they are the fields whose absence produces a raw `KeyError`
  today.
- **Enum coverage check — this is the cheapest, highest-signal canary
  assertion available, and it's exactly what caught #662 (manually, by a
  user) rather than automatically.** For every `defaultPositionId` and
  `eligibleSlots` entry seen across the fetched player pool, assert it
  exists as a key in `POSITION_MAP`/`PRO_TEAM_MAP`. An unmapped ID appearing
  is a leading indicator of drift *before* anything crashes — it degrades
  silently into an empty string (`POSITION_MAP.get(x, '')`) rather than an
  exception, so it will never surface as a Python error; only an explicit
  assertion catches it.
- **Stat-ID coverage check**, same idea, against `PLAYER_STATS_MAP` (234
  entries) using `stats[].stats`/`appliedStats` keys from a real box score.
- **Round-trip a `raw --view X` call for every view in §1.4's table**, not
  just the ones the object model happens to touch on the bootstrap call —
  a view we only use for one command (e.g. `kona_player_info` for
  free-agents) can drift without ever showing up in a "does league load"
  smoke test.

### 4.5 `SCHEMA_DRIFT` — how to actually implement it, given `espn-api` gives us nothing

Because the library raises bare `KeyError`/`TypeError`/`IndexError` from
inside object constructors (§3.3) rather than a typed exception, our
`providers/espn.py` needs to be the layer that:

1. Wraps every `espn-api` call site in a `try/except (KeyError, TypeError,
   IndexError)` and re-raises as our own `SchemaDriftError`, capturing
   which view/field was involved from the exception's context (Python
   `KeyError` args give you the missing key name directly — log it).
2. Separately, wraps HTTP-status exceptions (`ESPNAccessDenied`,
   `ESPNInvalidLeague`, `ESPNUnknownError`) and maps them per §2.3's actual
   semantics — **not** a naive 1:1 status-code mapping, given the 401
   ambiguity.
3. Adds the 429 detection `espn-api` doesn't have (§2.3, §5) — this
   requires bypassing `league_get()`'s status handling or catching the
   generic `ESPNUnknownError("ESPN returned an HTTP 429")` message and
   pattern-matching the status code out of the string, since the library
   doesn't preserve the status code as a structured exception attribute.
   **[implementation note]:** the cleanest fix might be a small, focused
   monkeypatch or a fork-and-vendor of just `checkRequestStatus` if this
   turns out to matter in practice — not in scope to decide here, but flag
   it as a real gap between what §5 of the architecture doc wants
   (`RATE_LIMITED` with `retry_after`) and what the library currently
   exposes (no retry-after header is even read).

---

## 5. Rate limits and performance

**No official published limits — confirmed absence, not absence of
searching.** Neither ESPN nor `espn-api`'s own docs/issues state a concrete
requests-per-minute ceiling. What the evidence shows:

- **`espn-api` implements zero client-side rate limiting or backoff.** Every
  call is a bare `requests.get()`; nothing sleeps, retries, or checks a
  `Retry-After` header anywhere in the source.
- **429 is not specially handled** — falls into the generic
  `ESPNUnknownError` branch (§2.3). We have no confirmed example of an
  actual 429 response body from ESPN's fantasy API in this research; the
  community consensus (ffscrapr docs, various blog posts) is "be
  respectful, cache aggressively, don't hammer it," which is advisory, not
  measured.
- **[inferred, not measured]** Given ESPN serves this API to power its own
  fantasy website at very high real-user volume, individual-script request
  rates in the range our `--no-cache` escape hatch would produce (a handful
  of calls per command invocation, likely single-digit calls/second even
  under heavy interactive use) are almost certainly well under whatever
  threshold exists. The `docs/ARCHITECTURE.md` §8 caching design (5–15 min
  TTLs, SQLite-backed) is the right mitigation regardless of the exact
  threshold — it's motivated by latency and "an agent exploring one question
  may make eight calls," not by a known rate-limit number, and that
  motivation holds up.
- **Latency: no benchmarked numbers available from this research.** The
  architecture doc's "ESPN is multi-second" claim is consistent with
  `espn-api`'s own design choices (the bootstrap call alone fetches 5 views
  in one request; `box_scores()` makes 3 sequential calls — matchup data,
  pro schedule, positional ratings) but I did not measure wall-clock timing
  myself (no live credentialed league available in this research context).
  **Recommend the canary (§4) record and publish per-view latency on every
  run** — that gives us real, current numbers for free, rather than a
  point-in-time benchmark that goes stale.
- **Which calls are expensive, structurally:** `box_scores()` is the most
  expensive command-equivalent — it chains a filtered `mMatchupScore`+`mScoreboard`
  call with a full `proTeamSchedules_wl` fetch and a `mPositionalRatings`
  fetch, every time, with no caching between them inside the library itself.
  `free_agents()` similarly always fetches pro-schedule and positional
  ratings alongside the player search. **Our own cache layer (§8 of the
  architecture doc) needs to sit below these composite calls, not just
  around `league.matchups`/`league.free_agents` as black boxes** — otherwise
  every cache miss re-triggers 3 ESPN calls instead of 1.

---

## 6. Season and scoring-period semantics

**Confirmed distinction (community-documented, consistent with
`Settings.matchup_periods` in source):**

- **`scoringPeriodId`** = one real-world NFL week of stats. Preseason is
  `0`; ranges up through `18` for a normal season's `finalScoringPeriod`
  (per fixture data: `firstScoringPeriod: 1, finalScoringPeriod: 16,
  latestScoringPeriod: 18` on a 2018-season league object — note
  `finalScoringPeriod` (last *matchup* week, 16 in that league's regular
  season config) and `latestScoringPeriod` (last real-world NFL week, 18)
  are **not the same field and are not the same number** — a third source
  of "which week number do I mean" confusion beyond the headline
  scoring/matchup distinction).
- **`matchupPeriodId`** = one entry in the schedule — normally 1:1 with a
  scoring period, but **a playoff round spanning two NFL weeks is one
  `matchupPeriodId` covering two `scoringPeriodId`s.** The authoritative
  mapping is `Settings.matchup_periods`, a dict literally shaped
  `{matchupPeriodId: [scoringPeriodId, ...]}` — e.g. from real fixture data:
  `{"1": [1], "2": [2], ..., "13": [13]}` for a non-playoff-spanning league,
  but leagues with 2-week playoff rounds produce entries like `{"14": [14, 15]}`.
- **When both are relevant and you must pick one:** per community
  documentation (not independently verified against ESPN's own words, since
  ESPN publishes none of this), scoring period takes precedence when a
  request supplies both — i.e. `scoringPeriodId` selects the actual stat
  data returned, `matchupPeriodId`/`filterMatchupPeriodIds` selects which
  matchup(s) the schedule filter matches. This matches how `box_scores()`
  uses both together: `scoringPeriodId` picks the week's stats,
  `x-fantasy-filter: {"schedule":{"filterMatchupPeriodIds":{"value":[matchup_period]}}}`
  picks which matchup entry to return.
- **Byes:** modeled as `opponent_id == team_id` (a team's own ID as its
  "opponent") in the raw schedule data — `Team._fetch_schedule` explicitly
  checks `if opponent_id == -1: opponent_id = self.team_id` to represent a
  bye. Any code iterating `team.schedule` needs to treat "opponent is
  myself" as "this was a bye week," not skip/crash on it. (This exact
  pattern — bye-as-self-matchup — was also the root cause of a real bug,
  `helper.py`'s divisional-record calculation inflating records by counting
  bye weeks as division wins, fixed in `78c239a`.)
- **Playoff weeks:** `matchup_type`/`playoffTierType` on `Matchup`/`BoxScore`
  (`'NONE'` for regular season, non-`'NONE'` values for playoff tiers) is
  the field that flags playoff status; `is_playoff` is a derived bool.
  `Settings.playoff_matchup_period_length` tells you how many scoring
  periods a single playoff matchup period spans (1 for single-week
  playoffs, 2 for two-week rounds).

**Practical implication for our CLI:** `fantasy-sports matchups --week N`
needs to decide up front whether `--week` means scoring period or matchup
period — they diverge exactly during whatever weeks are configured as
multi-week playoff rounds, which is league-configurable
(`playoff_matchup_period_length`), not a fixed calendar fact. Recommend
`--week` mean **scoring period** (the NFL-week, human-intuitive meaning) and
have the command internally resolve which `matchupPeriodId` that scoring
period belongs to via `settings.matchup_periods`, mirroring exactly what
`League.box_scores()` already does internally (`football/league.py`, the
`for matchup_id in self.settings.matchup_periods: if week in
self.settings.matchup_periods[matchup_id]` loop).

---

## 7. Practical gotchas — everything a first-time implementer gets wrong

### 7.1 Free-agent queries require the `x-fantasy-filter` header — confirmed exact shape

Not a query param. A JSON-encoded **header**, `x-fantasy-filter`. Omitting
it does not error — it silently returns ESPN's default player set (some
sorted/limited subset), which will look like "free agents" but isn't
filtered the way you asked. Confirmed exact filter shape from source
(`football/league.py::free_agents`):

```python
filters = {
    "players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "filterSlotIds": {"value": slot_filter},   # e.g. [4] for WR
        "limit": size,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
    }
}
headers = {"x-fantasy-filter": json.dumps(filters)}
```

The same header mechanism gates transactions (`filterType`), activity feed
(`topicsByType`/message-type IDs), message board (`topicsByType`), and the
player card (`filterIds`/`filterStatsForTopScoringPeriodIds`). **Any `raw`
command support for these views needs to let the user pass a custom
`x-fantasy-filter` header, not just a `view` name** — the view alone often
returns nothing useful without it.

### 7.2 "Pagination" is a flat limit, not real pagination

`free_agents(size=50)` → `filters.players.limit = size`. No confirmed
`offset` support for this specific filter in the library (contrast:
`recent_activity(offset=...)` does support offset via a different filter
key, `offset` at the top level of its `topics` filter). If a league has more
free agents at a position than the requested `size`, increasing `size` is
the only lever the library gives you — there is no "give me players 51–100."
**[unverified]** whether ESPN's `kona_player_info` filter itself accepts an
`offset` key we could add via `raw`/a custom header; not confirmed in
source or docs.

### 7.3 Player ID stability

**[inferred, not directly documented anywhere]** `playerId` appears
consistent across weeks/seasons within the object model — `player_map` is
built once per league-year at bootstrap and reused for all name lookups
across the season. I found no documented case of ESPN reusing a `playerId`
for a different real player, and the library's design (caching a permanent
`player_map`) implicitly assumes stability. No evidence either way on
cross-season stability of the same physical player's ID — plausible ESPN
keeps a stable ID per real player across their career, matching how most
sports platforms model it, but I could not confirm this from any of the
sources reviewed.

### 7.4 Position ID → name map (`POSITION_MAP`) is bidirectional and overloaded

`espn_api/football/constant.py`'s `POSITION_MAP` maps **both directions in
the same dict** — numeric ID → string name (`0: 'QB'`) *and* string name →
numeric ID (`'QB': 0`) as separate key/value pairs in the one dict. Code
that does `POSITION_MAP[x]` needs to already know whether `x` is an int or
a string; there's no type-based disambiguation, it works purely because int
and str keys never collide in a Python dict. **Do not iterate this dict
expecting only one direction** — `list(POSITION_MAP.values())[:n]`, used in
`Settings.__init__` to build `position_slot_counts`, relies on dict
insertion order putting all the int→str entries first, before the
str→int reverse-lookup entries. That's a real, if subtle, correctness
dependency on dict ordering + literal source ordering, not something a
schema-drift-safe implementation should replicate as-is if we ever build
our own copy of this map.

### 7.5 `proTeamId` → NFL team abbreviation map (`PRO_TEAM_MAP`)

Confirmed 32-plus-`0`("None") entries, football module. IDs are **not**
contiguous/alphabetical — e.g. `13: 'LV'`, `33: 'BAL'`, `34: 'HOU'` (the
higher numbers reflect teams ESPN added IDs for later — Baltimore and
Houston expansion-era additions post-date the original ID assignment).
**A new ID appearing here (expansion team, relocation) is a legitimate
canary signal** — confirmed as a live category of drift by a real commit in
this very repo's recent history: `679f7a0` (2026-08-18, "Merge pull request
#695 from kohzy/add-2026-wnba-expansion-teams") — WNBA expansion teams
needed a constant-map update. Same failure class as #662's hockey position
ID, different sport.

### 7.6 Timezone handling on transactions/dates

All ESPN timestamps in these payloads are **Unix epoch milliseconds**
(confirmed throughout: `Player.schedule[key]['date']` via
`datetime.fromtimestamp(game['date']/1000.0)`; `Offer.dateTime` via
`datetime.fromtimestamp(int(data['processDate'] / 1000))`;
`status.activatedDate` in raw fixture JSON is a 13-digit ms timestamp).
`datetime.fromtimestamp()` with no `tz=` argument produces a **naive
datetime in the local system timezone** of whatever machine runs the code —
not UTC, not ESPN's timezone. **This is a real, live gotcha for any
cron/server deployment**: the exact same ESPN response produces different
`datetime` values depending on the host's configured timezone, since the
library never passes `tz=timezone.utc`. Our own `output/` layer, which the
architecture doc says renders `generated_at` in `Z`-suffixed UTC ISO-8601,
needs to independently convert any timestamp we pull through `espn-api`'s
object model back through `.timestamp()` (or bypass the library's naive
`datetime` and re-derive from the raw epoch-ms field ourselves) rather than
trusting the library's already-localized `datetime` objects.

### 7.7 Two views combined ≠ two views called separately

Confirmed independently by two sources: a stmorse.github.io blog post
("requesting two views produces a different set of information than just
concatenating the two views independently") and issue #596 (a specific
`lineupSlotId` corruption tied to a particular view combination on
basketball). **Test cassettes should cover the exact view combinations the
library actually sends** (`['mMatchupScore', 'mScoreboard']` together, the
5-view bootstrap combo together) — not just each view name in isolation —
because ESPN's server-side response shape for a combined request is not
guaranteed to be the union of the individual responses.

### 7.8 Historical endpoint returns a JSON list, not an object

Confirmed in source: `league_get()` does
`return response[0] if isinstance(response, list) else response`. Anyone
hand-rolling a request against the `/leagueHistory/` shape and forgetting
this will get `list indices must be integers` or silently operate on the
wrong structure. Relevant directly to any `raw --view X --season <old year>`
usage — our `raw` command's output needs the same unwrap, or needs to
document clearly that it doesn't unwrap and returns the raw list.

---

## 8. Implications for our design

Numbered for direct action against `docs/ARCHITECTURE.md`:

1. **§5 error taxonomy — `AUTH_EXPIRED` cannot be a naive 401→code mapping.**
   A bare 401 from ESPN is ambiguous between "bad/expired cookies" and
   "wrong current-vs-historical URL shape for this season" (§2.3). Our
   `providers/espn.py` needs the same double-probe `espn-api` does
   internally before classifying a 401 as `AUTH_EXPIRED` — otherwise a
   season-boundary bug reports as "your cookies died" and sends the user on
   a wild goose chase re-extracting cookies that were never the problem.

2. **§5 — `SCHEMA_DRIFT` is entirely on us to build; `espn-api` gives zero
   help.** No typed exception exists for payload-shape problems; they
   surface as raw `KeyError`/`TypeError`/`IndexError` from inside object
   constructors that do unguarded direct dict access (§3.3, §4.5). Budget
   real implementation time for a wrapping layer that catches these,
   extracts the offending key/view from the exception, and re-raises as our
   structured error — this is not a trivial `except X: raise Y` shim, it
   needs enough context-capture to make the resulting GitHub issue (§11.2)
   actually actionable.

3. **§5 — `RATE_LIMITED` needs code we have to write from scratch.**
   `espn-api` doesn't special-case 429 at all (folds into generic
   `ESPNUnknownError`, no `Retry-After` reading). If we want the
   `retry_after` field the architecture doc's error envelope promises, we
   either need to intercept before the library's status-check runs, or
   parse the status code back out of the generic exception's message
   string. Flag as a concrete implementation task, not a given.

4. **§8 caching — cache below the composite calls, not around them.**
   `box_scores()` and `free_agents()` each fan out into 2–3 sequential ESPN
   calls internally (matchup/scoreboard + pro-schedule + positional-ratings)
   with no de-duplication between them. A cache wrapped only around
   `league.matchups`-as-a-whole still pays for 3 ESPN round-trips on every
   miss. Consider caching at the `espn_request.*_get()` layer (per URL+params
   fingerprint) in addition to/instead of at the command layer, so
   `free_agents()` and `matchups()` sharing the same `mPositionalRatings`
   fetch for the same week get a real cache hit on the shared sub-call.

5. **§11.1 canary — must isolate ESPN-side drift from our-own-build noise,
   with a live, dated example proving why.** `espn-api`'s own live CI
   canary went red for 12 straight days (2026-08-07→18) from a transitive
   dependency incompatibility (`idna` + Python 3.8), not an ESPN change
   (§4.2). Pin the canary's dependencies as tightly as the runtime, and
   classify failures by *where* the exception occurred (before vs. after a
   real HTTP response came back from ESPN) before deciding it's
   `SCHEMA_DRIFT`. A canary that cries wolf on its own build breakage will
   train everyone to ignore it.

6. **§11.1 canary — the exact assertions to run, concretely enumerable now**
   (§4.4): required-field presence at the specific paths every constructor
   dereferences unguarded; enum coverage of every observed
   `defaultPositionId`/`eligibleSlots`/`proTeamId`/stat-ID against our maps
   (this is the cheapest check and the one that would have caught #662
   automatically instead of waiting for a user to notice); a same-league/
   same-view re-run comparing against the *previous* canary run's result to
   catch access-policy regressions (§1.3/§4.3) specifically, since those
   manifest as a 200→401/404 flip on a previously-working call, not a
   payload shape change.

7. **§11 — a ready-made public canary league already exists and is proven
   stable over years of daily use: `league_id=1234, year=2018`.** It's the
   exact league `espn-api`'s own live integration test (`tests/football/integration/test_league.py::test_league_init`)
   has run against, unattended, daily, since at least the workflow's
   creation — meaning it's been publicly accessible and structurally stable
   for years, which is exactly the property our canary league needs. Reuse
   it (or the pattern of pinning to a long-lived, known-public league)
   rather than standing up a fresh one from scratch.

8. **§12 v0.1 scope — drop or explicitly unverify any assumption of an
   `mBoxscore` view.** It is not a real view name `espn-api` sends (§1.4).
   "Box scores" are `mMatchupScore` + `mScoreboard` combined with two
   additional side-calls, stitched client-side. Similarly, there is no
   dedicated `mPendingTransactions` view — pending waiver claims come
   through `mTransactions2` with status filtering, not a separate view.

9. **§7 (multi-league config) / §6 (auth) — the `auth login` guided
   extraction should explicitly validate and, if needed, repair the SWID
   value's curly braces before saving.** Confirmed as the single
   most-repeated manual-extraction mistake across every community source
   reviewed (§2.1) — cheap to guard against, meaningfully reduces the
   "works for me, fails for the user" support burden of a manual-cookie-only
   auth model.

10. **§8 / output layer — never trust `espn-api`'s own `datetime` objects
    for anything we render as UTC.** They're constructed via
    `datetime.fromtimestamp()` with no `tz=`, i.e. naive-and-host-local
    (§7.6). Our JSON envelope's `generated_at` and any transaction/matchup
    timestamp we surface need to re-derive from the raw epoch-ms value (or
    explicitly `.replace(tzinfo=...)` correctly) rather than passing the
    library's `datetime` straight through — otherwise our own "versioned,
    agent-native" output contract silently produces wrong timestamps
    depending on what timezone the CLI happens to run in.

11. **§9 (future writes) — no programmatic auth exists to build against, and
    ESPN closed the one path (username/password + reCAPTCHA-defeat) that
    used to exist (§2.2).** This is a confirmation, not new information, but
    worth stating plainly: v0.2+ mutation support will always require a
    human-extracted cookie, same as reads. There is no lower-friction path
    to design toward.

---

## Sources

- `cwendt94/espn-api` GitHub repository, cloned at `v0.46.0` / master as of
  2026-08-26 — full source read, git history via `git log`, GitHub Actions
  run history via `gh api repos/cwendt94/espn-api/actions/runs`, all 218
  issues (open+closed) via `gh issue list --state all`.
- Specific commits cited: `0e39576`, `59f57ef`, `689dd5c`, `0e5a423`,
  `2bb23c1`, `78c239a`, `985e043`, `679f7a0`, `f0637bd`, `bda5ad7`.
- Specific issues cited: #36, #99, #100, #148, #164, #218, #245, #272, #498,
  #546, #547, #549, #596, #605, #617, #650, #658, #662.
- [ESPN_2 and SWID Credentials · Discussion #150](https://github.com/cwendt94/espn-api/discussions/150)
- [ESPN: Private Leagues — ffscrapr](https://ffscrapr.ffverse.com/articles/espn_authentication.html)
- [Using ESPN's new Fantasy API (v3) — Steven Morse](https://stmorse.github.io/journal/espn-fantasy-v3.html)
- [pseudo-r/Public-ESPN-API](https://github.com/pseudo-r/Public-ESPN-API)
- `espn-api` GitHub Wiki (page structure only, via WebFetch)
