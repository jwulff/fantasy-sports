"""``auth/reimport.py`` and ``[auth] auto_reimport`` (jwulff/fantasy-sports#159).

Every browser, Keychain and config touch is injected: ``search`` stands in for
``find_espn_cookies`` and ``save`` for ``save_credentials``, so nothing here
reads a real profile, the real Keychain, or the network. Where a test does go
through the module-level defaults it patches them at call time, and none of them
calls ``find_espn_cookies`` for real, so ``sys.platform`` never matters
(``docs/memory/default-args-defeat-monkeypatch.md``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fantasy_sports.auth import reimport
from fantasy_sports.auth.browser import BrowserSearch, ProfileCookies
from fantasy_sports.auth.chain import (
    CredentialSet,
    CredentialSource,
    ResolvedCredential,
    Secret,
)
from fantasy_sports.config.auth import auto_reimport_enabled
from fantasy_sports.core.errors import ConfigInvalidError
from fantasy_sports.core.redaction import forget_secrets

SWID = "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"
OTHER_SWID = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"
STORED_S2 = "AEB" + "o" * 120
BROWSER_S2 = "AEB" + "n" * 120
SENT = {"espn_s2": STORED_S2, "swid": SWID}
EXPIRES = datetime(2027, 3, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    forget_secrets()


def credential_set(source: CredentialSource = CredentialSource.KEYCHAIN, **sources: Any):
    resolved = {
        name: ResolvedCredential(
            name=name, source=sources.get(name, source), secret=Secret(SENT[name])
        )
        for name in SENT
    }
    return CredentialSet(resolved=resolved)


def profile(
    s2: str | None = BROWSER_S2,
    swid: str | None = SWID,
    *,
    browser: str = "Chrome",
    name: str = "Default",
    expires: datetime | None = EXPIRES,
) -> ProfileCookies:
    return ProfileCookies(
        browser=browser,
        profile=name,
        espn_s2=None if s2 is None else Secret(s2),
        swid=None if swid is None else Secret(swid),
        espn_s2_expires_at=expires,
    )


class Saver:
    """A fake ``save_credentials``. ``fail_after`` lets N calls succeed, then raises."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.raises = raises

    def __call__(self, values: dict[str, str], **kwargs: Any) -> tuple[str, ...]:
        if self.raises is not None:
            raise self.raises
        self.calls.append((values, kwargs))
        return ()


def run(
    *profiles: ProfileCookies,
    cred: Any = None,
    sent: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    config_path: Path | None = None,
):
    """``attempt`` over fixed profiles. Returns ``(result, saver)``; ``attempt`` never saves."""
    saver = Saver()
    result = reimport.attempt(
        SENT if sent is None else sent,
        credential_set() if cred is None else cred,
        environ={} if environ is None else environ,
        config_path=config_path,
        search=lambda: BrowserSearch(profiles=tuple(profiles)),
    )
    return result, saver


# --------------------------------------------------------------------------- #
# The trigger: a different pair, compared per cookie
# --------------------------------------------------------------------------- #


def test_a_different_espn_s2_is_found_with_its_expiry_and_nothing_is_saved():
    """The maintainer's real case: SWID matched, Chrome's espn_s2 did not.

    Saving waits for the retry to succeed (``commit``), so a pair ESPN never
    accepted is never stored.
    """
    found, saver = run(profile())
    assert found is not None
    assert found.source == {"browser": "Chrome", "profile": "Default"}
    assert found.espn_s2.reveal() == BROWSER_S2 and found.swid.reveal() == SWID
    assert found.expires_at["espn_s2"] == EXPIRES
    assert found.saved is False and saver.calls == []


def test_a_different_swid_is_another_account_and_is_never_adopted():
    """The issue's wording counts a differing SWID as "different"; the irreversible
    commands derive ownership from the SWID, so it is not taken automatically."""
    found, _ = run(profile(STORED_S2, OTHER_SWID), profile(BROWSER_S2, OTHER_SWID, name="B"))
    assert found is None


def test_an_identical_pair_changes_nothing_and_saves_nothing():
    found, saver = run(profile(STORED_S2, SWID))
    assert found is None and saver.calls == []


def test_no_complete_pair_in_any_profile_changes_nothing():
    found, saver = run(profile(s2=None), profile(swid=None))
    assert found is None and saver.calls == []


def test_no_profile_at_all_changes_nothing():
    found, saver = run()
    assert found is None and saver.calls == []


def test_a_stale_duplicate_profile_does_not_veto_a_rotated_cookie_for_the_same_account():
    """An inactive profile that kept the old pair must not block the fresh one."""
    found, _ = run(profile(STORED_S2, SWID, name="Old"), profile(name="Active"))
    assert found is not None and found.profile == "Active"


def test_another_account_in_another_profile_is_ignored_in_favour_of_ours():
    stranger = profile(
        "AEB" + "z" * 120, OTHER_SWID, name="Other", expires=datetime(2030, 1, 1, tzinfo=UTC)
    )
    found, _ = run(stranger, profile(name="Mine"))
    assert found is not None and found.profile == "Mine"


def test_of_several_rotated_profiles_the_latest_expiry_wins():
    later = datetime(2028, 1, 1, tzinfo=UTC)
    early = profile(BROWSER_S2, SWID, name="Early", expires=EXPIRES)
    late = profile("AEB" + "y" * 120, SWID, name="Late", expires=later)
    found, _ = run(early, late)
    assert found is not None and found.profile == "Late"


def test_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[bytes, bytes]] = []
    real = reimport.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(reimport.hmac, "compare_digest", spy)
    run(profile(BROWSER_S2, SWID))
    # Both cookies compared, none skipped by a short circuit.
    assert len(seen) >= 2
    assert (BROWSER_S2.encode(), STORED_S2.encode()) in seen
    assert (SWID.strip("{}").encode(), SWID.strip("{}").encode()) in seen


def test_a_non_ascii_browser_value_is_just_a_different_value():
    found, _ = run(profile("AEB" + "é" * 20, SWID))
    assert found is not None
    assert reimport.rotated(profile("AEB" + "é" * 20, SWID), {"espn_s2": "x"}) is False
    assert reimport.rotated(profile("x", SWID), {"espn_s2": "AEB" + "é", "swid": SWID}) is True


# --------------------------------------------------------------------------- #
# Who may trigger it
# --------------------------------------------------------------------------- #


def never_searched() -> BrowserSearch:
    pytest.fail("the browser was read")


@pytest.mark.parametrize(
    "cred",
    [
        credential_set(CredentialSource.ENV),
        credential_set(CredentialSource.KEYCHAIN, swid=CredentialSource.ENV),
        credential_set(CredentialSource.CONFIG, espn_s2=CredentialSource.ENV),
        None,
        CredentialSet(),
    ],
    ids=["env", "mixed-env-swid", "mixed-env-s2", "unattributed", "empty"],
)
def test_environment_or_unattributed_credentials_never_read_the_browser(cred: Any):
    found = reimport.attempt(SENT, cred, environ={}, config_path=None, search=never_searched)
    assert found is None


@pytest.mark.parametrize("source", [CredentialSource.KEYCHAIN, CredentialSource.CONFIG])
def test_keychain_and_config_credentials_may_trigger_it(source: CredentialSource):
    found, _ = run(profile(), cred=credential_set(source))
    assert found is not None


# --------------------------------------------------------------------------- #
# Opting out
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " on "])
def test_the_environment_variable_opts_out(value: str):
    found = reimport.attempt(
        SENT, credential_set(), environ={reimport.ENV_OPT_OUT: value}, search=never_searched
    )
    assert found is None


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_other_values_of_the_variable_do_not_opt_out(value: str):
    found, _ = run(profile(), environ={reimport.ENV_OPT_OUT: value})
    assert found is not None


def config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_config_switch_opts_out(tmp_path: Path):
    path = config(tmp_path, "[auth]\nauto_reimport = false\n")
    found = reimport.attempt(
        SENT, credential_set(), environ={}, config_path=path, search=never_searched
    )
    assert found is None


def test_the_config_switch_set_true_or_absent_allows_it(tmp_path: Path):
    for body in ("[auth]\nauto_reimport = true\n", "[leagues]\n", ""):
        found, _ = run(profile(), config_path=config(tmp_path, body))
        assert found is not None


def test_a_damaged_config_means_do_not_import_rather_than_mask_the_original_error(
    tmp_path: Path,
):
    for body in ('[auth]\nauto_reimport = "false"\n', "auth = 3\n", "this is [not toml"):
        found = reimport.attempt(
            SENT,
            credential_set(),
            environ={},
            config_path=config(tmp_path, body),
            search=never_searched,
        )
        assert found is None


def test_the_config_reader_defaults_on_and_rejects_damage_by_name(tmp_path: Path):
    assert auto_reimport_enabled(tmp_path / "missing.toml") is True
    assert auto_reimport_enabled(config(tmp_path, "[credentials]\nx = 'y'\n")) is True
    assert auto_reimport_enabled(config(tmp_path, "[auth]\nauto_reimport = false\n")) is False
    with pytest.raises(ConfigInvalidError) as err:
        auto_reimport_enabled(config(tmp_path, "[auth]\nauto_reimport = 0\n"))
    assert err.value.details["key"] == "auto_reimport"
    with pytest.raises(ConfigInvalidError):
        auto_reimport_enabled(config(tmp_path, "auth = 'on'\n"))


# --------------------------------------------------------------------------- #
# Failing softly, and never leaking
# --------------------------------------------------------------------------- #


def test_a_failed_browser_search_means_no_import():
    def boom() -> BrowserSearch:
        raise RuntimeError("cookie store is locked")

    assert reimport.attempt(SENT, credential_set(), environ={}, search=boom) is None


def test_the_result_renders_without_either_cookie():
    found, _ = run(profile())
    assert found is not None
    shown = " ".join([repr(found), str(found), f"{found}", str(found.source), found.label])
    for value in (BROWSER_S2, SWID, STORED_S2):
        assert value not in shown


def reimported() -> reimport.Reimported:
    found, _ = run(profile())
    assert found is not None
    return found


class FakeKeychain:
    """A dict-backed stand-in for the three Keychain functions ``commit`` uses."""

    def __init__(self, held: dict[str, str], *, fail_write_of: str | None = None) -> None:
        self.held = dict(held)
        self.fail_write_of = fail_write_of
        self.deleted: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
        from fantasy_sports.auth import chain, staleness

        monkeypatch.setattr(reimport, "_held", self.held.get)
        monkeypatch.setattr(chain, "write_to_keychain", self.write)
        monkeypatch.setattr(chain, "delete_from_keychain", self.delete)
        monkeypatch.setattr(staleness, "record_stored", lambda *a, **k: None)
        return self

    def write(self, name: str, value: str) -> None:
        if name == self.fail_write_of:
            raise RuntimeError("keychain is locked")
        self.held[name] = value

    def delete(self, name: str) -> bool:
        self.deleted.append(name)
        return self.held.pop(name, None) is not None


def test_commit_saves_the_pair_with_its_expiry(monkeypatch: pytest.MonkeyPatch):
    FakeKeychain(SENT).install(monkeypatch)
    saver = Saver()
    assert reimport.commit(reimported(), save=saver) is True
    [(values, kwargs)] = saver.calls
    assert values == {"espn_s2": BROWSER_S2, "swid": SWID}
    assert kwargs["expires_at"]["espn_s2"] == EXPIRES


def test_the_default_commit_goes_through_save_credentials(monkeypatch: pytest.MonkeyPatch):
    """Not a seam: the real default writes via the chain, looked up at call time."""
    from fantasy_sports.auth import browser

    keychain = FakeKeychain(SENT).install(monkeypatch)
    monkeypatch.setattr(
        browser, "find_espn_cookies", lambda *a, **k: BrowserSearch(profiles=(profile(),))
    )
    found = reimport.attempt(SENT, credential_set(), environ={})
    assert found is not None and keychain.held == SENT, "finding saves nothing"
    assert reimport.commit(found) is True
    assert keychain.held == {"espn_s2": BROWSER_S2, "swid": SWID}


def test_a_commit_that_fails_halfway_puts_the_keychain_back_exactly(
    monkeypatch: pytest.MonkeyPatch,
):
    """The real save writes espn_s2, then swid; the second write fails."""
    keychain = FakeKeychain(SENT, fail_write_of="swid").install(monkeypatch)
    found = reimported()

    assert reimport.commit(found) is False
    assert keychain.held == SENT and keychain.deleted == []


def test_a_failed_commit_deletes_entries_it_created_rather_than_inventing_old_ones(
    monkeypatch: pytest.MonkeyPatch,
):
    """Credentials that came from config.toml have no Keychain entry to restore.

    Writing the in-use (config) values back would create Keychain entries that
    then outrank the config file on every later run.
    """
    keychain = FakeKeychain({}, fail_write_of="swid").install(monkeypatch)
    found = reimported()

    assert reimport.commit(found) is False
    assert keychain.held == {}
    assert sorted(keychain.deleted) == ["espn_s2", "swid"]


def test_a_rollback_that_also_fails_is_swallowed(monkeypatch: pytest.MonkeyPatch):
    from fantasy_sports.auth import chain

    FakeKeychain(SENT).install(monkeypatch)

    def broken(*args: Any) -> Any:
        raise RuntimeError("keychain is locked")

    monkeypatch.setattr(chain, "write_to_keychain", broken)
    assert reimport.commit(reimported(), save=Saver(raises=RuntimeError("x"))) is False


def test_the_keychain_snapshot_never_renders_a_value(monkeypatch: pytest.MonkeyPatch):
    FakeKeychain(SENT).install(monkeypatch)
    seen: list[str] = []

    def boom(values: dict[str, str], **kwargs: Any) -> None:
        import sys

        seen.append(repr(sys._getframe(1).f_locals.get("before")))
        raise RuntimeError("x")

    reimport.commit(reimported(), save=boom)
    assert seen and STORED_S2 not in seen[0] and SWID not in seen[0]


def test_an_unreadable_keychain_means_nothing_is_written_at_all(monkeypatch: pytest.MonkeyPatch):
    """Absence and a locked Keychain look alike to a fail-soft read; a rollback built
    on that could delete entries it merely failed to see."""
    keychain = FakeKeychain(SENT).install(monkeypatch)

    def locked(name: str) -> str | None:
        raise RuntimeError("keychain is locked")

    monkeypatch.setattr(reimport, "_held", locked)
    saver = Saver()
    assert reimport.commit(reimported(), save=saver) is False
    assert saver.calls == [] and keychain.held == SENT and keychain.deleted == []


def test_the_strict_read_asks_keyring_for_the_service(monkeypatch: pytest.MonkeyPatch):
    import keyring

    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        keyring, "get_password", lambda service, name: asked.append((service, name)) or "v"
    )
    assert reimport._held("espn_s2") == "v"
    assert asked == [("fantasy-sports", "espn_s2")]


def test_an_expired_cookie_is_not_a_candidate_and_does_not_outrank_a_session_cookie():
    now = datetime(2027, 6, 1, tzinfo=UTC)
    expired = profile(BROWSER_S2, SWID, name="Expired", expires=datetime(2027, 1, 1, tzinfo=UTC))
    session = profile("AEB" + "y" * 120, SWID, name="Session", expires=None)
    chosen = reimport._choose(BrowserSearch(profiles=(expired, session)), SENT, now)
    assert chosen is not None and chosen.profile == "Session"
    assert reimport._choose(BrowserSearch(profiles=(expired,)), SENT, now) is None


def test_a_live_dated_cookie_beats_a_session_cookie_as_a_tie_break():
    now = datetime(2027, 1, 1, tzinfo=UTC)
    dated = profile(BROWSER_S2, SWID, name="Dated")
    session = profile("AEB" + "y" * 120, SWID, name="Session", expires=None)
    chosen = reimport._choose(BrowserSearch(profiles=(session, dated)), SENT, now)
    assert chosen is not None and chosen.profile == "Dated"


def test_swid_case_and_braces_do_not_make_it_another_account():
    spelled = profile(BROWSER_S2, SWID.lower().strip("{}"))
    found, _ = run(spelled)
    assert found is not None


def test_a_lowercase_braced_swid_is_the_same_account_as_an_uppercase_one():
    found, _ = run(profile(BROWSER_S2, SWID.lower()))
    assert found is not None
    assert reimport.rotated(profile(BROWSER_S2, SWID.lower()), SENT) is True
    assert reimport.rotated(profile(STORED_S2, SWID.lower()), SENT) is False
    # espn_s2 stays case-sensitive: a case-only difference is a different cookie.
    assert reimport.rotated(profile(STORED_S2.swapcase(), SWID), SENT) is True
