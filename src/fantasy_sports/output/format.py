"""Which renderer runs, and who decides.

ADR-0004: JSON when stdout is not a TTY, a rich table when it is, CSV on
request, and an explicit ``--output`` overriding the detection in both
directions. Detection is a *default*, never an override — a caller who typed
``--output table`` into a pipe wants a table, and a caller who typed
``--output json`` at a terminal wants JSON.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Any

__all__ = ["OutputFormat", "resolve_format"]


class OutputFormat(StrEnum):
    """The three surfaces. Adding one is a user-facing contract change."""

    JSON = "json"
    TABLE = "table"
    CSV = "csv"


def resolve_format(requested: OutputFormat | str | None = None, stream: Any = None) -> OutputFormat:
    """Pick a format: ``requested`` if given, otherwise detect from ``stream``.

    A stream with no ``isatty`` — a ``StringIO``, a custom writer, a closed
    handle — counts as not a TTY, which is the safe default: a machine gets
    machine-readable output and the worst case is a human piping to a file.
    """
    if requested is not None:
        try:
            return OutputFormat(str(requested))
        except ValueError:
            valid = ", ".join(f.value for f in OutputFormat)
            raise ValueError(
                f"Unknown output format {requested!r}; expected one of {valid}"
            ) from None

    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    try:
        interactive = bool(isatty()) if callable(isatty) else False
    except (ValueError, OSError):
        # A detached or closed stream raises rather than answering. Not a TTY.
        interactive = False
    return OutputFormat.TABLE if interactive else OutputFormat.JSON
