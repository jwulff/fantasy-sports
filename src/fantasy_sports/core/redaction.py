"""Credential scrubbing (CLAUDE.md rule 5), in two mechanisms.

They answer different questions and neither replaces the other:

**The registry** — :func:`remember_secret`, :func:`redact`, :func:`scrub` —
blanks values this process was *handed*. Right for our own ``espn_s2`` and
``SWID``, which arrive through the auth chain and are wrapped in a
:class:`~fantasy_sports.auth.chain.Secret` on the way in.

**The patterns** — :data:`CREDENTIAL_PATTERNS`,
:func:`scrub_credential_patterns` — match the credential *shape*. Right for
values we never held, which is the case the registry structurally cannot
cover: ESPN echoes **other league members'** SWID GUIDs inline in roster
payloads, and you cannot register a secret you have never seen. These landed
with the cassette scrub-before-write hook (jwulff/fantasy-sports#12) and were
promoted here so the HTTP cache (#8) could redact a response body before
writing it to SQLite. ``tests/conftest.py`` imports them rather than defining
its own; the repo-wide fixture *scanner* stays in tests, where it belongs.

This module lives in ``core/`` rather than in ``auth/`` for one structural reason:
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

import gzip
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "CREDENTIAL_PATTERNS",
    "CREDENTIAL_PLACEHOLDER",
    "CREDENTIAL_QUERY_PARAMS",
    "REDACTED",
    "SWID_PLACEHOLDER",
    "UnscrubbableResponseError",
    "decode_body",
    "forget_secrets",
    "redact",
    "remember_secret",
    "scrub",
    "scrub_body",
    "scrub_credential_patterns",
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


# --------------------------------------------------------------------------- #
# Pattern-based scrubbing: credentials we were never handed
# --------------------------------------------------------------------------- #

CREDENTIAL_PLACEHOLDER = "REDACTED"
"""What a pattern-scrubbed value is replaced with.

Deliberately *not* :data:`REDACTED`. It is chosen so that no pattern below can
match it — ``espn_s2=REDACTED`` has to read as clean, or a second scrubbing
pass would keep finding the placeholder it just wrote.
"""

SWID_PLACEHOLDER = "{SWID-REDACTED}"
"""SWID GUIDs keep their brace shape so a scrubbed payload stays parseable."""

CREDENTIAL_QUERY_PARAMS = frozenset({"espn_s2", "swid"})
"""Query-parameter names that carry a credential when ESPN is called by URL.

Lower-cased; compare against ``name.lower()``. The cache excludes these from
its key so a rotated cookie does not invalidate every entry, and strips them
from the URL it stores.
"""

# A SWID is a brace-wrapped GUID: {1A2B3C4D-5E6F-...}. This is the shape ESPN
# echoes inside roster/owner payloads, which is the case header filtering misses.
_SWID_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"
)

# A GUID sitting under a ``swid``-ish key without its braces.
_SWID_KEYED_RE = re.compile(
    r"""(swid)                       # the key
        (["']?\s*[:=]\s*["']?)       # separator, quoted on either side or not
        (?!REDACTED\b)
        [0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ``espn_s2=<blob>``. The value class excludes ``$``, ``{`` and ``}`` so that a
# CI expression such as ``ESPN_S2: ${{ secrets.ESPN_S2 }}`` is not a finding.
_ESPN_S2_RE = re.compile(
    r"""(espn_s2)
        (["']?\s*[:=]\s*["']?)
        (?!REDACTED\b)
        [^\s"';,&{}$\[\]]{8,}
    """,
    re.IGNORECASE | re.VERBOSE,
)

CREDENTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "SWID GUID": _SWID_GUID_RE,
    "GUID under a swid key": _SWID_KEYED_RE,
    "espn_s2 value": _ESPN_S2_RE,
}
"""Name -> pattern. Names appear in failure output; matched text never does."""


class UnscrubbableResponseError(RuntimeError):
    """A body could not be read, so it could not be proven scrubbed.

    Raised instead of recording or caching it. A regex over bytes it cannot
    decode finds nothing: the body "passes", and the credential is on disk.
    ``requests`` sends ``Accept-Encoding: gzip, deflate`` and ESPN answers
    gzipped, so this is the ordinary case, not an exotic one
    (``docs/memory/cassette-scrubbing-blind-spots.md``).

    **A body that cannot be read cannot be proven clean.** Callers may decline
    to persist it; they must not persist it unscrubbed.
    """


def scrub_credential_patterns(text: str) -> str:
    """Replace every credential *shape* in ``text`` with its placeholder.

    Idempotent: :data:`SWID_PLACEHOLDER` is not hex and the other two patterns
    carry a negative lookahead on the placeholder, so a second pass over
    already-scrubbed text changes nothing. The cache relies on that — a
    refreshed entry is scrubbed again on its way back to the store.
    """
    text = _SWID_GUID_RE.sub(SWID_PLACEHOLDER, text)
    text = _SWID_KEYED_RE.sub(rf"\1\2{SWID_PLACEHOLDER}", text)
    return _ESPN_S2_RE.sub(rf"\1\2{CREDENTIAL_PLACEHOLDER}", text)


def decode_body(body: bytes | str) -> str:
    """An HTTP body as text, decompressing gzip first.

    Order is the whole point. Scrubbing before decoding is a silent no-op that
    reports success, which is exactly the hole ``decode_compressed_response``
    closes for cassettes — and the cache has no vcrpy doing it on its behalf.

    Raises :class:`UnscrubbableResponseError` for anything that does not come
    back as UTF-8 text: a truncated stream, a ``Content-Encoding`` with no
    decompressor here (zstd, brotli), genuinely binary bytes. Failing closed is
    deliberate; a caller that cannot read a body must not store it.
    """
    if isinstance(body, str):
        return body
    raw = body
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise UnscrubbableResponseError(
                "Refusing to handle a body that claims to be gzip but does not "
                "decompress, so it cannot be scanned for credentials."
            ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnscrubbableResponseError(
            "Refusing to handle a body that is not UTF-8 text, so it cannot be "
            "scanned for credentials. If it is compressed with something other "
            "than gzip, decompress it before handing it over."
        ) from exc


def scrub_body(body: bytes | str) -> str:
    """:func:`decode_body` then :func:`scrub_credential_patterns`, in that order.

    Also passes the result through :func:`redact`, so a credential this process
    *was* handed is removed even where its shape does not match a pattern.
    """
    return redact(scrub_credential_patterns(decode_body(body)))
