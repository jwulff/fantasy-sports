"""The CSV renderer — the spreadsheet surface, and deliberately lossy.

**CSV carries the rows and nothing else.** No schema version, no provider, no
data age, no error. That is a decision, not an oversight: a file that mixes
metadata lines into a CSV body is not a CSV any reader parses correctly, and
repeating the same envelope fields as columns on every row is redundancy a
consumer would immediately have to strip. ``--output csv`` is opt-in, so a
caller asking for it has chosen the spreadsheet over the contract. JSON is the
contract; the table states the provenance for a human; CSV is for pasting into
a sheet.

**A cell holds a scalar.** Anything nested — ``raw``, a player mapping, a list
of eligible slots — is rendered as compact JSON in a single cell. The
alternative, flattening nested mappings into dotted columns, sounds tidier and
is not: every normalized object carries ``raw`` (``CLAUDE.md`` rule 3), so
dotted flattening would turn a roster into several hundred columns of ESPN
internals. One dense cell loses nothing and stays readable.

Named ``csv`` inside the ``output`` package; ``import csv`` here still resolves
to the standard library, because Python 3 has no implicit relative imports.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from typing import Any

from fantasy_sports.output.envelope import Envelope

__all__ = ["render"]


def _cell(value: Any) -> Any:
    """One CSV cell. Scalars pass through; anything nested becomes compact JSON."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, ensure_ascii=True)


def _rows(data: Any) -> tuple[list[str], list[list[Any]]]:
    """Header and rows for a payload, without guessing what is inside it."""
    if data is None:
        return [], []
    if isinstance(data, Mapping):
        return [str(key) for key in data], [[_cell(value) for value in data.values()]]
    if isinstance(data, list):
        if not data:
            return [], []
        if all(isinstance(item, Mapping) for item in data):
            header: list[str] = []
            for item in data:
                header.extend(str(key) for key in item if str(key) not in header)
            return header, [[_cell(item.get(key)) for key in header] for item in data]
        return ["value"], [[_cell(item)] for item in data]
    return ["value"], [[_cell(data)]]


def render(envelope: Envelope) -> str:
    """Render the envelope's payload as RFC 4180 CSV, or ``""`` when empty."""
    header, rows = _rows(envelope.to_dict()["data"])
    if not header:
        return ""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()
