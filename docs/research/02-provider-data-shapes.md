# Research: Data Models of the Top Fantasy Sports Providers

**Question:** What do ESPN, Sleeper, Yahoo, NFL.com, and CBS actually model, in what
shape, and how much of it is truly shared? This informs `core/` (the normalized
80%), `providers/base.py` (the `Provider` Protocol), and the `raw` passthrough
design in `docs/ARCHITECTURE.md` §3.

**Scope note:** only ESPN ships in v0.1. This brief exists so the `core/` shape and
the `Provider` Protocol we lock in now don't have to be torn up when provider #2
(almost certainly Sleeper) arrives. Endpoint-level ESPN detail (breakage history,
full view catalog) belongs in `03-espn-api-surface.md`, not here — this brief stays
comparative.

**Method:** read the actual object-model source of `espn-api` (cwendt94, the
library named in ARCHITECTURE.md §2), `sleeper-api-wrapper` (SwapnikKatkoori), and
`yfpy` (uberfastman) directly from GitHub, cross-checked against `ffscrapr`
(ffverse — the one library that implements ESPN, Sleeper, MFL, *and* Fleaflicker
side by side, making its cross-platform column unification the best comparative
evidence available) and community write-ups for the two providers with no Python
library at all (NFL.com, CBS). Confidence is marked per claim: **[source]** = read
directly, **[inferred]** = derived from adjacent evidence, **[gap]** = could not
confirm, flagged rather than guessed.

---

## 1. Per-provider profiles

### 1.1 ESPN

- **Auth:** none for public leages; `espn_s2` + `SWID` cookies (manual DevTools
  extraction) for private leagues. No official auth flow — this is the whole reason
  ARCHITECTURE.md §6 calls ESPN cookie expiry "silent" and the #1 operational
  failure mode. **[source: espn-api base_league.py]**
- **Base URL:** `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<YEAR>/segments/0/leagues/<ID>`
  for current seasons; migrated from `fantasy.espn.com` in April 2024 — a real,
  undocumented breaking change with no changelog, deprecation notice, or advance
  warning. Historical seasons (2017 and earlier) use a different
  `leagueHistory` endpoint shape entirely. **[source: stmorse.github.io/journal/espn-fantasy-v3.html]**
- **Officially supported/documented:** no. Entirely reverse-engineered. This is
  the root cause of every dead project in ARCHITECTURE.md §0.
- **Rate limits:** undocumented; `espn-api` does not implement backoff/retry
  itself — one reason ARCHITECTURE.md's `RATE_LIMITED`/`PROVIDER_UNAVAILABLE`
  error codes exist at the CLI layer instead of relying on the library.
- **Sports covered:** NFL, NBA, MLB (via a *different* package,
  `espn_api.baseball`/`.basketball`/`.football`/`.hockey`/`.wbasketball`), each
  with structurally similar but not identical object models. **[source: espn_api/ dir listing]**
- **Auth model shape:** the "view" query parameter selects which JSON shard comes
  back (`mTeam`, `mRoster`, `mMatchup`, `mSettings`, `kona_player_info`,
  `mTransactions2`, `kona_league_communication`, etc.) — ESPN's API is one giant
  endpoint with a slice selector, not a REST resource tree. **[source: espn-api league.py — every method builds `params = {'view': ...}`]**

**Core objects it exposes** (from `espn_api/football/`): `League`, `Team`,
`Player`, `BoxPlayer` (Player + matchup-week context), `Matchup`, `BoxScore`,
`Transaction`/`TransactionItem`, `Activity`, `Settings`, `BasePick` (draft),
`Offer` (waiver/auction bid). **[source]**

**Roster** (`league.teams[i].roster` → `list[Player]`, or
`team._fetch_roster(week)` for a specific week):
```python
# espn_api/football/team.py + player.py
team.roster  # list[Player]
player.playerId  # int
player.name  # str
player.position  # str, e.g. "RB"
player.eligibleSlots  # list[str], e.g. ["RB","RB/WR","RB/WR/TE","OP","BE","IR"]
player.lineupSlot  # str, current slot e.g. "BE"
player.proTeam  # str, NFL team abbrev
player.injuryStatus  # str
player.total_points  # float, season total
player.projected_total_points
player.percent_owned  # float
player.stats  # dict[int scoring_period -> {points, breakdown, projected_points, ...}]
```
**[source: espn_api/football/player.py, team.py]**

**Matchup** (`league.scoreboard(week)` → `list[Matchup]`; `league.box_scores(week)`
for the richer version with per-player lineups):
```python
matchup.home_team  # Team instance (post-resolution)
matchup.home_score  # float
matchup.away_team
matchup.away_score
matchup.matchup_type  # "NONE" | playoff tier string
matchup.is_playoff  # bool
# BoxScore adds:
box.home_lineup  # list[BoxPlayer] — full lineup incl. bench/IR
box.home_projected  # float
```
**[source: espn_api/football/matchup.py, box_score.py]**

**Transactions** (`league.transactions(scoring_period, types={...})`):
```python
txn.team  # Team
txn.type  # str, one of TRANSACTION_TYPES (see §2 below)
txn.status  # str
txn.scoring_period  # int
txn.bid_amount  # int | None (FAAB)
txn.items  # list[TransactionItem]  # {type, playerId, player}
```
There is a *second*, overlapping surface — `league.recent_activity()` — which
returns `Activity` objects built from a different ESPN endpoint
(`kona_league_communication`) with a different vocabulary
(`'FA ADDED'`, `'WAIVER ADDED'`, `'DROPPED'`, `'TRADED'`) than `Transaction.type`'s
`TRANSACTION_TYPES` set (`DRAFT`, `WAIVER`, `FREEAGENT`, `TRADE_ACCEPT`, ...).
**These are not the same vocabulary and do not 1:1 map.** `Activity` also
synthesizes two rows per trade (`TRADE_SENT` from the source team + `TRADE_RECEIVED`
to the destination team) that `Transaction` does not. **[source: espn_api/football/transaction.py, activity.py, constant.py — verified by reading both class bodies directly]**

### 1.2 Sleeper

- **Auth:** none for reads. Genuinely public — any league ID or username works
  without a token. **[source: docs.sleeper.com; sleeper_wrapper/base_api.py has zero auth handling]**
- **Base URL:** `https://api.sleeper.app/v1/`. **[source]**
- **Officially supported/documented:** yes — `docs.sleeper.com` is Sleeper's own
  documentation, actively maintained. This is the single largest structural
  difference from ESPN/NFL/CBS: Sleeper is the only provider in this brief with a
  first-party, intentional, versioned-in-spirit public API.
- **Rate limits:** documented — stay under 1000 req/min. **[source: docs.sleeper.com]**
- **Sports covered:** NFL only (the `<sport>` path segment exists but NFL is
  effectively the only populated value in practice).
- **Important:** `sleeper-api-wrapper` (the library named in ARCHITECTURE.md §2)
  is *not* a data-modeling library the way `espn-api` and `yfpy` are — it is a
  ~270-line thin HTTP GET wrapper that returns **raw dicts**, not typed objects.
  Every method is `return requests.get(url).json()`. **[source: read all 7 files in sleeper_wrapper/]**
  This matters for the Protocol design in §5: for Sleeper, "the provider's object
  model" *is* the raw JSON schema published at docs.sleeper.com, not a Python
  library's abstraction over it. Whatever `providers/sleeper.py` builds, it is
  building the modeling layer that Sleeper (unlike ESPN) never asked a community
  library to build for it.

**Core objects** (all plain JSON, per `docs.sleeper.com`, verified against
`sleeper_wrapper`'s endpoint list):

**League:**
```json
{
  "league_id": "...", "name": "...", "season": "2026", "status": "in_season",
  "sport": "nfl", "total_rosters": 12,
  "settings": { "...": "flat key-value league config" },
  "scoring_settings": { "pass_td": 4, "pass_yd": 0.04, "rec": 0.5, "rec_td": 6, "fum_lost": -2, "...": "..." },
  "roster_positions": ["QB","RB","RB","WR","WR","TE","FLEX","FLEX","BN","BN","BN","BN","BN","DEF","K"],
  "previous_league_id": "...",
  "draft_id": "..."
}
```
`scoring_settings` is a **flat dict of stat-abbreviation → point value**, not a
nested per-position/per-stat-type tree like ESPN. `roster_positions` is a **flat
array with repeated entries** (one array element per roster slot, `"BN"` for each
bench slot) rather than a count-per-position map — a `SUPER_FLEX` league literally
has the string `"SUPER_FLEX"` in the array; a dynasty league adds `"TAXI"` entries.
**[source: docs.sleeper.com league object shape; `roster_positions` array-of-slots
convention and TAXI-slot behavior corroborated by support.sleeper.com dynasty docs — labeled [inferred/high-confidence], not read from a raw payload]**

**Roster** (`GET /league/{id}/rosters`):
```json
{
  "roster_id": 1, "owner_id": "user_id_string",
  "players": ["1234", "5678", "..."],
  "starters": ["1234", "0", "..."],
  "reserve": ["9999"],
  "taxi": ["..."],
  "settings": { "wins": 7, "losses": 5, "ties": 0, "fpts": 1234.5, "fpts_against": 1100.2, "waiver_budget_used": 40 }
}
```
Players are referenced by **Sleeper player ID strings only** — a roster has no
player names, positions, or teams embedded. You must join against the separate
`GET /players/nfl` dump (a ~5MB, all-players, all-fields JSON blob Sleeper
explicitly says to cache and call **at most once a day**) to get anything human
readable. **[source: docs.sleeper.com; sleeper_wrapper/players.py]**

**Matchup** (`GET /league/{id}/matchups/{week}`):
```json
{ "matchup_id": 3, "roster_id": 1, "starters": [...], "players": [...], "points": 118.4, "custom_points": null }
```
Two roster objects share the same `matchup_id` — you pair them yourself. No
`home`/`away` distinction exists; Sleeper matchups are symmetric. **[source]**

**Transactions** (`GET /league/{id}/transactions/{round}`, where `round` is a
week number for regular-season, but is **also overloaded to mean "leg" for other
contexts** — a real vocabulary trap for the Protocol in §5):
```json
{
  "type": "waiver", "status": "complete",
  "roster_ids": [1],
  "adds": { "player_id": roster_id }, "drops": { "player_id": roster_id },
  "draft_picks": [...],
  "waiver_budget": [{"sender": 1, "receiver": 2, "amount": 15}],
  "creator": "user_id", "created": 1234567890
}
```
`adds`/`drops` are dicts (player_id → roster_id), not lists. Trades and waiver
claims share the same `Transaction` shape distinguished only by `type`. **[source: docs.sleeper.com]**

**Playoffs:** `winners_bracket`/`losers_bracket` return a flat array of match
nodes (`{"r": round, "m": match_id, "t1": roster_id, "t2": roster_id, "w": winner, "l": loser, "t1_from": {...}, "t2_from": {...}}`)
that encode bracket *progression* (which match a team advances from) rather than
a nested tree — you reconstruct the bracket shape yourself. **No other provider
in this brief models a losers bracket at all**, which is a genuinely Sleeper-native
concept (dynasty/redraft leagues with consolation stakes). **[source]**

### 1.3 Yahoo

- **Auth:** OAuth2, Yahoo Developer Network app registration required. Real
  refresh-token lifecycle, unlike ESPN's silent-cookie-death or Sleeper's no-auth.
  **[source: ARCHITECTURE.md §6, corroborated by yfpy's existence as an OAuth-handling wrapper]**
- **Base URL:** Yahoo Fantasy Sports REST API (`fantasysports.yahooapis.com`),
  official and documented by Yahoo, though the docs are dated and incomplete —
  `yfpy`'s own docstrings repeatedly say `ATTRIBUTE MEANING UNKNOWN` for fields
  Yahoo's docs never explained. **[source: yfpy/models.py — e.g. `Team.can_edit_current_week`, `Team.done_week`, `League.is_highscore`]**
- **Officially supported:** yes, nominally — a real Yahoo product with an app
  registration flow — but under-documented enough that `yfpy` (17 major versions,
  actively maintained) is still reverse-engineering field meanings from live
  responses years in.
- **Sports covered:** NFL, NHL, NBA, MLB via the same `game_code`-parameterized API.

**Core objects** (`yfpy/models.py`, 44 model classes — by far the richest object
graph of the three libraries read): `League`, `Team`, `Roster`, `Player`,
`Matchup`, `Transaction`, `TransactionData`, `Pick`, `DraftResult`, `Standings`,
`TeamStandings`, `Settings`, `RosterPosition`, `StatCategories`, `StatModifiers`,
`SelectedPosition`, `Manager`, `Division`. **[source]**

**Roster** (`Team.roster.players` → `list[Player]`):
```python
player.player_key  # str, Yahoo's global player identifier
player.player_id  # int
player.full_name  # str
player.editorial_team_abbr  # str, NFL team
player.display_position  # str
player.eligible_positions  # list[str]
player.selected_position_value  # str — the CURRENT lineup slot for this roster context
player.selected_position.is_flex  # bool — Yahoo explicitly flags flex-slot occupancy
player.status  # str, "IR"/"Q"/"O"/etc
player.percent_owned_value  # float
player.player_points_value  # float
```
**[source: yfpy/models.py Player, SelectedPosition classes]**

**Team / standings** — Yahoo's `Team` object is the single richest object of the
three providers: it embeds roster, points, projected points, standings, matchups,
manager(s), and draft results all as nested sub-objects on one class, with derived
convenience fields flattened up (`team.wins`, `team.points_for` are pulled from
`team.team_standings.outcome_totals.wins` etc. at construction time). **[source:
yfpy/models.py Team.__init__ — literally reads `self.wins = self._get_nested_value(self.team_standings, ["outcome_totals", "wins"], 0, int)`]**

**Matchup:**
```python
matchup.teams  # list[Team] (both teams, embedded — no home/away split, like Sleeper)
matchup.is_playoffs  # bool
matchup.is_consolation  # bool
matchup.winner_team_key  # str
matchup.matchup_grades  # list[MatchupGrade] — Yahoo assigns a LETTER GRADE (A+ to F-) to a team's roster-management performance for the week. No other provider has this concept.
```
**[source: yfpy/models.py Matchup, MatchupGrade]**

**Transactions:**
```python
txn.type  # "add" | "drop" | "trade" | ...
txn.status  # "successful" | ...
txn.players  # list[Player]
txn.picks  # list[Pick] — draft-pick-as-trade-asset, modeled distinctly from Player
txn.faab_bid  # int | None
txn.trader_team_key / tradee_team_key  # for trades specifically
```
Draft picks as tradeable assets get their own `Pick` model
(`original_team_key`/`source_team_key`/`destination_team_key`, tracking a pick's
provenance through re-trades) — structurally close to what dynasty leagues need,
and notably absent from ESPN's transaction model entirely. **[source: yfpy/models.py Transaction, Pick]**

### 1.4 NFL.com Fantasy

- **Auth:** requires a logged-in NFL.com session; no public API key flow.
- **Base URL / official docs:** `apidocs.fantasy.nfl.com` — **the domain does not
  currently resolve** (verified 2026-08-26, DNS failure on fetch). Whatever
  official documentation once existed there is effectively gone. **[source: direct verification, this research pass]**
- **Officially supported/documented:** no, in practice. Community tooling
  (`nfl_fantasy_scraper`, `DeadlyChambers/fantasy-scraper` — the latter's own
  README says it exists *because the team is migrating away from NFL.com to
  Sleeper*) scrapes authenticated HTML/JSON rather than calling a stable API.
  **[source: GitHub search, this research pass]**
- **Rate limits, core objects, roster/matchup/transaction shape:** **[gap]** — no
  reliable public documentation or maintained Python library exists to read from.
  `ffscrapr` (the most comparative-minded tool in this space, covering ESPN,
  Sleeper, MFL, and Fleaflicker) **does not implement NFL.com at all**, which is
  itself evidence: the ffverse team, who has clearly done the reconnaissance on
  every other provider in this brief, chose not to build against it.

**Implication for the roadmap:** NFL.com is not "provider #2, deferred" the way
Sleeper is — it is closer to "not viable to build against without a dedicated,
much larger reverse-engineering effort than ESPN's, against a provider with an
actively shrinking community-tooling ecosystem." If NFL.com is ever wanted, budget
for it as its own research spike, not as a Protocol-conformance exercise.

### 1.5 CBS Sports Fantasy

- **Auth:** an API key/token model existed, gated behind "must belong to at least
  one CBS fantasy league" — i.e., it was never meant for general third-party
  developers, only for the fantasy-sports-app ecosystem CBS was courting at the
  time. **[source: geoffharcourt/cbs_fantasy_sports_api_token_fetcher README, search results this pass]**
- **Officially supported/documented:** **deprecated**. Described directly as "the
  API is deprecated but still available" by the ffcbs R-package documentation
  team, who scrape it anyway. **[source: rdrr.io/github/dfs-with-r/ffcbs]**
- **Rate limits, core objects, roster/matchup/transaction shape:** **[gap]** — the
  only tooling found (`ffcbs`, an R scraper, and a Python `cbs-sports-api`
  package on PyPI of unclear maintenance) documents *that* CBS has league/roster/
  transaction data, not the field-level shape of it. No Python library comparable
  to `espn-api`/`yfpy` exists.

**Implication:** same as NFL.com — CBS is not "the next Sleeper." A deprecated,
gatekept, undocumented API is a worse starting point than ESPN's undocumented-but-
actively-reverse-engineered one. Both NFL.com and CBS should be read as "confirmed
present in the market, confirmed not worth building against soon" rather than
silently dropped from the roadmap.

### 1.6 Briefly: Fleaflicker and MyFantasyLeague (MFL)

Not in the original five, but both changed my recommendation in §5-6, so both get
a real entry.

**Fleaflicker** — genuinely public, JSON **and** protobuf, no auth for public
league reads, real published docs at `fleaflicker.com/api-docs`. Endpoints are
verb-named RPC calls rather than REST resources (`FetchLeagueRosters`,
`FetchLeagueStandings`, `FetchLeagueScoreboard`, `FetchLeagueTransactions`,
`FetchLeagueDraftBoard`, `FetchPlayerListing`), each taking `sport`/`league_id`/
`season`/`scoring_period` params. **[source: fleaflicker.com/api-docs/index.html]**
Notably, `FetchLeagueRosters` and `FetchLeagueDraftBoard` both accept an
`external_id_type[]` parameter — Fleaflicker will hand back **Sportradar IDs**
alongside its own, natively supporting cross-provider player-identity resolution
in a way none of ESPN/Sleeper/Yahoo do. This is a real, replicable idea for
`fantasy-sports` even before a second provider ships.

**MyFantasyLeague (MFL)** — the deepest, most explicit API of any provider
researched, cookie-auth (`login` endpoint, username/password → session cookie),
XML by default with a `JSON=1` opt-in on select endpoints, verb-and-noun query
style (`export?TYPE=league|rosters|transactions|leagueStandings|draftResults|
selectedKeepers|...`). **[source: myfantasyleague.com api_info pages]** Two things
stand out for the "what does not normalize" section:
- **`selectedKeepers` is a first-class export type**, and `IS_KEEPER`/rookie-only
  filtering (`N`/`K`/`R`) is a first-class query parameter on ADP/AAV data. MFL is
  the only provider of the five-plus-two researched here that treats
  keeper/dynasty status as a modeled dimension rather than a league-culture
  convention layered on top of a redraft data model.
- **Franchise IDs are 4-digit strings** (`"0001"`), with `"0000"` reserved for
  commissioner/system actions — a real, distinct identity scheme from every other
  provider's integer team IDs.

---

## 2. THE COMPARISON MATRIX

Columns: **ESPN** (espn-api), **Sleeper** (raw JSON per docs.sleeper.com — the
wrapper library does no modeling), **Yahoo** (yfpy), **NFL.com**, **CBS**, with
**Fleaflicker (FF)** and **MFL** as bonus columns where they show something the
five don't.

| Concept | ESPN | Sleeper | Yahoo | NFL.com | CBS | FF / MFL (notable) |
|---|---|---|---|---|---|---|
| League | `League` object, `leagueId` int | `league_id` str, in URL path | `League.league_key` str (`"portal_key.l.id"`) | `[gap]` | `[gap]` | — |
| Season/year | `year` param, int | `season` field, **string** `"2026"` | `season` int; `game_code` disambiguates sport-season | `[gap]` | `[gap]` | — |
| Team | `Team`, `team_id` int | `roster_id` int (see below) | `Team`, `team_key` str | `[gap]` | `[gap]` | MFL: 4-digit string franchise ID |
| Manager/owner | `members` list on league, linked via `owners` on team | `owner_id` = Sleeper `user_id` on **roster**, not team — Sleeper has no "team" object, only "roster" | `Manager`/`Team.managers` (list — **co-managers are native**) | `[gap]` | `[gap]` | — |
| Player | `Player`, `playerId` int | `player_id` str, external dump only | `Player`, `player_key`/`player_id` | `[gap]` | `[gap]` | FF: exposes Sportradar ID alongside native ID |
| Roster | `team.roster` → `list[Player]` w/ embedded stats | `roster.players` → **list of ID strings only**, no embedded player data | `Team.roster.players` → `list[Player]` | `[gap]` | `[gap]` | — |
| Roster slot / lineup position | `eligibleSlots` (list) + `lineupSlot` (current), via `POSITION_MAP` int codes | slot is **positional** — `roster_positions` is a flat array, one slot per array element | `SelectedPosition` object w/ explicit `is_flex` boolean | `[gap]` | `[gap]` | — |
| Starter vs bench | `lineupSlot == 'BE'` (bench) / `'IR'` | player ID present in `starters` array vs `players`-minus-`starters` | `selected_position_value` string, incl. `"BN"` | `[gap]` | `[gap]` | — |
| Matchup | `Matchup`/`BoxScore`, explicit `home_team`/`away_team` | symmetric — two roster-level entries share a `matchup_id`, no home/away | `Matchup.teams` list, symmetric like Sleeper, but has `winner_team_key` | `[gap]` | `[gap]` | — |
| Week / scoring period | `scoringPeriodId` (int) ≠ `matchupPeriodId` (int) — **can diverge** (multi-week playoff matchups collapse several scoring periods into one matchup period) | `week` int, used directly for matchups/transactions | `week` int; separate `current_week` on league | `[gap]` | `[gap]` | — |
| Standings | `League.standings()` — client-side sort with tiebreaker hierarchy the library implements itself, not returned by ESPN | not modeled server-side at all — `sleeper_wrapper.get_standings()` computes it client-side too, from `roster.settings.wins/fpts` | `Standings.teams` — Yahoo **does** return a server-computed standings list | `[gap]` | `[gap]` | — |
| Record | `team.wins/losses/ties` + `streak_length`/`streak_type` | `roster.settings.wins/losses/ties` | `team_standings.outcome_totals.{wins,losses,ties,percentage}` | `[gap]` | `[gap]` | — |
| Points for/against | `points_for`, `points_against` (season) | `roster.settings.fpts`, `fpts_against` | `points_for`, `points_against` (derived from `team_standings`) | `[gap]` | `[gap]` | — |
| Transaction | `Transaction` (types: `WAIVER`,`FREEAGENT`,`TRADE_ACCEPT`,`DRAFT`,...) **and separately** `Activity` (types: `'FA ADDED'`,`'WAIVER ADDED'`,`'DROPPED'`,`'TRADED'`) — two non-identical vocabularies from two different endpoints | one shape (`type: waiver\|free_agent\|trade`) covers both adds/drops and trades | `Transaction.type` (`add`/`drop`/`trade`), `TransactionData` nests source/destination type per player | `[gap]` | `[gap]` | — |
| Waiver | `types={"WAIVER","WAIVER_ERROR"}` filter on `Transaction`; separate `Offer`/`offers_report()` for auction/FAAB bids w/ status codes (`Outbid`,`Budget Exceeded`,`Position Limit Exceeded`,...) | `type: "waiver"` txn + `waiver_budget` list on the txn itself | `uses_faab` league setting; `faab_bid` field on `Transaction` | `[gap]` | `[gap]` | — |
| FAAB | `Settings.faab` (bool) + `acquisition_budget` | league `settings` (flat dict, key convention undocumented in wrapper — **[gap]** on exact key) | `Settings.uses_faab` (int-bool) + `Team.faab_balance` | `[gap]` | `[gap]` | — |
| Trade | `Transaction.type == 'TRADE_ACCEPT'` or `Activity` `'TRADED'` (splits into `TRADE_SENT`/`TRADE_RECEIVED` rows) | `type: "trade"`, multi-roster `adds`/`drops` in one txn | `Transaction` w/ `trader_team_key`/`tradee_team_key`, and `picks` as tradeable Pick objects | `[gap]` | `[gap]` | — |
| Draft | `BasePick` — `round_num`, `round_pick`, `bid_amount`, `keeper_status` per pick | `GET /draft/{id}/picks` — `player_id`,`round`,`draft_slot`,`pick_no`,`metadata` | `DraftResult` — `pick`,`round`,`cost`,`player_key`,`team_key` | `[gap]` | `[gap]` | MFL: `auctionResults` distinct export from `draftResults` |
| Draft pick (as asset) | not modeled as a tradeable object — `keeper_status` bool is the closest concept | `traded_picks` endpoint, keyed by `(season, round, roster_id, previous_owner_id, owner_id)` | `Pick` object, explicit `original_team_key`/`source_team_key`/`destination_team_key` provenance chain | `[gap]` | `[gap]` | — |
| Keeper/dynasty | `Settings.keeper_count` (int); `BasePick.keeper_status` per pick | `previous_league_id` chains seasons; `taxi` roster array; league `settings` flags (dict, keys undocumented in wrapper) | `Player.is_keeper` (bool) — per-player, transactional, not a league-level structural concept in the model | `[gap]` | `[gap]` | **MFL: only provider with a dedicated `selectedKeepers` export type and `IS_KEEPER` query filter — the most explicit modeling of the five-plus-two** |
| Playoff bracket | `Settings.playoff_team_count`, `playoff_seed_tie_rule`; bracket itself reconstructed from `Matchup.matchup_type`/`is_playoff`, not returned as a tree | `winners_bracket`/`losers_bracket` — flat list of match nodes w/ `t1_from`/`t2_from` progression pointers; **losers bracket is Sleeper-only among all providers here** | not modeled as a distinct object — playoffs inferred from `Matchup.is_playoffs`/`is_consolation` flags | `[gap]` | `[gap]` | — |
| Division/conference | `Settings.division_map` (id→name dict) | not modeled — Sleeper has no native divisions concept | `Division` object + `Team.division_id` | `[gap]` | `[gap]` | — |
| Scoring settings | `Settings.scoring_format` — list of `{id, abbr, label, points}` per **stat**, plus `position_slot_counts` | `scoring_settings` — **flat dict**, stat-abbreviation string keys → point value | `StatCategories` + `StatModifiers` + `Settings.stat_categories/stat_modifiers` — split into groups (`Group`) and per-stat objects (`Stat`), structurally closer to ESPN than Sleeper | `[gap]` | `[gap]` | MFL: `rules` export type, separate from `league` |
| Free agent pool | `League.free_agents(week, position, size)` — server-side filter | not a distinct endpoint — computed client-side as "all players not on any roster" from the full player dump | not read directly in this pass — `[gap]`, but Yahoo's `League.players` list is the analogous surface |  `[gap]` | `[gap]` | — |

**Reading the matrix:** the row that should worry the implementer most is
**Transaction** — every provider has *two* half-overlapping ways to ask "what
happened," and ESPN's own library ships both without reconciling them. Anyone
building `fantasy-sports transactions` against ESPN alone, without knowing
`Activity` exists, will ship a command that silently misses however many trades
route only through the `kona_league_communication` endpoint.

---

## 3. What genuinely normalizes

These are the fields where every provider researched (ESPN/Sleeper/Yahoo; NFL/CBS
excluded — no confirmed shape) agree closely enough in *shape* (not necessarily
field name) to justify a shared `core/` model, per ARCHITECTURE.md §3's mandate.

**`core.League`**
```python
provider: Literal["espn", "sleeper", "yahoo", ...]
provider_id: str  # provider's native league ID, always string (Yahoo's own code
# explicitly converts to string "to handle leading zeros" — a
# real bug ESPN/Sleeper integer IDs don't have but Yahoo does)
name: str
season: int
sport: str
team_count: int
current_week: int
raw: dict  # untouched provider payload
```
All three name it, all three give it a numeric-ish season, all three give a team
count and a current week. Field types are consistent enough to force to one shape
with no semantic loss.

**`core.Team`**
```python
provider_id: str
name: str
owner_names: list[str]  # ESPN/Yahoo support co-managers; normalize to a list even
# for the 1-owner-1-team common case
wins: int
losses: int
ties: int
points_for: float
points_against: float
standing: int | None  # current rank; all three expose this in some form
raw: dict
```

**`core.RosterSlot`** (a player *in a specific roster context*, not a bare
player)
```python
player_provider_id: str
player_name: str
position: str  # provider's own position string, NOT normalized further —
# "RB/WR" (ESPN) vs "FLEX" (Sleeper) vs a flex boolean
# (Yahoo) genuinely don't agree; see §4
slot: str  # the lineup slot this player currently occupies
is_starter: bool  # every provider gives you *some* way to derive this —
# ESPN via lineupSlot != 'BE'/'IR', Sleeper via starters[]
# membership, Yahoo via selected_position_value != 'BN'
raw: dict
```

**`core.Matchup`**
```python
week: int
team_a_provider_id: str
team_a_score: float
team_b_provider_id: str
team_b_score: float
is_playoff: bool  # every provider has this concept even where the shape
# of "how you know" differs wildly
raw: dict
```
Note this drops ESPN's `home`/`away` distinction to the symmetric `team_a`/`team_b`
shape Sleeper and Yahoo already use natively — normalizing *to* home/away would be
inventing structure ESPN alone has and the other two would have to fake.

**`core.Transaction`**
```python
provider_id: str
type: Literal["add", "drop", "trade", "waiver_claim"]  # a narrow, lossy, but honest
# normalization — see §4 for why
# this is the riskiest normalized field in the whole model
team_provider_id: str | None
players_in: list[str]  # player provider_ids gained by team_provider_id
players_out: list[str]  # player provider_ids lost
faab_spent: int | None
timestamp: datetime | None
raw: dict
```

**`core.FreeAgent`** — same shape as `RosterSlot` minus `slot`/`is_starter`, plus
`percent_owned: float | None` (present on ESPN and Yahoo, absent/differently-shaped
on Sleeper where ownership isn't tracked per-player the same way).

This is deliberately thin. Every field above earns its place because all three
libraries expose *something* structurally equivalent for it, confirmed by reading
source, not inferred from a shared vocabulary that might paper over real
differences.

---

## 4. What does NOT normalize, and why

### Player identity

**There is no shared player ID across providers, confirmed.** ESPN uses an
integer `playerId` (~5 digits), Sleeper a string `player_id` (~4 digits), Yahoo a
`player_key` string embedding the game code (`"449.p.12345"` shape) plus a bare
integer `player_id`. None of the three libraries read here contain any
cross-reference to another provider's ID.

**The crosswalk that exists:** `nflreadr::load_ff_playerids()`
(nflverse/nflreadr, backed by DynastyProcess.com's community-maintained database)
publishes a single table — **12,470 players, 35 ID columns** as of the most recent
check (2026-08-05) — with `gsis_id`, `sleeper_id`, `espn_id`, `yahoo_id`, `mfl_id`
(the *primary key* of the table — MFL IDs are described as "unique and complete"),
`fleaflicker_id`, `cbs_id`, `nfl_id`, `pfr_id`, `sportradar_id`, `pff_id`,
`fantasypros_id`, and several fantasy-content-site IDs (KeepTradeCut, Rotowire,
Rotoworld). **[source: nflreadr.nflverse.com dictionary_ff_playerids]**

**How hard is cross-provider mapping, really?** Two honest findings:
1. It's a **solved problem for research/analysis use** — the crosswalk exists,
   is actively maintained, and is a simple `pip install nfl_data_py` /
   R-package-equivalent away. This is meaningfully easier than the architecture
   doc's "deferred to a future ADR" framing implies for the *data availability*
   half of the problem.
   2. It is **not solved for live, in-product identity resolution** — the crosswalk
   is a periodically-refreshed table with explicitly acknowledged gaps
   ("workflows can't be perfect... there will always be mismatches or missing
   IDs" — DynastyProcess's own framing), not a live join service. A rookie two
   days into the league, or a practice-squad/waiver-wire churn player, may not be
   in the table yet on the day someone needs `fantasy-sports` to resolve them.
   Fleaflicker is the interesting outlier here: it's the only provider that
   returns a **Sportradar ID inline** with roster data via `external_id_type[]`,
   meaning at least one provider is trying to solve this at the API layer instead
   of leaving it to a third-party crosswalk.

Recommendation for the deferred ADR: when cross-provider player identity is
needed, bundle `nflreadr`'s crosswalk (or a periodically-refreshed local copy of
the same DynastyProcess data) as a `players/crosswalk.py` lookup table, joined on
whichever ID a given provider already returns — not a live API call, and not a
custom-built mapping.

### Scoring settings

Structurally incompatible in a way that resists forcing into one shape without
loss, confirmed from actual source, not just from the architecture doc's prior:

- **ESPN**: a *flat list of per-stat rules*, each `{stat_id, abbr, label, points}`
  keyed by ESPN's own numeric stat-ID constants (`PLAYER_STATS_MAP` in
  `espn_api/football/constant.py` has 60+ entries covering everything from
  `passingAttempts` to kicker distance-bucket stats like `'FGAY50'`), plus a
  *separate* `position_slot_counts` dict for roster shape. Point overrides for
  specific leagues layer on top (`pointsOverrides.get('16')` — a magic key the
  library doesn't explain).
- **Sleeper**: a *flat dict*, `{stat_abbreviation: point_value}` — `pass_td: 4`,
  `rec: 0.5`. No stat-ID indirection; the string key *is* the identity.
- **Yahoo**: split across **two objects** — `StatCategories` (which stats exist,
  grouped via `Group`) and `StatModifiers` (point values per stat), each with
  their own nested `Stat`/`Bonus` sub-objects, plus per-position applicability via
  `StatPositionType`. Structurally closer to ESPN's ID-indirection approach than
  to Sleeper's flat dict, but organized as two parallel trees instead of one list.

None of the three even agree on whether a "stat" is identified by an integer ID,
a string abbreviation, or both. This confirms ARCHITECTURE.md §3's decision to
leave scoring settings entirely in `raw` — there is no honest normalized shape
here, only a translation table per provider that would need constant maintenance
as stat catalogs change.

### Roster slot eligibility ("can this player fill FLEX")

Three different mechanisms, not just three different vocabularies:

- **ESPN**: `eligibleSlots` is a **list of position codes** on the player itself
  (`["RB","RB/WR","RB/WR/TE","OP","BE","IR"]`) — eligibility is enumerated
  per-player, and a combined slot like `RB/WR/TE` is itself one of the codes in
  `POSITION_MAP`, not a computed union.
- **Sleeper**: eligibility is **not on the player at all in the roster context** —
  it's implicit in the league's flat `roster_positions` array (does the array
  contain `"FLEX"`?) combined with the player's `position` field from the separate
  players dump. The client has to compute "can fill FLEX" itself from
  provider-defined FLEX-eligible position sets, which Sleeper doesn't publish as
  data — it's convention (`RB`/`WR`/`TE` typically flex, but `SUPER_FLEX` also
  allows `QB`, and league customization can differ).
- **Yahoo**: `SelectedPosition.is_flex` is an **explicit boolean already computed
  by Yahoo** for the player's *current* selection — the only one of the three that
  tells you directly, rather than making you derive it, though only for where the
  player currently sits, not "is this player FLEX-eligible in general."

This is a real trap for an implementer who designs against ESPN first: ESPN's
"eligibility is data on the player" pattern will not transfer to Sleeper, where it
is data on the *league* crossed with a hardcoded convention the API never states.

### Transactions

The vocabularies do **not** cleanly map, confirmed by reading the actual
transaction-type enums:

- ESPN alone has **two non-identical transaction vocabularies** from two
  different endpoints (`Transaction.type` ∈ `TRANSACTION_TYPES` = `{DRAFT,
  TRADE_ACCEPT, WAIVER, TRADE_VETO, FUTURE_ROSTER, ROSTER, RETRO_ROSTER,
  TRADE_PROPOSAL, TRADE_UPHOLD, FREEAGENT, TRADE_DECLINE, WAIVER_ERROR,
  TRADE_ERROR}` vs. `Activity` messages mapped through `ACTIVITY_MAP` = `{'FA
  ADDED', 'WAIVER ADDED', 'DROPPED', 'TRADED'}`). Any `core.Transaction.type`
  normalization built against ESPN alone risks baking in *one* of these two
  vocabularies and silently missing whatever the other endpoint would have caught
  — this already happened once, inside ESPN's own ecosystem, before a second
  provider is even involved.
- Sleeper folds trades and add/drop into one `type` field
  (`waiver`/`free_agent`/`trade`) with structurally different payload shapes
  underneath (trades have multi-roster `adds`/`drops`; waiver claims have
  `waiver_budget`).
- Yahoo's `Transaction.type` (`add`/`drop`/`trade`) is closest to a clean
  three-way split, but trade-specific fields (`trader_team_key`/`tradee_team_key`,
  `picks`) only populate for trades — same "one object, conditionally-populated
  fields" pattern as Sleeper, different exact fields.

The `core.Transaction.type` normalization proposed in §3 (`add|drop|trade|
waiver_claim`) is achievable, but it is a genuine narrowing — ESPN's
`TRADE_VETO`/`TRADE_PROPOSAL`/`TRADE_DECLINE`/`WAIVER_ERROR` states have no
normalized-model equivalent and must live in `raw` only. Anyone filtering on
`core.Transaction.type == "trade"` needs to know that "proposed but declined"
trades are invisible to that filter unless they also read `raw.status`.

### Week/period semantics

Real, provider-specific traps, not just naming differences:

- **ESPN separates `scoringPeriodId` from `matchupPeriodId`, and they diverge.**
  A single matchup period can span multiple scoring periods (multi-week playoff
  matchups collapse 2+ weeks of scoring into one matchup) — `league.box_scores()`
  has to look up which `matchup_period` a given `scoringPeriodId` belongs to via
  `self.settings.matchup_periods[matchup_id]` before it can even form the right
  API call. "Week 1" is unambiguous in the regular season but this two-ID system
  exists specifically because ESPN's playoff weeks are *not* 1:1 with scoring
  weeks. **[source: espn_api/football/league.py `box_scores()` method — the
  matchup_period/scoring_period resolution loop]**
- **Sleeper's `week` is a single flat integer** used identically for matchups and
  transactions — no matchup-period/scoring-period split — but `Stats.get_week_stats`
  and the transactions endpoint both take a "round" parameter that is described
  ambiguously enough in community docs to be a real footgun (regular-season weeks
  vs. playoff "rounds" are not guaranteed to be the same numbering in every league
  configuration). Bye weeks are handled entirely client-side — nothing in the
  Sleeper roster/matchup shape marks "this player is on bye"; you infer it from
  the separate NFL schedule.
- **Yahoo's `Matchup.week`** is a plain int, but `League.current_week` and
  `League.matchup_week` are two separate fields the library exposes without
  documenting why — a real chance the two disagree mid-transition-week that
  `yfpy`'s own docstrings don't resolve. **[gap: yfpy comments don't explain the
  distinction; flagged rather than guessed]**

`core.Matchup.week` should be documented explicitly as "the provider's matchup
week, not necessarily the NFL week" — and ESPN's `raw` should always retain both
`scoringPeriodId` and `matchupPeriodId` since the normalized model can only carry
one.

### Dynasty/keeper concepts

Genuinely different depth of native modeling, confirmed:

- **ESPN**: shallow. `Settings.keeper_count` (an int) and a `keeper_status` flag
  per draft pick (`BasePick.keeper_status`) is the entire surface. No concept of
  a taxi squad, no season-to-season league linkage in the model itself (though
  ESPN leagues *do* roll over year to year in the product — the library just
  doesn't expose lineage as data).
- **Sleeper**: medium-deep. `previous_league_id` on the league object explicitly
  chains dynasty leagues season-to-season (so `fantasy-sports` could walk league
  history automatically); `taxi` is a first-class roster array parallel to
  `starters`/`reserve`; taxi-squad settings (slot count 0-10, experience
  eligibility, deadline) are configurable league settings, not conventions.
- **Yahoo**: shallow and transactional. `Player.is_keeper` is a per-player boolean
  with no league-level keeper-count/rules modeling visible in `yfpy`'s classes.
- **MFL**: deepest of everything researched. A dedicated `selectedKeepers` export
  type, an `IS_KEEPER` filter (`N`/`K`/`R` for redraft/keeper/rookie-only) on
  ADP/AAV market data, explicit dynasty-community framing in the product itself.

This confirms ARCHITECTURE.md §3's decision to keep dynasty/keeper out of `core/`
entirely — not because it's unimportant, but because "keeper" means three
different things at three different levels of the object model depending on
provider, and normalizing it would mean either flattening Sleeper's taxi-squad
richness down to ESPN's single boolean, or inventing structure ESPN and Yahoo
don't have.

---

## 5. Recommended `Provider` Protocol

```python
from __future__ import annotations
from datetime import datetime
from typing import Protocol, runtime_checkable

# core/ models referenced here are the ones defined in §3 above.
from fantasy_sports.core import (
    League,
    Team,
    RosterSlot,
    Matchup,
    Transaction,
    FreeAgent,
    CredentialSpec,
)


@runtime_checkable
class Provider(Protocol):
    """One method per normalized concept from §3. Every method returns
    normalized objects carrying `.raw`; nothing here returns bare provider JSON.
    Scoring settings, draft/keeper detail, and playoff bracket structure are
    deliberately NOT part of this Protocol — reach them via `fetch_raw()`.
    """

    name: str  # "espn" | "sleeper" | "yahoo" | ...

    # --- auth (ARCHITECTURE.md §6) ---
    def credential_specs(self) -> list[CredentialSpec]:
        """What this provider needs to authenticate, and how staleness is detected."""
        ...

    # --- core reads ---
    def fetch_league(self, league_id: str, season: int) -> League: ...

    def fetch_teams(self, league_id: str, season: int) -> list[Team]: ...

    def fetch_roster(
        self, league_id: str, season: int, team_id: str, week: int | None = None
    ) -> list[RosterSlot]:
        """week=None means the CURRENT roster; a specific week is a historical
        or in-progress lineup. Not every provider can serve arbitrary past weeks
        cheaply (ESPN: yes, via `mRoster` + `scoringPeriodId`. Sleeper: yes, but
        requires a client-side join against the players dump per week. Yahoo:
        yes, via `Roster.week` + `coverage_type`)."""
        ...

    def fetch_standings(self, league_id: str, season: int) -> list[Team]:
        """Returns Teams sorted by the provider's own rank/standing field.
        ESPN and Sleeper compute this CLIENT-SIDE in their libraries from
        wins/points — providers are not guaranteed to return a pre-sorted
        standings list; the Protocol implementation owns the sort, and the
        provider adapter must document its own tiebreaker source (Yahoo returns
        one server-side; ESPN/Sleeper require reimplementing the league's own
        tiebreaker rules, which is real, provider-specific logic — see
        espn-api's `standings_weekly()` for how deep this gets)."""
        ...

    def fetch_matchups(self, league_id: str, season: int, week: int) -> list[Matchup]:
        """`week` is the PROVIDER's matchup week — see §4 "Week/period semantics."
        For ESPN specifically, callers needing playoff-period matchups should
        expect `week` to NOT be 1:1 with `scoringPeriodId`; the adapter resolves
        this internally and records both in `.raw`."""
        ...

    def fetch_transactions(
        self, league_id: str, season: int, since: datetime | None = None
    ) -> list[Transaction]:
        """`type` on the returned Transaction is narrowed per §4 (add/drop/trade/
        waiver_claim only) — provider-specific states (TRADE_VETO, TRADE_PROPOSAL,
        WAIVER_ERROR, ...) are readable only via `.raw.status`/`.raw.type`.
        ESPN adapters MUST reconcile both `mTransactions2` and
        `kona_league_communication` (`Activity`) internally — do not let this
        method silently expose only one; see §2 "Transaction" row and §4."""
        ...

    def fetch_free_agents(
        self, league_id: str, season: int, week: int, position: str | None = None
    ) -> list[FreeAgent]: ...

    # --- explicit non-normalized escape hatch (ARCHITECTURE.md §3) ---
    def fetch_raw(self, league_id: str, season: int, **provider_params) -> dict:
        """Direct passthrough. `provider_params` are NOT normalized across
        providers — ESPN takes `view=...`, Sleeper takes an endpoint path
        suffix, Yahoo takes a resource/sub-resource pair. This is intentional:
        `raw` access should look like using the provider's own API, not like
        translating through a fake unified query language."""
        ...
```

**Per-method conformance, by provider:**

| Method | ESPN | Sleeper | Yahoo | Notes |
|---|---|---|---|---|
| `credential_specs` | cookie pair, no refresh, silent expiry | none required — returns `[]` | OAuth2 refresh token | trivially satisfiable by all three |
| `fetch_league` | ✅ direct | ✅ direct | ✅ direct | — |
| `fetch_teams` | ✅ direct | ⚠️ requires joining `rosters` + `users` (Sleeper has no single "team" endpoint — a "team" is `roster` ⋈ `user`) | ✅ direct | Sleeper adapter does real join work here that ESPN/Yahoo don't need |
| `fetch_roster` | ✅ direct, embedded player data | ⚠️ returns bare ID list; adapter MUST join against the cached daily players dump or every roster call returns unnamed players | ✅ direct | Sleeper's players-dump caching (≤1×/day per docs.sleeper.com) is a real implementation obligation, not optional — belongs in `cache/` per ARCHITECTURE.md §8, own TTL bucket |
| `fetch_standings` | ⚠️ client computes tiebreakers | ⚠️ client computes tiebreakers | ✅ server-provided | ESPN/Sleeper adapters both need real tiebreaker logic; do not assume "sort by wins" is correct — see espn-api's multi-rule `standings_weekly()` |
| `fetch_matchups` | ✅ but week≠scoringPeriodId trap | ✅ symmetric, no home/away | ✅ symmetric, no home/away | `core.Matchup`'s team_a/team_b (not home/away) shape from §3 exists because of this |
| `fetch_transactions` | ⚠️ MUST reconcile 2 endpoints | ✅ single endpoint, single vocabulary | ✅ single endpoint | see §4 |
| `fetch_free_agents` | ✅ server-side filter | ⚠️ client-computed set difference against full roster + player dump | `[gap]` — not read this pass, but `League.players` is the likely analog | — |
| `fetch_raw` | ✅ | ✅ | ✅ | trivial by construction |

No method in this Protocol requires contortion for any of the three providers
with real Python libraries — the ⚠️ rows are real *implementation* work
(joins, tiebreaker reimplementation, caching discipline) but not Protocol
*design* problems. That is the test this Protocol was built to pass.

---

## 6. Warnings for the ESPN-first implementer

These are the specific ways an ESPN-only v0.1 will produce an abstraction that
breaks, or silently underperforms, when Sleeper (or any second provider) arrives
— the single most valuable section of this brief per the assignment.

1. **`core.Team` must not assume one owner.** ESPN's `owners` is a list from day
   one (`Team.owners` in `espn_api/football/team.py`, populated from `data.get('owners', [])`
   filtered against league `members`) and Yahoo's `Team.managers` is explicitly
   plural with an `is_comanager` flag. If `core.Team.owner_name: str` ships as a
   single string because ESPN's common case is one owner, every co-managed league
   — which both ESPN and Yahoo support natively — silently loses a manager. Ship
   `owner_names: list[str]` from the start; §3 already reflects this.

2. **Do not build `fetch_transactions` against only `mTransactions2`.** ESPN
   itself has a second transaction surface (`Activity`/`kona_league_communication`)
   with a non-overlapping vocabulary. An ESPN-only implementer who never needed
   the `recent_activity()` method (because `transactions()` "worked") will not
   discover this gap until a second provider's *single* transaction endpoint
   makes them ask "wait, why did ESPN need two?" Handle the reconciliation now,
   inside the ESPN adapter, so `core.Transaction` never has this asymmetry baked
   into its assumptions.

3. **Do not model roster-slot eligibility as "data on the player."** ESPN's
   `eligibleSlots` list-on-player pattern is clean and will tempt a
   `Player.eligible_slots: list[str]` field on the normalized model. Sleeper has
   no equivalent — eligibility is computed from the league's slot configuration
   crossed with position, not returned as player data. If `core.RosterSlot`
   grows a `player.eligible_slots` field because ESPN made it easy, the Sleeper
   adapter will have to *synthesize* it via hardcoded FLEX/SUPER_FLEX position-set
   conventions Sleeper's API never states as data — a source of drift the moment
   a league customizes FLEX eligibility beyond the default RB/WR/TE. Prefer
   `slot: str` (current occupancy only, per §3) over modeling general eligibility
   in `core/` at all; eligibility questions belong in `raw` until a second
   provider proves what's actually shareable.

4. **`week` is not a portable integer.** ESPN's scoring-period/matchup-period
   split (§4) is invisible if you only ever call `box_scores()` without a `week`
   argument, because it defaults to `current_week` and the split only bites during
   playoff-format weeks. An ESPN-only implementer who tests in-season and never
   exercises the playoff path will ship a `fetch_matchups(week: int)` signature
   that looks portable and isn't — Sleeper's flat `week` and Yahoo's
   `current_week`/`matchup_week` split (unexplained even in `yfpy`) are different
   enough traps that "just pass an int" undersells the real complexity every
   provider hides here. Keep both `scoringPeriodId`-equivalent and
   `matchupPeriodId`-equivalent values in `raw` for every provider, even ones
   where they're currently identical, so the shape doesn't change later.

5. **Standings are not a provider-returned resource for ESPN (or Sleeper).**
   `espn-api`'s `League.standings()` and `standings_weekly()` implement a
   multi-rule tiebreaker cascade (head-to-head → points-for → division record →
   points-against → coin flip, selectable per league settings) **entirely
   client-side** — ESPN's API does not hand back a sorted standings list at all.
   Sleeper is the same: `sleeper_wrapper`'s `get_standings()` does client-side
   sort-by-`(wins, losses, points)` with no tiebreaker sophistication whatsoever.
   Yahoo is the outlier — `Standings.teams` **is** server-computed. An ESPN-first
   implementer who copies `espn-api`'s tiebreaker logic into `core/standings.py`
   as "the" standings algorithm will find Yahoo already disagrees with it (Yahoo's
   own server-side ranking may not match a from-scratch tiebreaker
   reimplementation), and will have built unnecessary logic Yahoo's adapter
   should have just skipped. `fetch_standings` in §5's Protocol is deliberately
   worded to make each adapter own its own tiebreaker source rather than sharing
   one "standings algorithm" in `core/`.

6. **Player IDs are not interchangeable even within the same provider across
   sports/years.** Not a cross-provider trap, but a real ESPN-specific one worth
   flagging before it's mistaken for a cross-provider design problem later: ESPN's
   `playerId` is stable within `nfl`/football but the *library itself* is
   split into separate top-level packages per sport
   (`espn_api.football`/`.basketball`/`.baseball`/`.hockey`/`.wbasketball`) with
   independently-defined `POSITION_MAP`/`PRO_TEAM_MAP` constants — there is no
   shared "ESPN player ID space" across sports either. If `fantasy-sports` ever
   grows beyond NFL, do not assume the ESPN adapter's player-ID handling
   generalizes across sport packages without re-verification.

7. **The health-manifest / SCHEMA_DRIFT design in ARCHITECTURE.md §11 is ESPN-
   specific by necessity, and that's fine — but don't let the Protocol assume
   every provider needs it equally.** Sleeper is documented, versioned-in-spirit,
   and has never required the kind of "silent breaking change, no changelog"
   canary ESPN's April 2024 base-URL migration demonstrated. A future Sleeper
   adapter's `credential_specs()` returning `[]` (no auth) is itself a signal that
   Sleeper's operational risk profile is categorically different from ESPN's —
   the canary/health-check machinery should stay pluggable per-provider rather
   than assuming every future provider needs the same drift-detection intensity
   ESPN does.

---

## Sources

- [cwendt94/espn-api](https://github.com/cwendt94/espn-api) — `espn_api/football/*.py`, `espn_api/base_league.py`, `base_settings.py`, `base_pick.py`, `base_offer.py` (read directly, this pass)
- [SwapnikKatkoori/sleeper-api-wrapper](https://github.com/SwapnikKatkoori/sleeper-api-wrapper) — `sleeper_wrapper/*.py` (read directly, this pass)
- [uberfastman/yfpy](https://github.com/uberfastman/yfpy) — `yfpy/models.py` (read directly, this pass)
- [Sleeper API docs](https://docs.sleeper.com/) — official
- [Steven Morse — Using ESPN's new Fantasy API (v3)](https://stmorse.github.io/journal/espn-fantasy-v3.html)
- [ffscrapr](https://ffscrapr.ffverse.com/) and its [function reference](https://ffscrapr.ffverse.com/reference/index.html), [`ff_rosters()`](https://ffscrapr.ffverse.com/reference/ff_rosters.html) — the only library implementing ESPN, Sleeper, MFL, and Fleaflicker side by side
- [nflreadr — FF Player IDs data dictionary](https://nflreadr.nflverse.com/articles/dictionary_ff_playerids.html) (DynastyProcess.com crosswalk)
- [Fleaflicker API docs](https://www.fleaflicker.com/api-docs/index.html)
- [MyFantasyLeague API info](https://www46.myfantasyleague.com/2020/api_info?L=33393&STATE=details)
- [ffcbs (CBS scraper) docs](https://rdrr.io/github/dfs-with-r/ffcbs/man/ffcbs_api.html) — CBS confirmed deprecated
- `apidocs.fantasy.nfl.com` — verified non-resolving, 2026-08-26, this pass
- [DeadlyChambers/fantasy-scraper](https://github.com/DeadlyChambers/fantasy-scraper), [ianderse/nfl_fantasy_scraper](https://github.com/ianderse/nfl_fantasy_scraper) — NFL.com community tooling, confirms no stable API to build against
