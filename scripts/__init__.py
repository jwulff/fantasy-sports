"""Repo-support scripts — never part of the shipped wheel.

Made a regular package (rather than left as loose standalone files, which is
how the rest of ``scripts/`` works) only so ``scripts.canary`` is importable
from tests and from ``.github/workflows/canary.yml``. See
``scripts/canary/README.md``.
"""
