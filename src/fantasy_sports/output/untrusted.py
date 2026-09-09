"""Rendering untrusted ESPN free text into markdown — ADR-0007, jwulff/fantasy-sports#17.

Every other renderer in this package (``json.py``, ``csv.py``, ``table.py``)
already carries untrusted content safely, because each one hands the value to
a real format library — ``json.dumps``, ``csv.writer``, ``rich.text.Text`` —
that escapes it structurally rather than by convention. Markdown has no such
library on this project's dependency budget (ADR-0008), and markdown's own
containers are conventions a hostile string can defeat: a fenced code block's
terminator is itself three backticks, and a team name that contains three
backticks closes the fence early and lets whatever follows render as ordinary
markdown — headers, links, an ``@mention`` that pings someone, a ``#123`` that
cross-references an issue.

**The fix ADR-0007 already decided on is indentation, not fencing.** A
markdown *indented* code block has no terminator token at all — it is
delimited by the absence of indentation on a following line — so there is
nothing for injected content to close early. :func:`render_untrusted_block`
is the one place that convention is implemented, so every future caller (the
client error reporter ADR-0007 describes, a scheduled report, anything else
that renders ESPN free text into a GitHub issue body) reaches for this
instead of re-deriving the same fence-escape bug once each.

Nothing here imports anything beyond the standard library.
"""

from __future__ import annotations

__all__ = ["render_untrusted_block"]


def render_untrusted_block(text: str) -> str:
    """Render ``text`` as a markdown indented code block.

    Every line — including a blank one — gets exactly four leading spaces,
    unconditionally. That is what makes this safe: an indented block is
    delimited by *the presence of indentation*, not by a token the block's
    own content could contain, so there is no character sequence ``text`` can
    hold that closes the block early. Contrast a fenced block, whose
    terminator is three backticks the content might already contain.

    The result is not itself a complete markdown document — a caller embeds
    it inside a larger issue body or report, typically after a blank line so
    the block starts clean. Round-tripping is exact: stripping the leading
    four spaces from every line of the result reproduces ``text`` exactly,
    so nothing this function does is lossy.

    :param text: Untrusted ESPN free text — a team name, a league name, a
        trade note, anything R1a labels in the envelope's ``untrusted`` map.
    """
    lines = text.split("\n")
    return "\n".join(f"    {line}" for line in lines)
