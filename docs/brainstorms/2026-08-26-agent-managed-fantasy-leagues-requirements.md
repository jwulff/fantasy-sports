---
date: 2026-08-26
topic: agent-managed-fantasy-leagues
---

# Agent-Managed Fantasy Leagues

## Summary

A CLI that gives John's agents a live, readable picture of his ESPN fantasy
leagues and the ability to act on them. Reads carry enough context for an agent
to reason; writes let it manage the team. The tool exposes capability and holds
no judgment. Built to a public standard, sequenced to serve John first.

---

## Problem Frame

The project was scoped from a research finding: no maintained CLI exists for
ESPN fantasy leagues, and every prior attempt died when ESPN silently changed
its unofficial API. That framing produced a plan optimized for a durable public
artifact — a canary, a health manifest, release automation, CI-enforced budgets.

The framing was incomplete. The actual need is not a tool John types into. It is
an interface his agents use to see and manage his leagues on his behalf, across
a season, without him in the loop for every action. Drafting is incidental.

Two consequences follow. Reads have to be rich enough that an agent can decide
from them rather than guess. And writes are not a later phase — an interface
that can only observe does not manage anything.

The prior plan also encoded safety designed for a human operator: dry-run
defaults and interactive confirmation. Those protect nobody here. An agent
passes `--yes`. What actually protects John is knowing what his agent did and
being able to undo it.

---

## Key Decisions

**The agent is the primary user; John is the auditor.** Command surface,
output shape, error text, and help output are designed for an agent to consume
without external documentation. John's direct use is real but secondary, and
his primary interaction with the tool is reviewing and reversing what his agents
did.

**Tools, not policy.** The CLI exposes capability and contains no decision
logic. Start/sit reasoning, waiver valuation, and trade evaluation live in the
agent. This removes an authorization model, a policy engine, and every "should
we" branch from scope.

**Writes are core scope, not a later phase.** This supersedes ADR-0006, which
deferred writes to v0.3. A read-only release would demonstrate the idea without
delivering it.

**Auditability replaces confirmation.** Also superseding ADR-0006: dry-run
defaults and interactive confirmation are removed. A gate the primary user
bypasses with a flag is friction, not safety. Explicit-state operations, a
mutation journal, and reversibility replace it. `--dry-run` survives as an
opt-in flag because an agent previewing its own change is a genuine use.

**Normalization serves readability, not portability.** This revises ADR-0002,
which defined the normalized layer as the structural intersection across
providers. Cross-provider equivalence is not a goal. ESPN's normalized output
may carry fields no other provider exposes. Raw payloads ship alongside
normalized ones in every response, so nothing is ever unreachable.

**Freshness is a correctness property.** Cache TTLs were tuned for latency. An
agent deciding a lineup against a kickoff deadline can be wrong because its data
was stale, which makes staleness a correctness concern and requires both a
stated data age and a way to demand a fresh read.

**Public release is a close second, not a byproduct.** The quality bar in
ADR-0008 holds from the first commit. Sequencing puts John's working tool first;
standards do not relax to get there.

---

## Actors

- A1. **John** — commissioner of several ESPN leagues. Sets what his agents are
  allowed to do, outside this tool. Audits and reverses their actions.
- A2. **John's agents** — the primary consumer. Run in environments with shell
  access, read to reason, write to act, operate unattended.
- A3. **ESPN** — unofficial, undocumented, changes without notice, offers no
  programmatic authentication.

---

## Requirements

**Reads for agent reasoning**

- R1. Every read command emits a normalized rendering and the provider's raw
  payload in the same response.
- R2. Normalized output is optimized for legibility; it is not constrained to
  fields other providers can match.
- R3. Roster, matchup, and free-agent output carries enough context to support a
  lineup decision without a follow-up call, including at minimum player status,
  position, opponent, and any projection the provider exposes.
- R4. Every response states the age of the data it carries.
- R5. A caller can demand a guaranteed-fresh read that bypasses cached data.

**Writes and auditability**

- R6. Write commands accept explicit target state rather than relative
  instructions.
- R7. Every write records prior state to a local journal before mutating.
- R8. Any journaled write can be reversed.
- R9. `--dry-run` is available on every write command and reports the change
  without applying it. It is not the default.
- R10. A write invalidates any cached data it affects.

**Agent interface contract**

- R11. Every response is a versioned envelope.
- R12. Every failure carries a stable machine code distinguishing credential,
  availability, throttling, and schema-drift causes.
- R13. Help output is complete enough for an agent to discover and correctly
  invoke any command without external documentation.

**Quality and sequencing**

- R14. The tool meets its published performance and test budgets from first
  release, independent of whether anyone outside John has installed it.
- R15. No public-facing release ships before John's agents can both read and
  write his leagues.

---

## Key Flows

- F1. **Agent answers a question about the team**
  - **Trigger:** John asks his agent something about a league.
  - **Actors:** A2, A3
  - **Steps:** Agent invokes read commands; receives normalized and raw output
    with a stated data age; reasons over it; answers.
  - **Covered by:** R1, R2, R3, R4, R11, R13

- F2. **Agent acts on the league**
  - **Trigger:** Agent determines a roster change is warranted.
  - **Actors:** A2, A3
  - **Steps:** Agent optionally previews with `--dry-run`; issues an
    explicit-state write; the tool journals prior state, applies the change, and
    invalidates affected cache.
  - **Outcome:** League state changed, prior state recoverable.
  - **Covered by:** R6, R7, R9, R10, R11

- F3. **John audits or reverses agent action**
  - **Trigger:** John wants to know what his agents did, or disagrees with it.
  - **Actors:** A1
  - **Steps:** John reads the journal; optionally reverses a recorded write.
  - **Covered by:** R7, R8

---

## Acceptance Examples

- AE1. **Covers R4, R5.** Given a cached roster fetched eight minutes ago, when
  an agent requests the roster, then the response includes the data's age; when
  the agent instead demands a fresh read, then cached data is bypassed and the
  response reflects a new fetch.
- AE2. **Covers R6.** Given a roster whose state has changed since the agent
  last read it, when the agent submits an explicit target lineup, then the
  result is that exact lineup or a clean failure — never a partially applied
  change.
- AE3. **Covers R7, R8.** Given an agent has set a lineup, when John inspects
  the journal, then he sees the prior lineup and the applied one; when he
  reverses it, then the prior lineup is restored.
- AE4. **Covers R9.** Given `--dry-run` on a write command, when it runs, then
  it reports the change it would make and league state is untouched.
- AE5. **Covers R12.** Given expired ESPN credentials, when any command runs,
  then the failure is distinguishable by machine code from an ESPN outage and
  from a throttling response, so an agent can tell "ask the human" from "retry
  later."
- AE6. **Covers R1.** Given any read command, when it returns successfully, then
  both the normalized rendering and the provider's raw payload are present.

---

## Scope Boundaries

**Deferred for later**

- Yahoo and Sleeper adapters — the provider interface is shaped for them,
  neither is built.
- An MCP server — the agents that matter have shell access; MCP becomes relevant
  only for surfaces that do not.
- Draft-specific tooling.
- Public release machinery — canary, release automation, published package.
  Sequenced after the core per R15, not cut.

**Outside this product's identity**

- All decision logic: start/sit reasoning, waiver valuation, trade evaluation,
  projection modeling. The agent owns judgment.
- An authorization or policy model governing what an agent may do. That belongs
  to whoever operates the agent.
- Cross-provider comparability. Normalized output is per-provider by design.

---

## Dependencies and Assumptions

- Depends on `espn-api` for ESPN access. One primary maintainer; the provider
  interface exists partly to contain that risk.
- ESPN authentication is manually extracted browser cookies with no refresh
  path and no programmatic alternative. This constrains unattended operation:
  agents run unattended only until credentials expire, and expiry is silent.
- ESPN has an unofficial API undergoing a platform overhaul, with its API
  documentation currently offline. A breaking change that cannot be adapted to
  is a live risk to the whole product.
- Assumes agents run where a shell is available. If that stops being true, the
  deferred MCP surface becomes load-bearing rather than optional.

---

## Outstanding Questions

**Resolve before planning**

- What is the journal's retention policy, and is it per-league or global?
- Does a guaranteed-fresh read bypass the cache entirely, or refresh it as a
  side effect?
- Which write operations ship first? Lineup changes are the obvious core;
  whether waiver claims, drops, and trades are in the same release is unsettled.

**Deferred to planning**

- Journal storage shape and location.
- How cache invalidation on write is scoped — the mutated resource only, or the
  league's derived views as well.

---

## Sources and Research

- `docs/research/01-telemetry-auto-issues.md` — client error reporting; the
  basis for using the operator's own credential rather than shipping one.
- `docs/research/02-provider-data-shapes.md` — provider comparison. Its
  warnings about protecting a portable abstraction are largely moot now that
  cross-provider equivalence is out of scope; its findings on co-managed teams
  and ambiguous week semantics still hold.
- `docs/research/03-espn-api-surface.md` — ESPN endpoint surface, authentication
  mechanics, and breakage history.
- `docs/research/04-python-cli-packaging.md` — packaging, testing, and
  distribution practice.
- `docs/adr/` — the eight decision records this brainstorm revises in part.
  ADR-0002 and ADR-0006 both require amendment; see Key Decisions.
