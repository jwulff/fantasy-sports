# `espn-api` reads shapes; everything a client owes you is ours

**Found:** 2026-09-05, building U7 (#4). **Applies to:** the ESPN adapter, the
canary, and any future provider that wraps a community library.

`cwendt94/espn-api` is worth depending on for one reason: it knows ESPN's
undocumented payload shapes. `PLAYER_STATS_MAP` is 234 reverse-engineered stat
ids, `BoxPlayer` resolves the per-week `proTeamId` for a player traded
mid-season, and the standings tiebreaker cascade matches what ESPN's own site
computes. Redoing any of that is a multi-week side quest with nothing to check
against.

What it is *not* is an HTTP client. Every one of the following had to be built
in `providers/espn.py`, and each is invisible until you look at the source.

## The transport seam, and why it has to be under the library

`espn-api` calls the module-global `requests.get` and hands the bare status code
to `checkRequestStatus`, which special-cases 401 and 404 and folds **everything
else** — 429 included — into `ESPNUnknownError("ESPN returned an HTTP {status}")`.
The status survives only as English; `Retry-After` does not survive at all,
because it is a header on a response object the exception never carries.

So `RATE_LIMITED` with a real `retry_after` can only be produced *before* the
library sees the status. The adapter replaces `league_get` and `get` on the
`EspnFantasyRequests` **instance** — not on the class, and not by patching the
`requests` module, either of which would leak into every other league object in
the process and into anything else sharing `requests`.

That same seam is the only place the HTTP cache can attach and still pay off.
`box_scores()` and `free_agents()` each fan out into two or three sequential
requests with no internal dedup, and they share sub-fetches with each other and
across leagues, so a cache wrapped around the library's public methods still
pays every round trip on a miss (ARCHITECTURE §14 item 4).

**One thing stays library-owned on purpose:** the 401 alternate-URL-shape
retry. See [[espn-401-tells-you-nothing]].

## Bare `Exception` is a normal return value

`espn-api` raises `Exception` — the base class, with a sentence in it — for
conditions that are not failures:

| Raised | What it actually means |
|---|---|
| `Exception('No transactions found')` | the scoring period was quiet. A successful empty result. |
| `Exception('Cant use box score before 2019')` | the library refuses, ESPN was never asked |
| `Exception('Cant use free agents before 2019')` | same |
| `Exception('Cant use recent activity before 2019')` | same |

The first one matters most: reporting an empty week as `PROVIDER_UNAVAILABLE`
tells an agent to retry an answer that was already correct. The wrapping layer
therefore needs a catch-all mapping to `PROVIDER_UNAVAILABLE` **and** a
special case ahead of it, keyed on the message text, because there is nothing
else to key on.

## `SCHEMA_DRIFT` is a context-capture layer, not an `except X: raise Y` shim

Every model constructor does unguarded dict access — `Team.__init__` reads
`data['record']['overall']['wins']` — so a renamed ESPN field arrives as a bare
`KeyError` from six frames down, indistinguishable from a bug in our own call.

What makes the resulting error actionable is three things captured together: the
**view** that was requested, the deepest `espn_api` frame in the traceback
(walked from `exc.__traceback__`), and the **key** from `KeyError.args[0]`. One
caveat on that last one: a `KeyError` argument is normally a field name, which is
what `details.path` is for — but `player_map[playerId]` raises with an *id*, and
an id is provider data rather than a field name. Non-string keys are reported by
type only (`<int>`).

## Two smaller traps with real consequences

**`Settings.position_slot_counts` is built on dict ordering.** It zips the
league's slot counts against `list(POSITION_MAP.values())[:n]` — and `POSITION_MAP`
holds *both* directions of the id/name mapping in one dict, so this depends on
all the int→str entries being declared before the str→int ones. Deriving
`roster_slots` by mapping each slot id through `POSITION_MAP` directly is the
same information without the dependency.

**`scoreboard(week)` filters on `matchupPeriodId`.** Passing a scoring period
straight in silently returns the wrong playoff round, or nothing — and the two
identifiers are equal every regular-season week, so an in-season test never sees
it. Resolve through `settings.matchup_periods` first, which is ESPN's own
`{matchupPeriodId: [scoringPeriodId, ...]}` table.

Related: [[espn-401-tells-you-nothing]], [[prior-art-graveyard]],
[[cache-redaction-and-tag-classes]]
