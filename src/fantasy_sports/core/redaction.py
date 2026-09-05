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

Why a brace-wrapped SWID becomes a pseudonym rather than a placeholder
----------------------------------------------------------------------

ESPN uses the SWID GUID as a **join key inside a single payload**:
``teams[].owners`` is a list of member SWIDs and ``members[].id`` is the member
SWID. Replacing every one of them with a single shared ``{SWID-REDACTED}``
collapses ten teams and ten members into a ten-by-ten ambiguity, and
``Team.owner_names`` cannot be derived. The probe on jwulff/fantasy-sports#29
found ``members[]`` carries no display names at all, so this join is the *only*
path from a team to a person — which makes the collapse a correctness bug in
the read path rather than a caching wrinkle
(jwulff/fantasy-sports#38, ``docs/memory/swid-pseudonyms.md``).

So :func:`swid_pseudonym` maps each distinct GUID to a distinct, stable,
GUID-shaped token: ``{00000000-XXXX-XXXX-XXXX-XXXXXXXXXXXX}``, the trailing 24
hex digits being a keyed BLAKE2b digest of the upper-cased GUID. The join
survives; the real GUID still never reaches disk.

**The reserved first group is load-bearing.** A well-formed synthetic GUID
matches :data:`CREDENTIAL_PATTERNS`' own SWID pattern, so without it a second
scrubbing pass would re-scrub a pseudonym into a *different* value — destroying
the idempotence the cache depends on when it re-scrubs a refreshed entry — and
every already-scrubbed cassette would become a finding for the repo-wide
credential scan. ``00000000`` is therefore reserved and excluded from the
pattern by a negative lookahead, which fixes the scrubber and the scanner in
one edit because they share the regex. The residual is that a *real* SWID whose
first group is ``00000000`` would pass through unscrubbed; that is one GUID in
2**32, and it is the price of a sentinel that has to stay hex to stay
GUID-shaped.

**The salt is a security decision, and it is the one place "one definition, not
two" does not hold.** A stable pseudonym is by construction a *confirmable
mapping*: anyone holding a real SWID can hash it under a known salt and test
whether that person appears in a committed cassette. That is presence, not
value — but it is a real, if modest, regression from a genuinely one-way
placeholder, and it is accepted deliberately rather than inherited. It is
accepted only where it has to be:

* A **cassette** is committed and must re-record byte-identically, so its salt
  must be deterministic and public: :data:`CASSETTE_SWID_SALT`. This is where
  the confirmable mapping exists.
* A **cache** is never committed, so it holds a random per-store salt
  (:func:`new_swid_salt`) stored alongside its entries, and gets unlinkability
  across machines for free. Discarding the store loses the salt with the rows
  it salted; rotating it independently would make older and newer entries
  disagree about the same member, which is the precise failure #38 exists to
  prevent.

One function, two salts, injected by the caller. Encounter-order counters were
rejected: the same member would get different pseudonyms in two payloads
recorded in different request orders, breaking the join *across* fixtures.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "CASSETTE_SWID_SALT",
    "CREDENTIAL_PATTERNS",
    "CREDENTIAL_PLACEHOLDER",
    "CREDENTIAL_QUERY_PARAMS",
    "REDACTED",
    "SWID_PLACEHOLDER",
    "SWID_PSEUDONYM_SENTINEL",
    "UnscrubbableResponseError",
    "decode_body",
    "forget_secrets",
    "is_swid_pseudonym",
    "new_swid_salt",
    "redact",
    "remember_secret",
    "scrub",
    "scrub_body",
    "scrub_credential_patterns",
    "swid_pseudonym",
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
"""What a *bare* GUID under a ``swid``-ish key becomes.

That shape is the credential itself — a cookie or a query parameter — not a
join key, so it is flattened rather than pseudonymised. Brace-wrapped GUIDs in
a response body go through :func:`swid_pseudonym` instead; see the module
docstring.
"""

SWID_PSEUDONYM_SENTINEL: Final[str] = "00000000"
"""The reserved first group of a pseudonym, excluded from the SWID pattern.

Reserved so that a pseudonym keeps GUID shape while being unmistakable to both
the scrubber (which must not re-scrub it, or idempotence is gone) and the
repo-wide credential scan (which must not report it, or every scrubbed cassette
becomes a finding). The two share :data:`CREDENTIAL_PATTERNS`, so one lookahead
covers both.
"""

CASSETTE_SWID_SALT: Final[str] = "fantasy-sports/cassette-swid/v1"
"""The deterministic, **public** salt used when scrubbing committed fixtures.

A cassette has to re-record byte-identically or every re-recording is a diff,
so its salt cannot be random and cannot be secret. It is spelled out here
rather than hidden precisely because it is the half of the trade-off that
costs something: with this constant in hand, anyone holding a real SWID can
confirm whether that member appears in a committed cassette. Presence, not
value — and a deliberate choice, not an accident. The cache does not use it;
see :func:`new_swid_salt`.
"""


def new_swid_salt() -> str:
    """A fresh random salt, for a store whose contents are never committed.

    ``os.urandom`` rather than ``secrets`` so this module keeps its
    zero-imports-beyond-what-it-already-has posture; the two call the same
    kernel CSPRNG.
    """
    return os.urandom(16).hex()


def swid_pseudonym(guid: str, *, salt: str) -> str:
    """A stable, one-way, GUID-shaped stand-in for one SWID GUID.

    ``guid`` may be brace-wrapped or bare and either case; it is normalised to
    upper-case without braces before hashing, so two spellings of one member
    agree. The result is
    ``{00000000-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`` — 24 hex digits of a BLAKE2b
    digest keyed with ``salt``, under the reserved sentinel first group.

    Stable within one payload and across payloads for a given salt, which is
    what makes the ``teams[].owners`` -> ``members[].id`` join survive
    redaction. One-way: there is no inverse, by design (#38 "out of scope").
    """
    normalised = guid.strip().strip("{}").upper().encode("utf-8")
    # Keyed BLAKE2b is a MAC, so the salt is a key rather than a prefix; the
    # key is capped at 64 bytes by the algorithm.
    digest = (
        hashlib.blake2b(normalised, key=salt.encode("utf-8")[:64], digest_size=12)
        .hexdigest()
        .upper()
    )
    return (
        f"{{{SWID_PSEUDONYM_SENTINEL}-{digest[0:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:24]}}}"
    )


CREDENTIAL_QUERY_PARAMS = frozenset({"espn_s2", "swid"})
"""Query-parameter names that carry a credential when ESPN is called by URL.

Lower-cased; compare against ``name.lower()``. The cache excludes these from
its key so a rotated cookie does not invalidate every entry, and strips them
from the URL it stores.
"""

# A SWID is a brace-wrapped GUID: {1A2B3C4D-5E6F-...}. This is the shape ESPN
# echoes inside roster/owner payloads, which is the case header filtering misses.
#
# The negative lookahead exempts the reserved sentinel first group, so a
# pseudonym this module emitted is neither re-scrubbed (idempotence) nor
# reported by the repo-wide scan (which reuses this pattern). Removing it is
# not a style change: it silently breaks both.
_SWID_GUID_RE = re.compile(
    rf"(?!\{{{SWID_PSEUDONYM_SENTINEL}-)"
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

# The complement of the lookahead above: what this module emits, and the one
# GUID shape the scrubber and the scanner both agree to leave alone.
_SWID_PSEUDONYM_RE = re.compile(
    rf"\{{{SWID_PSEUDONYM_SENTINEL}-"
    r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}"
)


def is_swid_pseudonym(text: str) -> bool:
    """Whether ``text`` is exactly one pseudonym this module would emit.

    For tests and for callers that want to assert a payload has been through
    the scrubber. It says nothing about *which* GUID produced it — that is the
    point.
    """
    return _SWID_PSEUDONYM_RE.fullmatch(text) is not None


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


def scrub_credential_patterns(text: str, *, swid_salt: str = CASSETTE_SWID_SALT) -> str:
    """Replace every credential *shape* in ``text``.

    Brace-wrapped GUIDs become a per-GUID :func:`swid_pseudonym` under
    ``swid_salt``, so the owner-to-member join survives; the other two shapes
    are the credential itself and are flattened to a placeholder.

    ``swid_salt`` defaults to the public :data:`CASSETTE_SWID_SALT` because a
    committed fixture is the case that has no other option. **The cache passes
    its own random per-store salt**; see the module docstring for why the
    function is shared and the salt is not.

    Idempotent: :data:`SWID_PLACEHOLDER` is not hex, a pseudonym carries the
    reserved sentinel the SWID pattern looks past, and the other two patterns
    carry a negative lookahead on the placeholder — so a second pass over
    already-scrubbed text changes nothing, whatever salt that second pass
    happens to be holding. The cache relies on that: a refreshed entry is
    scrubbed again on its way back to the store.
    """
    text = _SWID_GUID_RE.sub(lambda match: swid_pseudonym(match.group(0), salt=swid_salt), text)
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


def scrub_body(body: bytes | str, *, swid_salt: str = CASSETTE_SWID_SALT) -> str:
    """:func:`decode_body` then :func:`scrub_credential_patterns`, in that order.

    Also passes the result through :func:`redact`, so a credential this process
    *was* handed is removed even where its shape does not match a pattern.

    ``swid_salt`` is forwarded; the cache passes its per-store salt so that a
    hit and a miss agree on every pseudonym.
    """
    return redact(scrub_credential_patterns(decode_body(body), swid_salt=swid_salt))
