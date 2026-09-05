"""The JSON renderer — the contract surface, and the only lossless one.

Two choices worth stating, because both look arbitrary until they are not:

* **``ensure_ascii=True``.** Every byte written is ASCII, so the output survives
  any stdout encoding a consumer's shell, cron daemon or CI runner happens to
  set. A team name with an emoji in it is a real payload, and a
  ``UnicodeEncodeError`` at write time would be a failure with no taxonomy code
  and no useful message. The JSON escapes parse back to the identical string.
* **``indent=2`` with a trailing newline.** A parser does not care and a human
  reading a broken pipe does. The trailing newline makes the output a
  well-formed line-oriented file.

This module is named ``json`` inside the ``output`` package and still imports
the standard library's ``json``: Python 3 has no implicit relative imports, so
``import json`` here resolves to the top-level module.
"""

from __future__ import annotations

import json

from fantasy_sports.output.envelope import Envelope

__all__ = ["render"]


def render(envelope: Envelope) -> str:
    """Render ``envelope`` as one JSON document, newline-terminated."""
    return json.dumps(envelope.to_dict(), indent=2, ensure_ascii=True) + "\n"
