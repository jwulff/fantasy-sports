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

**A cell that starts like a formula gets a leading apostrophe.** ``csv.writer``
quotes a cell correctly for any CSV *parser*, but Excel, Numbers and Sheets
are not parsers first — they evaluate a cell whose text begins with ``=``,
``+``, ``-``, ``@``, a tab or a carriage return, quoted or not. Team and
league names are set by any league member (the ``untrusted`` map exists for
exactly that reason, ADR-0007), so a team named ``=HYPERLINK(...)`` would
become a live formula the moment someone opened the export. The defence is
the OWASP one: prefix such a cell with a single quote, which spreadsheets
read as "this is text". It is applied to *every* string cell, not only the
labelled ones, because the label is advisory and the cost of a false
positive is one visible apostrophe. A consumer that parses the CSV by machine
strips the leading ``'``; the README states this as part of the contract.

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


# What a spreadsheet treats as the start of a formula: OWASP's CSV-injection
# list. ``-`` is on it, which is why only source ``str`` values are guarded —
# a numeric ``-3.5`` is handed to ``csv.writer`` as a float and written as
# ``-3.5``, and a spreadsheet reads that back as a number, not a formula.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _guard(text: str) -> str:
    """Neutralise a string cell that a spreadsheet would evaluate as a formula.

    Only the first character matters: ``a=b`` is inert, ``=b`` is not. The
    guard is a single leading apostrophe, applied unconditionally to every
    ``str`` cell that begins with a trigger — untrusted-labelled or not — so
    that safety never depends on the labelling being complete.
    """
    if text.startswith(_FORMULA_TRIGGERS):
        return f"'{text}"
    return text


def _cell(value: Any) -> Any:
    """One CSV cell. Scalars pass through; anything nested becomes compact JSON.

    Strings are formula-guarded (see :func:`_guard`); numbers and booleans are
    handed to ``csv.writer`` as-is, which stringifies them itself, so a
    negative number never grows an apostrophe. Nested values become JSON,
    which always starts with ``[`` or ``{`` and so needs no guard.
    """
    if isinstance(value, str):
        return _guard(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return json.dumps(value, ensure_ascii=True)


def _rows(data: Any) -> tuple[list[str], list[list[Any]]]:
    """Header and rows for a payload, without guessing what is inside it."""
    if data is None:
        return [], []
    if isinstance(data, Mapping):
        return [_guard(str(key)) for key in data], [[_cell(value) for value in data.values()]]
    if isinstance(data, list):
        if not data:
            return [], []
        if all(isinstance(item, Mapping) for item in data):
            header: list[str] = []
            for item in data:
                header.extend(str(key) for key in item if str(key) not in header)
            rows = [[_cell(item.get(key)) for key in header] for item in data]
            return [_guard(key) for key in header], rows
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
