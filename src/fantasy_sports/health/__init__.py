"""The client-side health system — ADR-0005 §11.3-§11.4, jwulff/fantasy-sports#10.

:mod:`fantasy_sports.health.manifest` parses ``health.json``;
:mod:`fantasy_sports.health.client` fetches it (fail-open, cached, opt-outable)
and folds it into a failing command's error envelope. ``fantasy_sports.commands
.doctor`` is the proactive projection: the same manifest fetch, forced, plus
every other check ARCHITECTURE §11.4 names.

Nothing here imports ``requests``, ``typer``, or ``espn_api`` at module scope.
"""

from __future__ import annotations
