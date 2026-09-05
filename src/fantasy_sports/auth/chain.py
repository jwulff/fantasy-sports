"""Credential resolution: env → macOS Keychain → config file.

ARCHITECTURE §6. The ordering is the load-bearing part. Env comes first
because a `launchd`/cron process or a CI runner has no unlockable Keychain and
no TTY to prompt against; if the Keychain were consulted first, every headless
run would depend on a lock state it cannot influence. Env-first means the
automated path never touches the Keychain at all.

Each later link **fails soft**. A locked Keychain, a missing backend, an
unreadable config file — none of them raise. They fall through to the next
link, and only the end of the chain is a failure, reported as ``AUTH_MISSING``.

Three rules this module exists to hold:

1. **Never render a credential.** Values are carried in :class:`Secret`, which
   redacts itself in ``repr``, ``str``, ``format``, and therefore in every
   traceback that captures locals. The scrub set and :func:`redact` live in
   ``core/redaction.py`` so that ``core.errors.FantasySportsError`` can scrub
   *every* error message at construction, not only this module's — a caller
   who interpolates a cookie into any error still cannot leak it. They are
   re-exported here because this is where they are used. See
   ``tests/unit/test_auth.py`` and ``docs/memory/credential-leak-channels.md``.
2. **Import ``keyring`` lazily.** ADR-0008 budgets ``--help`` at 50 ms and
   ``tests/unit/test_imports.py`` asserts ``keyring`` is absent from
   ``sys.modules`` on cheap paths. The import lives inside
   :func:`read_from_keychain` and nowhere else.
3. **Never import typer, click, rich, or espn_api.** ``auth/`` is a plain
   library; the CLI is a projection over it (ADR-0003).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from fantasy_sports.config import credentials as config_credentials
from fantasy_sports.core.errors import AuthMissingError
from fantasy_sports.core.models import CredentialSpec
from fantasy_sports.core.redaction import (
    REDACTED,
    forget_secrets,
    redact,
    remember_secret,
)

__all__ = [
    "ENV_PREFIX",
    "ESPN_CREDENTIALS",
    "REDACTED",
    "SERVICE",
    "CredentialSet",
    "CredentialSource",
    "CredentialSpec",
    "ResolvedCredential",
    "Secret",
    "forget_secrets",
    "normalize_credential",
    "normalize_opaque_cookie",
    "normalize_swid",
    "read_from_config",
    "read_from_env",
    "read_from_keychain",
    "redact",
    "remember_secret",
    "require_credentials",
    "resolve_credential",
    "resolve_credentials",
    "save_credentials",
]

SERVICE = "fantasy-sports"
"""Keychain service name. One entry per credential, keyed by credential name."""

ENV_PREFIX = "FANTASY_SPORTS_"

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# `REDACTED`, `redact`, `remember_secret`, and `forget_secrets` are imported
# from `core.redaction` above and re-exported. They live in `core/` because
# `core.errors.FantasySportsError` scrubs its message at construction, and
# `core/` cannot import `auth/` — this module imports `core.models`. Callers
# that already say `chain.redact(...)` are unaffected.


class Secret:
    """A credential value that refuses to render itself.

    ``repr``/``str``/``format`` all return :data:`REDACTED`. That is what keeps
    a credential out of a traceback rendered with ``capture_locals=True``,
    which reprs every local in every frame — a plain ``str`` in the same
    position leaks in full.

    Use :meth:`reveal` at the exact point the value is handed to a transport,
    and nowhere else.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value
        remember_secret(value)

    def reveal(self) -> str:
        """Return the underlying value. The only way out."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return REDACTED

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _auth_missing(message: str, *, remediation: str | None = None) -> AuthMissingError:
    """Build the ``AUTH_MISSING`` error this module raises for every failure.

    Every auth failure here is ``AUTH_MISSING``, a *malformed* credential
    included. The correct agent response is identical — ask the human to run
    ``auth login`` — so a second code would describe an internal distinction
    rather than a different action, and adding a taxonomy code is an API change
    (``CLAUDE.md`` rule 4). Decision recorded on jwulff/fantasy-sports#34.

    ``remediation`` is the human-facing next step and is per-instance: it names
    the actual environment variables that would satisfy *this* failure, which
    the class-level ``agent_action`` cannot. It is a first-class key on the
    error payload (ADR-0004 as amended by U5, decided on
    jwulff/fantasy-sports#6) rather than an entry in ``details``. Message and
    remediation are scrubbed by
    :class:`~fantasy_sports.core.errors.FantasySportsError` at construction.
    """
    return AuthMissingError(message, remediation=remediation)


# ---------------------------------------------------------------------------
# Credential specs
# ---------------------------------------------------------------------------


class CredentialSource(StrEnum):
    """Which link in the chain produced a value."""

    ENV = "env"
    KEYCHAIN = "keychain"
    CONFIG = "config"


ESPN_CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        name="espn_s2",
        label="ESPN_S2 cookie",
        env_vars=(f"{ENV_PREFIX}ESPN_S2", "ESPN_S2"),
        guidance=(
            "Log in at fantasy.espn.com, open DevTools → Application → Cookies "
            "→ https://fantasy.espn.com, and copy the value of espn_s2."
        ),
    ),
    CredentialSpec(
        name="swid",
        label="SWID cookie",
        env_vars=(f"{ENV_PREFIX}SWID", "ESPN_SWID", "SWID"),
        guidance=(
            "In the same cookie list, copy SWID. Keep the surrounding curly "
            "braces — dropping them is the most common extraction mistake."
        ),
    ),
)
"""ESPN's two manually-extracted cookies. There is no programmatic auth to
design toward, ever (ARCHITECTURE §14 item 10)."""


# ---------------------------------------------------------------------------
# Normalization and validation
# ---------------------------------------------------------------------------

_SWID_RE = re.compile(
    r"^\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?$"
)


def _unquote(value: str) -> str:
    """Strip whitespace and one layer of surrounding quotes from a paste."""
    stripped = value.strip()
    for quote in ('"', "'"):
        if len(stripped) >= 2 and stripped.startswith(quote) and stripped.endswith(quote):
            stripped = stripped[1:-1].strip()
            break
    return stripped


def normalize_swid(value: str) -> str:
    """Return ``value`` as a braced SWID, repairing missing braces.

    ARCHITECTURE §14 item 9: a SWID pasted without its curly braces is the
    single most-repeated manual-extraction mistake across every community
    source reviewed, and ESPN rejects the unbraced form silently. Repair it
    here, once, rather than letting it become an ambiguous 401 later.

    A value that is not a GUID at all is rejected — that is a different
    mistake (usually the wrong cookie entirely) and repairing it would only
    delay the diagnosis.

    :raises AuthMissingError: if ``value`` is not a GUID, with or without braces.
    """
    candidate = _unquote(value)
    match = _SWID_RE.match(candidate)
    if match is None:
        # The value is never echoed: a malformed credential is still a
        # credential, and the user is about to paste the right one anyway.
        raise _auth_missing(
            f"SWID is not a GUID (got {len(candidate)} characters). "
            "Expected 8-4-4-4-12 hex digits, optionally wrapped in curly braces.",
            remediation="Re-copy the SWID cookie value from DevTools, braces included.",
        )
    return "{" + match.group(1) + "}"


def normalize_opaque_cookie(value: str) -> str:
    """Validate an opaque cookie value such as ``espn_s2``.

    No format is asserted beyond "one cookie value" — ESPN publishes none, and
    inventing a length or alphabet rule here would reject valid credentials the
    first time ESPN changes its encoding. What *is* checked is the shape of the
    common paste mistakes: an empty value, or a whole ``Cookie:`` header
    pasted instead of one value.
    """
    candidate = _unquote(value)
    if not candidate:
        raise _auth_missing(
            "Cookie value is empty.",
            remediation="Copy the cookie value from DevTools and try again.",
        )
    if any(ch.isspace() for ch in candidate) or ";" in candidate:
        raise _auth_missing(
            f"Cookie value contains whitespace or ';' ({len(candidate)} characters). "
            "That usually means a whole Cookie header was pasted instead of one value.",
            remediation="Paste only the value of the cookie, not the whole header.",
        )
    return candidate


_NORMALIZERS: dict[str, Callable[[str], str]] = {"swid": normalize_swid}


def normalize_credential(name: str, value: str) -> str:
    """Normalize ``value`` for the credential called ``name``."""
    return _NORMALIZERS.get(name, normalize_opaque_cookie)(value)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def read_from_env(spec: CredentialSpec, environ: Mapping[str, str] | None = None) -> str | None:
    """First link. Cheap, always available, and the only link cron can use."""
    env = os.environ if environ is None else environ
    for var in spec.env_vars:
        value = env.get(var)
        if value and value.strip():
            return value
    return None


def read_from_keychain(spec: CredentialSpec) -> str | None:
    """Second link. Returns ``None`` on *any* failure rather than raising.

    A locked Keychain, a headless session with no backend, a
    ``KeyringError`` from the macOS `security` shell-out — all of them mean
    "this link has nothing for you", not "abort". The chain has another link;
    raising here would turn a soft, recoverable condition into a hard failure
    on the exact hosts (cron, CI, SSH) that the config fallback exists for.

    ``keyring`` is imported here and only here, so ``--help`` never pays for it.
    """
    try:
        import keyring
    except ImportError:  # pragma: no cover - keyring is a declared dependency
        return None
    try:
        return keyring.get_password(SERVICE, spec.name)
    except Exception:
        # Deliberately broad. Backends raise their own exception types —
        # KeyringLocked, KeyringError, OSError from the macOS `security`
        # shell-out, backend-specific D-Bus errors on Linux. Enumerating them
        # would make this link brittle in exactly the situation it must not be.
        return None


def read_from_config(spec: CredentialSpec, config: Mapping[str, str] | None = None) -> str | None:
    """Third link. Reads ``[credentials]`` through the config layer.

    Passing ``config`` injects an already-parsed table — the seam the tests and
    the provider layer use. Otherwise the read goes through
    :func:`fantasy_sports.config.credentials.load_credentials`, which fails
    soft for the reason the chain needs it to: an unreadable config file is an
    absent credential, not an auth failure.
    """
    table = config_credentials.load_credentials() if config is None else config
    value = table.get(spec.name)
    return value if value and value.strip() else None


@dataclass(frozen=True)
class ResolvedCredential:
    """One credential and the link of the chain it came from."""

    name: str
    source: CredentialSource
    secret: Secret

    def reveal(self) -> str:
        return self.secret.reveal()


@dataclass(frozen=True)
class CredentialSet:
    """The outcome of running the chain over a provider's credential specs."""

    resolved: Mapping[str, ResolvedCredential] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    def __contains__(self, name: object) -> bool:
        return name in self.resolved

    def get(self, name: str) -> ResolvedCredential | None:
        return self.resolved.get(name)

    def reveal(self, name: str) -> str:
        """Return one credential's value. Raises ``AUTH_MISSING`` if absent."""
        found = self.resolved.get(name)
        if found is None:
            raise _auth_missing(
                f"No value for credential {name!r}.",
                remediation="Run `fantasy-sports auth login`.",
            )
        return found.reveal()

    def as_mapping(self) -> dict[str, str]:
        """Revealed values, for handing to a transport. Never log the result."""
        return {name: cred.reveal() for name, cred in self.resolved.items()}


def resolve_credential(
    spec: CredentialSpec,
    *,
    environ: Mapping[str, str] | None = None,
    keychain_reader: Callable[[CredentialSpec], str | None] | None = None,
    config: Mapping[str, str] | None = None,
) -> ResolvedCredential | None:
    """Run the chain for one credential. ``None`` means every link was empty.

    The seams (``environ``, ``keychain_reader``, ``config``) exist so unit
    tests can exercise the ordering and the locked-Keychain fallback without
    touching a real Keychain, a real environment, or the network.
    """
    env_value = read_from_env(spec, environ)
    if env_value is not None:
        # Return before the Keychain is even considered: this is the branch
        # cron and CI take, and it must not import keyring.
        return _wrap(spec, env_value, CredentialSource.ENV)

    reader = read_from_keychain if keychain_reader is None else keychain_reader
    try:
        keychain_value = reader(spec)
    except Exception:
        # Fail-soft is a property of the *chain*, not of one reader. The
        # default reader already swallows backend errors, but a caller can
        # inject its own (the provider layer, a test, a future Linux keyring
        # shim) and the locked-keychain guarantee must not depend on that
        # reader remembering to. Belt and braces, deliberately.
        keychain_value = None
    if keychain_value is not None and keychain_value.strip():
        return _wrap(spec, keychain_value, CredentialSource.KEYCHAIN)

    config_value = read_from_config(spec, config)
    if config_value is not None:
        return _wrap(spec, config_value, CredentialSource.CONFIG)

    return None


def _wrap(spec: CredentialSpec, value: str, source: CredentialSource) -> ResolvedCredential:
    """Wrap a resolved raw value, repairing it where the format is known.

    A stored SWID that lost its braces somewhere upstream — an env var set by
    hand, a config file edited in an editor — is repaired on read as well as on
    save. Repair failures are not fatal on the read path: an unusable value is
    still better diagnosed by the provider's 401 handling than by refusing to
    start, so the raw value is passed through and the ambiguity is resolved
    where it can actually be probed (ARCHITECTURE §14 item 1).
    """
    try:
        cleaned = normalize_credential(spec.name, value)
    except AuthMissingError:
        cleaned = value.strip()
    return ResolvedCredential(name=spec.name, source=source, secret=Secret(cleaned))


def resolve_credentials(
    specs: Iterable[CredentialSpec] = ESPN_CREDENTIALS,
    *,
    environ: Mapping[str, str] | None = None,
    keychain_reader: Callable[[CredentialSpec], str | None] | None = None,
    config: Mapping[str, str] | None = None,
) -> CredentialSet:
    """Run the chain for every spec. Never raises for an absent credential."""
    resolved: MutableMapping[str, ResolvedCredential] = {}
    missing: list[str] = []
    for spec in specs:
        found = resolve_credential(
            spec, environ=environ, keychain_reader=keychain_reader, config=config
        )
        if found is None:
            missing.append(spec.name)
        else:
            resolved[spec.name] = found
    return CredentialSet(resolved=dict(resolved), missing=tuple(missing))


def require_credentials(
    specs: Iterable[CredentialSpec] = ESPN_CREDENTIALS,
    *,
    environ: Mapping[str, str] | None = None,
    keychain_reader: Callable[[CredentialSpec], str | None] | None = None,
    config: Mapping[str, str] | None = None,
) -> CredentialSet:
    """:func:`resolve_credentials`, but ``AUTH_MISSING`` if a *required* one is absent.

    This is the gate a command calls before touching a provider. Absent
    credentials produce a typed error with a stable code and a remediation —
    never a ``KeyError`` from a caller that assumed the value was there.

    Only ``required`` specs block. ``CredentialSpec.required`` reaches this
    layer for the first time now that the provider-facing and resolution-facing
    specs are one class (jwulff/fantasy-sports#35); a provider that declares an
    optional credential must not have every command refuse to run until it is
    configured. ESPN's two cookies are both required, so nothing here changes
    for the only provider that exists yet.

    ``CredentialSet.missing`` still lists **everything** that did not resolve,
    optional included. It is what ``auth status`` reports, and an optional
    credential being absent is a fact worth reporting even though it is not a
    fact worth failing on.
    """
    specs = tuple(specs)
    credentials = resolve_credentials(
        specs, environ=environ, keychain_reader=keychain_reader, config=config
    )
    required = {spec.name for spec in specs if spec.required}
    blocking = tuple(name for name in credentials.missing if name in required)
    if blocking:
        names = ", ".join(blocking)
        raise _auth_missing(
            f"No credentials configured for: {names}.",
            remediation=_remediation_for(specs, blocking),
        )
    return credentials


def _remediation_for(specs: Iterable[CredentialSpec], missing: Iterable[str]) -> str:
    """The next step for a human, naming the env vars that would satisfy it.

    ``CredentialSpec.env_vars`` is optional now that the provider-facing and
    resolution-facing specs are one class, so a spec may legitimately declare
    no environment fallback. Naming a variable that does not exist would be
    worse than naming none, so the clause is dropped rather than invented.
    """
    absent = set(missing)
    variables = [spec.env_vars[0] for spec in specs if spec.name in absent and spec.env_vars]
    login = "Run `fantasy-sports auth login`"
    if not variables:
        return f"{login}."
    return f"{login}, or set " + " and ".join(variables) + " in the environment."


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def write_to_keychain(name: str, value: str) -> None:
    """Store one credential in the Keychain. Interactive path only."""
    import keyring

    keyring.set_password(SERVICE, name, value)


def save_credentials(
    values: Mapping[str, str],
    *,
    specs: Iterable[CredentialSpec] = ESPN_CREDENTIALS,
    writer: Callable[[str, str], None] | None = None,
    state_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Normalize and store credentials; return the names that were repaired.

    Repair happens **before** the write, so a SWID that arrived without braces
    is stored braced and every later read is already correct. A value that
    cannot be normalized is rejected outright and nothing is written — a
    partially-saved credential pair is worse than an unsaved one.

    Returns the names whose stored form differs from what was passed in, so
    ``auth login`` (#9) can tell the user what it fixed. Credential values are
    never returned, logged, or included in the raised error.
    """
    known = {spec.name: spec for spec in specs}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise _auth_missing(f"Unknown credential name(s): {', '.join(unknown)}.")

    # Validate everything first; write nothing until all of it is good.
    normalized: dict[str, str] = {}
    repaired: list[str] = []
    for name, value in values.items():
        cleaned = normalize_credential(name, value)
        normalized[name] = cleaned
        if cleaned != value:
            repaired.append(name)

    write = write_to_keychain if writer is None else writer
    for name, cleaned in normalized.items():
        write(name, cleaned)
        Secret(cleaned)  # register with the scrubber; the object is not needed

    # Imported inside the function: staleness imports this module for
    # CredentialSource, so a module-scope import here would be a cycle.
    from fantasy_sports.auth import staleness

    staleness.record_stored(normalized.keys(), now=now, path=state_path)
    return tuple(repaired)
