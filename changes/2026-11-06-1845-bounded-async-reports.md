# Bound expert reports for async consumers

*Date: 2026-11-06 1845*

## Why
Refs #184. Fantasy monitoring completion notices were repeatedly truncated or ambiguous when full FantasyPros position payloads were handed to an async supervisor. A bounded, one-line JSON mode lets the caller keep the result parseable and within a predictable output budget.

## How
Added `--limit` to the expert rankings and projections commands. It bounds only normalized rows after the complete upstream read, preserving provider coverage and making the caller's bound explicit. Added `--output json-compact`, which emits the same versioned envelope as one newline-terminated JSON document without indentation. Documented the recommended async invocation: `--limit N --output json-compact --no-raw`.

## Key Design Decisions
- The upstream request remains unchanged: an output bound must not be mistaken for provider pagination or completeness.
- Default output and existing `json`, `table`, and `csv` modes remain unchanged.
- Roster state, writes, credentials, and supervisor implementation remain out of scope.

## Validation
- `uv run pytest -q` — 2,812 passed, 20 skipped before generated-help refresh; the two expected generated-sample mismatches were refreshed and the affected 565-test suite passed afterward.
- `uv run pytest tests/unit/test_readme_samples.py tests/unit/test_experts.py tests/unit/test_output.py -q` — 568 passed.
- `uv run ruff format` on changed Python files — passed.
- `uv run ruff check src tests/unit/test_experts.py tests/unit/test_output.py` — passed.
- `git diff --check` — passed.

## What's Next
The OpenClaw root delivery issue was separately fixed by disabling `exec.notifyOnExit`. This change remains useful for bounded machine-readable reports and does not alter platform delivery settings.
