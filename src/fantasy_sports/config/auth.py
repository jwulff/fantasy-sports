"""The ``[auth]`` table in ``config.toml``: behaviour switches for the auth layer.

Today it holds one key, ``auto_reimport`` (jwulff/fantasy-sports#159): whether a
refused ESPN request may re-read the user's browser for a newer cookie pair
(``auth/reimport.py``). It defaults to **on** because the failure it repairs is
silent -- ESPN rotates ``espn_s2`` while the browser session lives on, and the
stored copy drifts behind it -- and to off only on an explicit ``false``.

Same split as ``config/credentials.py``: *absent* (no file, no table, no key, or
a file that cannot be opened) is ordinary and returns the default; *damaged* (a
file that will not parse, an ``[auth]`` that is not a table, an
``auto_reimport`` that is not a boolean) raises ``CONFIG_INVALID``. The caller
on the failure path decides what damage means there -- see
``auth.reimport.opted_out``, which treats it as "do not import" so a typo can
never replace the original ESPN error.

``config.toml`` is a shared namespace, so this reads only its own table
(``docs/memory/config-toml-is-a-shared-namespace.md``). ``bool`` is an ``int``
in Python, but TOML has real booleans and a string ``"false"`` is not one; the
check below is by identity, not truthiness.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from fantasy_sports.config import paths
from fantasy_sports.core.errors import ConfigInvalidError

__all__ = ["TABLE", "auto_reimport_enabled"]

TABLE = "auth"
"""The top-level table these switches live in."""


def auto_reimport_enabled(path: Path | None = None) -> bool:
    """Whether ``[auth] auto_reimport`` allows a browser re-import. Default ``True``.

    :raises ConfigInvalidError: the file is present but damaged, naming the key.
    """
    target = paths.config_file() if path is None else path
    try:
        with target.open("rb") as handle:
            document: dict[str, Any] = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalidError(
            f"{target} is not valid TOML, so [{TABLE}] could not be read.",
            remediation=f"Fix the TOML syntax in {target}. The parser reported: {exc}",
            details={"path": str(target), "table": TABLE},
        ) from exc
    except OSError:
        # Absent, or environmental (permissions, a bad mount): the default.
        return True

    table = document.get(TABLE)
    if table is None:
        return True
    if not isinstance(table, dict):
        raise ConfigInvalidError(
            f"[{TABLE}] in {target} is not a table.",
            remediation=f"Write it as a TOML table: [{TABLE}] then auto_reimport = false.",
            details={"path": str(target), "table": TABLE},
        )
    value = table.get("auto_reimport", True)
    if not isinstance(value, bool):
        raise ConfigInvalidError(
            f"auto_reimport in {target} is a {type(value).__name__}, not a boolean.",
            remediation=f"Write it unquoted: auto_reimport = false under [{TABLE}].",
            details={"path": str(target), "table": TABLE, "key": "auto_reimport"},
        )
    return value
