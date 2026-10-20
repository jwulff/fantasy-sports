# `experts` commands: rankings, projections, injuries, news (#129)

## Why

On waiver night an agent needs what ESPN's own data lacks: where the expert
panel ranks a player, what it projects for him this week and for the rest of
the season, who is hurt and how likely he is to play, and what the wire says.
#128 built the FantasyPros client; this adds the four reads over it. The
first consumer is `jwulff/league-gazette` (#132), which builds facts from the
rows and is written against the exact field names, so the row shapes in the
issue and its 2026-10-20 contract comment are treated as an API. FantasyPros'
terms ask for attribution, so the README and the command reference say
"Rankings and projections by FantasyPros."

## How

- **Not league commands.** All four are `takes_league=False` and declare the
  options they honour. `--league` exists on `rankings` and `projections` only,
  to read ESPN's receptions setting (`scoringItems`, stat 53: 1.0 PPR, 0.5
  half, else standard) and pick the scoring; it is never required, and an
  unknown league fails before the 4 MB players table is fetched. Settings with
  no `scoringItems` at all are `SCHEMA_DRIFT`, not a quiet "standard".
- **The players table does three jobs from one cached request:** the
  `espn_id` join on every row (a string, `null` when FantasyPros has none or
  does not list the player), `--player` resolution, and the default week
  (FantasyPros reports the week in progress on it, as a string).
- **`--player`** takes a FantasyPros id, an ESPN id, or a name. An exact name
  wins over longer names it prefixes; otherwise a unique prefix; otherwise
  `CONFIG_INVALID kind=argument` with `details.candidates` and nothing sent.
  A number that is one player's FantasyPros id and another's ESPN id is
  ambiguous rather than guessed. `--pos` narrows a name, and a named player
  sends no `positions` (the position only narrowed the name).
- **Closed weeks are served.** `--week N` is passed straight through; verified
  live that week 5 comes back with `game_status: closed`, the last pre-game
  publish, `start_sit_grade` and `r2p_pts`.
- **`--ros`** sends `type=ros` and no week on rankings (a small addition to
  `consensus_rankings`: `week` is optional only with `ros`, and the two
  together are refused) and `ros=true` on projections. `--ros` with `--week`
  is `CONFIG_INVALID`: a rest-of-season list has no week.
- **Numbers are numbers.** FantasyPros sends `rank_ave`, `r2p_pts`, `bye_week`
  and `probability_of_playing` as strings; every one is parsed to a float or
  int, and an empty string or an unparseable value is `null`, never `0`.
  `created_at`, `updated_at` and `kickoff` are UTC ISO-8601 with `Z`.
- **Free text is untrusted.** News title, summary and impact are third-party
  prose that reaches an agent able to write; they are labelled in the
  envelope's `untrusted` map by path, as ESPN's free text is.
- **`--output table` takes columns.** `CommandSpec.table_columns` (dotted row
  paths) narrows the table to the columns an agent scans; a stats map or a
  practice report would wrap a 100-column table past reading. JSON and CSV are
  the contract and never narrow.
- **A second cassette.** `tests/cassettes/fantasypros/experts_2026.yaml`,
  built by `scripts/build_experts_cassette.py` from live captures of
  2026-10-20 (current week 7, closed week 5, the ROS list, a players table
  that actually contains the players the lists name, plus the name fixtures
  and the rows that join to null). It is separate from #128's so the two PRs
  do not both rewrite one 45 KB file. The key is scrubbed by the same hooks and
  the finished file is scanned and read back against the live key.
- **Samples.** `render_readme_samples.py --only-experts` regenerates just the
  new samples and `--help` offline and leaves every live ESPN sample as
  committed; the offline regeneration test covers the new ones too.

## Validation

- `uv run pytest`: 2300+ passed, coverage 99%; ruff check and format clean.
- Each command's row field list is asserted verbatim in `test_experts.py`,
  because it is the contract with the gazette.
- One live smoke test (`uv run pytest -m live tests/live/test_experts_live.py`)
  spends three requests of the 500 a day and passed against the premium key.
- Roughly twenty live requests were spent in total, all on building the
  cassette and the one smoke test.

## Out of scope, filed or left

Joining onto ESPN rosters and pools is #130. Expert accuracy tables
(`rankings/experts`) and `compare-players` stay out as the issue says.
`rankings` rows do not say which scoring or week they are for; the envelope
has no slot for it and the row list is a contract, so a caller that needs it
passes `--scoring` and `--week` explicitly.
