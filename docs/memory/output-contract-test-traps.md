# Two ways a test of the output contract lies to you

**Found:** 2026-09-05, building U5 (#6). **Applies to:** any test that proves a
TTY behaviour, and every golden file rendered by `rich` — U8 (#9) and U10 (#10)
both add more of both.

The output layer's whole claim is "a program can parse this". Two of the tests
that check that claim can pass while measuring nothing, and neither failure is
visible from the assertion.

## 1. Draining a pty after the child exits returns nothing on macOS

The honest way to prove "a TTY gets a table and a pipe gets JSON" is to run the
real thing on a real pty rather than to mock `isatty`. The obvious shape —
`subprocess.run(stdout=secondary)`, then close the secondary fd, then
`os.read(primary, ...)` until EOF — returns an **empty string** on macOS. Closing
the last secondary descriptor discards whatever is still buffered in the pty, so
the read loop sees EOF immediately. The test then asserts
`"Team Chaos" in ""`, which fails; the dangerous version is the one written as
`assert "{" not in captured`, which *passes* against an empty string and proves
nothing at all.

The fix is to read **while the child is still running**: `Popen`, close the
secondary fd in the parent immediately, drain `primary` until `os.read` raises
`OSError` (EIO once every writer has closed — that is EOF, not an error), and
only then `wait()`. `tests/unit/test_output.py::test_a_tty_invocation_emits_a_table_and_output_still_overrides_it`
carries the working shape.

The generalisation: **a capture assertion phrased as an absence passes against an
empty capture.** Pair every "the output is not JSON" with a "the output contains
this specific string", or the test survives its own plumbing breaking.

## 2. Golden files rendered by `rich` are hostage to a dependency we do not pin

`rich` is not a direct dependency — it arrives through `typer`, and ADR-0008's
five-dependency budget is why it stays that way (see the module docstring in
`output/table.py`). So `uv.lock` can move `rich` on a `typer` bump, and a rich
release that changes box-drawing characters, padding, or wrapping turns
`tests/unit/golden/envelope.table.txt` red with **no change to our code**.

Two things keep that from being a mystery. The console is constructed with an
explicit `width=100`, `no_color=True`, `force_terminal=False` and
`box=SIMPLE`, so the only thing that can move the output is rich itself rather
than a terminal, a `COLUMNS` value, or a CI runner's width. And the table is
declared, in the ADR and in the module, to be a *convenience surface* — JSON is
the contract, nothing parses the table.

So: **if the table golden goes red immediately after a dependency bump and the
JSON and CSV goldens stay green, regenerate it rather than debugging it.** If the
JSON golden moves, that is a real contract change and the schema version is in
play. The split between the three goldens is what makes that diagnosis one
glance instead of an investigation.

Related: [[import-budget-and-the-fastpath]], [[typer-vendors-click]]
