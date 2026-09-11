"""``auth logout`` — every link cleared, every outcome reported, no value shown.

**Every test in this file runs against a fake ``keyring`` module.** The
developer's real macOS Keychain holds real ESPN cookies under the exact
service name the code uses, and this command *deletes* by that name. The
autouse fixture below installs the fake into ``sys.modules`` before any test
body runs, so ``import keyring`` inside ``delete_from_keychain`` can never
reach the real backend — not on a passing run, not on a failing one, not on a
test someone adds later without reading this docstring. ``isolate_home``
gives every test its own XDG tree for the same reason on the config side.

Offline throughout. ``pytest-socket`` is on and nothing here needs a socket.
"""

from __future__ import annotations

import json
import stat
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import FAKE_S2, FAKE_SWID, isolate_home

from fantasy_sports.auth import chain, staleness
from fantasy_sports.auth.logout import LinkOutcome, clear_credentials
from fantasy_sports.cli.app import run
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands import auth as auth_commands
from fantasy_sports.config import credentials as config_credentials
from fantasy_sports.core.errors import ConfigInvalidError
from fantasy_sports.core.redaction import forget_secrets

ENV_VARS = ("FANTASY_SPORTS_ESPN_S2", "ESPN_S2", "FANTASY_SPORTS_SWID", "ESPN_SWID", "SWID")


class _NoKeyringError(RuntimeError):
    """Stands in for `keyring.errors.NoKeyringError` without importing keyring."""


class _KeyringLocked(Exception):
    """Stands in for `keyring.errors.KeyringLocked`."""


class FakeKeyring:
    """An in-memory stand-in for the ``keyring`` module's three functions.

    ``fail_reads`` makes every call raise (no backend at all); ``fail_deletes``
    lets the read succeed and the delete raise (a Keychain that unlocked for
    the read and not for the write — the macOS ``security`` shell-out can do
    exactly that). Both are how "unavailable" is produced on purpose.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_reads: BaseException | None = None
        self.fail_deletes: BaseException | None = None
        self.deleted: list[tuple[str, str]] = []

    def get_password(self, service: str, name: str) -> str | None:
        if self.fail_reads is not None:
            raise self.fail_reads
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        if self.fail_deletes is not None:
            raise self.fail_deletes
        if (service, name) not in self.store:
            raise _KeyringLocked("NotFound")
        del self.store[(service, name)]
        self.deleted.append((service, name))


@pytest.fixture(autouse=True)
def keyring_module(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """The safety rail: every ``import keyring`` in this file gets this fake."""
    fake = FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    module.delete_password = fake.delete_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    return fake


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private XDG tree with **no** credential env vars set.

    ``isolate_home`` exports the synthetic pair so read commands resolve from
    the environment. Here the environment is one of the links under test, so
    it starts empty and each test sets exactly what it means to.
    """
    home = isolate_home(tmp_path, monkeypatch)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield home
    forget_secrets()


CONFIG_WITH_CREDENTIALS = (
    'default = "synthetic"\n'
    "\n"
    "[leagues.synthetic]\n"
    'provider = "espn"\n'
    'league_id = "99"\n'
    "season = 2026\n"
    "\n"
    "[credentials]\n"
    f'espn_s2 = "{FAKE_S2}"\n'
    f'swid = "{FAKE_SWID}"\n'
    'other_provider_token = "keep-me"\n'
)


def _config_path(home: Path) -> Path:
    return home / "config" / "fantasy-sports" / "config.toml"


def _seed_keychain(fake: FakeKeyring) -> None:
    fake.set_password(chain.SERVICE, "espn_s2", FAKE_S2)
    fake.set_password(chain.SERVICE, "swid", FAKE_SWID)


def _rows(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in data["credentials"]}


def _assert_no_values(payload: Any) -> None:
    rendered = json.dumps(payload)
    assert FAKE_S2 not in rendered
    assert FAKE_SWID not in rendered
    assert FAKE_SWID.strip("{}") not in rendered


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_logout_is_registered_beside_login_and_status():
    spec = REGISTRY["auth logout"]
    assert spec.group == "auth"
    assert spec.shape is DataShape.OBJECT
    assert spec.takes_league is False
    assert spec.params == ()


def test_keychain_and_config_are_both_cleared(keyring_module: FakeKeyring, isolated_home: Path):
    """The whole point: every stored copy goes, not just the first one found."""
    _seed_keychain(keyring_module)
    _config_path(isolated_home).write_text(CONFIG_WITH_CREDENTIALS, encoding="utf-8")

    envelope = auth_commands.logout()
    data = envelope.data

    assert envelope.ok
    assert data["removed"] == ["espn_s2", "swid"]
    assert data["still_set"] == []
    rows = _rows(data)
    for name in ("espn_s2", "swid"):
        assert rows[name]["keychain"] == "removed"
        assert rows[name]["config"] == "removed"
        assert rows[name]["env"] == "absent"
        assert rows[name]["env_vars"] == []
    assert data["keychain_service"] == chain.SERVICE
    assert data["config_path"] == str(_config_path(isolated_home))
    assert data["warnings"] == []

    assert keyring_module.store == {}
    remaining = config_credentials.load_credentials(_config_path(isolated_home))
    assert "espn_s2" not in remaining
    assert "swid" not in remaining
    _assert_no_values(envelope.to_dict())


def test_the_rest_of_the_config_file_survives(keyring_module: FakeKeyring, isolated_home: Path):
    """A leak remediation that also wiped the league profiles would be its own incident."""
    path = _config_path(isolated_home)
    path.write_text(CONFIG_WITH_CREDENTIALS, encoding="utf-8")

    auth_commands.logout()

    import tomllib

    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["default"] == "synthetic"
    assert document["leagues"]["synthetic"] == {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
    }
    assert document["credentials"] == {"other_provider_token": "keep-me"}


def test_nothing_stored_is_a_success_with_every_link_absent(
    keyring_module: FakeKeyring, isolated_home: Path
):
    """The outcome the user wanted is the one they already have: exit 0."""
    path = _config_path(isolated_home)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    envelope = auth_commands.logout()

    assert envelope.ok
    assert envelope.data["removed"] == []
    assert envelope.data["still_set"] == []
    assert envelope.data["warnings"] == []
    for row in envelope.data["credentials"]:
        assert row["keychain"] == "absent"
        assert row["config"] == "absent"
        assert row["env"] == "absent"
    # Nothing was rewritten and no state file was invented.
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert not staleness.auth_state_path().exists()


def test_nothing_stored_exits_zero_through_the_cli(capsys: pytest.CaptureFixture[str]):
    assert run(["auth", "logout"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] is None
    assert payload["data"]["removed"] == []


def test_the_cli_projection_never_prints_a_value(
    keyring_module: FakeKeyring, isolated_home: Path, capsys: pytest.CaptureFixture[str]
):
    _seed_keychain(keyring_module)
    _config_path(isolated_home).write_text(CONFIG_WITH_CREDENTIALS, encoding="utf-8")

    assert run(["auth", "logout"]) == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert FAKE_S2 not in stream
        assert FAKE_SWID not in stream
    assert json.loads(captured.out)["data"]["removed"] == ["espn_s2", "swid"]


def test_logout_is_idempotent(keyring_module: FakeKeyring, isolated_home: Path):
    _seed_keychain(keyring_module)
    _config_path(isolated_home).write_text(CONFIG_WITH_CREDENTIALS, encoding="utf-8")

    first = auth_commands.logout().data
    second = auth_commands.logout().data

    assert first["removed"] == ["espn_s2", "swid"]
    assert second["removed"] == []
    assert all(row["keychain"] == "absent" for row in second["credentials"])
    assert all(row["config"] == "absent" for row in second["credentials"])


# ---------------------------------------------------------------------------
# The environment: reported, never changed
# ---------------------------------------------------------------------------


def test_env_credentials_are_reported_still_set_and_left_alone(
    keyring_module: FakeKeyring, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A process cannot unset its parent's variables; saying so beats pretending."""
    import os

    monkeypatch.setenv("FANTASY_SPORTS_ESPN_S2", FAKE_S2)
    monkeypatch.setenv("FANTASY_SPORTS_SWID", FAKE_SWID)
    _seed_keychain(keyring_module)

    data = auth_commands.logout().data

    assert data["still_set"] == ["espn_s2", "swid"]
    rows = _rows(data)
    assert rows["espn_s2"]["env"] == "still-set"
    assert rows["espn_s2"]["env_vars"] == ["FANTASY_SPORTS_ESPN_S2"]
    assert rows["swid"]["env_vars"] == ["FANTASY_SPORTS_SWID"]
    # The Keychain was still cleared: env being set is not a reason to stop.
    assert data["removed"] == ["espn_s2", "swid"]
    assert keyring_module.store == {}
    # And the variables themselves are untouched.
    assert os.environ["FANTASY_SPORTS_ESPN_S2"] == FAKE_S2
    assert os.environ["FANTASY_SPORTS_SWID"] == FAKE_SWID
    assert any("FANTASY_SPORTS_ESPN_S2" in note for note in data["warnings"])
    assert any("FANTASY_SPORTS_SWID" in note for note in data["warnings"])
    _assert_no_values(data)


def test_every_set_alias_is_named_not_just_the_first(monkeypatch: pytest.MonkeyPatch):
    """`read_from_env` stops at the first hit; the report must not, or the
    second variable resurfaces the moment the first is unset."""
    monkeypatch.setenv("FANTASY_SPORTS_SWID", FAKE_SWID)
    monkeypatch.setenv("SWID", FAKE_SWID)

    rows = _rows(auth_commands.logout().data)
    assert rows["swid"]["env_vars"] == ["FANTASY_SPORTS_SWID", "SWID"]
    assert rows["espn_s2"]["env"] == "absent"


def test_a_blank_env_var_is_not_still_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FANTASY_SPORTS_ESPN_S2", "   ")
    rows = _rows(auth_commands.logout().data)
    assert rows["espn_s2"]["env"] == "absent"
    assert rows["espn_s2"]["env_vars"] == []


# ---------------------------------------------------------------------------
# Links that cannot be reached are unavailable, not silently absent
# ---------------------------------------------------------------------------


def test_no_keyring_backend_is_unavailable_and_config_is_still_cleared(
    keyring_module: FakeKeyring, isolated_home: Path
):
    keyring_module.fail_reads = _NoKeyringError("No recommended backend was available")
    _config_path(isolated_home).write_text(CONFIG_WITH_CREDENTIALS, encoding="utf-8")

    envelope = auth_commands.logout()
    data = envelope.data

    assert envelope.ok, "an unreachable link degrades the report; it does not fail the command"
    rows = _rows(data)
    assert rows["espn_s2"]["keychain"] == "unavailable"
    assert rows["swid"]["keychain"] == "unavailable"
    assert rows["espn_s2"]["config"] == "removed"
    assert data["removed"] == ["espn_s2", "swid"]
    assert sum("Keychain could not be reached" in note for note in data["warnings"]) == 2
    _assert_no_values(data)


def test_a_keychain_that_reads_but_will_not_delete_is_unavailable(
    keyring_module: FakeKeyring,
):
    """The macOS `security` shell-out can unlock for a read and refuse a write."""
    _seed_keychain(keyring_module)
    keyring_module.fail_deletes = _KeyringLocked("User interaction is not allowed")

    data = auth_commands.logout().data

    rows = _rows(data)
    assert rows["espn_s2"]["keychain"] == "unavailable"
    assert data["removed"] == []
    # Nothing was deleted, and the report says so rather than claiming otherwise.
    assert len(keyring_module.store) == 2
    assert data["warnings"]


def test_an_unreadable_config_file_is_unavailable_not_absent(
    keyring_module: FakeKeyring, isolated_home: Path
):
    """A directory where the file should be raises an `OSError` on every platform."""
    path = _config_path(isolated_home)
    path.unlink()
    path.mkdir()
    _seed_keychain(keyring_module)

    envelope = auth_commands.logout()
    data = envelope.data

    assert envelope.ok
    rows = _rows(data)
    assert rows["espn_s2"]["config"] == "unavailable"
    assert rows["espn_s2"]["keychain"] == "removed"
    assert any("could not be read or rewritten" in note for note in data["warnings"])


def test_a_damaged_config_file_is_config_invalid_not_a_silent_skip(
    keyring_module: FakeKeyring, isolated_home: Path
):
    """Same split as `load_credentials`: absent is soft, damaged says so."""
    _config_path(isolated_home).write_text('[credentials]\nespn_s2 = "unterminated\n')
    _seed_keychain(keyring_module)

    with pytest.raises(ConfigInvalidError):
        auth_commands.logout()
    # The Keychain step ran first, so a second logout after the fix finishes.
    assert keyring_module.store == {}


# ---------------------------------------------------------------------------
# The Keychain link in isolation
# ---------------------------------------------------------------------------


def test_delete_from_keychain_uses_the_service_namespace(keyring_module: FakeKeyring):
    keyring_module.set_password(chain.SERVICE, "espn_s2", FAKE_S2)
    assert chain.delete_from_keychain("espn_s2") is True
    assert keyring_module.deleted == [(chain.SERVICE, "espn_s2")]


def test_delete_from_keychain_reports_an_absent_entry_without_deleting(
    keyring_module: FakeKeyring,
):
    assert chain.delete_from_keychain("espn_s2") is False
    assert keyring_module.deleted == []


def test_delete_from_keychain_raises_rather_than_guessing(keyring_module: FakeKeyring):
    """Fail-soft belongs to `logout.py`; a `False` here would mean "nothing stored"."""
    keyring_module.fail_reads = _KeyringLocked("locked")
    with pytest.raises(_KeyringLocked):
        chain.delete_from_keychain("espn_s2")


# ---------------------------------------------------------------------------
# The config link in isolation
# ---------------------------------------------------------------------------


def test_remove_credentials_returns_only_the_names_it_removed(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = "x"\nkeep = "y"\n', encoding="utf-8")
    assert config_credentials.remove_credentials(["espn_s2", "swid"], path) == ("espn_s2",)
    assert config_credentials.load_credentials(path) == {"keep": "y"}


def test_remove_credentials_drops_an_emptied_table(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('default = "a"\n\n[credentials]\nespn_s2 = "x"\n', encoding="utf-8")
    config_credentials.remove_credentials(["espn_s2"], path)
    text = path.read_text(encoding="utf-8")
    assert "[credentials]" not in text
    assert 'default = "a"' in text


def test_remove_credentials_writes_nothing_when_nothing_matches(tmp_path: Path):
    path = tmp_path / "config.toml"
    original = "# hand-written comment\ndefault = 'a'\n"
    path.write_text(original, encoding="utf-8")
    assert config_credentials.remove_credentials(["espn_s2"], path) == ()
    assert path.read_text(encoding="utf-8") == original, "no rewrite means the comment survives"


def test_remove_credentials_on_a_missing_file_is_empty_and_creates_nothing(tmp_path: Path):
    path = tmp_path / "nope" / "config.toml"
    assert config_credentials.remove_credentials(["espn_s2"], path) == ()
    assert not path.parent.exists()


def test_remove_credentials_keeps_the_file_mode(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = "x"\nswid = "y"\n', encoding="utf-8")
    path.chmod(0o600)
    config_credentials.remove_credentials(["espn_s2"], path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_remove_credentials_when_the_table_is_not_a_table_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('credentials = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        config_credentials.remove_credentials(["espn_s2"], path)


# ---------------------------------------------------------------------------
# Staleness bookkeeping: an age for a value that is gone is not an age
# ---------------------------------------------------------------------------


def test_logout_forgets_stored_at_but_keeps_last_success(
    keyring_module: FakeKeyring, tmp_path: Path
):
    state_path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2", "swid"], path=state_path)
    staleness.record_success(["espn_s2"], path=state_path)
    _seed_keychain(keyring_module)

    clear_credentials(
        chain.ESPN_CREDENTIALS,
        environ={},
        config_path=tmp_path / "config.toml",
        state_path=state_path,
    )

    state = staleness.load_auth_state(state_path)
    assert state.for_name("espn_s2").stored_at is None
    assert state.for_name("espn_s2").last_success_at is not None
    assert "swid" not in state.entries, "nothing left to say about it"


def test_an_unavailable_keychain_keeps_stored_at(keyring_module: FakeKeyring, tmp_path: Path):
    """The entry may still be there, so the timestamp may still describe it."""
    state_path = tmp_path / "auth-state.json"
    staleness.record_stored(["espn_s2"], path=state_path)
    keyring_module.fail_reads = _NoKeyringError("none")

    clear_credentials(
        chain.ESPN_CREDENTIALS,
        environ={},
        config_path=tmp_path / "config.toml",
        state_path=state_path,
    )

    assert staleness.load_auth_state(state_path).for_name("espn_s2").stored_at is not None


def test_forget_stored_writes_nothing_when_there_is_nothing_to_forget(tmp_path: Path):
    state_path = tmp_path / "nope" / "auth-state.json"
    staleness.forget_stored(["espn_s2"], path=state_path)
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------


def test_clear_credentials_accepts_an_injected_remover(tmp_path: Path):
    removed: list[str] = []

    def remover(name: str) -> bool:
        removed.append(name)
        return name == "swid"

    report = clear_credentials(
        chain.ESPN_CREDENTIALS,
        environ={},
        keychain_remover=remover,
        config_path=tmp_path / "config.toml",
        state_path=tmp_path / "auth-state.json",
    )

    assert removed == ["espn_s2", "swid"]
    by_name = {row.name: row for row in report.credentials}
    assert by_name["espn_s2"].keychain is LinkOutcome.ABSENT
    assert by_name["swid"].keychain is LinkOutcome.REMOVED
    assert report.removed == ("swid",)


def test_an_injected_remover_that_raises_is_unavailable(tmp_path: Path):
    """Fail-soft is a property of the walk, not of whichever remover is plugged in."""

    def remover(name: str) -> bool:
        raise _KeyringLocked("locked")

    report = clear_credentials(
        chain.ESPN_CREDENTIALS,
        environ={},
        keychain_remover=remover,
        config_path=tmp_path / "config.toml",
        state_path=tmp_path / "auth-state.json",
    )
    assert all(row.keychain is LinkOutcome.UNAVAILABLE for row in report.credentials)


def test_logout_module_imports_no_cli_or_provider_machinery():
    """ADR-0003 holds for the new module the same as the rest of `auth/`."""
    import ast

    path = Path("src/fantasy_sports/auth/logout.py")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert not (roots & {"typer", "click", "rich", "espn_api", "keyring"})
