#!/usr/bin/env python3
"""Fail CI when an ADR-0008 budget is exceeded.

Checked here: direct runtime dependency count and built wheel size. Cold start
lives in ``check_startup.py``; coverage is enforced by ``[tool.coverage.report]``.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

MAX_DIRECT_DEPENDENCIES = 5
MAX_WHEEL_BYTES = 150 * 1024

ROOT = Path(__file__).resolve().parent.parent


def check_dependencies() -> tuple[bool, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    ok = len(deps) <= MAX_DIRECT_DEPENDENCIES
    names = ", ".join(d.split(">")[0].split("=")[0].split("[")[0] for d in deps)
    return ok, f"{len(deps)}/{MAX_DIRECT_DEPENDENCIES} direct runtime dependencies ({names})"


def check_wheel(dist: Path) -> tuple[bool, str]:
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        return False, f"no wheel found in {dist}"
    size = max(w.stat().st_size for w in wheels)
    return size < MAX_WHEEL_BYTES, f"{size:,} bytes (budget {MAX_WHEEL_BYTES:,})"


def main() -> int:
    dist = ROOT / "dist"
    if not dist.exists():
        subprocess.run(["uv", "build", "--wheel"], cwd=ROOT, check=True, capture_output=True)
    results = [("direct dependencies", *check_dependencies()), ("wheel size", *check_wheel(dist))]
    failed = False
    for name, ok, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'} {name:<22} {detail}")
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
