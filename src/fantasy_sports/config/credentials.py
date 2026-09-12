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

:func:`remove_credentials` is the write half, added for ``auth logout``
(jwulff/fantasy-sports#62). It edits only the ``[credentials]`` table and
carries every other table through untouched, because a leak remediation
that also wiped the user's league profiles would be its own incident. It
inverts the read's soft/hard split in one place: an *unreadable or
unwritable* file is raised, not swallowed, because a removal that silently
did nothing leaves the leaked value on disk while telling the user it is gone.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fantasy_sports.config import paths
from fantasy_sports.config.document import write_atomically
from fantasy_sports.core.errors import ConfigInvalidError

__all__ = ["load_credentials", "remove_credentials"]

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
        document = _parse(target)
    except OSError:
        # Absent, or environmental (permissions, a bad mount) rather than
        # malformed. Same class as the locked-Keychain fallthrough one link
        # earlier in the chain.
        return {}

    table = _table(document, target)
    if table is None:
        return {}

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


def remove_credentials(names: Iterable[str], path: Path | None = None) -> tuple[str, ...]:
    """Delete ``names`` from ``[credentials]``; return the names actually removed.

    Nothing is written unless at least one named key was present: a logout on
    a host that never used the config fallback must not create the file, touch
    its mtime, or rewrite it. When something *is* removed the whole document
    is round-tripped through ``tomli-w``, which keeps every other table and key
    but drops comments and hand formatting — TOML writers do not preserve
    them, and a credential left behind is worse than a comment lost. An
    emptied ``[credentials]`` table is dropped rather than left as a header.

    The rewrite is atomic (temp file, then ``replace``) and keeps the original
    file's mode, so a ``0600`` config stays ``0600``.

    :raises ConfigInvalidError: if the file is present but damaged, exactly as
        :func:`load_credentials` would — a file that cannot be parsed cannot
        be edited safely.
    :raises OSError: if the file exists but cannot be read or rewritten. The
        caller (``auth logout``) reports that link as unavailable rather than
        removed; swallowing it here would report a removal that did not happen.
    """
    # Resolved once: the read and the rewrite must land on the same referent
    # even if a symlinked config is retargeted between them.
    target = (paths.config_file() if path is None else path).resolve()
    wanted = set(names)
    try:
        document = _parse(target)
    except FileNotFoundError:
        return ()

    table = _table(document, target)
    if table is None:
        return ()
    removed = tuple(key for key in table if key in wanted)
    if not removed:
        return ()

    for key in removed:
        del table[key]
    if not table:
        del document[TABLE]
    write_atomically(target, document)
    return removed


def _parse(target: Path) -> dict[str, Any]:
    """Parse the whole document. Raises ``OSError`` for absent or unreadable.

    The two callers disagree about what an ``OSError`` means — soft for a
    read, hard for a removal — so the classification is theirs, not this
    helper's. Damage is ``ConfigInvalidError`` for both.
    """
    try:
        with target.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalidError(
            f"{target} is not valid TOML, so credentials could not be read.",
            remediation=f"Fix the TOML syntax in {target}. The parser reported: {exc}",
            details={"path": str(target), "table": TABLE},
        ) from exc


def _table(document: dict[str, Any], target: Path) -> dict[str, Any] | None:
    """The ``[credentials]`` table, ``None`` if absent, a raise if not a table."""
    if TABLE not in document:
        return None
    table = document[TABLE]
    if not isinstance(table, dict):
        raise ConfigInvalidError(
            f"[{TABLE}] in {target} is not a table.",
            remediation=f'Write credentials as a TOML table, e.g. [{TABLE}] then espn_s2 = "...".',
            details={"path": str(target), "table": TABLE, "found_type": type(table).__name__},
        )
    return table
