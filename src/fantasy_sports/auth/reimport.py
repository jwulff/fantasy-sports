"""Re-read the browser when ESPN refuses the cookies we stored (#159).

The obvious trigger -- "on ``AUTH_EXPIRED``, re-import and retry" -- would be
dead code. ESPN answers a bad cookie, a missing cookie, and a league the account
is not in with the byte-identical 401 ``AUTH_LEAGUE_NOT_VISIBLE``, and the
adapter deliberately reports that as ``LEAGUE_NOT_FOUND``
(``docs/memory/espn-401-tells-you-nothing.md``). That classification is not
touched here. The trigger is instead evidence we *can* get: the browser now
holds a **different** cookie than the one we sent. ESPN rotates ``espn_s2``
while the browser session lives on, so the stored copy drifts behind it; on the
maintainer's machine Chrome's ``SWID`` matched the Keychain's and its
``espn_s2`` did not, which is why each cookie is compared independently.

What this module decides, and what it leaves to the caller
----------------------------------------------------------

:func:`attempt` answers one question for the adapter -- "is there a newer pair
worth trying?" -- and returns a :class:`Reimported` or ``None``. It **saves
nothing**: the adapter retries with the pair first and only then calls
:func:`commit`. Persisting before the retry would let an ambiguous 401 (a wrong
league id, a lost membership) overwrite good stored credentials with whatever
other account a browser happens to hold; persisting after means a pair that
ESPN actually accepted is the only one that is ever stored. Whether to retry,
and the at-most-once rule, are the adapter's, because they are about requests.

It returns ``None`` (the caller re-raises the original error untouched) when:

* the user opted out (``FANTASY_SPORTS_NO_BROWSER_REIMPORT=1`` or
  ``[auth] auto_reimport = false``). A damaged config also counts as opted out:
  the failure path must never replace ESPN's error with a config error;
* the credentials did not come from the Keychain or config. An environment
  credential is how cron and CI say "use exactly this", and a headless host has
  no browser worth reading;
* no browser profile holds a complete pair with our SWID and a different
  ``espn_s2`` (an identical pair means the browser agrees with us, so the
  refusal is not drift; a different SWID is another account).

Which profile wins: only one holding **our SWID** with a different ``espn_s2`` --
the rotation this exists for -- is a candidate; the latest expiry wins, and an
inactive profile that still holds the stale pair does not veto it. A pair for a
*different SWID* is another ESPN account and is never adopted automatically,
even though the issue's wording would count it as "different": the irreversible
commands (``add``, ``drop``, ``claim``) derive team ownership from the SWID, so
silently switching it mid-run could execute a move as an account nobody chose.
Changing account stays an explicit ``auth login``.

Values never leave :class:`~fantasy_sports.auth.chain.Secret` except at the one
``reveal()`` that hands them to ``save_credentials``, and are compared with
:func:`hmac.compare_digest`, so neither timing nor a log line carries them
(``docs/memory/credential-leak-channels.md``).
"""

from __future__ import annotations

import contextlib
import hmac
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from fantasy_sports.auth.chain import CredentialSource, Secret

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.auth.browser import BrowserSearch, ProfileCookies

__all__ = ["ENV_OPT_OUT", "Reimported", "attempt", "commit", "opted_out", "rotated"]

ENV_OPT_OUT: Final[str] = "FANTASY_SPORTS_NO_BROWSER_REIMPORT"
"""Set to ``1`` to disable the re-import, e.g. for a host that must stay pinned."""

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_NAMES: Final[tuple[str, str]] = ("espn_s2", "swid")


@dataclass(frozen=True)
class Reimported:
    """A newer pair, already saved. Values stay ``Secret``; ``source`` is safe to show."""

    browser: str
    profile: str
    espn_s2: Secret
    swid: Secret
    expires_at: Mapping[str, datetime | None] = field(default_factory=dict)
    saved: bool = False
    """Set by the adapter once :func:`commit` stored the pair."""
    fresh: bool = True
    """False when the adapter is reporting a pair a read already used earlier."""

    @property
    def source(self) -> dict[str, str]:
        """``{"browser", "profile"}`` -- what ``details.reimported_from_browser`` carries."""
        return {"browser": self.browser, "profile": self.profile}

    @property
    def label(self) -> str:
        return f"{self.browser}/{self.profile}"

    def secrets(self) -> dict[str, Secret]:
        return {"espn_s2": self.espn_s2, "swid": self.swid}


def opted_out(*, environ: Mapping[str, str] | None = None, config_path: Path | None = None) -> bool:
    """Whether the user turned the re-import off, by environment or by ``config.toml``."""
    env = os.environ if environ is None else environ
    if env.get(ENV_OPT_OUT, "").strip().lower() in _TRUTHY:
        return True
    from fantasy_sports.config.auth import auto_reimport_enabled
    from fantasy_sports.core.errors import FantasySportsError

    try:
        return not auto_reimport_enabled(config_path)
    except FantasySportsError:
        return True


def sourced_from_storage(credential_set: Any) -> bool:
    """True only if **both** cookies came from the Keychain or ``config.toml``.

    A plain mapping handed to the adapter has no recorded source, and an
    unattributable credential is treated like an environment one: not ours to
    replace.
    """
    resolved = getattr(credential_set, "resolved", None)
    if resolved is None or not all(name in resolved for name in _NAMES):
        return False
    return all(
        resolved[name].source in (CredentialSource.KEYCHAIN, CredentialSource.CONFIG)
        for name in _NAMES
    )


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _guid(value: str) -> str:
    """A SWID in one spelling: GUID hex is case-insensitive and the braces optional."""
    return value.strip().strip("{}").upper()


def rotated(found: ProfileCookies, sent: Mapping[str, str]) -> bool:
    """Whether this profile is our account with a different ``espn_s2``.

    Each cookie is compared on its own and both comparisons always run -- no
    short circuit -- so the answer does not leak which cookie matched through
    timing.
    """
    assert found.espn_s2 is not None and found.swid is not None  # `complete`
    s2_same = _same(found.espn_s2.reveal(), sent.get("espn_s2", ""))
    swid_same = _same(_guid(found.swid.reveal()), _guid(sent.get("swid", "")))
    return swid_same and not s2_same


def _choose(
    search: BrowserSearch, sent: Mapping[str, str], now: datetime | None = None
) -> ProfileCookies | None:
    """The rotated profile to try. Expired cookies are not candidates.

    A browser keeps expired rows until it sweeps them, and a dated cookie in the
    past must not outrank a live session cookie (``expires_at is None``). Among
    live candidates the latest dated expiry wins and a session cookie ranks
    after any dated one, which is only a tie-break: either is what the browser
    would send right now.
    """
    moment = datetime.now(UTC) if now is None else now
    floor = datetime.min.replace(tzinfo=UTC)
    live = [
        found
        for found in search.complete
        if rotated(found, sent)
        and (found.espn_s2_expires_at is None or found.espn_s2_expires_at > moment)
        and (found.swid_expires_at is None or found.swid_expires_at > moment)
    ]
    return max(live, key=lambda found: found.espn_s2_expires_at or floor, default=None)


def attempt(
    sent: Mapping[str, str],
    credential_set: Any,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    search: Callable[[], BrowserSearch] | None = None,
) -> Reimported | None:
    """Find a browser pair that differs from ``sent``; ``None`` if there is none to try.

    ``sent`` is the revealed pair the refused request carried. ``search`` is a
    seam for tests; the default is looked up here, at call time, so a
    monkeypatch of ``browser.find_espn_cookies`` reaches it
    (``docs/memory/default-args-defeat-monkeypatch.md``). Never raises for a
    browser problem -- the caller is already holding the error that matters.
    Does not save: see :func:`commit`.
    """
    if opted_out(environ=environ, config_path=config_path):
        return None
    if not sourced_from_storage(credential_set):
        return None

    try:
        if search is None:
            from fantasy_sports.auth import browser

            found = browser.find_espn_cookies()
        else:
            found = search()
        chosen = _choose(found, sent)
    except Exception:  # noqa: BLE001 - fail-soft: the original ESPN error is the one to raise
        return None
    if chosen is None:
        return None
    assert chosen.espn_s2 is not None and chosen.swid is not None  # `complete`
    return Reimported(
        browser=chosen.browser,
        profile=chosen.profile,
        espn_s2=chosen.espn_s2,
        swid=chosen.swid,
        expires_at=chosen.expires_at(),
    )


def _held(name: str) -> str | None:
    """One Keychain entry, **raising** on a backend failure.

    ``chain.read_from_keychain`` is fail-soft by design and cannot tell "no
    entry" from "the Keychain is locked", which a rollback must not confuse.
    """
    import keyring

    from fantasy_sports.auth.chain import SERVICE

    return keyring.get_password(SERVICE, name)


def commit(found: Reimported, *, save: Callable[..., Any] | None = None) -> bool:
    """Store a pair ESPN just accepted. True if stored; False if it was not.

    ``save_credentials`` writes the two cookies one after the other, so a
    failure between them could leave the Keychain holding half of each pair.
    The Keychain is therefore read first, strictly -- the real prior entries,
    not the credentials that were in use, which may have come from
    ``config.toml`` -- and if that read fails nothing is written, because a
    rollback could not be trusted. On any failure of the save each name is put
    back exactly as it was, or deleted if it did not exist. The staleness
    record is not touched by the rollback: a failed save did not get to update
    it, or it still describes the old pair. Snapshot values are ``Secret``s,
    never bare locals. ``save`` is a seam for tests; the Keychain writes are
    looked up on ``auth.chain`` at call time.
    """
    from fantasy_sports.auth import chain

    before: dict[str, Secret | None] = {}
    try:
        for spec in chain.ESPN_CREDENTIALS:
            held = _held(spec.name)
            before[spec.name] = Secret(held) if held else None
    except Exception:  # noqa: BLE001 - cannot establish the prior state, so do not touch it
        return False
    saver = chain.save_credentials if save is None else save
    try:
        saver(
            {"espn_s2": found.espn_s2.reveal(), "swid": found.swid.reveal()},
            expires_at=found.expires_at,
        )
    except Exception:  # noqa: BLE001 - the request already succeeded; storing is best effort
        for name, prior in before.items():
            with contextlib.suppress(Exception):
                if prior is None:
                    chain.delete_from_keychain(name)
                else:
                    chain.write_to_keychain(name, prior.reveal())
        return False
    return True
