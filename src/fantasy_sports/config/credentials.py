"""The ``[credentials]`` table in ``config.toml`` (ARCHITECTURE §6).

The last link of the credential chain. ``auth/chain.py`` used to read this
file itself with its own ``tomllib`` call and its own XDG helper, because the
config layer did not exist when that branch was cut
(jwulff/fantasy-sports#35). It does now, and every layer that reads
``config.toml`` reads it from here.

**This reader fails soft, and that is not an oversight.** Unlike
:func:`fantasy_sports.config.leagues.load`, a missing, unreadable, or
malformed config file produces an empty mapping rather than
:class:`~fantasy_sports.core.errors.ConfigInvalidError`. The two readers want
opposite things from the same file:

* ``leagues.load()`` is asked "what is configured?" — a file it cannot parse
  is a real answer to give the user, under ``CONFIG_INVALID``.
* this reader is one link in a chain whose *whole design* is that each later
  link falls through quietly (``auth/chain.py``). An unreadable file here is
  an absent credential, which the end of the chain already reports as
  ``AUTH_MISSING`` with a remediation. Raising would convert a soft,
  recoverable condition into a hard failure on exactly the headless hosts the
  config fallback exists for.

``config.toml`` is a shared namespace, so this reads only its own table and
ignores everything else (``docs/memory/config-toml-is-a-shared-namespace.md``).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from fantasy_sports.config import paths

__all__ = ["load_credentials"]

TABLE = "credentials"
"""The top-level table credential values live in."""


def load_credentials(path: Path | None = None) -> Mapping[str, str]:
    """Parse the ``[credentials]`` table out of ``config.toml``. Fails soft.

    Non-string values are dropped rather than coerced: a credential that TOML
    parsed as an integer or a boolean is a mistake in the file, and handing a
    coerced version of it to ESPN would produce an ambiguous 401 instead of a
    diagnosable absence.
    """
    target = paths.config_file() if path is None else path
    try:
        with target.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    table = document.get(TABLE)
    if not isinstance(table, dict):
        return {}
    return {key: value for key, value in table.items() if isinstance(value, str)}
