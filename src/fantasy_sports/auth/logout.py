"""Credential removal — what ``auth logout`` does and renders.

The mirror image of the chain in ``chain.py``. Resolution walks env → Keychain
→ config and stops at the first hit; removal walks the same three links and
**stops at none of them**, because a leak remediation that clears the Keychain
and leaves a plaintext copy in ``config.toml`` has not remediated anything
(jwulff/fantasy-sports#62, found writing ``SECURITY.md``).

Three things this module holds that the individual link functions cannot:

1. **The environment is reported, never changed.** A process cannot unset a
   variable in its parent's shell, a ``launchd`` plist, or a CI secret store.
   Pretending otherwise — returning success with the variable still set — is
   the one outcome worse than doing nothing, so an env-supplied credential is
   reported as *still set*, named by the exact variable, with a warning that
   says where to go and unset it.
2. **Fail-soft is decided here, per link.** :func:`~fantasy_sports.auth.chain.delete_from_keychain`
   and :func:`~fantasy_sports.config.credentials.remove_credentials` both raise
   on a failure they cannot classify. This module catches, classifies the link
   as *unavailable*, and keeps going, because a locked Keychain must not stop
   the config file from being cleaned, and vice versa. The same reasoning
   ``credential-leak-channels.md`` gives for the chain: a soft-failure
   guarantee lives at the layer that promises it.
3. **Nothing is printed and no value is carried.** The report names
   credentials, links, outcomes, and variable names. It never holds a value,
   so there is nothing to redact — the only value-shaped thing that ever
   exists during a logout is the ``get_password`` result compared in place
   inside ``delete_from_keychain``.

A logout is idempotent. Running it twice reports every link *absent* the
second time and writes nothing.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fantasy_sports.auth import staleness
from fantasy_sports.auth.chain import (
    ESPN_CREDENTIALS,
    SERVICE,
    CredentialSource,
    CredentialSpec,
    delete_from_keychain,
)
from fantasy_sports.config import credentials as config_credentials
from fantasy_sports.config import paths

__all__ = [
    "CredentialRemoval",
    "LinkOutcome",
    "LogoutReport",
    "clear_credentials",
]


class LinkOutcome(StrEnum):
    """What ``auth logout`` can honestly say about one link for one credential."""

    REMOVED = "removed"
    """A stored value was there and is now gone."""

    ABSENT = "absent"
    """The link held nothing under this name. Not an error."""

    STILL_SET = "still-set"
    """Only the environment produces this: the value is there and this process
    cannot remove it."""

    UNAVAILABLE = "unavailable"
    """The link could not be reached — locked Keychain, no backend, an
    unreadable file — so whatever it holds is **still there**."""


@dataclass(frozen=True)
class CredentialRemoval:
    """One credential's row in the report. Carries no value."""

    name: str
    env: LinkOutcome
    env_vars: tuple[str, ...]
    """The environment variables found set for this credential, so the user
    knows which ones to unset. Empty when ``env`` is :attr:`LinkOutcome.ABSENT`."""

    keychain: LinkOutcome
    config: LinkOutcome

    @property
    def removed(self) -> bool:
        return LinkOutcome.REMOVED in (self.keychain, self.config)

    @property
    def still_set(self) -> bool:
        return self.env is LinkOutcome.STILL_SET

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            CredentialSource.ENV.value: self.env.value,
            "env_vars": list(self.env_vars),
            CredentialSource.KEYCHAIN.value: self.keychain.value,
            CredentialSource.CONFIG.value: self.config.value,
        }


@dataclass(frozen=True)
class LogoutReport:
    """The whole ``auth logout`` payload."""

    credentials: tuple[CredentialRemoval, ...]
    keychain_service: str
    config_path: Path
    warnings: tuple[str, ...]

    @property
    def removed(self) -> tuple[str, ...]:
        """Names removed from at least one stored link."""
        return tuple(row.name for row in self.credentials if row.removed)

    @property
    def still_set(self) -> tuple[str, ...]:
        """Names the environment still supplies after this call."""
        return tuple(row.name for row in self.credentials if row.still_set)

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Names with a stored link this call could not reach.

        A value may still be there. This is the one outcome the command must
        not report as success (jwulff/fantasy-sports#89): a script or an agent
        branching on the exit status has to learn that the remediation is not
        finished, and a warning inside a success envelope does not tell it.
        """
        return tuple(
            row.name
            for row in self.credentials
            if LinkOutcome.UNAVAILABLE in (row.keychain, row.config)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "removed": list(self.removed),
            "still_set": list(self.still_set),
            "unavailable": list(self.unavailable),
            "credentials": [row.to_payload() for row in self.credentials],
            "keychain_service": self.keychain_service,
            "config_path": str(self.config_path),
            "warnings": list(self.warnings),
        }


def clear_credentials(
    specs: Iterable[CredentialSpec] = ESPN_CREDENTIALS,
    *,
    environ: Mapping[str, str] | None = None,
    keychain_remover: Callable[[str], bool] | None = None,
    config_path: Path | None = None,
    state_path: Path | None = None,
) -> LogoutReport:
    """Remove every stored copy of each credential and report what happened.

    Walks the links in chain order. The seams (``environ``,
    ``keychain_remover``, ``config_path``, ``state_path``) exist so the tests
    never touch a real environment, a real Keychain — which on a developer's
    machine holds real cookies under this exact service name — or a real
    config file.

    Never raises for an absent or unreachable link. It does raise
    :class:`~fantasy_sports.core.errors.ConfigInvalidError` for a config file
    that is present but will not parse: that file cannot be edited safely, the
    user can fix it in seconds, and the Keychain step has already run by then,
    so a second logout after the fix finishes the job.
    """
    specs = tuple(specs)
    remove = delete_from_keychain if keychain_remover is None else keychain_remover
    target = paths.config_file() if config_path is None else config_path
    warnings: list[str] = []

    env_outcomes = {spec.name: _env_outcome(spec, environ) for spec in specs}
    keychain_outcomes = {spec.name: _keychain_outcome(spec.name, remove) for spec in specs}
    config_outcomes = _config_outcomes([spec.name for spec in specs], target)

    rows: list[CredentialRemoval] = []
    for spec in specs:
        env, env_vars = env_outcomes[spec.name]
        row = CredentialRemoval(
            name=spec.name,
            env=env,
            env_vars=env_vars,
            keychain=keychain_outcomes[spec.name],
            config=config_outcomes[spec.name],
        )
        rows.append(row)
        warnings.extend(_warnings_for(row, target))

    # A Keychain that answered — removed or absent — holds nothing this tool
    # wrote, so the `stored_at` we recorded for it describes nothing now.
    settled = [
        name
        for name, outcome in keychain_outcomes.items()
        if outcome in (LinkOutcome.REMOVED, LinkOutcome.ABSENT)
    ]
    staleness.forget_stored(settled, path=state_path)

    return LogoutReport(
        credentials=tuple(rows),
        keychain_service=SERVICE,
        config_path=target,
        warnings=tuple(warnings),
    )


def _env_outcome(
    spec: CredentialSpec, environ: Mapping[str, str] | None
) -> tuple[LinkOutcome, tuple[str, ...]]:
    """Report, never change. Names *every* set variable, aliases included.

    :func:`~fantasy_sports.auth.chain.read_from_env` stops at the first hit,
    which is right for resolution and wrong here: a user with both
    ``FANTASY_SPORTS_SWID`` and a bare ``SWID`` exported needs to be told about
    both, or the second one resurfaces the moment the first is unset. Blank
    values are not credentials, the same rule the chain applies.
    """
    env = os.environ if environ is None else environ
    found = tuple(var for var in spec.env_vars if (env.get(var) or "").strip())
    return (LinkOutcome.STILL_SET, found) if found else (LinkOutcome.ABSENT, ())


def _keychain_outcome(name: str, remove: Callable[[str], bool]) -> LinkOutcome:
    """Classify the Keychain link; a raise of any kind is *unavailable*."""
    try:
        return LinkOutcome.REMOVED if remove(name) else LinkOutcome.ABSENT
    except Exception:
        # Deliberately broad, for the reason `read_from_keychain` gives: the
        # backends raise their own types, and the answer is the same for all
        # of them — the entry, if there is one, is still there.
        return LinkOutcome.UNAVAILABLE


def _config_outcomes(names: list[str], target: Path) -> dict[str, LinkOutcome]:
    """Classify the config link for every name in one read-modify-write."""
    try:
        removed = set(config_credentials.remove_credentials(names, target))
    except OSError:
        # Present but unreadable or unwritable. Reported, not swallowed: the
        # value, if there is one, is still on disk.
        return dict.fromkeys(names, LinkOutcome.UNAVAILABLE)
    return {name: LinkOutcome.REMOVED if name in removed else LinkOutcome.ABSENT for name in names}


def _warnings_for(row: CredentialRemoval, target: Path) -> list[str]:
    """Human-readable warnings, one per link that still holds the credential."""
    notes: list[str] = []
    if row.still_set:
        variables = ", ".join(row.env_vars)
        notes.append(
            f"{row.name}: {variables} is still set in the environment. A process cannot "
            "unset its parent's variables; remove it from your shell profile, launchd "
            "plist, or CI secrets."
        )
    if row.keychain is LinkOutcome.UNAVAILABLE:
        notes.append(
            f"{row.name}: the Keychain could not be reached (locked, no backend, or a "
            f"backend error), so any entry under service {SERVICE!r} was not removed. "
            "Unlock the Keychain and run `fantasy-sports auth logout` again."
        )
    if row.config is LinkOutcome.UNAVAILABLE:
        notes.append(
            f"{row.name}: {target} could not be read or rewritten, so any value in its "
            "[credentials] table was not removed. Fix the file's permissions and run "
            "`fantasy-sports auth logout` again."
        )
    return notes
