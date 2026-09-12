"""Rewriting ``config.toml`` as a whole document.

``config.toml`` is a shared namespace (``docs/memory/config-toml-is-a-shared-
namespace.md``): the league profiles, the ``[credentials]`` fallback, and
whatever a later version adds all live in one file. Any writer that edits
its own table therefore has to carry every *other* table through untouched,
and both writers — ``credentials.remove_credentials`` and ``leagues.save`` —
do it with this one helper rather than a copy each
(jwulff/fantasy-sports#62, #85).

The rewrite is atomic (a temp file in the same directory, then ``replace``)
so no reader ever sees a partial document, and it keeps the existing file's
mode so a ``0600`` file holding cookies stays ``0600``. A file that does not
exist yet is created private (``0600``, ``mkstemp``'s own default), because
the next ``auth login`` may put cookies in it.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["write_atomically"]


def write_atomically(target: Path, document: dict[str, Any]) -> None:
    """Serialize ``document`` over ``target`` without a window where it is partial."""
    import tomli_w

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(tomli_w.dumps(document))
        if mode is not None:
            temp.chmod(mode)
        temp.replace(target)
    except BaseException:  # pragma: no cover - defensive cleanup
        temp.unlink(missing_ok=True)
        raise
