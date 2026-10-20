# Send `position=` for a single projections position

## Why

`FantasyProsClient.projections(positions=["WR"])` sent `positions=WR`, and
FantasyPros ignores a single value in that parameter and answers with the
default position, RB (gotcha 3 in the vault's FantasyPros note; the same trap
the research scripts hit on 2026-10-15). `experts projections --pos WR`
therefore returned 117 running backs, and the first live week-6 gazette
snapshot joined expert projections onto only 24 of 90 starters, all of them
RBs, with every other position null.

## How

Exactly one position sends the singular `position=<POS>`, which FantasyPros
honours; two or more still send the colon list `positions=A:B`. The cache
facet follows the parameter (`position=RB`), so the week-6 entries recorded
under the wrong parameter are simply not reused. Cassette URIs and the
parameter assertions move to the singular form.

## Validation

`uv run pytest` (the facet, parameter and cassette tests under the new
name); ruff clean; `preflight run` PASS (receipt in the PR body); then a live
`experts projections --pos WR --week 6` on Johns-Air-2 returns wide receivers.
