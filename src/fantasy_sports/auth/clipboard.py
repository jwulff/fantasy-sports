"""Read ESPN's two cookies off the system clipboard, then clear it (#160).

``auth login --from-clipboard`` is the any-browser path: Safari, Windows,
Linux with a keyring. It needs the browser and the CLI to share a clipboard,
so a remote host (SSH) is not covered. A bookmarklet (:data:`BOOKMARKLET`) runs
on fantasy.espn.com, where ``document.cookie`` can read both cookies because
neither is HttpOnly, and writes ``espn_s2=...; SWID={...}`` to the clipboard.
This module reads that string back, picks the two cookies out of it, and
overwrites the clipboard so the credentials do not linger there.

The clipboard text is a credential. It is wrapped in
:class:`~fantasy_sports.auth.chain.Secret` the moment it leaves the subprocess,
and nothing here puts any of it in an error: every failure is a data-free
:class:`ClipboardError` carrying a ``reason`` tag. See
``docs/memory/credential-leak-channels.md`` for why: a frame's locals are
reprd by tracebacks and by pytest, so ``subprocess`` results (whose repr
includes stdout) never stay bound to a name.

The platform and the subprocess runner are resolved at call time, never bound
as default arguments, so tests exercise all three platform branches on a Linux
runner (``docs/memory/default-args-defeat-monkeypatch.md``). ``subprocess`` is
imported inside the functions that use it: ``--help`` must not pay for it
(``docs/memory/import-budget-and-the-fastpath.md``).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fantasy_sports.auth import chain
from fantasy_sports.auth.chain import Secret

__all__ = [
    "BOOKMARKLET",
    "BOOKMARKLET_INSTRUCTIONS",
    "ClipboardError",
    "clear_clipboard",
    "cookies_are_valid",
    "parse_cookies",
    "read_clipboard",
]

BOOKMARKLET = (
    "javascript:(function(){"
    "var want=['espn_s2','SWID'],"
    "found=document.cookie.split(';')"
    ".map(function(c){return c.trim()})"
    ".filter(function(c){return want.indexOf(c.split('=')[0])>-1});"
    "if(found.length<2){"
    "alert('Not logged in: ESPN cookies espn_s2 and SWID not found. "
    "Log in at fantasy.espn.com and try again.');return}"
    "navigator.clipboard.writeText(found.join('; ')).then("
    "function(){alert('ESPN cookies copied. Now run: fantasy-sports auth login --from-clipboard')},"
    "function(){alert('Could not write to the clipboard.')})"
    "})();"
)
"""The static bookmarklet. It holds no secret: it reads ``document.cookie`` at
click time, keeps only the two named cookies, and writes them to the clipboard."""

BOOKMARKLET_INSTRUCTIONS = (
    "Create a browser bookmark whose URL is the bookmarklet, open fantasy.espn.com while "
    "logged in, click it, then run `fantasy-sports auth login --from-clipboard`."
)

_COMMANDS: Mapping[str, Sequence[tuple[Sequence[str], Sequence[str]]]] = {
    # platform -> candidates of (read argv, clear argv); clearing feeds the tool empty stdin
    "darwin": [(("pbpaste",), ("pbcopy",))],
    "linux": [
        (("wl-paste", "--no-newline"), ("wl-copy", "--clear")),
        (("xclip", "-o", "-selection", "clipboard"), ("xclip", "-selection", "clipboard")),
    ],
    "win32": [
        (
            ("powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"),
            ("powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $null"),
        )
    ],
}

_TIMEOUT_SECONDS = 10

Runner = Callable[..., Any]


class ClipboardError(Exception):
    """The clipboard could not be read. Carries a tag, never any clipboard text."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _platform_key(platform: str | None) -> str:
    current = platform if platform is not None else sys.platform
    return "linux" if current.startswith("linux") else current


def _capture(run: Runner, argv: Sequence[str]) -> tuple[int, Secret]:
    """Run ``argv`` and return its exit status and stdout, wrapped at once.

    The ``CompletedProcess`` is never bound to a name: its repr includes stdout.
    """
    done = run(list(argv), capture_output=True, timeout=_TIMEOUT_SECONDS, check=False)
    return done.returncode, Secret(bytes(done.stdout or b"").decode("utf-8", errors="replace"))


def read_clipboard(*, platform: str | None = None, run: Runner | None = None) -> Secret:
    """Return the clipboard text as a :class:`Secret`.

    :raises ClipboardError: ``unsupported_platform``, ``no_clipboard_tool`` (none
        of the platform's tools is installed) or ``read_failed``.
    """
    import subprocess

    key = _platform_key(platform)
    candidates = _COMMANDS.get(key)
    if candidates is None:
        raise ClipboardError("unsupported_platform", f"No clipboard support for platform {key!r}.")
    runner = run or subprocess.run
    failed = False
    for read_argv, _clear in candidates:
        try:
            status, text = _capture(runner, read_argv)
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError):
            failed = True
            continue
        if status == 0:
            return text
        failed = True
    if failed:
        raise ClipboardError("read_failed", "The clipboard tool failed to read the clipboard.")
    tools = ", ".join(candidate[0][0] for candidate in candidates)
    raise ClipboardError("no_clipboard_tool", f"No clipboard tool found (looked for {tools}).")


def clear_clipboard(*, platform: str | None = None, run: Runner | None = None) -> bool:
    """Best-effort overwrite of the clipboard with nothing. ``True`` if a tool accepted it."""
    import subprocess

    candidates = _COMMANDS.get(_platform_key(platform), ())
    runner = run or subprocess.run
    for _read, clear_argv in candidates:
        try:
            done = runner(
                list(clear_argv),
                input=b"",
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return True
    return False


def parse_cookies(clipboard: Secret) -> dict[str, Secret]:
    """Pick ``espn_s2`` and ``SWID`` out of a ``document.cookie``-style string.

    Order does not matter and other cookies are ignored. Returns only what was
    found, each value already wrapped; it never raises, so no frame holding the
    plain text can be on an exception's path. A name seen twice keeps its first
    value.
    """
    found: dict[str, Secret] = {}
    for piece in clipboard.reveal().split(";"):
        name, sep, value = piece.strip().partition("=")
        key = {"espn_s2": "espn_s2", "swid": "swid"}.get(name.strip().lower()) if sep else None
        if key is not None and key not in found and value.strip():
            found[key] = Secret(value.strip())
    return found


def cookies_are_valid(found: Mapping[str, Secret]) -> bool:
    """Whether each parsed value passes its credential's normalizer.

    ``save_credentials`` would reject a bad one too, but with the plain-string
    ``values`` mapping alive in the raising frame, where a captured-locals
    traceback would render it. Checking here, with the failure turned into a
    flag *outside* the ``except`` block, keeps the value off every exception's
    path; the caller raises a data-free error of its own.
    """
    from fantasy_sports.core.errors import FantasySportsError

    valid = True
    for name, value in found.items():
        try:
            chain.normalize_credential(name, value.reveal())
        except FantasySportsError:
            valid = False
    return valid
