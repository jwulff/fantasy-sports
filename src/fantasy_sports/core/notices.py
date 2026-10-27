"""Notices a successful run should surface, whichever command produced the envelope.

The browser re-import (jwulff/fantasy-sports#159) happens deep inside a provider,
and a command can reach a provider by several routes: ``open_read`` and then
``commands.context.success``, but also ``experts --league``, which builds its own
envelope around a provider it opened, and ``doctor --live``, which reads every
configured league. Threading ``provider.warnings`` through each of those would be
a convention that the next command forgets, so the provider also posts here and
the dispatch layer (``cli.app._dispatch``) folds whatever was posted into the
envelope it is about to render, for every command alike.

A process-wide list is right for a CLI invocation (one command, one process) and
is drained at the start and the end of every dispatch so an in-process caller
(the tests, a future long-lived MCP server) never sees a stale notice. Notices
are plain sentences and never carry a credential value.
"""

from __future__ import annotations

__all__ = ["add", "drain"]

_posted: list[str] = []


def add(notice: str) -> None:
    """Post ``notice`` once; a repeat of the same sentence is dropped."""
    if notice not in _posted:
        _posted.append(notice)


def drain() -> tuple[str, ...]:
    """Everything posted since the last drain, which is then forgotten."""
    taken = tuple(_posted)
    _posted.clear()
    return taken
