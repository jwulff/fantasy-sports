"""``auth login --from-browser``: reading ESPN's cookies out of local browser profiles (#156).

Everything runs against fixture SQLite stores built in ``tmp_path``. Chrome
values are encrypted with the test-only AES encrypt (``_aes_encrypt``) under a
fake Safe Storage password injected where ``read_safe_storage_password`` is
called; no real browser profile, no real Keychain, and no ``security`` child
process is ever touched (``_no_real_subprocess`` makes that a failure, not a
hope). The redaction tests come first and follow ``test_auth.py``'s pattern:
render a real string, search it for a sentinel.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _aes_encrypt import aes128_cbc_encrypt, pkcs7_pad
from _harness import FAKE_SWID, isolate_home

from fantasy_sports.auth import browser, chain, staleness
from fantasy_sports.commands import auth as auth_commands
from fantasy_sports.core.errors import AuthMissingError, ConfigInvalidError

browser_real = browser.find_espn_cookies

# Sentinels that cannot occur by accident in any rendered output.
COOKIE_S2 = "AEBsentinelS2cookievalue0123456789abcdefXYZ%2Fzz"
PASSWORD = "SafeStorageSENTINELpassword/with+chars=="
SWID = FAKE_SWID

EXPIRES_S2 = datetime(2027, 11, 29, 19, 21, 31, tzinfo=UTC)
EXPIRES_SWID = datetime(2027, 10, 18, 19, 21, 41, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _forget_secrets():
    chain.forget_secrets()
    yield
    chain.forget_secrets()


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch: pytest.MonkeyPatch):
    """The real `security` binary must be unreachable from this file."""

    def refuse(*args: Any, **kwargs: Any):
        pytest.fail("a test reached the real subprocess / Keychain")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture(autouse=True)
def _pin_platform_to_macos(monkeypatch: pytest.MonkeyPatch):
    """Chrome-family stores are only read on macOS, so the fixtures here assume it.

    The command and CLI tests reach ``find_espn_cookies`` without the ``platform``
    seam, so on a Linux runner ``sys.platform`` would skip every Chrome-family
    scan. The default is pinned at call time (a default argument would be bound
    once, ``docs/memory/default-args-defeat-monkeypatch.md``); a test that passes
    ``platform=`` explicitly, like the Linux ones, still wins.
    """
    real = browser.find_espn_cookies

    def pinned(*args: Any, **kwargs: Any) -> browser.BrowserSearch:
        kwargs.setdefault("platform", "darwin")
        return real(*args, **kwargs)

    monkeypatch.setattr(browser, "find_espn_cookies", pinned)


@pytest.fixture
def passwords(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject Safe Storage passwords by service. A missing key raises like the real reader."""
    table: dict[str, Any] = {
        "Chrome Safe Storage": PASSWORD,
        "Brave Safe Storage": PASSWORD + "-brave",
    }
    asked: list[str] = []

    def fake(service: str) -> chain.Secret:
        asked.append(service)
        outcome = table.get(service)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise browser.KeychainEntryMissing(f"No {service!r} Keychain entry.")
        return chain.Secret(outcome)

    monkeypatch.setattr(browser, "read_safe_storage_password", fake)
    table["_asked"] = asked
    return table


# ---------------------------------------------------------------------------
# Fixture stores
# ---------------------------------------------------------------------------


def _chrome_micros(moment: datetime) -> int:
    return int((moment - datetime(1601, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1))


def _seal(value: str, password: str, host_key: str, version: int) -> bytes:
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)
    prefix = hashlib.sha256(host_key.encode()).digest() if version >= 24 else b""
    return b"v10" + aes128_cbc_encrypt(key, b" " * 16, pkcs7_pad(prefix + value.encode()))


def chrome_store(
    path: Path,
    *,
    version: int = 24,
    password: str = PASSWORD,
    s2: str | None = COOKIE_S2,
    swid: str | None = SWID,
    s2_expires: datetime | None = EXPIRES_S2,
    swid_expires: datetime | None = EXPIRES_SWID,
    extra: list[tuple[Any, ...]] = (),  # type: ignore[assignment]
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT UNIQUE NOT NULL, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    db.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER)"
    )
    host = ".espn.com"
    rows: list[tuple[Any, ...]] = []
    if s2 is not None:
        expiry = 0 if s2_expires is None else _chrome_micros(s2_expires)
        rows.append((host, "espn_s2", "", _seal(s2, password, host, version), expiry))
    if swid is not None:
        expiry = 0 if swid_expires is None else _chrome_micros(swid_expires)
        rows.append((host, "SWID", "", _seal(swid, password, host, version), expiry))
    db.executemany("INSERT INTO cookies VALUES (?,?,?,?,?)", [*rows, *extra])
    db.commit()
    db.close()
    return path


def firefox_store(
    path: Path, *, s2: str | None = COOKIE_S2, swid: str | None = SWID, expiry: int = 1827000000
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)")
    if s2 is not None:
        db.execute("INSERT INTO moz_cookies VALUES ('.espn.com','espn_s2',?,?)", (s2, expiry))
    if swid is not None:
        db.execute("INSERT INTO moz_cookies VALUES ('.espn.com','SWID',?,?)", (swid, expiry))
    db.commit()
    db.close()
    return path


def support(home: Path) -> Path:
    return home / "Library" / "Application Support"


def chrome_profile(home: Path, name: str = "Default", *, network: bool = False, **kw: Any) -> Path:
    base = support(home) / "Google" / "Chrome" / name
    return chrome_store(base / ("Network/Cookies" if network else "Cookies"), **kw)


def search(home: Path, **kwargs: Any) -> browser.BrowserSearch:
    return browser.find_espn_cookies(home=home, platform="darwin", **kwargs)


# ---------------------------------------------------------------------------
# Redaction — the security control
# ---------------------------------------------------------------------------


def _render_everything(search_result: browser.BrowserSearch) -> str:
    return "\n".join(
        [
            repr(search_result),
            str(search_result),
            *(repr(p) for p in search_result.profiles),
            *(f"{p.espn_s2} {p.swid} {p.espn_s2!r} {p.swid!r}" for p in search_result.profiles),
            *search_result.skipped,
        ]
    )


def test_a_found_profile_renders_without_either_cookie(tmp_path: Path, passwords):
    chrome_profile(tmp_path)
    found = search(tmp_path)
    assert found.complete
    text = _render_everything(found)
    for sentinel in (COOKIE_S2, SWID, SWID.strip("{}"), PASSWORD):
        assert sentinel not in text
    assert all(isinstance(p.espn_s2, chain.Secret) for p in found.profiles)
    assert all(isinstance(p.swid, chain.Secret) for p in found.profiles)


def _boom_with_plaintext_local(*args: Any, **kwargs: Any) -> str:
    plaintext = COOKIE_S2
    raise RuntimeError("decrypt blew up mid-way" if plaintext else "")


def _capture_locals(exc: BaseException) -> str:
    rendered = traceback.TracebackException.from_exception(exc, capture_locals=True)
    return "".join(rendered.format())


def test_control_a_plain_str_local_does_leak_in_a_captured_locals_traceback():
    """If this stops leaking, the test below has stopped measuring anything."""
    with pytest.raises(RuntimeError) as caught:
        _boom_with_plaintext_local()
    assert COOKIE_S2 in _capture_locals(caught.value)


def test_a_failure_mid_decrypt_leaves_no_plaintext_in_a_captured_locals_traceback(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(browser, "_decrypt_text", _boom_with_plaintext_local)
    key = browser._derive_key(chain.Secret(PASSWORD))
    with pytest.raises(browser.CookieDecryptError) as caught:
        browser._decrypt_value("espn_s2", b"v10" + bytes(16), key, strip_host_hash=True)
    rendered = _capture_locals(caught.value) + str(caught.value) + repr(caught.value)
    assert COOKIE_S2 not in rendered
    assert PASSWORD not in rendered
    assert caught.value.__context__ is None  # the failed frame is not left attached


def test_the_derived_key_and_password_redact_themselves():
    key = browser._derive_key(chain.Secret(PASSWORD))
    assert key.reveal().hex() not in f"{key!r}{key}"
    assert PASSWORD not in f"{chain.Secret(PASSWORD)!r}"


def test_a_plaintext_cookie_that_fails_validation_never_echoes_it():
    with pytest.raises(browser.CookieDecryptError) as caught:
        browser._plain_value("SWID", chain.Secret("not-a-guid-" + COOKIE_S2))
    assert COOKIE_S2 not in str(caught.value) + _capture_locals(caught.value)


def test_wrong_password_is_a_skipped_profile_and_names_no_value(tmp_path: Path, passwords):
    chrome_profile(tmp_path, password="a-different-password")
    found = search(tmp_path)
    assert found.profiles == ()
    assert any("Chrome/Default" in line and "decrypt" in line for line in found.skipped)
    assert COOKIE_S2 not in _render_everything(found)
    assert PASSWORD not in _render_everything(found)


@pytest.mark.parametrize(
    "error",
    [
        browser.KeychainAccessDenied(
            f"Access to the {'Chrome Safe Storage'!r} Keychain entry was denied."
        ),
        browser.KeychainEntryMissing("No 'Chrome Safe Storage' Keychain entry."),
        browser.CookieDecryptError("Could not decrypt the espn_s2 cookie (wrong key?)."),
    ],
)
def test_error_messages_carry_no_secret(error: Exception):
    assert PASSWORD not in str(error) and COOKIE_S2 not in str(error)


# ---------------------------------------------------------------------------
# Reading the Keychain
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["security"], returncode, stdout=stdout, stderr="")


def test_the_password_is_read_from_stdout_and_never_appears_in_argv(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any):
        seen.append(argv)
        return _completed(0, PASSWORD + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    password = browser.read_safe_storage_password("Chrome Safe Storage")
    assert isinstance(password, chain.Secret)
    assert password.reveal() == PASSWORD
    assert seen == [["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"]]
    assert PASSWORD not in " ".join(seen[0])


def test_the_derived_key_never_reaches_a_child_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No subprocess is spawned by decryption at all; the pure-Python AES does it."""
    chrome_profile(tmp_path)
    spawned: list[Any] = []
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: spawned.append(a) or _completed(0, PASSWORD)
    )
    monkeypatch.setattr(browser, "read_safe_storage_password", lambda svc: chain.Secret(PASSWORD))
    assert search(tmp_path).complete
    assert spawned == []


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (44, browser.KeychainEntryMissing),
        (128, browser.KeychainAccessDenied),
        (36, browser.KeychainAccessDenied),
    ],
)
def test_security_exit_codes_map_to_typed_errors(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: type[Exception]
):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(returncode))
    with pytest.raises(expected):
        browser.read_safe_storage_password("Chrome Safe Storage")


def test_an_empty_password_counts_as_a_missing_entry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0, "\n"))
    with pytest.raises(browser.KeychainEntryMissing):
        browser.read_safe_storage_password("Chrome Safe Storage")


@pytest.mark.parametrize(
    "failure", [OSError("no security binary"), subprocess.TimeoutExpired("security", 1)]
)
def test_a_security_that_cannot_run_is_access_denied(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
):
    def fail(*a: Any, **k: Any):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(browser.KeychainAccessDenied):
        browser.read_safe_storage_password("Chrome Safe Storage")


# ---------------------------------------------------------------------------
# Chrome-family stores
# ---------------------------------------------------------------------------


def test_schema_24_drops_the_host_hash_prefix_and_reads_real_expiry(tmp_path: Path, passwords):
    chrome_profile(tmp_path, version=24)
    (profile,) = search(tmp_path).profiles
    assert (profile.browser, profile.profile) == ("Chrome", "Default")
    assert profile.espn_s2 is not None and profile.espn_s2.reveal() == COOKIE_S2
    assert profile.swid is not None and profile.swid.reveal() == SWID
    assert profile.espn_s2_expires_at == EXPIRES_S2
    assert profile.swid_expires_at == EXPIRES_SWID
    assert profile.complete


def test_a_pre_24_store_has_no_prefix_to_drop(tmp_path: Path, passwords):
    chrome_profile(tmp_path, version=23)
    (profile,) = search(tmp_path).profiles
    assert profile.espn_s2 is not None and profile.espn_s2.reveal() == COOKIE_S2
    assert profile.swid is not None and profile.swid.reveal() == SWID


def test_reading_a_pre_24_store_as_24_would_not_yield_the_cookie(tmp_path: Path, passwords):
    """The two schemas differ for real: the version number is what selects the strip."""
    chrome_profile(tmp_path, version=23)
    db = support(tmp_path) / "Google/Chrome/Default/Cookies"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '24'")
    conn.commit()
    conn.close()
    assert search(tmp_path).complete == ()


def test_network_cookies_is_found_and_profiles_without_a_store_are_ignored(
    tmp_path: Path, passwords
):
    chrome_profile(tmp_path, "Profile 2", network=True)
    (support(tmp_path) / "Google/Chrome/System Profile").mkdir(parents=True)
    found = search(tmp_path)
    assert [p.profile for p in found.profiles] == ["Profile 2"]
    assert found.searched == ("Chrome/Profile 2",)


def test_a_profile_without_espn_cookies_is_searched_but_not_listed(tmp_path: Path, passwords):
    chrome_profile(tmp_path, "Profile 1", s2=None, swid=None)
    found = search(tmp_path)
    assert found.profiles == ()
    assert found.searched == ("Chrome/Profile 1",)
    assert passwords["_asked"] == []  # nothing to decrypt, so no Keychain prompt


def test_only_espn_hosts_and_names_match(tmp_path: Path, passwords):
    decoy = (".example.com", "espn_s2", "", _seal("x" * 40, PASSWORD, ".example.com", 24), 1)
    other = (".espn.com", "unrelated", "", _seal("y" * 40, PASSWORD, ".espn.com", 24), 1)
    chrome_profile(tmp_path, s2=None, swid=None, extra=[decoy, other])
    assert search(tmp_path).profiles == ()


@pytest.mark.parametrize("host", [".espn.com", "espn.com", "fantasy.espn.com", ".fantasy.espn.com"])
def test_every_listed_host_is_accepted(tmp_path: Path, passwords, host: str):
    rows = [
        (host, "espn_s2", "", _seal(COOKIE_S2, PASSWORD, host, 24), _chrome_micros(EXPIRES_S2)),
        (host, "SWID", "", _seal(SWID, PASSWORD, host, 24), _chrome_micros(EXPIRES_SWID)),
    ]
    chrome_profile(tmp_path, s2=None, swid=None, extra=rows)
    assert search(tmp_path).complete


def test_duplicate_rows_resolve_to_the_latest_expiry(tmp_path: Path, passwords):
    later = EXPIRES_S2 + timedelta(days=30)
    row = (
        ".fantasy.espn.com",
        "espn_s2",
        "",
        _seal("Z" * 50, PASSWORD, ".fantasy.espn.com", 24),
        _chrome_micros(later),
    )
    chrome_profile(tmp_path, extra=[row])
    (profile,) = search(tmp_path).profiles
    assert profile.espn_s2_expires_at == later
    assert profile.espn_s2 is not None and profile.espn_s2.reveal() == "Z" * 50


def test_a_session_cookie_has_no_expiry(tmp_path: Path, passwords):
    chrome_profile(tmp_path, s2_expires=None, swid_expires=None)
    (profile,) = search(tmp_path).profiles
    assert profile.espn_s2_expires_at is None and profile.swid_expires_at is None


def test_a_plaintext_value_with_no_encrypted_blob_is_accepted(tmp_path: Path, passwords):
    rows = [
        (".espn.com", "espn_s2", COOKIE_S2, b"", _chrome_micros(EXPIRES_S2)),
        (".espn.com", "SWID", SWID, b"", _chrome_micros(EXPIRES_SWID)),
    ]
    chrome_profile(tmp_path, s2=None, swid=None, extra=rows)
    (profile,) = search(tmp_path).profiles
    assert profile.espn_s2 is not None and profile.espn_s2.reveal() == COOKIE_S2
    assert passwords["_asked"] == []


def test_the_scratch_copy_is_deleted_and_the_original_is_untouched(
    tmp_path: Path, passwords, monkeypatch: pytest.MonkeyPatch
):
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch_root))
    live = chrome_profile(tmp_path)
    before = live.read_bytes()
    assert search(tmp_path).complete
    assert list(scratch_root.iterdir()) == []
    assert live.read_bytes() == before


def test_the_scratch_copy_is_deleted_even_when_the_store_is_unreadable(
    tmp_path: Path, passwords, monkeypatch: pytest.MonkeyPatch
):
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch_root))
    bad = support(tmp_path) / "Google/Chrome/Default/Cookies"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a sqlite database at all" * 20)
    found = search(tmp_path)
    assert any("could not be read" in line for line in found.skipped)
    assert list(scratch_root.iterdir()) == []


def test_each_chrome_family_browser_uses_its_own_safe_storage_service(tmp_path: Path, passwords):
    layout = {
        "Chromium": ("Chromium", "Chromium Safe Storage"),
        "Brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage"),
        "Edge": ("Microsoft Edge", "Microsoft Edge Safe Storage"),
        "Arc": ("Arc/User Data", "Arc Safe Storage"),
    }
    for label, (directory, service) in layout.items():
        passwords[service] = f"pw-for-{label}"
        chrome_store(
            support(tmp_path) / directory / "Default" / "Cookies", password=f"pw-for-{label}"
        )
    found = search(tmp_path)
    assert sorted(p.browser for p in found.profiles) == sorted(layout)
    assert sorted(passwords["_asked"]) == sorted(service for _, service in layout.values())


def test_the_password_is_asked_once_per_browser_not_once_per_profile(tmp_path: Path, passwords):
    chrome_profile(tmp_path, "Default")
    chrome_profile(tmp_path, "Profile 1")
    assert len(search(tmp_path).profiles) == 2
    assert passwords["_asked"] == ["Chrome Safe Storage"]


def test_browser_and_profile_narrow_the_search(tmp_path: Path, passwords):
    chrome_profile(tmp_path, "Default")
    chrome_profile(tmp_path, "Profile 1")
    chrome_store(
        support(tmp_path) / "BraveSoftware/Brave-Browser/Default/Cookies",
        password=PASSWORD + "-brave",
    )
    assert [p.browser for p in search(tmp_path, browser="BRAVE").profiles] == ["Brave"]
    assert [
        p.profile for p in search(tmp_path, browser="chrome", profile="Profile 1").profiles
    ] == ["Profile 1"]


def test_an_unknown_browser_is_a_value_error(tmp_path: Path):
    with pytest.raises(ValueError, match="firefox"):
        search(tmp_path, browser="netscape")


def test_a_denied_keychain_prompt_is_reported_and_stops_that_browser(tmp_path: Path, passwords):
    passwords["Chrome Safe Storage"] = browser.KeychainAccessDenied("denied")
    chrome_profile(tmp_path, "Default")
    chrome_profile(tmp_path, "Profile 1")
    found = search(tmp_path)
    assert found.keychain_denied
    assert found.profiles == ()
    assert any("Keychain access" in line and "denied" in line for line in found.skipped)
    assert passwords["_asked"] == ["Chrome Safe Storage"]


def test_a_missing_safe_storage_entry_is_skipped_not_raised(tmp_path: Path, passwords):
    del passwords["Chrome Safe Storage"]
    chrome_profile(tmp_path)
    found = search(tmp_path)
    assert not found.keychain_denied
    assert any("no 'Chrome Safe Storage' Keychain entry" in line for line in found.skipped)


def test_a_v11_blob_is_skipped_as_unsupported_not_crashed_on(tmp_path: Path, passwords):
    rows = [
        (".espn.com", "espn_s2", "", b"v11" + bytes(32), 1),
        (".espn.com", "SWID", "", b"v11" + bytes(32), 1),
    ]
    chrome_profile(tmp_path, s2=None, swid=None, extra=rows)
    found = search(tmp_path)
    assert found.profiles == ()
    assert any("unsupported cookie encryption (v11)" in line for line in found.skipped)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_chrome_family_off_macos_is_reported_unsupported(tmp_path: Path, platform: str):
    (tmp_path / ".config" / "google-chrome").mkdir(parents=True)
    found = browser.find_espn_cookies(home=tmp_path, platform=platform)
    assert found.profiles == ()
    if platform == "linux":
        assert any(line.startswith("Chrome: unsupported on linux") for line in found.skipped)
    else:
        assert any("unsupported on win32" in line for line in found.skipped)


def test_asking_for_a_named_browser_off_macos_always_says_so(tmp_path: Path):
    found = browser.find_espn_cookies("brave", home=tmp_path, platform="linux")
    assert any(line.startswith("Brave: unsupported on linux") for line in found.skipped)


# ---------------------------------------------------------------------------
# Firefox
# ---------------------------------------------------------------------------


def test_firefox_macos_profiles_are_read_in_plain_text(tmp_path: Path, passwords):
    firefox_store(support(tmp_path) / "Firefox/Profiles/abc.default-release/cookies.sqlite")
    (profile,) = search(tmp_path).profiles
    assert (profile.browser, profile.profile) == ("Firefox", "abc.default-release")
    assert profile.espn_s2 is not None and profile.espn_s2.reveal() == COOKIE_S2
    assert profile.espn_s2_expires_at == datetime.fromtimestamp(1827000000, tz=UTC)
    assert passwords["_asked"] == []


def test_firefox_linux_profiles_are_read(tmp_path: Path):
    firefox_store(tmp_path / ".mozilla/firefox/xyz.default/cookies.sqlite")
    found = browser.find_espn_cookies(home=tmp_path, platform="linux")
    assert [p.label for p in found.profiles] == ["Firefox/xyz.default"]


def test_firefox_millisecond_expiry_is_normalised():
    assert browser._firefox_expiry(1827000000000) == datetime.fromtimestamp(1827000000, tz=UTC)
    assert browser._firefox_expiry(0) is None
    assert browser._firefox_expiry(None) is None


def test_firefox_with_only_one_cookie_is_listed_but_incomplete(tmp_path: Path):
    firefox_store(support(tmp_path) / "Firefox/Profiles/p/cookies.sqlite", swid=None)
    (profile,) = search(tmp_path).profiles
    assert not profile.complete
    assert search(tmp_path).complete == ()


def test_firefox_wal_sidecar_is_copied_alongside(tmp_path: Path):
    db_path = support(tmp_path) / "Firefox/Profiles/p/cookies.sqlite"
    db_path.parent.mkdir(parents=True)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)")
    db.execute("INSERT INTO moz_cookies VALUES ('.espn.com','espn_s2',?,1827000000)", (COOKIE_S2,))
    db.execute("INSERT INTO moz_cookies VALUES ('.espn.com','SWID',?,1827000000)", (SWID,))
    db.commit()  # left in the -wal file: the connection stays open
    assert db_path.with_name("cookies.sqlite-wal").exists()
    try:
        assert search(tmp_path).complete
    finally:
        db.close()


def test_a_garbage_firefox_value_skips_the_profile_without_echoing_it(tmp_path: Path):
    firefox_store(
        support(tmp_path) / "Firefox/Profiles/p/cookies.sqlite", swid="not a guid " + COOKIE_S2
    )
    found = search(tmp_path)
    assert found.profiles == ()
    assert COOKIE_S2 not in _render_everything(found)


# ---------------------------------------------------------------------------
# auth login --from-browser
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    isolate_home(tmp_path, monkeypatch)
    return tmp_path


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    written: dict[str, str] = {}
    monkeypatch.setattr("fantasy_sports.auth.chain.write_to_keychain", written.__setitem__)
    return written


def test_login_from_browser_stores_through_save_credentials_and_records_expiry(
    home: Path, passwords, keychain
):
    chrome_profile(home)
    envelope = auth_commands.login(from_browser="chrome")
    data = envelope.data
    assert keychain == {"espn_s2": COOKIE_S2, "swid": SWID}
    assert data["stored"] == ["espn_s2", "swid"]
    assert data["source"] == {"browser": "Chrome", "profile": "Default"}
    assert data["expires_at"] == {"espn_s2": "2027-11-29T19:21:31Z", "swid": "2027-10-18T19:21:41Z"}
    state = staleness.load_auth_state()
    assert state.for_name("espn_s2").stored_at is not None
    assert state.for_name("espn_s2").expires_at == EXPIRES_S2
    assert state.for_name("swid").expires_at == EXPIRES_SWID


def test_a_braceless_swid_from_a_browser_is_repaired_on_the_way_in(home: Path, passwords, keychain):
    chrome_profile(home, swid=SWID.strip("{}"))
    # The browser module already normalizes, so the stored form is braced.
    auth_commands.login(from_browser="chrome")
    assert keychain["swid"] == SWID


def test_the_envelope_carries_no_value_length_or_prefix(home: Path, passwords, keychain):
    chrome_profile(home)
    rendered = json.dumps(auth_commands.login(from_browser="chrome").data)
    for fragment in (COOKIE_S2, COOKIE_S2[:8], SWID, SWID.strip("{}"), PASSWORD):
        assert fragment not in rendered
    assert str(len(COOKIE_S2)) not in json.dumps(
        auth_commands.login(from_browser="chrome").data["source"]
    )


def test_bare_flag_value_searches_every_browser(home: Path, passwords, keychain):
    from fantasy_sports.commands import FROM_BROWSER_ANY

    firefox_store(support(home) / "Firefox/Profiles/p/cookies.sqlite")
    data = auth_commands.login(from_browser=FROM_BROWSER_ANY).data
    assert data["source"]["browser"] == "Firefox"


def test_several_complete_profiles_pick_the_latest_s2_expiry_and_name_the_others(
    home: Path, passwords, keychain
):
    other_s2 = "Q" * 60
    chrome_profile(home, "Default", s2_expires=EXPIRES_S2)
    chrome_profile(home, "Profile 1", s2=other_s2, s2_expires=EXPIRES_S2 + timedelta(days=60))
    chrome_profile(home, "Profile 2", s2=None)
    data = auth_commands.login(from_browser="chrome").data
    assert keychain["espn_s2"] == other_s2
    assert data["source"]["profile"] == "Profile 1"
    seen = {(row["profile"], row["complete"], row["chosen"]) for row in data["profiles_seen"]}
    assert seen == {
        ("Default", True, False),
        ("Profile 1", True, True),
        ("Profile 2", False, False),
    }
    assert data["searched"] == ["Chrome/Default", "Chrome/Profile 1", "Chrome/Profile 2"]


def test_profile_narrows_to_one_directory(home: Path, passwords, keychain):
    chrome_profile(home, "Default")
    chrome_profile(home, "Profile 1", s2="Q" * 60, s2_expires=EXPIRES_S2 + timedelta(days=60))
    data = auth_commands.login(from_browser="chrome", profile="Default").data
    assert data["source"]["profile"] == "Default"
    assert keychain["espn_s2"] == COOKIE_S2


def test_no_complete_profile_is_auth_missing_listing_what_was_searched(
    home: Path, passwords, keychain
):
    chrome_profile(home, "Default", swid=None)
    firefox_store(support(home) / "Firefox/Profiles/p/cookies.sqlite", s2=None)
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_browser="*")
    assert keychain == {}
    remediation = caught.value.remediation or ""
    assert "Chrome/Default" in remediation and "Firefox/p" in remediation
    assert "fantasy.espn.com" in remediation and "auth login" in remediation
    for text in (str(caught.value), remediation):
        assert COOKIE_S2 not in text and PASSWORD not in text


def test_no_browsers_at_all_says_what_was_looked_for(home: Path, keychain):
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_browser="*")
    assert "no profiles found" in (caught.value.remediation or "")
    assert "firefox" in (caught.value.remediation or "")


def test_keychain_denied_is_auth_missing_and_says_so(home: Path, passwords, keychain):
    passwords["Chrome Safe Storage"] = browser.KeychainAccessDenied("denied")
    chrome_profile(home)
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_browser="chrome")
    assert "Keychain access was denied" in (caught.value.remediation or "")
    assert keychain == {}


def test_an_unknown_browser_name_is_a_usage_style_config_invalid(home: Path):
    with pytest.raises(ConfigInvalidError, match="Unknown browser") as caught:
        auth_commands.login(from_browser="netscape")
    assert caught.value.details["kind"] == "argument"


def test_profile_without_from_browser_is_rejected_before_any_prompt(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": pytest.fail("must not prompt"))
    with pytest.raises(ConfigInvalidError, match="--from-browser"):
        auth_commands.login(profile="Default")


def test_without_the_flag_the_prompt_path_is_unchanged(
    home: Path, monkeypatch: pytest.MonkeyPatch, keychain
):
    entered = iter([COOKIE_S2, SWID, ""])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(entered))
    data = auth_commands.login().data
    assert set(data) == {"stored", "repaired", "keychain_service"}
    assert staleness.load_auth_state().for_name("espn_s2").expires_at is None


def test_a_prompted_login_clears_an_expiry_left_by_an_earlier_import(
    home: Path, passwords, keychain, monkeypatch: pytest.MonkeyPatch
):
    chrome_profile(home)
    auth_commands.login(from_browser="chrome")
    assert staleness.load_auth_state().for_name("espn_s2").expires_at == EXPIRES_S2
    entered = iter([COOKIE_S2, SWID, ""])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(entered))
    auth_commands.login()
    assert staleness.load_auth_state().for_name("espn_s2").expires_at is None


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_bare_from_browser_gets_its_registered_default_in_argv():
    from fantasy_sports.cli.app import _fill_optional_values

    assert _fill_optional_values(["auth", "login", "--from-browser"]) == [
        "auth",
        "login",
        "--from-browser",
        "*",
    ]
    assert _fill_optional_values(["auth", "login", "--from-browser", "--profile", "P"]) == [
        "auth",
        "login",
        "--from-browser",
        "*",
        "--profile",
        "P",
    ]
    assert _fill_optional_values(["auth", "login", "--from-browser", "firefox"]) == [
        "auth",
        "login",
        "--from-browser",
        "firefox",
    ]
    assert _fill_optional_values(["auth", "status"]) == ["auth", "status"]


def test_the_login_spec_declares_both_params_and_the_handler_accepts_them():
    from fantasy_sports.commands import REGISTRY

    spec = REGISTRY["auth login"]
    assert [p.name for p in spec.params] == ["from_browser", "profile", "from_clipboard"]
    assert spec.params[0].cli_flags == ("--from-browser",)
    assert spec.params[0].display == "--from-browser [TEXT]"


def test_cli_end_to_end_from_browser_never_touches_the_real_keychain(
    home: Path, passwords, keychain, capsys: pytest.CaptureFixture[str]
):
    from fantasy_sports.cli.app import run

    chrome_profile(home)
    assert run(["auth", "login", "--from-browser", "--output", "json"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["data"]["source"] == {"browser": "Chrome", "profile": "Default"}
    assert COOKIE_S2 not in out.out + out.err and PASSWORD not in out.out + out.err


# ---------------------------------------------------------------------------
# Edge cases the stores really produce
# ---------------------------------------------------------------------------


def test_absurd_expiries_are_no_expiry_rather_than_a_crash():
    assert browser._chrome_expiry(10**30) is None
    assert browser._chrome_expiry(-5) is None
    assert browser._firefox_expiry(10**30) is None


def test_a_non_numeric_meta_version_is_read_as_pre_24(tmp_path: Path, passwords):
    chrome_profile(tmp_path, version=23)
    conn = sqlite3.connect(support(tmp_path) / "Google/Chrome/Default/Cookies")
    conn.execute("UPDATE meta SET value = 'garbage'")
    conn.commit()
    conn.close()
    assert search(tmp_path).complete


def test_a_session_cookie_never_beats_a_dated_one():
    assert browser._later(None, EXPIRES_S2) is False
    assert browser._later(EXPIRES_S2, None) is True


def test_an_unreadable_firefox_store_is_skipped(tmp_path: Path):
    bad = support(tmp_path) / "Firefox/Profiles/p/cookies.sqlite"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not sqlite" * 50)
    found = search(tmp_path)
    assert found.skipped == ("Firefox/p: cookie store could not be read",)


def test_asking_for_firefox_on_windows_says_unsupported(tmp_path: Path):
    found = browser.find_espn_cookies("firefox", home=tmp_path, platform="win32")
    assert found.skipped == ("Firefox: unsupported on win32",)


def test_the_no_cookie_message_names_a_profile_narrowing(home: Path, passwords, keychain):
    chrome_profile(home, "Default")
    with pytest.raises(AuthMissingError, match="profile Profile 9") as caught:
        auth_commands.login(from_browser="chrome", profile="Profile 9")
    assert caught.value.details["searched"] == []


def test_a_session_cookie_import_reports_null_expiry(home: Path, passwords, keychain):
    chrome_profile(home, s2_expires=None, swid_expires=None)
    data = auth_commands.login(from_browser="chrome").data
    assert data["expires_at"] == {"espn_s2": None, "swid": None}
    assert staleness.load_auth_state().for_name("espn_s2").expires_at is None


def test_the_default_platform_is_what_the_interpreter_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """With no seam and no pin, Linux skips the Chrome family: CI's view."""
    monkeypatch.setattr(browser, "find_espn_cookies", browser_real)
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / ".config" / "google-chrome").mkdir(parents=True)
    found = browser.find_espn_cookies(home=tmp_path)
    assert any(line.startswith("Chrome: unsupported on linux") for line in found.skipped)
