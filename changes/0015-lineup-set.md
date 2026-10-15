# `lineup set` and `journal reverse`: the first writes (#15, #16)

An agent managing a league has to change a lineup every week, unattended,
without a confirmation prompt, and be able to undo it. This is the first
command that writes to a real league. It is also where the shared write path
gets its first end-to-end use: the transport and ownership guard (#107), the
journal (#16), and the two write codes (#106). The irreversible commands
(#18) reuse the same flow with `move_players`.

## What changed

- **`commands/lineup.py`** adds `lineup set --team T --set PLAYER=SLOT ...
  [--dry-run]`. The handler reads fresh, resolves the team and the players,
  plans the diff, and hands off to `execute_lineup`, which `journal reverse`
  shares.
  - `parse_assignments` accepts a player id, an exact case-insensitive name,
    or a fragment unique on that one roster. A malformed assignment, an
    unknown or ambiguous player, and a player named twice are each
    `CONFIG_INVALID` with `kind: "argument"`.
  - `plan_lineup` diffs the target against the roster just read. It refuses
    an ineligible slot, a locked player who would move, an empty diff, and a
    final lineup that overfills a slot. The count check runs over the final
    state, because ESPN validates the final lineup (brief §3), and only for
    slots a move fills. Items list vacates first.
  - `execute_lineup` checks ownership, journals, sends, and reads back, in
    that order.
- **`commands/journal.py`** adds `journal reverse --id E [--dry-run]`. It
  sends the entry's `prior_state` as the target, journaled as kind `reverse`
  with `reverses`. It finds the league profile whose id the entry names.
- **`commands/__init__.py`** registers both commands as OBJECT shapes.
- **`providers/espn.py`**: the constructor's `now` defaults to `None` and
  resolves `utc_now` at call time. Tests and the sample generator patch the
  module's clock to fix "before kickoff". The old bound default ignored the
  patch (`docs/memory/default-args-defeat-monkeypatch.md`).
- **`tests/_harness.py`** gains `WritableEspn`. It replays the synthetic
  league for reads and answers a POST as brief 05 captured ESPN answering.
  `apply` updates the rosters later reads see, `ignore` returns the same 200
  without applying, and `reject` returns p8's 409.
- **`tests/unit/test_lineup_set.py`** adds 47 offline tests through the real
  adapter.
- **`tests/live/test_lineup_set_live.py`** swaps a starter and a bench
  player on the session's own team, reverses the swap, and compares the
  roster slot for slot.
- **Samples**: `lineup-set`, `lineup-set.dry-run`, `journal-reverse`,
  `error-write-rejected`, and `error-write-diverged`. The two codes leave
  `AWAITING_A_COMMAND`, and that set is gone. The goldens stay, because
  `test_output.py` renders them.
- README gains a "Writing" section, and the exit range an agent parses
  becomes `3`–`12`. ARCHITECTURE §9, the command reference, and the
  CHANGELOG are updated.

## Decisions worth recording

- **The diff is computed in the command, not through the adapter's private
  builder.** The command already has the roster, and `--dry-run` and the
  preflight both need the diff. The adapter re-reads `mRoster` live and
  re-diffs before it sends. A world that moved in between therefore shows up
  as an adapter refusal or in the read-back. The `WritingProvider` Protocol
  is unchanged. `items` are reported as player id and slot names, the tool's
  vocabulary, not ESPN's slot ids.
- **Ownership is checked twice.** The adapter's guard refuses before it
  builds a body. The command checks `session_role` first as well, so a
  refused team leaves no journal row and `--dry-run` never plans for a team
  the tool will not write.
- **Read-back closes the journal entry.** A clean read-back calls
  `complete` with `executed`. A mismatch calls `complete` with
  `status="diverged"` rather than `diverge()`, so the transaction id and the
  echoed member are kept in their columns. The error has the same ids in
  `details`.
- **An identity divergence from the adapter uses `diverge()`.** Its applied
  state is `{"items": …}` verbatim from the error's details, because the
  command cannot name ESPN slot ids.
- **Any other exception leaves the entry `pending`.** A timeout says nothing
  about whether the write applied, and `rejected` would claim it did not.
- **A failed read-back is an unverified success.** ESPN executed the write,
  and the agent needs the transaction id. `read_back.verified` is false, and
  `warnings` says to read the roster.
- **A journal that cannot be completed is a warning.** The write already
  happened, and the entry stays `pending`, which `doctor` reports.
- **`warnings` and `note` appear only when they apply.** Every key in the
  plan's success shape is always present.
- **`journal reverse` reverses only executed or diverged lineup entries.**
  A `pending` or `rejected` entry is `CONFIG_INVALID`, because nothing is
  known to have applied. Add, drop, claim, and cancel entries are
  `NOT_AVAILABLE` before a provider is even built.
- **The live test needs a second opt-in.** The read-only live suite points
  `FANTASY_SPORTS_ESPN_LEAGUE` at whatever league is handy, possibly a real
  one. This test writes only when `FANTASY_SPORTS_ESPN_WRITE_LEAGUE` equals
  it. Its journal and cache live in a temp XDG tree.
- **The write samples use an ordinary-shaped invented SWID.** The generator's
  `PLACEHOLDER_SWID` sits in the pseudonym sentinel range. The scrubber
  leaves it alone, and secret redaction then blanks it out of `mNav`, so the
  session would own nothing.

## Validation

- `uv run pytest`: 1766 passed, 17 skipped. Line and branch coverage is
  100% on `commands/lineup.py` and `commands/journal.py`, and 99% overall.
- `ruff check`, `ruff format --check`, `check_budgets.py`,
  `check_startup.py`, and `render_readme_samples.py --check` are all clean.
- Live, once, against the throwaway test league (team 7): `lineup set`
  swapped an unlocked starter and bench player, and ESPN returned
  `EXECUTED`. The read-back verified, and the journal entry was `executed`
  with the transaction id and echoed member. `journal reverse` sent the
  second transaction, which was also verified. The roster then matched the
  pre-test snapshot slot for slot.
