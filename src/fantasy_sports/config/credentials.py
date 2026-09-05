"""The ``[credentials]`` table in ``config.toml`` (ARCHITECTURE §6).

The last link of the credential chain. ``auth/chain.py`` used to read this
file itself with its own ``tomllib`` call and its own XDG helper, because the
config layer did not exist when that branch was cut
(jwulff/fantasy-sports#35). It does now, and every layer that reads
``config.toml`` reads it from here.

**This reader fails soft for absence and hard for damage** — the split is
the point, and it is drawn where the user's ability to act changes
(jwulff/fantasy-sports#37).

*Absent* is ordinary and stays quiet. No config file, no ``[credentials]``
table: the overwhelmingly common case is a host configured entirely through
environment variables, and this reader is one link in a chain whose whole
design is that each later link falls through
(``auth/chain.py``). The end of the chain already reports ``AUTH_MISSING``
with a remediation. Raising on absence would convert a normal condition into
a hard failure on exactly the headless hosts the config fallback exists for.

*Damaged* is not ordinary and must say so. A file that will not parse, a
``[credentials]`` table that is not a table, a credential whose value is not
a string: each is a mistake in a file the user can fix in seconds. Failing
soft on those reports ``AUTH_MISSING``, which sends the human off to
re-extract a cookie they already have while the real problem — a typo three
lines away — is never mentioned. That is what
:class:`~fantasy_sports.core.errors.ConfigInvalidError` exists to say: user
fixable, not retryable.

An *unreadable* file (permissions, a bad mount) stays soft. It is an
environmental condition rather than a malformed one, and it is the same class
as the locked-Keychain fallthrough one link earlier in the chain.

``config.toml`` is a shared namespace, so this reads only its own table and
ignores everything else (``docs/memory/config-toml-is-a-shared-namespace.md``).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from fantasy_sports.config import paths
from fantasy_sports.core.errors import ConfigInvalidError

__all__ = ["load_credentials"]

TABLE = "credentials"
"""The top-level table credential values live in."""


def load_credentials(path: Path | None = None) -> Mapping[str, str]:
    """Parse the ``[credentials]`` table out of ``config.toml``.

    Returns an empty mapping when the file or the table is simply absent, or
    when the file cannot be opened at all. Raises
    :class:`~fantasy_sports.core.errors.ConfigInvalidError` when the file is
    present but damaged, naming the file and the offending key.

    A non-string value is a raise rather than a silent drop. Coercing it and
    handing the result to ESPN produces an ambiguous 401; dropping it produces
    an equally ambiguous ``AUTH_MISSING``. Neither tells the human that TOML
    read their credential as an integer because they forgot the quotes.
    """
    target = paths.config_file() if path is None else path
    try:
        with target.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except OSError:
        # Environmental, not malformed. Same class as the locked-Keychain
        # fallthrough one link earlier in the chain.
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalidError(
            f"{target} is not valid TOML, so credentials could not be read.",
            remediation=f"Fix the TOML syntax in {target}. The parser reported: {exc}",
            details={"path": str(target), "table": TABLE},
        ) from exc

    if TABLE not in document:
        return {}
    table = document[TABLE]
    if not isinstance(table, dict):
        raise ConfigInvalidError(
            f"[{TABLE}] in {target} is not a table.",
            remediation=f'Write credentials as a TOML table, e.g. [{TABLE}] then espn_s2 = "...".',
            details={"path": str(target), "table": TABLE, "found_type": type(table).__name__},
        )

    credentials: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str):
            raise ConfigInvalidError(
                f'Credential "{key}" in {target} is a {type(value).__name__}, not a string.',
                remediation=f'Quote the value: {key} = "..." under [{TABLE}] in {target}.',
                details={"path": str(target), "table": TABLE, "key": key},
            )
        credentials[key] = value
    return credentials
