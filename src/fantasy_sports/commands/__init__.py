"""The command registry.

Commands are plain typed functions. The typer CLI and the future MCP server are
both thin projections over this registry (ADR-0003), so **nothing in this
package may import typer**. A test enforces that.

Each entry is a :class:`CommandSpec`. ``handler`` is a dotted path resolved
lazily at call time, so building the registry — and therefore rendering
``--help`` — never imports a provider, an HTTP stack, or a renderer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandSpec:
    """One command, independent of how it is invoked."""

    name: str
    """The command's own name, e.g. ``standings``."""

    summary: str
    """One line, written for an agent reading ``--help`` as its documentation."""

    handler: str
    """Dotted path to the implementing function, e.g.
    ``fantasy_sports.commands.league:standings``. Resolved on demand."""

    group: str | None = None
    """Sub-command group, e.g. ``auth``. ``None`` means top level."""

    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> tuple[str, ...]:
        return (self.group, self.name) if self.group else (self.name,)

    @property
    def invocation(self) -> str:
        return " ".join(self.path)

    def resolve(self) -> Callable[..., Any]:
        """Import and return the implementing function."""
        from importlib import import_module

        module_name, _, attr = self.handler.partition(":")
        if not attr:
            raise ValueError(f"handler must be 'module:function', got {self.handler!r}")
        return getattr(import_module(module_name), attr)


REGISTRY: dict[str, CommandSpec] = {}
"""Every command, keyed by its full invocation (``"auth status"``).

Empty until the read commands land (jwulff/fantasy-sports#9). The CLI and its
help output are generated from whatever is registered here, so a command
becomes visible in both surfaces by being registered and nowhere else.
"""


def register(spec: CommandSpec) -> CommandSpec:
    """Add ``spec`` to the registry. Raises on a duplicate invocation."""
    if spec.invocation in REGISTRY:
        raise ValueError(f"command {spec.invocation!r} is already registered")
    REGISTRY[spec.invocation] = spec
    return spec


def groups() -> list[str]:
    """Registered sub-command group names, sorted."""
    return sorted({spec.group for spec in REGISTRY.values() if spec.group})


def top_level() -> list[CommandSpec]:
    """Registered commands that sit at the top level, sorted by name."""
    return sorted((s for s in REGISTRY.values() if not s.group), key=lambda s: s.name)


def in_group(group: str) -> list[CommandSpec]:
    """Registered commands inside ``group``, sorted by name."""
    return sorted((s for s in REGISTRY.values() if s.group == group), key=lambda s: s.name)
