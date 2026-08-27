# ADR 0006: Read-only in v0.1; writes gated behind dry-run and confirmation

**Status:** Accepted
**Date:** 2026-08-26

## Context

The obvious value of this tool includes writes: setting a lineup, submitting a
waiver claim, dropping a player. Those are also irreversible actions with real
cost — a miscalculated waiver claim cannot be undone, and an automated one can
fire while the user is asleep.

Separately, the read path is running against an API that we do not yet know
empirically. Building writes on top of a read layer that has not survived contact
with a real season compounds risk.

## Decision

**v0.1 is read-only.**

Writes land in v0.3, behind all of:

- `--dry-run` as the **default**; mutation requires explicit `--commit`
- a printed diff of before/after state
- interactive confirmation unless `--yes`
- any autonomous/scheduled write routes through `/sanity-gate`

## Consequences

**Easier:** v0.1 ships before the season starts. The read path gets weeks of real
use before anything can mutate state. No possibility of the tool costing the user
a roster spot during its least-tested phase.

**Harder:** The tool is not fully useful for a season until v0.3.

**Accepted:** This follows the standing high-stakes/irreversible-actions rule in
`~/.claude/CLAUDE.md`. The gate is not negotiable later for convenience.

## Alternatives considered

**Writes in v0.1 behind a flag** — tempting for immediate season usefulness.
Rejected: it front-loads the riskiest surface onto the least-proven code.

**Full write automation from the start** — rejected outright for v0.1.
