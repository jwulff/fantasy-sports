"""Credential resolution, SWID repair, and staleness reporting (#5).

The redaction tests come first on purpose. Rule 5 in ``CLAUDE.md`` — never log,
print, or record a credential — is a security control, and a control that is
only asserted by *not* printing something is not asserted at all. Every
redaction test below renders a real string (a repr, a ``str(exc)``, a formatted
traceback, an error payload, the on-disk state file) and searches it for a
sentinel secret value.
"""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta

import pytest

from fantasy_sports.auth import chain, staleness

# A sentinel that cannot occur by accident anywhere in rendered output.
SECRET = "AEBqp7SENTINELs2cookievalue0123456789abcdefXYZ%2Fzz"
SWID_BARE = "1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D"
SWID_BRACED = "{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}"


@pytest.fixture(autouse=True)
def _forget_secrets():
    """Keep the process-wide scrub set from leaking between tests."""
    chain.forget_secrets()
    yield
    chain.forget_secrets()


# ---------------------------------------------------------------------------
# Redaction — the security control
# ---------------------------------------------------------------------------


def test_secret_never_renders_its_value():
    secret = chain.Secret(SECRET)
    rendered = [repr(secret), str(secret), f"{secret}", f"{secret!r}", format(secret)]
    for text in rendered:
        assert SECRET not in text
        assert chain.REDACTED in text


def test_secret_still_reveals_on_demand():
    assert chain.Secret(SECRET).reveal() == SECRET


def test_resolved_credential_repr_is_redacted():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    assert SECRET not in repr(resolved)
    assert SECRET not in str(resolved)


def test_credential_set_repr_is_redacted():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    credentials = chain.CredentialSet(resolved={"espn_s2": resolved}, missing=("swid",))
    assert SECRET not in repr(credentials)


def test_error_message_is_scrubbed_even_when_a_caller_interpolates_a_secret():
    """The failure mode this defends against is a *caller* mistake.

    Someone writes ``raise AuthError(f"bad cookie {value}")`` in a future unit.
    The value never reaches ``args``, ``str()``, or the payload.
    """
    chain.Secret(SECRET)  # registers the value with the scrubber
    err = chain.AuthError(f"rejected cookie {SECRET} for espn_s2")

    assert SECRET not in str(err)
    assert SECRET not in repr(err)
    assert SECRET not in "".join(str(a) for a in err.args)
    assert SECRET not in json.dumps(err.to_payload())
    assert chain.REDACTED in str(err)


def test_secret_is_absent_from_a_rendered_traceback():
    chain.Secret(SECRET)
    try:
        raise chain.AuthError(f"boom {SECRET}")
    except chain.AuthError as exc:
        rendered = "".join(traceback.format_exception(exc))
    assert SECRET not in rendered


def test_secret_is_absent_from_a_traceback_that_captures_local_variables():
    """``capture_locals=True`` renders every local by ``repr``.

    This is the strongest form of the claim: a raw ``str`` credential held in a
    frame *would* appear here, and a :class:`~fantasy_sports.auth.chain.Secret`
    does not. It is why credentials are carried in a wrapper rather than as
    plain strings.
    """

    def inner() -> None:
        held = chain.Secret(SECRET)  # noqa: F841 — deliberately live in the frame
        raise chain.AuthError("resolution failed")

    try:
        inner()
    except chain.AuthError as exc:
        rendered = "".join(
            traceback.TracebackException.from_exception(exc, capture_locals=True).format()
        )

    assert SECRET not in rendered
    assert chain.REDACTED in rendered


def test_a_raw_string_credential_would_have_leaked():
    """Control case: proves the previous test is measuring something real."""

    def inner() -> None:
        held = SECRET  # noqa: F841
        raise RuntimeError("resolution failed")

    try:
        inner()
    except RuntimeError as exc:
        rendered = "".join(
            traceback.TracebackException.from_exception(exc, capture_locals=True).format()
        )

    assert SECRET in rendered


def test_redact_scrubs_arbitrary_text_such_as_a_request_url():
    chain.Secret(SECRET)
    url = f"https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET}&swid={SWID_BRACED}"
    chain.Secret(SWID_BRACED)
    scrubbed = chain.redact(url)
    assert SECRET not in scrubbed
    assert SWID_BRACED not in scrubbed
    assert "fantasy.espn.com" in scrubbed


def test_redact_ignores_values_too_short_to_scrub_safely():
    """Scrubbing a 3-character value would corrupt unrelated text."""
    chain.Secret("abc")
    assert chain.redact("abcdef") == "abcdef"


def test_redact_is_a_no_op_when_nothing_is_registered():
    assert chain.redact("nothing to see") == "nothing to see"


def test_rejected_swid_error_does_not_echo_the_value():
    bad = "NOTAGUID" + SECRET
    with pytest.raises(chain.AuthError) as excinfo:
        chain.normalize_swid(bad)
    rendered = str(excinfo.value) + json.dumps(excinfo.value.to_payload())
    assert bad not in rendered
    assert SECRET not in rendered


def test_auth_status_payload_contains_no_credential_values():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    credentials = chain.CredentialSet(resolved={"espn_s2": resolved}, missing=("swid",))
    status = staleness.build_auth_status(credentials, environ={})
    assert SECRET not in json.dumps(status.to_payload())


def test_state_file_on_disk_never_contains_a_credential_value(tmp_path):
    path = tmp_path / "auth-state.json"
    chain.save_credentials(
        {"espn_s2": SECRET, "swid": SWID_BARE},
        writer=lambda name, value: None,
        state_path=path,
    )
    assert SECRET not in path.read_text()
    assert SWID_BARE not in path.read_text()


# ---------------------------------------------------------------------------
# Resolution order: env → Keychain → config
# ---------------------------------------------------------------------------

ESPN_S2 = chain.ESPN_CREDENTIALS[0]
SWID = chain.ESPN_CREDENTIALS[1]


def _keychain(values: dict[str, str]):
    return lambda spec: values.get(spec.name)


def _exploding_keychain(exc: BaseException):
    def reader(spec):
        raise exc

    return reader


def test_env_beats_keychain_and_config():
    resolved = chain.resolve_credential(
        ESPN_S2,
        environ={"FANTASY_SPORTS_ESPN_S2": "from-env-value"},
        keychain_reader=_keychain({"espn_s2": "from-keychain-value"}),
        config={"espn_s2": "from-config-value"},
    )
    assert resolved is not None
    assert resolved.source is chain.CredentialSource.ENV
    assert resolved.reveal() == "from-env-value"


def test_keychain_beats_config():
    resolved = chain.resolve_credential(
        ESPN_S2,
        environ={},
        keychain_reader=_keychain({"espn_s2": "from-keychain-value"}),
        config={"espn_s2": "from-config-value"},
    )
    assert resolved is not None
    assert resolved.source is chain.CredentialSource.KEYCHAIN
    assert resolved.reveal() == "from-keychain-value"


def test_config_is_the_last_link():
    resolved = chain.resolve_credential(
        ESPN_S2, environ={}, keychain_reader=_keychain({}), config={"espn_s2": "from-config-value"}
    )
    assert resolved is not None
    assert resolved.source is chain.CredentialSource.CONFIG


def test_every_link_empty_resolves_to_none():
    assert (
        chain.resolve_credential(ESPN_S2, environ={}, keychain_reader=_keychain({}), config={})
        is None
    )


def test_the_unprefixed_env_alias_is_accepted():
    """`tests/live/` and the canary workflow both export bare ESPN_S2/ESPN_SWID."""
    assert chain.read_from_env(ESPN_S2, {"ESPN_S2": "alias-value"}) == "alias-value"
    assert chain.read_from_env(SWID, {"ESPN_SWID": "alias-value"}) == "alias-value"


def test_the_namespaced_env_var_wins_over_the_alias():
    environ = {"FANTASY_SPORTS_ESPN_S2": "namespaced", "ESPN_S2": "bare"}
    assert chain.read_from_env(ESPN_S2, environ) == "namespaced"


def test_a_blank_env_var_is_not_a_credential():
    assert chain.read_from_env(ESPN_S2, {"FANTASY_SPORTS_ESPN_S2": "   "}) is None


def test_a_blank_keychain_entry_falls_through_to_config():
    resolved = chain.resolve_credential(
        ESPN_S2, environ={}, keychain_reader=_keychain({"espn_s2": "  "}), config={"espn_s2": "c"}
    )
    assert resolved is not None
    assert resolved.source is chain.CredentialSource.CONFIG


def test_a_blank_config_entry_is_not_a_credential():
    assert chain.read_from_config(ESPN_S2, {"espn_s2": "  "}) is None


# ---------------------------------------------------------------------------
# The locked-keychain fallback — the reason each link fails soft
# ---------------------------------------------------------------------------


class _KeyringLocked(Exception):
    """Stands in for `keyring.errors.KeyringLocked` without importing keyring."""


@pytest.mark.parametrize(
    "exc",
    [
        _KeyringLocked("keychain is locked"),
        OSError("no `security` binary"),
        RuntimeError("backend"),
    ],
    ids=["locked", "oserror", "runtime"],
)
def test_a_keychain_read_failure_falls_through_to_config(exc):
    """A locked keychain must not abort the chain.

    This is the headless case: launchd/cron/SSH have no unlockable keychain and
    no TTY to prompt against. Raising here would break the exact hosts the
    config fallback exists for.
    """
    resolved = chain.resolve_credential(
        ESPN_S2, environ={}, keychain_reader=_exploding_keychain(exc), config={"espn_s2": "cfg"}
    )
    assert resolved is not None
    assert resolved.source is chain.CredentialSource.CONFIG


def test_read_from_keychain_swallows_backend_failures(monkeypatch):
    """The real reader, with a keyring module that raises."""
    import sys
    import types

    fake = types.ModuleType("keyring")

    def boom(service, name):
        raise _KeyringLocked("locked")

    fake.get_password = boom
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert chain.read_from_keychain(ESPN_S2) is None


def test_read_from_keychain_returns_a_stored_value(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: f"{service}:{name}"
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert chain.read_from_keychain(ESPN_S2) == "fantasy-sports:espn_s2"


def test_write_to_keychain_uses_the_service_namespace(monkeypatch):
    import sys
    import types

    calls: list[tuple[str, str, str]] = []
    fake = types.ModuleType("keyring")
    fake.set_password = lambda service, name, value: calls.append((service, name, value))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    chain.write_to_keychain("espn_s2", "v")
    assert calls == [(chain.SERVICE, "espn_s2", "v")]


def test_resolving_from_env_never_imports_keyring():
    """ADR-0008: the cron/CI path must not pay for a keyring backend.

    It is also the correctness claim behind the ordering — env-first is only
    meaningful if the Keychain is genuinely untouched.
    """
    import json as _json
    import subprocess
    import sys

    code = (
        "import os, sys, json\n"
        "os.environ['FANTASY_SPORTS_ESPN_S2'] = 'x' * 40\n"
        "os.environ['FANTASY_SPORTS_SWID'] = '{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}'\n"
        "from fantasy_sports.auth.chain import resolve_credentials\n"
        "assert resolve_credentials().complete\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    modules = set(_json.loads(proc.stdout.strip().splitlines()[-1]))
    assert "keyring" not in modules
    assert not (modules & {"typer", "click", "rich", "espn_api", "requests"})


# ---------------------------------------------------------------------------
# Config file reading (the seam that #7 will replace)
# ---------------------------------------------------------------------------


def test_load_config_credentials_reads_the_credentials_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default = "dynasty"\n\n[credentials]\nespn_s2 = "abc"\nswid = "{x}"\n')
    assert chain.load_config_credentials(path) == {"espn_s2": "abc", "swid": "{x}"}


def test_load_config_credentials_ignores_non_string_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = 12\nswid = "ok"\n')
    assert chain.load_config_credentials(path) == {"swid": "ok"}


def test_a_missing_config_file_is_not_an_error(tmp_path):
    assert chain.load_config_credentials(tmp_path / "nope.toml") == {}


def test_a_malformed_config_file_is_not_an_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml\n")
    assert chain.load_config_credentials(path) == {}


def test_a_config_file_without_a_credentials_table_is_empty(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default = "dynasty"\n')
    assert chain.load_config_credentials(path) == {}


def test_config_dir_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert chain.config_dir() == tmp_path / "fantasy-sports"


def test_config_dir_falls_back_to_dot_config(monkeypatch, tmp_path):
    """macOS gets `~/.config` too — never Application Support (ARCHITECTURE §7)."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(chain.Path, "home", classmethod(lambda cls: tmp_path))
    assert chain.config_dir() == tmp_path / ".config" / "fantasy-sports"


def test_read_from_config_falls_back_to_the_real_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "fantasy-sports"
    directory.mkdir()
    (directory / "config.toml").write_text('[credentials]\nespn_s2 = "on-disk"\n')
    assert chain.read_from_config(ESPN_S2) == "on-disk"


# ---------------------------------------------------------------------------
# SWID brace repair — the most-repeated manual-extraction mistake
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [SWID_BARE, SWID_BRACED, f"  {SWID_BARE}  ", f'"{SWID_BRACED}"', f"'{SWID_BARE}'"],
    ids=["bare", "braced", "padded", "double-quoted", "single-quoted"],
)
def test_swid_is_repaired_to_the_braced_form(given):
    assert chain.normalize_swid(given) == SWID_BRACED


@pytest.mark.parametrize(
    "given",
    ["", "   ", "not-a-guid", SWID_BARE[:-1], SWID_BARE + "extra", "{}", "{1A2B3C4D}"],
    ids=["empty", "blank", "words", "truncated", "trailing", "empty-braces", "short"],
)
def test_a_genuinely_malformed_swid_is_rejected(given):
    with pytest.raises(chain.AuthError) as excinfo:
        chain.normalize_swid(given)
    assert excinfo.value.code == "AUTH_MISSING"
    assert excinfo.value.remediation


def test_swid_case_is_preserved():
    lower = SWID_BARE.lower()
    assert chain.normalize_swid(lower) == "{" + lower + "}"


def test_normalize_credential_dispatches_on_name():
    assert chain.normalize_credential("swid", SWID_BARE) == SWID_BRACED
    assert chain.normalize_credential("espn_s2", '  "abc"  ') == "abc"


@pytest.mark.parametrize(
    "given",
    ["", "   ", "espn_s2=abc; SWID={x}", "a b"],
    ids=["empty", "blank", "whole-header", "space"],
)
def test_a_malformed_opaque_cookie_is_rejected(given):
    with pytest.raises(chain.AuthError):
        chain.normalize_opaque_cookie(given)


def test_a_stored_swid_that_lost_its_braces_is_repaired_on_read():
    resolved = chain.resolve_credential(
        SWID, environ={"FANTASY_SPORTS_SWID": SWID_BARE}, keychain_reader=_keychain({}), config={}
    )
    assert resolved is not None
    assert resolved.reveal() == SWID_BRACED


def test_an_unrepairable_value_is_passed_through_rather_than_refused():
    """ARCHITECTURE §14 item 1: a bare 401 is ambiguous, so let the provider probe.

    Refusing to start on an odd-looking cookie would turn a diagnosable
    upstream question into an undiagnosable local one.
    """
    resolved = chain.resolve_credential(
        SWID,
        environ={"FANTASY_SPORTS_SWID": " garbage "},
        keychain_reader=_keychain({}),
        config={},
    )
    assert resolved is not None
    assert resolved.reveal() == "garbage"


# ---------------------------------------------------------------------------
# CredentialSet and AUTH_MISSING
# ---------------------------------------------------------------------------


def test_absent_credentials_produce_a_credential_set_not_a_crash():
    credentials = chain.resolve_credentials(environ={}, keychain_reader=_keychain({}), config={})
    assert credentials.missing == ("espn_s2", "swid")
    assert not credentials.complete
    assert credentials.get("espn_s2") is None
    assert "espn_s2" not in credentials


def test_require_credentials_raises_auth_missing():
    with pytest.raises(chain.AuthError) as excinfo:
        chain.require_credentials(environ={}, keychain_reader=_keychain({}), config={})
    err = excinfo.value
    assert err.code == "AUTH_MISSING"
    assert err.to_payload()["code"] == "AUTH_MISSING"
    assert "auth login" in (err.remediation or "")
    assert "FANTASY_SPORTS_ESPN_S2" in (err.remediation or "")


def test_require_credentials_returns_a_complete_set():
    credentials = chain.require_credentials(
        environ={"FANTASY_SPORTS_ESPN_S2": "abcdefgh", "FANTASY_SPORTS_SWID": SWID_BRACED},
    )
    assert credentials.complete
    assert credentials.as_mapping() == {"espn_s2": "abcdefgh", "swid": SWID_BRACED}
    assert credentials.reveal("swid") == SWID_BRACED


def test_revealing_an_absent_credential_is_auth_missing():
    credentials = chain.CredentialSet()
    with pytest.raises(chain.AuthError) as excinfo:
        credentials.reveal("espn_s2")
    assert excinfo.value.code == "AUTH_MISSING"


def test_an_error_without_remediation_omits_it_from_the_payload():
    assert chain.AuthError("plain").to_payload() == {"code": "AUTH_MISSING", "message": "plain"}


def test_secret_equality_and_hashing():
    assert chain.Secret("abcdefgh") == chain.Secret("abcdefgh")
    assert chain.Secret("abcdefgh") != chain.Secret("hgfedcba")
    assert chain.Secret("abcdefgh") != "abcdefgh"
    assert len({chain.Secret("abcdefgh"), chain.Secret("abcdefgh")}) == 1
    assert len(chain.Secret("abcdefgh")) == 8
    assert bool(chain.Secret("abcdefgh"))
    assert not bool(chain.Secret(""))


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_save_repairs_the_swid_and_reports_what_it_fixed(tmp_path):
    written: dict[str, str] = {}
    repaired = chain.save_credentials(
        {"espn_s2": "abcdefghij", "swid": SWID_BARE},
        writer=lambda name, value: written.__setitem__(name, value),
        state_path=tmp_path / "auth-state.json",
    )
    assert repaired == ("swid",)
    assert written == {"espn_s2": "abcdefghij", "swid": SWID_BRACED}


def test_save_writes_nothing_when_any_value_is_malformed(tmp_path):
    written: dict[str, str] = {}
    with pytest.raises(chain.AuthError):
        chain.save_credentials(
            {"espn_s2": "abcdefghij", "swid": "nonsense"},
            writer=lambda name, value: written.__setitem__(name, value),
            state_path=tmp_path / "auth-state.json",
        )
    assert written == {}, "a half-saved credential pair is worse than an unsaved one"


def test_save_rejects_an_unknown_credential_name(tmp_path):
    with pytest.raises(chain.AuthError) as excinfo:
        chain.save_credentials(
            {"yahoo_token": "x"}, writer=lambda n, v: None, state_path=tmp_path / "s.json"
        )
    assert "yahoo_token" in str(excinfo.value)


def test_save_records_when_the_credential_was_stored(tmp_path):
    path = tmp_path / "auth-state.json"
    when = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    chain.save_credentials(
        {"espn_s2": "abcdefghij"}, writer=lambda n, v: None, state_path=path, now=when
    )
    state = staleness.load_auth_state(path)
    assert state.for_name("espn_s2").stored_at == when


# ---------------------------------------------------------------------------
# Staleness state
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def test_state_round_trips(tmp_path):
    path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2"], now=NOW, path=path)
    staleness.record_success(["espn_s2"], now=NOW + timedelta(days=1), path=path)
    events = staleness.load_auth_state(path).for_name("espn_s2")
    assert events.stored_at == NOW
    assert events.last_success_at == NOW + timedelta(days=1)


def test_recording_a_success_preserves_stored_at(tmp_path):
    path = tmp_path / "auth-state.json"
    staleness.record_stored(["swid"], now=NOW, path=path)
    staleness.record_success(["swid"], now=NOW + timedelta(days=3), path=path)
    staleness.record_success(["swid"], now=NOW + timedelta(days=5), path=path)
    events = staleness.load_auth_state(path).for_name("swid")
    assert events.stored_at == NOW
    assert events.last_success_at == NOW + timedelta(days=5)


def test_state_file_is_owner_readable_only(tmp_path):
    path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2"], now=NOW, path=path)
    assert path.stat().st_mode & 0o077 == 0


def test_state_defaults_use_the_real_time_when_none_is_given(tmp_path):
    path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2"], path=path)
    stored = staleness.load_auth_state(path).for_name("espn_s2").stored_at
    assert stored is not None and stored.tzinfo is not None


def test_a_missing_state_file_is_an_empty_state(tmp_path):
    assert staleness.load_auth_state(tmp_path / "nope.json").entries == {}


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"credentials": 3}', '{"credentials": {"espn_s2": 7}}'],
    ids=["garbage", "list", "wrong-type", "wrong-entry-type"],
)
def test_a_corrupt_state_file_is_an_empty_state(tmp_path, content):
    path = tmp_path / "auth-state.json"
    path.write_text(content)
    assert staleness.load_auth_state(path).for_name("espn_s2") == staleness.CredentialEvents()


def test_an_unparseable_timestamp_is_dropped_not_guessed(tmp_path):
    path = tmp_path / "auth-state.json"
    path.write_text('{"credentials": {"espn_s2": {"stored_at": "yesterday"}}}')
    assert staleness.load_auth_state(path).for_name("espn_s2").stored_at is None


def test_a_naive_timestamp_is_read_as_utc(tmp_path):
    """ARCHITECTURE §14 item 8: a naive datetime would skew age by the host offset."""
    path = tmp_path / "auth-state.json"
    path.write_text('{"credentials": {"espn_s2": {"stored_at": "2026-09-05T12:00:00"}}}')
    assert staleness.load_auth_state(path).for_name("espn_s2").stored_at == NOW


def test_auth_state_path_sits_beside_the_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert staleness.auth_state_path() == tmp_path / "fantasy-sports" / "auth-state.json"


def test_save_auth_state_defaults_to_the_real_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    written = staleness.save_auth_state(staleness.AuthState(entries={}))
    assert written == tmp_path / "fantasy-sports" / "auth-state.json"
    assert staleness.load_auth_state().entries == {}


# ---------------------------------------------------------------------------
# The staleness threshold — a heuristic, labelled as one
# ---------------------------------------------------------------------------


def test_the_default_threshold_is_the_documented_heuristic():
    limit, source = staleness.resolve_threshold(environ={})
    assert limit == staleness.DEFAULT_STALE_AFTER
    assert source is staleness.ThresholdSource.DEFAULT


def test_the_threshold_is_overridable_from_the_environment():
    limit, source = staleness.resolve_threshold(environ={staleness.STALE_AFTER_ENV: "7"})
    assert limit == timedelta(days=7)
    assert source is staleness.ThresholdSource.ENVIRONMENT


def test_an_explicit_threshold_beats_the_environment():
    limit, source = staleness.resolve_threshold(
        timedelta(days=2), environ={staleness.STALE_AFTER_ENV: "7"}
    )
    assert limit == timedelta(days=2)
    assert source is staleness.ThresholdSource.ARGUMENT


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5"], ids=["words", "zero", "negative"])
def test_a_bad_override_falls_back_rather_than_raising(raw):
    """A typo in a shell profile must not stop `auth status` reporting presence."""
    limit, source = staleness.resolve_threshold(environ={staleness.STALE_AFTER_ENV: raw})
    assert limit == staleness.DEFAULT_STALE_AFTER
    assert source is staleness.ThresholdSource.DEFAULT


def test_the_threshold_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(staleness.STALE_AFTER_ENV, "3")
    assert staleness.resolve_threshold()[0] == timedelta(days=3)


def test_the_payload_labels_the_threshold_unverified():
    """The one claim this tool must never make is a concrete ESPN cookie lifetime."""
    status = staleness.build_auth_status(chain.CredentialSet(missing=("espn_s2",)), environ={})
    threshold = status.to_payload()["staleness_threshold"]
    assert threshold["verified"] is False
    assert "Unverified heuristic" in threshold["note"]
    assert staleness.STALE_AFTER_ENV in threshold["note"]
    assert status.threshold_verified is False


# ---------------------------------------------------------------------------
# `auth status`
# ---------------------------------------------------------------------------


def _keychain_set(name: str = "espn_s2") -> chain.CredentialSet:
    return chain.CredentialSet(
        resolved={
            name: chain.ResolvedCredential(
                name=name,
                source=chain.CredentialSource.KEYCHAIN,
                secret=chain.Secret("abcdefghij"),
            )
        },
        missing=(),
    )


def test_status_reports_age_for_a_keychain_credential():
    state = staleness.AuthState(
        entries={"espn_s2": staleness.CredentialEvents(stored_at=NOW - timedelta(days=3))}
    )
    status = staleness.build_auth_status(_keychain_set(), state=state, now=NOW, environ={})
    row = status.credentials[0]
    assert row.present
    assert row.age == timedelta(days=3)
    assert row.age_days == 3.0
    assert row.age_basis == "stored_at"
    assert row.freshness is staleness.Freshness.FRESH
    assert status.warnings == ()


def test_status_warns_past_the_threshold_and_says_it_is_a_heuristic():
    state = staleness.AuthState(
        entries={"espn_s2": staleness.CredentialEvents(stored_at=NOW - timedelta(days=45))}
    )
    status = staleness.build_auth_status(_keychain_set(), state=state, now=NOW, environ={})
    assert status.credentials[0].freshness is staleness.Freshness.STALE
    warning = status.warnings[0]
    assert "45.0 days ago" in warning
    assert "Unverified heuristic" in warning


def test_status_reports_last_successful_use():
    last = NOW - timedelta(hours=6)
    state = staleness.AuthState(
        entries={
            "espn_s2": staleness.CredentialEvents(
                stored_at=NOW - timedelta(days=1), last_success_at=last
            )
        }
    )
    status = staleness.build_auth_status(_keychain_set(), state=state, now=NOW, environ={})
    assert status.credentials[0].last_success_at == last
    assert status.to_payload()["credentials"][0]["last_success_at"] == "2026-09-05T06:00:00Z"


def test_age_is_unknown_for_an_env_credential_rather_than_wrongly_attributed():
    """The `stored_at` we recorded describes what *we* wrote to the Keychain.

    An env var may hold a completely different cookie, so reporting our
    timestamp against it would be a confident wrong answer.
    """
    credentials = chain.CredentialSet(
        resolved={
            "espn_s2": chain.ResolvedCredential(
                name="espn_s2",
                source=chain.CredentialSource.ENV,
                secret=chain.Secret("abcdefghij"),
            )
        }
    )
    state = staleness.AuthState(
        entries={"espn_s2": staleness.CredentialEvents(stored_at=NOW - timedelta(days=400))}
    )
    status = staleness.build_auth_status(credentials, state=state, now=NOW, environ={})
    row = status.credentials[0]
    assert row.age is None
    assert row.freshness is staleness.Freshness.UNKNOWN
    assert row.age_basis == "not-tracked:env"
    assert status.warnings == ()


def test_age_is_unknown_for_a_keychain_credential_we_never_recorded():
    status = staleness.build_auth_status(
        _keychain_set(), state=staleness.AuthState(entries={}), now=NOW, environ={}
    )
    row = status.credentials[0]
    assert row.freshness is staleness.Freshness.UNKNOWN
    assert row.age_basis == "unknown"


def test_status_reports_absent_credentials_without_crashing():
    credentials = chain.resolve_credentials(environ={}, keychain_reader=_keychain({}), config={})
    status = staleness.build_auth_status(
        credentials, state=staleness.AuthState(entries={}), now=NOW
    )
    assert not status.complete
    assert [row.name for row in status.credentials] == ["espn_s2", "swid"]
    assert all(row.freshness is staleness.Freshness.MISSING for row in status.credentials)
    assert all("auth login" in warning for warning in status.warnings)
    payload = status.to_payload()
    assert payload["complete"] is False
    assert payload["credentials"][0]["source"] is None
    assert payload["credentials"][0]["age_days"] is None


def test_status_loads_state_from_disk_when_none_is_supplied(tmp_path):
    path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2"], now=NOW - timedelta(days=2), path=path)
    status = staleness.build_auth_status(_keychain_set(), state_path=path, now=NOW, environ={})
    assert status.credentials[0].age == timedelta(days=2)


def test_status_defaults_to_now_when_no_clock_is_supplied():
    status = staleness.build_auth_status(
        chain.CredentialSet(missing=("espn_s2",)),
        state=staleness.AuthState(entries={}),
        environ={},
    )
    assert status.generated_at.tzinfo is not None
    assert status.to_payload()["generated_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Layering: `auth/` is a plain library
# ---------------------------------------------------------------------------


def test_auth_modules_import_no_cli_or_provider_machinery():
    """ADR-0003: the CLI is a projection over these functions, not the reverse.

    Checked by AST rather than by `sys.modules` so a *lazy* import inside a
    function is caught too — the point is that `auth/` has no dependency on
    typer, rich, or espn_api at all, not merely that `--help` avoids paying
    for one.
    """
    import ast
    from pathlib import Path as _Path

    forbidden = {"typer", "click", "rich", "espn_api"}
    for path in sorted(_Path("src/fantasy_sports/auth").rglob("*.py")):
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert not (roots & forbidden), f"{path} imports {sorted(roots & forbidden)}"


def test_keyring_is_imported_lazily_and_only_where_it_is_needed():
    """ADR-0008 forbids a module-scope `import keyring` anywhere in `src/`."""
    import ast
    from pathlib import Path as _Path

    for path in sorted(_Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert "keyring" not in {a.name.split(".")[0] for a in node.names}, path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "keyring", path
