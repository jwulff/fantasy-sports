"""Read ESPN's two cookies out of the user's own browser profiles.

``auth login --from-browser`` exists because the cookies already sit, readable,
in a profile on disk, and copying them through DevTools is the step people get
wrong. Reading them also yields the cookie's **real expiry**, which is the one
number ``auth/staleness.py`` could only guess at.

What this module reads
----------------------

* **Chrome-family on macOS** — Chrome, Chromium, Brave, Edge, Arc. Every
  profile directory with a ``Cookies`` (or ``Network/Cookies``) SQLite file.
  Values are ``v10`` AES-128-CBC blobs; the key is derived from the browser's
  "Safe Storage" Keychain password (``hashlib.pbkdf2_hmac``, SHA-1, salt
  ``saltysalt``, 1003 rounds), the IV is sixteen spaces, and from cookie-DB
  schema 24 the plaintext starts with 32 bytes of SHA-256(host_key) that must
  be dropped. The cipher is :mod:`fantasy_sports.auth._aes` (pure Python — the
  dependency budget is spent, and ``openssl -K`` would put the key in argv).
* **Firefox** on macOS and Linux — plain-text ``moz_cookies``.

Not Safari (needs Full Disk Access), not Windows (DPAPI / app-bound
encryption), not Linux Chrome-family (``v11`` / libsecret). Those are reported
as *skipped*, never crashed on.

How credentials stay out of the leak channels
---------------------------------------------

``docs/memory/credential-leak-channels.md``: the channels are captured-locals
reprs and caller-formatted messages. So every decrypted value is wrapped in
:class:`~fantasy_sports.auth.chain.Secret` by the function that produced it,
before any other code can see a plain ``str``; the Safe Storage password is a
``Secret`` the moment it leaves the subprocess's stdout; the derived key is a
redacting :class:`_Key`; and the functions that hold plaintext convert every
failure into a data-free :class:`CookieDecryptError` *outside* their ``except``
block, so the failed frame is not left attached as ``__context__``. The
password is read from the ``security`` command's **stdout**; its argv carries
only the service name.

``sqlite3``, ``subprocess``, ``hashlib``, ``shutil`` and ``tempfile`` are
imported inside the functions that use them: ``--help`` must not pay for them
(``docs/memory/import-budget-and-the-fastpath.md``).
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fantasy_sports.auth import _aes
from fantasy_sports.auth.chain import Secret, normalize_credential

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

__all__ = [
    "BROWSER_NAMES",
    "BrowserSearch",
    "CookieDecryptError",
    "KeychainAccessDenied",
    "KeychainEntryMissing",
    "ProfileCookies",
    "find_espn_cookies",
    "read_safe_storage_password",
]

ESPN_HOSTS = (".espn.com", "espn.com", "fantasy.espn.com", ".fantasy.espn.com")
COOKIE_NAMES = ("espn_s2", "SWID")

_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_IV = b" " * 16
_SALT = b"saltysalt"
_ITERATIONS = 1003
_HASH_PREFIX_SCHEMA = 24
"""Cookie-DB ``meta.version`` from which plaintext carries SHA-256(host_key)."""
_KEYCHAIN_TIMEOUT_SECONDS = 120
_SECURITY_ITEM_NOT_FOUND = 44


@dataclass(frozen=True)
class _ChromeFamily:
    name: str
    label: str
    support_dir: str
    """Under ``~/Library/Application Support``."""
    keychain_service: str
    linux_dir: str
    """Under ``~/.config``; only used to report that a Linux store exists but is unsupported."""


_CHROME_FAMILY: tuple[_ChromeFamily, ...] = (
    _ChromeFamily("chrome", "Chrome", "Google/Chrome", "Chrome Safe Storage", "google-chrome"),
    _ChromeFamily("chromium", "Chromium", "Chromium", "Chromium Safe Storage", "chromium"),
    _ChromeFamily(
        "brave",
        "Brave",
        "BraveSoftware/Brave-Browser",
        "Brave Safe Storage",
        "BraveSoftware/Brave-Browser",
    ),
    _ChromeFamily(
        "edge", "Edge", "Microsoft Edge", "Microsoft Edge Safe Storage", "microsoft-edge"
    ),
    _ChromeFamily("arc", "Arc", "Arc/User Data", "Arc Safe Storage", ""),
)

BROWSER_NAMES: tuple[str, ...] = (*(family.name for family in _CHROME_FAMILY), "firefox")
"""Every name ``--from-browser NAME`` accepts, in search order."""


# ---------------------------------------------------------------------------
# Errors and result types — none of them can carry a value
# ---------------------------------------------------------------------------


class CookieDecryptError(Exception):
    """One cookie could not be decrypted or did not validate. Message has no data."""


class KeychainAccessDenied(Exception):
    """The user declined the Keychain prompt, or the Keychain could not be read."""


class KeychainEntryMissing(Exception):
    """The browser has no "Safe Storage" Keychain entry."""


@dataclass(frozen=True)
class ProfileCookies:
    """The ESPN cookies one browser profile holds. Values are always ``Secret``."""

    browser: str
    profile: str
    espn_s2: Secret | None = None
    swid: Secret | None = None
    espn_s2_expires_at: datetime | None = None
    swid_expires_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return self.espn_s2 is not None and self.swid is not None

    @property
    def label(self) -> str:
        return f"{self.browser}/{self.profile}"

    def expires_at(self) -> dict[str, datetime | None]:
        """Credential name → expiry (``None`` for a session cookie)."""
        return {"espn_s2": self.espn_s2_expires_at, "swid": self.swid_expires_at}


@dataclass(frozen=True)
class BrowserSearch:
    """Everything a search found, and everything it looked at to find it."""

    profiles: tuple[ProfileCookies, ...] = ()
    """Profiles holding at least one ESPN cookie."""
    searched: tuple[str, ...] = ()
    """``Browser/Profile`` labels whose cookie store was opened."""
    skipped: tuple[str, ...] = ()
    """Human-readable reasons a browser or profile could not be read."""
    keychain_denied: bool = False

    @property
    def complete(self) -> tuple[ProfileCookies, ...]:
        """Profiles holding both cookies."""
        return tuple(p for p in self.profiles if p.complete)


# ---------------------------------------------------------------------------
# Keychain
# ---------------------------------------------------------------------------


def read_safe_storage_password(service: str) -> Secret:
    """Read a browser's "Safe Storage" password from the macOS Keychain.

    The password comes back on **stdout**; argv carries only the service name,
    so nothing secret is visible to ``ps``. macOS shows its own approval prompt
    the first time ("security wants to use your confidential information": the
    requester is ``/usr/bin/security``). Callers should advise **Allow**, not
    Always Allow, which would let any process run this same command silently.

    :raises KeychainEntryMissing: the browser has no such entry.
    :raises KeychainAccessDenied: the user declined, or the read failed.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            check=False,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise KeychainAccessDenied(f"Could not read the {service!r} Keychain entry.") from None
    if proc.returncode == _SECURITY_ITEM_NOT_FOUND:
        raise KeychainEntryMissing(f"No {service!r} Keychain entry.")
    if proc.returncode != 0:
        raise KeychainAccessDenied(f"Access to the {service!r} Keychain entry was denied.")
    password = Secret(proc.stdout.rstrip("\r\n"))
    if not password:
        raise KeychainEntryMissing(f"The {service!r} Keychain entry is empty.")
    return password


class _Key:
    """A derived AES key that refuses to render itself."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def reveal(self) -> bytes:
        return self._raw

    def __repr__(self) -> str:
        return "<AES key redacted>"

    __str__ = __repr__


def _derive_key(password: Secret) -> _Key:
    import hashlib

    return _Key(
        hashlib.pbkdf2_hmac("sha1", password.reveal().encode("utf-8"), _SALT, _ITERATIONS, 16)
    )


# ---------------------------------------------------------------------------
# Decryption — each function returns a Secret; none lets plaintext escape
# ---------------------------------------------------------------------------


def _cookie_credential_name(cookie: str) -> str:
    return "swid" if cookie.lower() == "swid" else "espn_s2"


def _decrypt_text(cookie: str, blob: bytes, key: _Key, *, strip_host_hash: bool) -> str:
    plain = _aes.pkcs7_unpad(_aes.aes128_cbc_decrypt(key.reveal(), _IV, blob[3:]))
    if strip_host_hash:
        plain = plain[32:]
    return normalize_credential(_cookie_credential_name(cookie), plain.decode("utf-8"))


def _decrypt_value(cookie: str, blob: bytes, key: _Key, *, strip_host_hash: bool) -> Secret:
    """Decrypt one ``v10`` blob straight into a ``Secret``.

    The failure is raised *after* the ``except`` block: raising inside it would
    leave the failed ``_decrypt_text`` frame — and its plaintext local — attached
    as ``__context__`` for a captured-locals traceback to repr.
    """
    secret: Secret | None = None
    # Any failure is "could not decrypt", never a value.
    with contextlib.suppress(Exception):
        secret = Secret(_decrypt_text(cookie, blob, key, strip_host_hash=strip_host_hash))
    if secret is None:
        raise CookieDecryptError(f"Could not decrypt the {cookie} cookie (wrong key?).")
    return secret


def _plain_value(cookie: str, raw: Secret) -> Secret:
    """Validate a cookie the browser stored unencrypted.

    Takes a ``Secret``, not a ``str``: an argument is a local of this frame, and
    a failure here would otherwise put the unvalidated value in front of a
    captured-locals traceback.
    """
    secret: Secret | None = None
    with contextlib.suppress(Exception):  # see _decrypt_value
        secret = Secret(normalize_credential(_cookie_credential_name(cookie), raw.reveal()))
    if secret is None:
        raise CookieDecryptError(f"The {cookie} cookie is not a valid value.")
    return secret


# ---------------------------------------------------------------------------
# SQLite access
# ---------------------------------------------------------------------------


@contextmanager
def _open_copy(db: Path) -> Iterator[sqlite3.Connection]:
    """Open a throwaway copy of ``db``; the copy is deleted on the way out.

    Chrome holds a lock on its live file, and Firefox keeps recent writes in a
    ``-wal`` sidecar, so the sidecar is copied alongside when present.
    """
    import shutil
    import sqlite3
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="fantasy-sports-cookies-"))
    try:
        copy = scratch / db.name
        shutil.copy2(db, copy)
        sidecar = db.with_name(db.name + "-wal")
        if sidecar.exists():
            shutil.copy2(sidecar, scratch / sidecar.name)
        connection = sqlite3.connect(copy)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _chrome_expiry(expires_utc: object) -> datetime | None:
    """``expires_utc`` is microseconds since 1601-01-01; ``0`` is a session cookie."""
    if not isinstance(expires_utc, int) or expires_utc <= 0:
        return None
    try:
        return _CHROME_EPOCH + timedelta(microseconds=expires_utc)
    except OverflowError:
        return None


def _firefox_expiry(expiry: object) -> datetime | None:
    """``moz_cookies.expiry`` is seconds since the epoch (milliseconds in old profiles)."""
    if not isinstance(expiry, int) or expiry <= 0:
        return None
    seconds = expiry / 1000 if expiry > 10**11 else expiry
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class _ChromeRow:
    cookie: str
    expires_at: datetime | None
    blob: bytes
    plain: Secret


def _chrome_rows(connection: sqlite3.Connection) -> tuple[int, list[_ChromeRow]]:
    """Return ``(schema_version, best row per cookie name)``.

    ``value`` is selected only where there is no ``encrypted_value``, so a
    plaintext cookie is the only kind that ever reaches a Python ``str`` here.
    """
    try:
        meta = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        version = int(meta[0]) if meta else 0
    except (ValueError, TypeError):
        version = 0
    hosts = ",".join("?" * len(ESPN_HOSTS))
    names = ",".join("?" * len(COOKIE_NAMES))
    query = (
        "SELECT name, expires_utc, encrypted_value, "
        "CASE WHEN length(encrypted_value) = 0 THEN value ELSE '' END "
        f"FROM cookies WHERE host_key IN ({hosts}) AND name IN ({names})"
    )
    best: dict[str, _ChromeRow] = {}
    for name, expires_utc, blob, plain in connection.execute(query, (*ESPN_HOSTS, *COOKIE_NAMES)):
        row = _ChromeRow(name, _chrome_expiry(expires_utc), bytes(blob or b""), Secret(plain or ""))
        held = best.get(name)
        if held is None or _later(row.expires_at, held.expires_at):
            best[name] = row
    return version, list(best.values())


def _later(candidate: datetime | None, held: datetime | None) -> bool:
    if candidate is None:
        return False
    return held is None or candidate > held


def _firefox_cookies(connection: sqlite3.Connection) -> dict[str, tuple[Secret, datetime | None]]:
    hosts = ",".join("?" * len(ESPN_HOSTS))
    names = ",".join("?" * len(COOKIE_NAMES))
    query = (
        f"SELECT name, value, expiry FROM moz_cookies WHERE host IN ({hosts}) AND name IN ({names})"
    )
    best: dict[str, tuple[Secret, datetime | None]] = {}
    for name, value, expiry in connection.execute(query, (*ESPN_HOSTS, *COOKIE_NAMES)):
        expires_at = _firefox_expiry(expiry)
        held = best.get(name)
        if held is None or _later(expires_at, held[1]):
            best[name] = (_plain_value(name, Secret(value or "")), expires_at)
    return best


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------


@dataclass
class _Collector:
    profiles: list[ProfileCookies] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    keychain_denied: bool = False

    def freeze(self) -> BrowserSearch:
        return BrowserSearch(
            profiles=tuple(self.profiles),
            searched=tuple(self.searched),
            skipped=tuple(self.skipped),
            keychain_denied=self.keychain_denied,
        )


def _profile_from(browser: str, profile: str, found: dict[str, tuple[Secret, datetime | None]]):
    s2 = found.get("espn_s2")
    swid = found.get("SWID")
    if s2 is None and swid is None:
        return None
    return ProfileCookies(
        browser=browser,
        profile=profile,
        espn_s2=s2[0] if s2 else None,
        swid=swid[0] if swid else None,
        espn_s2_expires_at=s2[1] if s2 else None,
        swid_expires_at=swid[1] if swid else None,
    )


def _cookie_db(profile_dir: Path) -> Path | None:
    for candidate in (profile_dir / "Network" / "Cookies", profile_dir / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _scan_chrome_family(
    family: _ChromeFamily, root: Path, only_profile: str | None, out: _Collector
) -> None:
    if not root.is_dir():
        return
    key: _Key | None = None
    for profile_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if only_profile is not None and profile_dir.name != only_profile:
            continue
        db = _cookie_db(profile_dir)
        if db is None:
            continue
        label = f"{family.label}/{profile_dir.name}"
        out.searched.append(label)
        try:
            with _open_copy(db) as connection:
                version, rows = _chrome_rows(connection)
        except Exception:  # noqa: BLE001 - an unreadable store is a skipped profile
            out.skipped.append(f"{label}: cookie store could not be read")
            continue
        if not rows:
            continue

        found: dict[str, tuple[Secret, datetime | None]] = {}
        try:
            for row in rows:
                if not row.blob:
                    found[row.cookie] = (_plain_value(row.cookie, row.plain), row.expires_at)
                    continue
                if not row.blob.startswith(b"v10"):
                    prefix = row.blob[:3].decode("ascii", "replace")
                    out.skipped.append(f"{label}: unsupported cookie encryption ({prefix})")
                    found.clear()
                    break
                if key is None:
                    key = _derive_key(read_safe_storage_password(family.keychain_service))
                secret = _decrypt_value(
                    row.cookie, row.blob, key, strip_host_hash=version >= _HASH_PREFIX_SCHEMA
                )
                found[row.cookie] = (secret, row.expires_at)
        except KeychainAccessDenied:
            out.keychain_denied = True
            out.skipped.append(
                f"{family.label}: macOS Keychain access to {family.keychain_service!r} was denied"
            )
            return
        except KeychainEntryMissing:
            out.skipped.append(f"{family.label}: no {family.keychain_service!r} Keychain entry")
            return
        except CookieDecryptError as exc:
            out.skipped.append(f"{label}: {exc}")
            continue
        profile = _profile_from(family.label, profile_dir.name, found)
        if profile is not None:
            out.profiles.append(profile)


def _firefox_roots(home: Path, platform: str) -> list[Path]:
    if platform == "darwin":
        return [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    return [home / ".mozilla" / "firefox"]


def _scan_firefox(home: Path, platform: str, only_profile: str | None, out: _Collector) -> None:
    for root in _firefox_roots(home, platform):
        if not root.is_dir():
            continue
        for profile_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if only_profile is not None and profile_dir.name != only_profile:
                continue
            db = profile_dir / "cookies.sqlite"
            if not db.is_file():
                continue
            label = f"Firefox/{profile_dir.name}"
            out.searched.append(label)
            try:
                with _open_copy(db) as connection:
                    found = _firefox_cookies(connection)
            except CookieDecryptError as exc:
                out.skipped.append(f"{label}: {exc}")
                continue
            except Exception:  # noqa: BLE001 - an unreadable store is a skipped profile
                out.skipped.append(f"{label}: cookie store could not be read")
                continue
            profile = _profile_from("Firefox", profile_dir.name, found)
            if profile is not None:
                out.profiles.append(profile)


def find_espn_cookies(
    browser: str | None = None,
    profile: str | None = None,
    *,
    home: Path | None = None,
    platform: str | None = None,
) -> BrowserSearch:
    """Search local browsers for ESPN's cookies.

    ``browser`` narrows to one of :data:`BROWSER_NAMES` (case-insensitive);
    ``profile`` narrows to one profile directory name. ``home`` and ``platform``
    are seams for tests. Never raises for a missing, locked, or unsupported
    store — each is an entry in :attr:`BrowserSearch.skipped`.

    :raises ValueError: ``browser`` is not a known name.
    """
    wanted = None if browser is None else browser.strip().lower()
    if wanted is not None and wanted not in BROWSER_NAMES:
        raise ValueError(f"Unknown browser {browser!r}; choose one of: {', '.join(BROWSER_NAMES)}.")
    root = Path.home() if home is None else home
    current = sys.platform if platform is None else platform
    out = _Collector()

    for family in _CHROME_FAMILY:
        if wanted not in (None, family.name):
            continue
        if current == "darwin":
            support = root / "Library" / "Application Support" / family.support_dir
            _scan_chrome_family(family, support, profile, out)
        elif wanted is not None or _linux_store_exists(root, family):
            out.skipped.append(
                f"{family.label}: unsupported on {current} "
                "(v11 / libsecret decryption is not implemented)"
            )

    if wanted in (None, "firefox"):
        if current == "darwin" or current.startswith("linux"):
            _scan_firefox(root, current, profile, out)
        elif wanted is not None:
            out.skipped.append(f"Firefox: unsupported on {current}")

    if wanted is None and current != "darwin" and not current.startswith("linux"):
        out.skipped.append(f"All browsers: unsupported on {current}")
    return out.freeze()


def _linux_store_exists(home: Path, family: _ChromeFamily) -> bool:
    return bool(family.linux_dir) and (home / ".config" / family.linux_dir).is_dir()
