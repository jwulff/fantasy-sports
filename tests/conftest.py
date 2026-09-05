"""Shared test configuration, and the cassette scrub-before-write hook.

Two things live here, and both are security controls rather than conveniences:

1. **The scrub-before-write hook.** ``vcrpy`` scrubs nothing by default: every
   credential it sees is written to the cassette verbatim. :func:`vcr_config`
   installs request/response filters that replace cookies, ``Authorization``
   headers, ``espn_s2``/``SWID`` query parameters, and SWID GUIDs echoed inside
   response bodies *before* anything reaches disk. ESPN returns owner SWIDs
   inline in roster payloads, so header filtering alone is not enough.

2. **The repo-wide credential scan.** :func:`scan_paths` re-reads what is
   actually on disk and fails if any committed fixture still matches a
   credential pattern. The hook is the control; the scan is the audit that the
   control was in force when the fixture was written.

Unit tests never touch the network: ``pytest-socket`` is enabled through
``addopts`` in ``pyproject.toml``, so a cassette miss or a stray live call
fails loudly instead of quietly reaching ESPN.

The scan's threat model is *recorded* fixture data — bytes captured from a real
ESPN session and written by a machine. It deliberately does not scan Python
sources: those are hand-authored, are covered by review, and legitimately
contain synthetic credential-shaped values (this file and
``tests/unit/test_scrubbing.py`` both do). A real credential must never be
typed into a source file in the first place.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
CASSETTE_LIBRARY_DIR = TESTS_DIR / "cassettes"

#: What a scrubbed value looks like in a cassette. Chosen so that no credential
#: pattern below can match it -- ``espn_s2=REDACTED`` must read as clean.
REDACTED = "REDACTED"
#: SWID GUIDs keep their brace shape so replayed payloads stay parseable.
SWID_PLACEHOLDER = "{SWID-REDACTED}"

#: Headers whose value is the credential itself. The ESPN request auth *is*
#: ``Cookie: espn_s2=...; SWID={...}``, which makes ``cookie`` load-bearing.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-espn-swid",
        "espn-s2",
    }
)

#: Query-parameter names that carry a credential when ESPN is called by URL
#: rather than by cookie.
SENSITIVE_QUERY_PARAMS = frozenset({"espn_s2", "swid"})


# --------------------------------------------------------------------------- #
# Credential patterns
# --------------------------------------------------------------------------- #

# A SWID is a brace-wrapped GUID: {1A2B3C4D-5E6F-...}. This is the shape ESPN
# echoes inside roster/owner payloads, which is the case header filtering misses.
_SWID_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"
)

# A GUID sitting under a ``swid``-ish key without its braces.
_SWID_KEYED_RE = re.compile(
    r"""(swid)                       # the key
        (["']?\s*[:=]\s*["']?)       # separator, quoted on either side or not
        (?!REDACTED\b)
        [0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ``espn_s2=<blob>``. The value class excludes ``$``, ``{`` and ``}`` so that a
# CI expression such as ``ESPN_S2: ${{ secrets.ESPN_S2 }}`` is not a finding.
_ESPN_S2_RE = re.compile(
    r"""(espn_s2)
        (["']?\s*[:=]\s*["']?)
        (?!REDACTED\b)
        [^\s"';,&{}$\[\]]{8,}
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Name -> pattern. Names appear in failure output; matched text never does.
CREDENTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "SWID GUID": _SWID_GUID_RE,
    "GUID under a swid key": _SWID_KEYED_RE,
    "espn_s2 value": _ESPN_S2_RE,
}


# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #


class UnscrubbableResponseError(RuntimeError):
    """A response body could not be read, so it could not be proven scrubbed.

    Raised instead of recording it. ``vcrpy`` stores a still-compressed body as
    opaque bytes, and a regex over opaque bytes finds nothing -- the recording
    would look clean and carry a credential. Failing loudly is the point of
    this unit.
    """


def scrub_text(text: str) -> str:
    """Replace every credential pattern in ``text`` with its placeholder."""
    text = _SWID_GUID_RE.sub(SWID_PLACEHOLDER, text)
    text = _SWID_KEYED_RE.sub(rf"\1\2{SWID_PLACEHOLDER}", text)
    return _ESPN_S2_RE.sub(rf"\1\2{REDACTED}", text)


def _scrub_header_value(name: str, value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub_header_value(name, item) for item in value]
    if not isinstance(value, str):
        return value
    if name.lower() in SENSITIVE_HEADERS:
        return REDACTED
    # A credential can hide in a header we do not consider sensitive by name.
    return scrub_text(value)


def scrub_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Replace sensitive header values, keeping the header itself present.

    Replacing rather than deleting is deliberate: a cassette that silently
    *drops* ``Cookie`` looks identical to one recorded without auth, so nobody
    can tell from the fixture whether the scrub ran.
    """
    if not headers:
        return {}
    return {name: _scrub_header_value(name, value) for name, value in headers.items()}


def scrub_uri(uri: str) -> str:
    """Redact credential query parameters and any GUID left in the URI."""
    parts = urlsplit(uri)
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [
            (key, REDACTED if key.lower() in SENSITIVE_QUERY_PARAMS else value)
            for key, value in pairs
        ]
        if scrubbed != pairs:
            parts = parts._replace(query=urlencode(scrubbed))
    return scrub_text(urlunsplit(parts))


def _scrub_body_value(body: Any, *, context: str) -> Any:
    if body is None:
        return None
    if isinstance(body, str):
        return scrub_text(body)
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnscrubbableResponseError(
                f"Refusing to record: the {context} body is not UTF-8 text, so it "
                "cannot be scanned for credentials. If it is compressed, set "
                "decode_compressed_response=True; if it is genuinely binary, "
                "drop it in before_record_response rather than recording it."
            ) from exc
        return scrub_text(text).encode("utf-8")
    # Anything else (BytesIO, dict, iterator) is left alone; vcrpy will refuse
    # to serialize what it cannot represent.
    return body


def scrub_request(request: Any) -> Any:
    """``before_record_request`` hook: scrub headers, URI, and body."""
    if request is None:
        return None
    request.headers = scrub_headers(request.headers)
    request.uri = scrub_uri(request.uri)
    request.body = _scrub_body_value(request.body, context="request")
    return request


def scrub_response(response: Any) -> Any:
    """``before_record_response`` hook: scrub headers and the body.

    This runs *after* vcrpy's ``decode_response`` (see
    :func:`build_vcr_config`), so a gzipped ESPN payload has already been
    decompressed and its SWIDs are visible as text.
    """
    if response is None:
        return None
    if not isinstance(response, dict):
        return response
    response["headers"] = scrub_headers(response.get("headers"))
    body = response.get("body")
    if isinstance(body, dict) and "string" in body:
        body["string"] = _scrub_body_value(body["string"], context="response")
    elif body is not None:
        response["body"] = _scrub_body_value(body, context="response")
    return response


def build_vcr_config() -> dict[str, Any]:
    """The vcrpy configuration that makes an unscrubbed cassette unwritable."""
    return {
        # Replacement values, not bare names: a bare name deletes the header,
        # and a deleted header is indistinguishable from one never sent.
        "filter_headers": [
            ("authorization", REDACTED),
            ("cookie", REDACTED),
            ("set-cookie", REDACTED),
        ],
        "filter_query_parameters": [
            ("espn_s2", REDACTED),
            ("SWID", REDACTED),
            ("swid", REDACTED),
        ],
        # Load-bearing. Without it vcrpy records the gzipped bytes ESPN
        # actually sent, and the body scrubber below matches nothing.
        "decode_compressed_response": True,
        "before_record_request": scrub_request,
        "before_record_response": scrub_response,
        # vcrpy's default matcher. U9 adds the ``x-fantasy-filter`` header
        # matcher on top of this; do not treat this list as final.
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "cassette_library_dir": str(CASSETTE_LIBRARY_DIR),
    }


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    """``pytest-recording`` reads this fixture for every ``@pytest.mark.vcr``."""
    return build_vcr_config()


# --------------------------------------------------------------------------- #
# Repo-wide credential scan
# --------------------------------------------------------------------------- #

#: Suffixes that hold recorded fixture data. Markdown is excluded on purpose:
#: the research briefs quote credential *patterns* as evidence.
FIXTURE_SUFFIXES = frozenset({".yaml", ".yml", ".json"})

_SKIP_DIRS = frozenset({".git", ".venv", ".claude", "__pycache__", "node_modules", ".ruff_cache"})


@dataclass(frozen=True)
class CredentialFinding:
    """One credential-shaped match. Never carries the matched text."""

    path: Path
    pattern: str
    location: str

    def __str__(self) -> str:
        return f"{self.path}: matched {self.pattern!r} at {self.location}"


def _git_listed_files(root: Path) -> set[Path] | None:
    """Files git would include in a commit: tracked plus untracked-not-ignored.

    Returns ``None`` when git is unavailable, in which case the caller falls
    back to a filesystem walk.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return {Path(name) for name in completed.stdout.split("\0") if name}


def iter_fixture_paths(root: Path | None = None) -> list[Path]:
    """Every fixture-shaped file in the tree that git would carry in a commit."""
    root = REPO_ROOT if root is None else root
    listed = _git_listed_files(root)
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        is_fixture = path.suffix.lower() in FIXTURE_SUFFIXES or "cassettes" in relative.parts
        if not is_fixture:
            continue
        if listed is None:
            # No git: approximate the ignore rules for the one pattern that
            # matters, so an intentionally unscrubbed recording is not a finding.
            if path.name.endswith(".unscrubbed.yaml"):
                continue
        elif relative not in listed:
            continue
        found.append(path)
    return found


def _iter_binary_scalars(node: Any, trail: str = "") -> Iterator[tuple[str, bytes]]:
    """Yield every ``bytes`` scalar in a parsed YAML document, with its path."""
    if isinstance(node, bytes):
        yield trail or "<root>", node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_binary_scalars(value, f"{trail}.{key}" if trail else str(key))
    elif isinstance(node, list | tuple):
        for index, value in enumerate(node):
            yield from _iter_binary_scalars(value, f"{trail}[{index}]")


def scan_text(text: str, *, path: Path, prefix: str = "line") -> list[CredentialFinding]:
    """Match every credential pattern against ``text``, line by line."""
    findings: list[CredentialFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in CREDENTIAL_PATTERNS.items():
            if pattern.search(line):
                findings.append(CredentialFinding(path, name, f"{prefix} {number}"))
    return findings


def scan_file(path: Path) -> list[CredentialFinding]:
    """Scan one fixture, as text and (for YAML) through its decoded binaries."""
    raw = path.read_bytes()
    findings = scan_text(raw.decode("utf-8", errors="replace"), path=path)

    # A ``!!binary`` scalar is base64 in the file, so the text scan above cannot
    # see inside it. vcrpy writes one whenever a body is not UTF-8 decodable.
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml

            document = yaml.safe_load(raw)
        except Exception:  # noqa: BLE001 - unparseable YAML still got a text scan
            document = None
        for trail, blob in _iter_binary_scalars(document):
            decoded = blob.decode("utf-8", errors="replace")
            for name, pattern in CREDENTIAL_PATTERNS.items():
                if pattern.search(decoded):
                    findings.append(CredentialFinding(path, name, f"binary value at {trail}"))
    return findings


def scan_paths(paths: Iterable[Path]) -> list[CredentialFinding]:
    """Scan many fixtures and return every finding."""
    findings: list[CredentialFinding] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings


def format_findings(findings: Iterable[CredentialFinding]) -> str:
    """Render findings for an assertion message, without echoing any secret."""
    return "\n".join(f"  - {finding}" for finding in findings)


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect ``live`` tests unless they were asked for by name or marker."""
    if config.getoption("-m"):
        return
    skip_live = pytest.mark.skip(reason="live test; run with -m live and real credentials")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
