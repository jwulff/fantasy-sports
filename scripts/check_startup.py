#!/usr/bin/env python3
"""Fail CI when cold start exceeds the ADR-0008 budget.

Measures the real console script, not an in-process import, because the budget
is about what a cron job or an agent actually pays. Reports the minimum of
several runs: the minimum is the honest floor, and a shared CI runner's noisy
maximum would make the gate flap.

The plan named ``hyperfine``; a min-of-N subprocess loop is used instead so the
gate needs no Rust toolchain in CI and runs identically on a laptop. Same
measurement, one fewer dependency.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

BUDGET_MS = 50.0
RUNS = 15
ROOT = Path(__file__).resolve().parent.parent


def entry_point() -> str:
    for candidate in (
        ROOT / ".venv" / "bin" / "fantasy-sports",
        ROOT / ".venv" / "Scripts" / "fantasy-sports.exe",
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("fantasy-sports")
    if not found:
        raise SystemExit("fantasy-sports is not installed; run `uv sync` first")
    return found


def measure(args: list[str]) -> float:
    exe = entry_point()
    timings = []
    for _ in range(RUNS):
        start = time.perf_counter()
        proc = subprocess.run([exe, *args], capture_output=True)
        timings.append((time.perf_counter() - start) * 1000)
        if proc.returncode != 0:
            raise SystemExit(f"{exe} {' '.join(args)} exited {proc.returncode}")
    return min(timings)


def main() -> int:
    failed = False
    for args in (["--version"], ["--help"]):
        best = measure(args)
        ok = best < BUDGET_MS
        failed |= not ok
        print(
            f"{'ok  ' if ok else 'FAIL'} cold start {' '.join(args):<12} "
            f"{best:6.1f} ms (budget {BUDGET_MS:.0f} ms)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
