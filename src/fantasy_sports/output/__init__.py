"""The output layer — the versioned contract every consumer parses.

``docs/ARCHITECTURE.md`` §5 and ADR-0004. This package is a plain layer with no
CLI in it: nothing here imports ``typer``, and commands are wired to it by
jwulff/fantasy-sports#9, so the same rendering serves the future MCP projection
without a rewrite (ADR-0003).

Three rules this module enforces, all of them about a *program* reading the
output:

1. **A failure writes nothing to stdout.** A consumer piping stdout into a
   parser must never receive half a payload followed by an error. The error
   goes to stderr, stdout stays byte-empty, and the exit status is nonzero.
2. **A failure is always JSON**, whatever ``--output`` asked for. An agent must
   be able to parse a failure the same way every time; a table-formatted error
   is prose again, which is what the taxonomy exists to escape.
3. **Detection is a default, not an override.** JSON when stdout is not a TTY,
   a table when it is, and an explicit format wins in both directions.
"""

from __future__ import annotations

import sys
from typing import Any

from fantasy_sports.output import csv as csv_renderer
from fantasy_sports.output import json as json_renderer
from fantasy_sports.output import table as table_renderer
from fantasy_sports.output.envelope import SCHEMA, DataSource, Envelope
from fantasy_sports.output.errors import (
    EXIT_CODES,
    EXIT_OK,
    EXIT_UNEXPECTED,
    classify,
    exit_code_for,
)
from fantasy_sports.output.format import OutputFormat, resolve_format

__all__ = [
    "EXIT_CODES",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "SCHEMA",
    "DataSource",
    "Envelope",
    "OutputFormat",
    "classify",
    "emit",
    "emit_failure",
    "exit_code_for",
    "render",
    "resolve_format",
]

_RENDERERS = {
    OutputFormat.JSON: json_renderer.render,
    OutputFormat.TABLE: table_renderer.render,
    OutputFormat.CSV: csv_renderer.render,
}


def render(envelope: Envelope, fmt: OutputFormat | str | None = None, *, stream: Any = None) -> str:
    """Render ``envelope`` in ``fmt``, or in the format ``stream`` implies."""
    return _RENDERERS[resolve_format(fmt, stream)](envelope)


def emit(
    envelope: Envelope,
    *,
    fmt: OutputFormat | str | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Write ``envelope`` to the right stream and return the process exit status.

    A success renders to stdout in the resolved format and returns ``0``. A
    failure renders as JSON to stderr, writes **nothing** to stdout, and returns
    that code's exit status.
    """
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    if envelope.error is not None:
        stderr.write(json_renderer.render(envelope))
        return exit_code_for(envelope.error)

    stdout.write(render(envelope, fmt, stream=stdout))
    return EXIT_OK


def emit_failure(
    exc: BaseException,
    *,
    provider: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Classify ``exc``, wrap it in an envelope, and emit it as a failure.

    The single entry point a command's error handling needs: an exception that
    already carries a taxonomy code keeps it, and anything else becomes
    ``PROVIDER_UNAVAILABLE`` rather than a traceback (see
    :func:`fantasy_sports.output.errors.classify`).
    """
    envelope = Envelope.failure(
        classify(exc), provider=provider, league_id=league_id, season=season
    )
    return emit(envelope, stdout=stdout, stderr=stderr)
