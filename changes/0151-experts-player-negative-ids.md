# Accept negative ESPN ids in `experts --player`

## Why

The league-gazette snapshot (jwulff/league-gazette#132) asks `experts news
--player <espn_id>` for every rostered player, and ESPN's team defenses carry
negative ids (`-16033` is the Ravens). `_Players.resolve` tested the token
with `str.isdigit()`, which is false for `-16033`, so the id fell through to
name matching and was refused as `CONFIG_INVALID` ("matches no player"). The
first live week-6 snapshot on 2026-10-20 stopped on it. The players table
does carry those ids (`experts rankings --pos DST` joins them), so the
resolver was the only gap.

## How

A token that is digits after an optional leading minus is an id. FantasyPros
ids are never negative, so a negative token is looked up only in the ESPN
index. Positive tokens behave exactly as before, including the ambiguity
rule.

## Validation

`uv run pytest` (the new test resolves `-16025`, the cassette's 49ers
defense, through to the news request); ruff clean; `preflight run` PASS
(receipt in the PR body).
