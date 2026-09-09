"""The live ESPN canary — detection only (ADR-0005, jwulff/fantasy-sports#11).

Not part of the shipped package: excluded from ``[tool.hatch.build.targets.wheel]``
because nothing here is needed by an installed ``fantasy-sports``. It exists
purely so ``.github/workflows/canary.yml`` and ``pytest`` can import it.

See ``scripts/canary/README.md`` for the design and what is deliberately out
of scope.
"""
