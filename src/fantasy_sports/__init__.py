"""fantasy-sports: an agent-native CLI for fantasy sports leagues.

Import this package cheaply. Nothing here — and nothing on the path to
``--version`` or ``--help`` — may import ``espn_api``, ``requests``,
``rich``, ``typer``, or ``keyring``. See ``CLAUDE.md`` for the budget those
imports are held to and ``tests/unit/test_imports.py`` for its enforcement.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
