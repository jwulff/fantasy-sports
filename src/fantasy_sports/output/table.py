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


def _add_rows(table: Any, data: Any) -> None:
    if isinstance(data, Mapping):
        table.add_column("field")
        table.add_column("value")
        for key, value in data.items():
            table.add_row(str(key), _cell(value))
        return
    if isinstance(data, list) and all(isinstance(item, Mapping) for item in data):
        columns: list[str] = []
        for item in data:
            columns.extend(str(key) for key in item if str(key) not in columns)
        for name in columns:
            table.add_column(name)
        for item in data:
            table.add_row(*(_cell(item.get(name)) for name in columns))
        return
    table.add_column("value")
    for item in data:
        table.add_row(_cell(item))


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
        _add_rows(table, data if isinstance(data, list | Mapping) else [data])
        console.print(table)

    if payload["sources"]:
        console.print(
            "sources: "
            + ", ".join(
                f"{s['name']} {s['age_seconds']}s{' (cached)' if s['cached'] else ''}"
                for s in payload["sources"]
            )
        )
    return console.file.getvalue()
