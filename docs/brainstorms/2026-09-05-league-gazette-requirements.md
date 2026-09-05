---
date: 2026-09-05
topic: league-gazette
status: draft — awaiting John's call on repo placement (see Outstanding Questions)
---

# The League Gazette — a weekly newspaper for one fantasy league

## Summary

A responsive, interactive web page published once a week for a single fantasy
league: a newspaper "issue" with a lead story, infographics, short blurbs on
every matchup, a data deep dive, awards, power rankings, and a **Constructors'
Championship** table for the league's couples. Modeled on the vault's `/wsj`
skill (a WSJ-style front page of John's week) but pointed at a league, hosted
where ten friends can open it from a group text on a phone.

The first league is **Supper Club** (ESPN league `713073439`), a 10-team,
5-couple league John created on 2026-08-25 and drafted on 2026-09-03. Week 1
kicks off 2026-09-10; the first regular issue publishes **Tuesday 2026-09-15**.
A **Draft Day special edition** (Issue 0) is the first deliverable and needs only
draft data, which already exists.

This is the first real consumer of the `fantasy-sports` CLI. It is also the
forcing function that gets the CLI's read path shipped.

---

## Problem Frame

Every fantasy league has a group chat and a standings page. Neither tells the
story. The commissioner's weekly recap, when it happens, is a wall of text
written at midnight. ESPN's own "recap" is generic and un-fun.

What John wants is the artifact `/wsj` proved out for his personal life: a real
front page — designed, illustrated, opinionated, with a masthead and running
gags — that makes the week feel like it happened somewhere. For a league that
means the page members screenshot, argue about, and wait for on Tuesday.

Two things make a league a *better* subject than a life:

1. **The data is structured and complete.** Every point, every lineup decision,
   every waiver bid is in ESPN. There is no "where was John really" problem. The
   infographics can be exact and the prose can be fact-checked mechanically.
2. **It repeats 17+ times on a fixed cadence.** A prompt-driven skill that costs
   a long session per edition is the wrong shape. This needs a pipeline: the same
   paper every week, different content, produced mostly by deterministic code,
   with a model writing only the color.

---

## Key Decisions

**Pipeline + template, not a prompt.** `/wsj` is an 1,879-line prompt that is
re-driven every week. The Gazette is a program: snapshot → compute → write →
render → publish. Deterministic code owns every number, table, and chart. A model
writes headlines and blurbs from the computed facts and never sees raw ESPN
data. This keeps cost per issue near zero, makes the design consistent across
the season, and makes every claim in the prose traceable to a field.

**Immutable weekly snapshots are the source of truth.** Each Tuesday the
pipeline pulls the week's JSON from the CLI into `data/<season>/week-NN/` and
commits it. Every issue rebuilds from snapshots, never from live ESPN. This is
the CLI's own thesis applied to its first consumer: when ESPN breaks, the paper
still prints last week, and the season archive never rots.

**The CLI is the only data path.** The Gazette shells out to `fantasy-sports …
--output json` and parses the versioned envelope. It does not import `espn-api`.
If a read the paper needs is missing from the CLI, the CLI grows the read. This
is the point of dogfooding: the output contract gets tested by a program that
depends on it. (One exception is allowed for Issue 0 if the CLI is not ready —
see Outstanding Questions.)

**Couples are a Gazette concept, not an ESPN concept.** ESPN has no notion of a
couple. The mapping `team_id → couple` lives in the Gazette's league config, and
the Constructors' Championship is computed there. The CLI stays neutral.

**The model writes color, never facts.** The copy desk receives `issue.json`
(computed facts, already phrased as statements with their numbers) and returns
headlines, deks, and blurbs. A fact-check pass rejects any number in the copy
that is not present in the input. Copy is written to a reviewable file so John
can edit a line before publish. Per frugal routing, drafting routes through
`model-route`; league data is friends' names and scores, not health or finance,
so local or Codex tiers are acceptable for drafts.

**Static site, one URL per issue, public-but-unlisted.** Members open it from
the group chat on phones. GitHub Pages from the Gazette repo is the default
(zero infrastructure, custom subdomain optional). A league page with first names
and team names is normal-web-public in the way every league site is; no health,
no money, no children's details ever appear on it.

**Tuesday cadence.** ESPN finalizes stat corrections Tuesday morning after Monday
Night Football. Publishing Tuesday matches the WSJ's cadence and guarantees
final numbers. One issue per week in v1. Preview or Thursday editions are later.

**A Ledger, as in `/wsj`.** Running gags, voices used, awards already given,
callbacks due. The paper should feel like it has a memory across the season.

---

## Actors

- **League members (10 people, 5 couples).** Read on phones. Never log in.
- **John, publisher.** Reviews copy Tuesday morning, edits a line, pushes
  publish, drops the link in the thread.
- **The Tuesday agent.** Runs the pipeline unattended up to the copy-review gate.
- **The CLI.** Supplies every fact.

---

## Requirements

### R1. One issue per week, as a responsive page
A standalone HTML page per week plus a season index. Readable at 375 px wide
with no horizontal scroll; every table and chart scrolls inside its own
container. Light and dark. Loads with no external dependencies beyond fonts
(charts are inline SVG; images are embedded or hot-linked from ESPN).

### R2. Newspaper structure, consistent every week
Masthead with issue number and date · **Lead story** (game of the week) · boxed
**"What's News"** ticker of the week's transactions and notable events ·
**Standings** (team view) and **Constructors' Championship** (couples view,
toggleable) · **Awards box** · **Power Rankings** with movement arrows · one
**Deep Dive** data story with its own infographic · **Around the League** — one
short blurb per matchup, tap to expand the box score · **Couples Corner** —
intra-couple comparisons and any spouse-vs-spouse matchup · **Next Week** —
matchups with projections · a season **Trophy Case** that accumulates.

### R3. Computed stats (deterministic, tested)
- Constructors' standings: combined W-L, combined points, per-week couple points,
  cumulative gap to leader, best/worst couple week.
- All-play record and **Luck Index** (actual wins minus expected wins).
- Optimal lineup vs actual: **bench points left**, would-have-won flags.
- Projection deltas per player and team: **MVP** (biggest over), **Bust**
  (biggest under), team-level over/underperformance.
- Highest and lowest score; biggest blowout; closest game; median score and
  who beat the median.
- Waiver Wire Hero: best performance by a player added that week.
- Power ranking formula (points, all-play, recency), with weekly movement.
- Season trend lines per team and per couple.
- Draft edition: draft board, position runs, pick vs projected rank
  (`draft_projected_rank`), best value and biggest reach, per-couple draft view.

### R4. Awards and gags with memory
Weekly awards are computed, then named and written by the copy desk. The Ledger
prevents repeating last week's gag verbatim and tracks trophies awarded so the
Trophy Case is cumulative and correct.

### R5. Copy desk with a fact gate
Headlines, deks, and blurbs generated from `issue.json` only. Every numeral in
the output must appear in the input or the line is rejected. Tone is sports-page
color with affectionate roasting; the audience includes spouses and the writer
never punches down. ESPN free text (team names, player names) is rendered as
text, never as HTML, and is labelled untrusted at the CLI boundary.

### R6. Publish flow
`gazette snapshot --week N` · `gazette build --week N` (compute + write + render
into `site/`) · John reviews `copy.md` and the local page · `gazette publish`
pushes `site/` to the hosting branch. The snapshot and build steps run from a
Tuesday cron; publish waits for John in v1.

### R7. League config
One TOML per league: ESPN league id and season, masthead name, colours, couples
mapping, member display names, publish target. A second league is a second file.

### R8. Season archive
Every issue stays at a stable URL. The index lists the season with each issue's
headline. Snapshots and copy are committed, so the whole site rebuilds from the
repo alone.

---

## Key Flows

**Tuesday, 6:00 AM.** Cron runs `snapshot` for the week that just closed: box
scores, standings, transactions, rosters, and the draft (once). JSON lands in
`data/2026/week-01/` and is committed. `build` computes `issue.json`, drafts
`copy.md`, renders `site/2026/week-01/index.html`, and opens a PR (or a local
review page). John reads the page over coffee, fixes a headline, merges.
`publish` deploys. John posts the link in the thread.

**Draft Day (Issue 0), before 2026-09-10.** Only `raw --view mDraftDetail`,
teams, and members are needed. Draft board infographic, per-couple draft
summary, best value and biggest reach, and the season's first Power Rankings
based on projections.

**Playoffs and Championship.** Special mastheads and a season-review issue.
Same pipeline; extra sections keyed on `is_playoff`.

---

## Acceptance Examples

- Open Week 3 on a phone: masthead, lead story, tap a matchup and see both
  lineups with per-player points, scroll the Constructors' table sideways
  without the page scrolling.
- Every number in a blurb can be found in that week's `issue.json`.
- Delete `site/`, run `gazette build --all`, and every issue renders
  byte-identically from committed snapshots and copy with ESPN unreachable.
- Bench points for a team equal the optimal-lineup total minus actual total,
  respecting the league's roster slots (FLEX eligibility from the CLI's `raw`
  settings, since slot semantics do not normalize).

---

## Scope Boundaries

**In:** one league configured, Tuesday issue, Draft Day special, the computed
stats above, static hosting, copy review gate, season archive.

**Deferred:** Thursday previews; member comments or reactions; per-member
personalised pages; push notifications; a second league (config supports it,
nobody runs it yet); generated editorial cartoons (nice, not needed to ship).

**Out:** any advice (start/sit, trades) — the paper reports, it does not coach;
any write to ESPN; anything about members beyond their fantasy teams.

---

## Dependencies and Assumptions

- **`fantasy-sports` v0.1 reads** (jwulff/fantasy-sports #2–#9), specifically
  matchups *with per-player box-score detail*, standings, transactions, rosters,
  teams and members (owner display names for the couples mapping), and draft
  results via `raw --view mDraftDetail`. Writes (#14–#18) and the health system
  (#10–#12) are not on the Gazette's path.
- ESPN cookies (`espn_s2`/`SWID`) for the private league, via the CLI's auth chain.
- Player headshots hot-linked from ESPN's CDN by player id; team logos from the
  league's team records. Both are optional decoration; the page must render
  without them.
- Hosting on GitHub Pages; a custom subdomain is a later nicety.
- Timeline is the risk: 10 days to Issue 1 with zero CLI code. Issue 0 is the
  buffer — it proves the pipeline on draft data while the CLI's weekly reads land.

---

## Outstanding Questions

1. **Repo placement.** Recommendation: a separate repo (working name
   `league-gazette`) that depends on the CLI. The CLI's identity is "tools, not
   policy", zero LLM in `core/`, ≤5 dependencies and a <150 KB wheel; a site
   generator with a copy desk breaks all of those. `reports/` in the CLI stays for
   terse memos. Alternative: build it as `fantasy-sports reports gazette`. Epic and
   children are filed in `jwulff/fantasy-sports` under `track:reports` for now and
   can be transferred with `gh issue transfer` once decided.
2. **Masthead name.** "The Supper Club Gazette"? "Supper Club Times"? John's call.
3. **Issue 0 shim.** If the CLI cannot produce draft data by ~2026-09-08, may
   Issue 0 read `espn-api` directly, to be replaced the moment the CLI can?
4. **Who else runs it.** Dragon Slayers and Big Mac Daddy D ERA are John's other
   two leagues; a second config is cheap, but the copy desk's voice and gag ledger
   would need to be per league.

---

## Sources and Research

- Vault: `Projects/Supper Club Fantasy League/Supper Club Fantasy League.md`
  (league, members, couples, the constructors idea from the 2026-08-26 thread).
- Vault: `.claude/commands/wsj.md` — the prior art this borrows structure from
  (ledger, voices, layout rotation, photo rules, self-improvement log).
- `docs/ARCHITECTURE.md` §10 (Reports), §12 (v0.1 scope), §13 (roadmap: this
  supersedes the v0.2 "weekly recap" line with a concrete product).
- `docs/research/02-provider-data-shapes.md`, `03-espn-api-surface.md` — what
  ESPN actually exposes per week (`mMatchupScore`+`mScoreboard` box scores,
  `mTransactions2`, `mDraftDetail`, `mPositionalRatings`).
- ADR-0003 (CLI is the primitive; consumers shell out to it).
