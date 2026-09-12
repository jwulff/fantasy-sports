# `roster` and `free-agents` name the opponent from the schedule (#86)

`fantasy-sports roster --team 11` on the eve of week 1 returned
`"opponent": null` for every player, while `box-scores --week 1` on the same
league resolved the opponent for all 151 lineup entries. The blog post's
terminal screenshot had to drop the opponent column because of it. #74 had
taught the box-score path to read the opponent from `proTeamSchedules_wl`
rather than trusting `espn-api`'s ratings-gated `pro_opponent`; the roster
path never received that fix — and on inspection it was worse than that.

## What changed

- **`_player` never set `opponent` at all.** It derived a kickoff from the
  schedule and then left the `Player.opponent` field at its default. So this
  was not a lost fix but a field that had never been populated on any read
  that goes through `_player` — `roster` and `free-agents` both. The
  `free-agents` check #86 asked for is therefore not a "check and note": it
  is the same defect, fixed in the same place.
- **`_player` now takes the opponent map** built by `_opponent_map`, the
  same helper `fetch_box_scores` uses, and keys it by `(proTeamId,
  scoringPeriod)` exactly as `_lineup_entry` does. `fetch_roster` and
  `fetch_free_agents` build the map once per read from the transport's
  responses; the schedule view is season-scoped and already cached, so this
  is a dictionary walk, not a request.
- **The pro-team lookup goes through `_pro_team_ids()`** instead of a linear
  scan over `PRO_TEAM_MAP` per player, and through `_library_str` first, so
  `espn-api`'s free-agent club `"None"` is a `None` team rather than a
  string that then fails to key anything. A club absent from the schedule
  for the period is a bye: `opponent` is `null`, never a guess and never the
  literal `"None"`.

## Why not read `espn-api`'s `Player.schedule`?

The same reason `_kickoff_map` does not (`docs/memory` and #72): the library
only names an opponent when the player's position appears under
`positionAgainstOpponent` in `mPositionalRatings`, and ESPN does not publish
that for every league on opening week. The schedule is the one source that
answers without a condition attached.

## Tests

Three new tests in `tests/unit/test_espn_provider.py`, against the
synthetic 2026 cassette (one week-2 game, KC at SEA): every rostered player
on team 1 has the club they are not on as their opponent; both free agents
resolve the same way; and a direct `_player` call shows a bye as `None` and
a `"None"` club as neither a team nor an opponent. No cassette changed.
