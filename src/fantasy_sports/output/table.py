"""The rich-table renderer — what a human gets at a terminal.

**``rich`` is imported inside :func:`render`, never at module scope.** It is not
a direct dependency: it arrives through ``typer``, and ADR-0008 caps this
project at five direct dependencies, which it already spends. Importing it at
module scope would also put it on the ``--help`` path, where it costs a
budget-relevant chunk of the 50 ms cold start for output nobody asked for.
``tests/unit/test_output.py`` asserts both halves — that no module under
``output/`` imports rich at module scope, and that rendering JSON or CSV in a
fresh interpreter leaves rich out of ``sys.modules``.

The table restates the envelope's provenance — provider, league, season,
generation time, and the age of the oldest contributing fetch — in a header
line above the rows, because origin R4 says every response states its data age
and a human reading a terminal is a consumer too.

The layout is a fixed 100 columns so it is reproducible in a golden file. It is
a convenience surface: JSON is the contract, and nothing should parse this.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import Any

from fantasy_sports.output.envelope import Envelope

__all__ = ["render"]

WIDTH = 100


def _cell(value: Any) -> str:
    """One table cell, as text. Nested structures render as compact JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _header_line(payload: Mapping[str, Any]) -> str:
    parts = [payload["provider"] or "-"]
    if payload["league_id"] is not None:
        parts.append(f"league {payload['league_id']}")
    if payload["season"] is not None:
        parts.append(f"season {payload['season']}")
    parts.append(f"generated {payload['generated_at']}")
    age = payload["data_age_seconds"]
    parts.append("data age unknown" if age is None else f"data age {age}s")
    return " · ".join(parts)


OMITTED_COLUMNS = frozenset({"raw"})
"""Keys the table leaves out. ``raw`` only, and only here.

Every normalized object carries the provider's own sub-object (``CLAUDE.md``
rule 3), which is right for the contract and ruinous for a hundred-column
terminal: one ESPN team payload wraps over a dozen lines and squeezes ``name``
and ``wins`` into six characters each, so ``standings`` at a TTY became
unreadable the moment real commands started emitting real objects
(jwulff/fantasy-sports#9). JSON and CSV still carry it, and :func:`render`
prints a line saying where it went — the table is the convenience surface, and
a convenience nobody can read is not one.
"""


def _visible(item: Mapping[str, Any]) -> dict[str, Any]:
    """``item`` without its omitted keys, at every depth.

    Recursive because the worst offender is nested: a roster slot's ``raw`` is
    modest, and the ``player`` it contains has a ``raw`` of its own that fills
    the cell on its own.
    """
    return {
        str(key): _visible_value(value)
        for key, value in item.items()
        if str(key) not in OMITTED_COLUMNS
    }


def _visible_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _visible(value)
    if isinstance(value, list):
        return [_visible_value(item) for item in value]
    return value


def _add_rows(table: Any, data: Any) -> bool:
    """Fill ``table`` from ``data``; return whether a column was omitted."""
    if isinstance(data, Mapping):
        table.add_column("field")
        table.add_column("value")
        shown = _visible(data)
        for key, value in shown.items():
            table.add_row(key, _cell(value))
        return shown != dict(data)
    if isinstance(data, list) and all(isinstance(item, Mapping) for item in data):
        columns: list[str] = []
        omitted = False
        rows = []
        for item in data:
            shown = _visible(item)
            omitted = omitted or shown != dict(item)
            columns.extend(key for key in shown if key not in columns)
            rows.append(shown)
        for name in columns:
            table.add_column(name)
        for row in rows:
            table.add_row(*(_cell(row.get(name)) for name in columns))
        return omitted
    table.add_column("value")
    for item in data:
        table.add_row(_cell(item))
    return False


def render(envelope: Envelope) -> str:
    """Render ``envelope`` as a header line plus a table."""
    from rich.box import SIMPLE
    from rich.console import Console
    from rich.table import Table

    payload = envelope.to_dict()
    console = Console(
        file=io.StringIO(),
        width=WIDTH,
        force_terminal=False,
        no_color=True,
        highlight=False,
        soft_wrap=False,
        legacy_windows=False,
    )
    console.print(_header_line(payload))

    data = payload["data"]
    if data is None or (isinstance(data, list | Mapping) and not data):
        console.print("(no rows)")
    else:
        table = Table(box=SIMPLE, pad_edge=False, show_edge=False)
        omitted = _add_rows(table, data if isinstance(data, list | Mapping) else [data])
        console.print(table)
        if omitted:
            console.print(
                "`raw` omitted from this table; use --output json for the provider payload."
            )

    if payload["sources"]:
        console.print(
            "sources: "
            + ", ".join(
                f"{s['name']} {s['age_seconds']}s{' (cached)' if s['cached'] else ''}"
                for s in payload["sources"]
            )
        )
    return console.file.getvalue()
