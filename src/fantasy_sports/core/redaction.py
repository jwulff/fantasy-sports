"""The process-global credential scrub set (CLAUDE.md rule 5).

This lives in ``core/`` rather than in ``auth/`` for one structural reason:
:class:`~fantasy_sports.core.errors.FantasySportsError` scrubs its message at
construction, and ``core/`` cannot import ``auth/`` — ``auth/chain.py`` imports
``core.models.CredentialSpec``, so the dependency has to run one way. The
registry is the shared part; :class:`~fantasy_sports.auth.chain.Secret`, which
is a credential *carrier*, stays in ``auth/``.

``docs/memory/credential-leak-channels.md`` records why this is a base-class
concern. The leak is rarely written by the code holding the credential; it is
written by a caller three layers away who interpolates a request URL into an
error message. Scrubbing at every raise site is the thing that gets forgotten,
so it happens once, in the constructor everything funnels through.

Nothing here imports anything beyond the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "REDACTED",
    "forget_secrets",
    "redact",
    "remember_secret",
    "scrub",
]

REDACTED = "***redacted***"

_MIN_SCRUBBABLE = 8
"""Values shorter than this are not added to the scrub set.

Scrubbing a three-character value out of arbitrary text would corrupt
unrelated words far more often than it would protect anything. A real ESPN
credential is orders of magnitude longer than this floor.
"""

_KNOWN_SECRETS: set[str] = set()
"""Every credential value this process has wrapped in a ``Secret``.

Process-global on purpose. Redaction has to work on text the credential was
merely *interpolated into* — a request URL, a formatted exception, a log line
assembled three layers away — and at that point the only thing linking the
text to the secret is the value itself.
"""


def remember_secret(value: str) -> None:
    """Register ``value`` so :func:`redact` will scrub it out of any text."""
    if value and len(value) >= _MIN_SCRUBBABLE:
        _KNOWN_SECRETS.add(value)


def forget_secrets() -> None:
    """Empty the scrub set. Exists for tests; nothing in production calls it."""
    _KNOWN_SECRETS.clear()


def redact(text: str) -> str:
    """Replace every known credential value in ``text`` with :data:`REDACTED`.

    Longest-first, so a value that contains another value cannot leave a
    fragment behind.
    """
    if not _KNOWN_SECRETS:
        return text
    for value in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def scrub(value: Any) -> Any:
    """:func:`redact` applied through a nested structure, leaving shape intact.

    Error ``details`` are field names, paths, and status codes — but they are
    assembled by callers, and a caller is exactly who puts a credential
    somewhere it does not belong. Non-string scalars are returned untouched.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item) for item in value)
    return value
