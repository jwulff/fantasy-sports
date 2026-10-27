"""``auth login --from-clipboard`` and ``auth bookmarklet`` (#160).

The clipboard is a secret store: it briefly holds ``espn_s2`` and ``SWID``. Every
test here mocks ``subprocess.run`` (``_fake_clipboard``) so no real clipboard,
Keychain, or network is touched, and the platform is passed or pinned at call
time so all three branches run on a Linux CI runner. The redaction tests come
first, in the pattern of ``test_auth.py``: render a real string, search it for a
sentinel.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _harness import FAKE_SWID, isolate_home

from fantasy_sports.auth import chain, clipboard, staleness
from fantasy_sports.commands import auth as auth_commands
from fantasy_sports.core.errors import AuthMissingError, ConfigInvalidError

_REAL_RUN = subprocess.run
COOKIE_S2 = "AEBsentinelS2cookievalue0123456789abcdefXYZ%2Fzz"
SWID = FAKE_SWID
GOOD = f"espn_s2={COOKIE_S2}; SWID={SWID}"
# Clipboard text that is not ours: must never be echoed back.
FOREIGN = "my-bank-password-SENTINEL-9f8e7d6c5b4a"


@pytest.fixture(autouse=True)
def _forget_secrets():
    chain.forget_secrets()
    yield
    chain.forget_secrets()


class FakeClipboard:
    """A stand-in for ``subprocess.run`` that behaves like the clipboard tools."""

    def __init__(self, text: str = GOOD, *, missing: tuple[str, ...] = (), fail: bool = False):
        self.text = text
        self.missing = missing
        self.fail = fail
        self.calls: list[list[str]] = []
        self.cleared_with: list[Any] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(argv)
        if argv[0] in self.missing:
            raise FileNotFoundError(argv[0])
        is_read = (
            argv[0] in ("pbpaste", "wl-paste")
            or (argv[0] == "xclip" and "-o" in argv)
            or "Get-Clipboard" in argv
        )
        if is_read:
            code = 1 if self.fail else 0
            return SimpleNamespace(returncode=code, stdout=self.text.encode(), stderr=b"")
        self.cleared_with.append(kwargs.get("input"))
        if not self.fail:
            self.text = ""
        return SimpleNamespace(returncode=1 if self.fail else 0, stdout=b"", stderr=b"")


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args: Any, **kwargs: Any):
        pytest.fail("a test reached the real subprocess / clipboard")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def pasteboard(monkeypatch: pytest.MonkeyPatch) -> FakeClipboard:
    fake = FakeClipboard()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    return fake


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    isolate_home(tmp_path, monkeypatch)
    return tmp_path


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    written: dict[str, str] = {}
    monkeypatch.setattr("fantasy_sports.auth.chain.write_to_keychain", written.__setitem__)
    return written


# ---------------------------------------------------------------------------
# Redaction: the clipboard text never reaches an error
# ---------------------------------------------------------------------------


def _unparseable() -> AuthMissingError:
    """Run the login against whatever the clipboard holds. Holds no clipboard text itself:
    a plain-string local here would be rendered by ``capture_locals`` and fail the test."""
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_clipboard=True)
    return caught.value


@pytest.mark.parametrize("text", [FOREIGN, f"espn_s2={FOREIGN}", f"SWID={FOREIGN}", ""])
def test_an_unparseable_clipboard_is_auth_missing_and_never_echoed(
    home: Path, pasteboard: FakeClipboard, keychain, text: str
):
    pasteboard.text = text
    error = _unparseable()
    rendered = " ".join(
        [
            str(error),
            repr(error),
            error.message,
            error.remediation or "",
            json.dumps(error.details),
            "".join(traceback.format_exception(error)),
            "".join(
                traceback.TracebackException.from_exception(error, capture_locals=True).format()
            ),
        ]
    )
    assert FOREIGN not in rendered
    assert error.code.value == "AUTH_MISSING"
    assert "bookmarklet" in (error.remediation or "")
    assert keychain == {}


def test_a_bad_swid_value_does_not_echo_the_clipboard(home: Path, pasteboard, keychain):
    pasteboard.text = f"espn_s2={COOKIE_S2}; SWID={FOREIGN}"
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_clipboard=True)
    rendered = "".join(
        traceback.TracebackException.from_exception(caught.value, capture_locals=True).format()
    )
    assert FOREIGN not in rendered and COOKIE_S2 not in rendered
    assert keychain == {}


def test_a_wrapped_clipboard_is_redacted_in_locals_but_a_plain_string_is_not():
    def inner() -> None:
        held = clipboard.parse_cookies(chain.Secret(GOOD))  # noqa: F841
        raise AuthMissingError("failed")

    with pytest.raises(AuthMissingError) as caught:
        inner()
    rendered = "".join(
        traceback.TracebackException.from_exception(caught.value, capture_locals=True).format()
    )
    assert COOKIE_S2 not in rendered

    def control() -> None:
        held = COOKIE_S2  # noqa: F841
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError) as leaked:
        control()
    assert COOKIE_S2 in "".join(
        traceback.TracebackException.from_exception(leaked.value, capture_locals=True).format()
    )


def test_a_clipboard_tool_failure_names_no_clipboard_text(home: Path, pasteboard, keychain):
    pasteboard.fail = True
    with pytest.raises(AuthMissingError) as caught:
        auth_commands.login(from_clipboard=True)
    assert caught.value.details == {"reason": "read_failed"}
    assert COOKIE_S2 not in str(caught.value)


def test_a_successful_envelope_carries_no_value_length_or_prefix(home, pasteboard, keychain):
    rendered = json.dumps(auth_commands.login(from_clipboard=True).data)
    for fragment in (COOKIE_S2, COOKIE_S2[:8], SWID, SWID.strip("{}"), str(len(COOKIE_S2))):
        assert fragment not in rendered


# ---------------------------------------------------------------------------
# Reading and clearing, per platform (platform injected at call time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "expected_first"),
    [
        ("darwin", ["pbpaste"]),
        ("linux", ["wl-paste", "--no-newline"]),
        ("win32", ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]),
    ],
)
def test_read_uses_the_platforms_tool(platform: str, expected_first: list[str]):
    fake = FakeClipboard()
    assert clipboard.read_clipboard(platform=platform, run=fake).reveal() == GOOD
    assert fake.calls == [expected_first]


def test_linux_falls_back_to_xclip_when_wl_paste_is_missing():
    fake = FakeClipboard(missing=("wl-paste",))
    assert clipboard.read_clipboard(platform="linux2", run=fake).reveal() == GOOD
    assert fake.calls[-1] == ["xclip", "-o", "-selection", "clipboard"]


def test_no_tool_installed_is_reported_without_text():
    fake = FakeClipboard(missing=("wl-paste", "xclip"))
    with pytest.raises(clipboard.ClipboardError) as caught:
        clipboard.read_clipboard(platform="linux", run=fake)
    assert caught.value.reason == "no_clipboard_tool"


def test_an_unsupported_platform_is_reported():
    with pytest.raises(clipboard.ClipboardError) as caught:
        clipboard.read_clipboard(platform="plan9", run=FakeClipboard())
    assert caught.value.reason == "unsupported_platform"


def test_a_tool_that_times_out_is_a_read_failure():
    def hang(argv: list[str], **kwargs: Any):
        raise subprocess.TimeoutExpired(argv, 10)

    with pytest.raises(clipboard.ClipboardError) as caught:
        clipboard.read_clipboard(platform="darwin", run=hang)
    assert caught.value.reason == "read_failed"


def test_the_default_platform_is_resolved_at_call_time(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClipboard()
    monkeypatch.setattr(sys, "platform", "win32")
    clipboard.read_clipboard(run=fake)
    assert fake.calls[0][0] == "powershell"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ["pbcopy"]),
        ("linux", ["wl-copy", "--clear"]),
        ("win32", ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $null"]),
    ],
)
def test_clear_overwrites_with_nothing(platform: str, expected: list[str]):
    fake = FakeClipboard()
    assert clipboard.clear_clipboard(platform=platform, run=fake) is True
    assert fake.calls == [expected]
    assert fake.cleared_with == [b""]


def test_clear_falls_back_on_linux_and_reports_failure():
    fake = FakeClipboard(missing=("wl-copy",))
    assert clipboard.clear_clipboard(platform="linux", run=fake) is True
    assert fake.calls[-1] == ["xclip", "-selection", "clipboard"]
    assert clipboard.clear_clipboard(platform="linux", run=FakeClipboard(fail=True)) is False
    assert clipboard.clear_clipboard(platform="plan9", run=FakeClipboard()) is False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse(text: str) -> dict[str, str]:
    return {k: v.reveal() for k, v in clipboard.parse_cookies(chain.Secret(text)).items()}


def test_parse_accepts_either_order_and_ignores_other_cookies():
    expected = {"espn_s2": COOKIE_S2, "swid": SWID}
    assert _parse(f"SWID={SWID}; espn_s2={COOKIE_S2}") == expected
    assert _parse(f"a=1; espn_s2={COOKIE_S2}; other=x=y; SWID={SWID}; z=9") == expected
    assert _parse(f"  espn_s2={COOKIE_S2};SWID={SWID}\n") == expected


def test_parse_keeps_the_first_of_a_repeated_name_and_skips_empty_values():
    assert _parse(f"espn_s2=; espn_s2={COOKIE_S2}; espn_s2=other") == {"espn_s2": COOKIE_S2}
    assert _parse("espn_s2; SWID") == {}


def test_parse_returns_secrets():
    found = clipboard.parse_cookies(chain.Secret(GOOD))
    assert all(isinstance(value, chain.Secret) for value in found.values())


# ---------------------------------------------------------------------------
# auth login --from-clipboard
# ---------------------------------------------------------------------------


def test_login_stores_via_save_credentials_then_clears_the_clipboard(
    home: Path, pasteboard: FakeClipboard, keychain
):
    data = auth_commands.login(from_clipboard=True).data
    assert keychain == {"espn_s2": COOKIE_S2, "swid": SWID}
    assert data["stored"] == ["espn_s2", "swid"]
    assert data["source"] == {"clipboard": True}
    assert data["clipboard_cleared"] is True
    assert pasteboard.text == ""
    assert staleness.load_auth_state().for_name("espn_s2").stored_at is not None


def test_a_braceless_swid_is_repaired(home, pasteboard, keychain):
    pasteboard.text = f"espn_s2={COOKIE_S2}; SWID={SWID.strip('{}')}"
    data = auth_commands.login(from_clipboard=True).data
    assert keychain["swid"] == SWID
    assert data["repaired"] == ["swid"]


def test_a_failed_clear_is_reported_not_fatal(home, pasteboard, keychain, monkeypatch):
    monkeypatch.setattr(clipboard, "clear_clipboard", lambda **_: False)
    data = auth_commands.login(from_clipboard=True).data
    assert data["clipboard_cleared"] is False
    assert keychain["espn_s2"] == COOKIE_S2


def test_the_clipboard_is_left_alone_when_nothing_was_stored(home, pasteboard, keychain):
    pasteboard.text = FOREIGN
    with pytest.raises(AuthMissingError):
        auth_commands.login(from_clipboard=True)
    assert pasteboard.text == FOREIGN
    assert pasteboard.cleared_with == []


def test_from_clipboard_and_from_browser_are_mutually_exclusive(home, pasteboard, keychain):
    with pytest.raises(ConfigInvalidError, match="mutually exclusive") as caught:
        auth_commands.login(from_clipboard=True, from_browser="chrome")
    assert caught.value.details["kind"] == "argument"
    assert pasteboard.calls == [] and keychain == {}


def test_profile_still_requires_from_browser(home, pasteboard):
    with pytest.raises(ConfigInvalidError, match="--from-browser"):
        auth_commands.login(from_clipboard=True, profile="Default")
    assert pasteboard.calls == []


def test_without_the_flag_the_clipboard_is_never_read(home, pasteboard, keychain, monkeypatch):
    entered = iter([COOKIE_S2, SWID, ""])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(entered))
    auth_commands.login()
    assert pasteboard.calls == []


# ---------------------------------------------------------------------------
# auth bookmarklet
# ---------------------------------------------------------------------------


def test_bookmarklet_is_static_and_does_what_the_issue_says():
    data = auth_commands.bookmarklet().data
    url = data["bookmarklet"]
    assert url.startswith("javascript:")
    for needle in ("document.cookie", "espn_s2", "SWID", "navigator.clipboard.writeText", "alert("):
        assert needle in url
    assert "not logged in" in url.lower()
    assert "--from-clipboard" in data["instructions"]
    assert "\n" not in data["instructions"]
    assert auth_commands.bookmarklet().data == data


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_the_login_spec_declares_from_clipboard_as_a_plain_flag():
    from fantasy_sports.commands import REGISTRY

    spec = REGISTRY["auth login"]
    param = next(p for p in spec.params if p.name == "from_clipboard")
    assert param.cli_flags == ("--from-clipboard",)
    assert param.display == "--from-clipboard"
    assert "auth bookmarklet" in REGISTRY


def test_cli_end_to_end_from_clipboard(home, pasteboard, keychain, capsys):
    from fantasy_sports.cli.app import run

    assert run(["auth", "login", "--from-clipboard", "--output", "json"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["data"]["clipboard_cleared"] is True
    assert COOKIE_S2 not in out.out + out.err


def test_cli_unparseable_clipboard_exits_with_auth_missing(home, pasteboard, keychain, capsys):
    from fantasy_sports.cli.app import run

    pasteboard.text = FOREIGN
    assert run(["auth", "login", "--from-clipboard", "--output", "json"]) != 0
    out = capsys.readouterr()
    assert "AUTH_MISSING" in out.err + out.out
    assert FOREIGN not in out.out + out.err


def test_cli_bookmarklet_end_to_end(capsys):
    from fantasy_sports.cli.app import run

    assert run(["auth", "bookmarklet", "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["bookmarklet"].startswith("javascript:")


def test_the_table_prints_the_bookmarklet_unbroken():
    """On a TTY the default output is a table; the URL must survive it as one line."""
    from fantasy_sports.output import render

    envelope = auth_commands.bookmarklet()
    text = render(envelope, "table", verbatim=("bookmarklet",))
    assert clipboard.BOOKMARKLET in text.splitlines()
    assert "instructions" in text


def test_the_table_folds_it_without_the_verbatim_declaration():
    """Control: the same render without ``verbatim`` breaks the URL across lines."""
    from fantasy_sports.output import render

    text = render(auth_commands.bookmarklet(), "table")
    assert clipboard.BOOKMARKLET not in text.splitlines()


def test_the_bookmarklet_command_declares_its_url_verbatim():
    from fantasy_sports.commands import REGISTRY

    assert REGISTRY["auth bookmarklet"].verbatim_fields == ("bookmarklet",)


def test_cli_table_output_keeps_the_bookmarklet_on_one_line(capsys):
    from fantasy_sports.cli.app import run

    assert run(["auth", "bookmarklet", "--output", "table"]) == 0
    assert clipboard.BOOKMARKLET in capsys.readouterr().out.splitlines()


def _failed_login() -> ConfigInvalidError:
    """Holds no secret in its own locals: a test frame on the traceback is rendered too."""
    with pytest.raises(ConfigInvalidError) as caught:
        auth_commands.login(from_clipboard=True)
    return caught.value


def test_a_post_write_failure_clears_the_clipboard_and_leaks_nothing(
    home, pasteboard, keychain, monkeypatch
):
    def explode(*args: Any, **kwargs: Any):
        raise OSError("state file unwritable")

    monkeypatch.setattr(staleness, "record_stored", explode)
    error = _failed_login()
    assert keychain["espn_s2"] == COOKIE_S2
    assert pasteboard.text == ""
    assert error.details == {
        "kind": "credential_store",
        "cause": "OSError",
        "clipboard_cleared": True,
    }
    assert error.__cause__ is None and error.__context__ is None
    rendered = " ".join(
        [
            str(error),
            json.dumps(error.details),
            "".join(
                traceback.TracebackException.from_exception(error, capture_locals=True).format()
            ),
        ]
    )
    for secret in (COOKIE_S2, SWID, SWID.strip("{}")):
        assert secret not in rendered


def _run_bookmarklet(cookie: str) -> list[str]:
    """Run the bookmarklet under node with a stubbed ``document``; return what it did.

    ``subprocess.run`` is patched to refuse in this file, so the real one is
    captured at import (``_REAL_RUN``). Skipped where node is absent.
    """
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    body = clipboard.BOOKMARKLET.removeprefix("javascript:")
    # Parameters, not globals: modern node defines `navigator` as a getter-only global.
    script = (
        f"var out=[];(function(document,navigator,alert){{{body}}})("
        f"{{cookie:{json.dumps(cookie)}}},"
        "{clipboard:{writeText:function(t){out.push('copied:'+t);"
        "return {then:function(ok){ok()}}}}},"
        "function(m){out.push('alert:'+m)});"
        "console.log(JSON.stringify(out))"
    )
    done = _REAL_RUN([node, "-e", script], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


def test_the_bookmarklet_copies_exactly_the_two_cookies():
    out = _run_bookmarklet("a=1; SWID={X}; espn_s2=abc; b=2")
    assert out[0] == "copied:SWID={X}; espn_s2=abc"
    assert out[1].startswith("alert:ESPN cookies copied")


@pytest.mark.parametrize("cookie", ["x=1", ""])
def test_the_bookmarklet_says_not_logged_in_only_when_both_are_absent(cookie: str):
    (alert,) = _run_bookmarklet(cookie)
    assert alert.startswith("alert:Not logged in")


def test_the_bookmarklet_names_the_httponly_case_when_only_swid_is_visible():
    (alert,) = _run_bookmarklet("SWID={X}; other=1")
    assert "Found SWID but not espn_s2" in alert
    assert "HttpOnly" in alert and "--from-browser" in alert and "DevTools" in alert
    assert "Not logged in" not in alert


def test_the_bookmarklet_does_not_count_duplicates_as_a_pair():
    (alert,) = _run_bookmarklet("espn_s2=old; espn_s2=new")
    assert "Found espn_s2 but not SWID" in alert


def test_a_clipboard_with_swid_but_no_espn_s2_gets_the_targeted_remediation(
    home, pasteboard, keychain
):
    pasteboard.text = f"SWID={SWID}; other={FOREIGN}"
    error = _unparseable()
    assert error.details == {"reason": "espn_s2_missing"}
    assert "HttpOnly" in error.remediation and "--from-browser" in error.remediation
    rendered = " ".join([str(error), error.remediation, json.dumps(error.details)])
    assert FOREIGN not in rendered and SWID not in rendered
    assert pasteboard.text.startswith("SWID=") and keychain == {}


def test_clearing_uses_the_tool_that_read_the_clipboard():
    """wl-paste fails, xclip supplies the cookies: a succeeding wl-copy must not count."""

    class Mixed(FakeClipboard):
        def __call__(self, argv: list[str], **kwargs: Any) -> Any:
            if argv[0] == "wl-paste":
                self.calls.append(argv)
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            return super().__call__(argv, **kwargs)

    fake = Mixed()
    text, source = clipboard.read_clipboard_from(platform="linux", run=fake)
    assert text.reveal() == GOOD and source == 1
    fake.calls.clear()
    assert clipboard.clear_clipboard(platform="linux", run=fake, source=source) is True
    assert fake.calls == [["xclip", "-selection", "clipboard"]]
    failing = FakeClipboard(missing=("xclip",))
    assert clipboard.clear_clipboard(platform="linux", run=failing, source=1) is False
    assert ["wl-copy", "--clear"] not in failing.calls
