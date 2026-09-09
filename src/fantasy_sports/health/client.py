"""The client-side health check — ADR-0005 §11.3, jwulff/fantasy-sports#10.

Converts an upstream breakage from a stack trace into actionable advice, for a
human and for an agent, by fetching the static manifest the canary publishes
(jwulff/fantasy-sports#11) and folding what it says into the failure already
being reported.

**Fail-open is absolute, not a best effort.** Every rule below exists to make
that literally true rather than merely likely:

1. Never on the happy path, ``AUTH_EXPIRED``, or ``RATE_LIMITED`` —
   :data:`TRIGGER_CODES` is the complete allow-list :func:`evaluate_failure`
   checks before doing anything else. An unclassified exception is not a
   fourth case: :func:`fantasy_sports.output.errors.classify` has already
   turned it into ``PROVIDER_UNAVAILABLE`` by the time this module sees it.
2. A 2-second timeout on the one network call this module makes.
3. A 6-hour cache at ``~/.cache/fantasy-sports/health.json``, so a command
   retried in a loop hits the network once, not on every failure.
4. No telemetry — an unauthenticated ``GET`` of a public static file. Nothing
   about the user, their leagues, or their query is ever in the request.
5. ``FANTASY_SPORTS_NO_HEALTH_CHECK=1`` or ``health_check = false`` in
   ``config.toml`` skips the network call entirely — checked *before* any
   fetch, not filtered out afterward.
6. PEP 440 comparison via ``packaging.version``, never a string compare — a
   string sorts ``"0.1.10" < "0.1.9"``.

And the part no rule above can express: **:func:`evaluate_failure` itself
never raises.** Every step inside it — the fetch, the JSON decode, the
manifest parse, the version comparison, the guidance text — is wrapped, so a
bug in this module degrades to "say nothing extra", exactly like an offline
network or a down GitHub. The health check must never be able to produce an
error of its own (ARCHITECTURE §11.3, rule 1).

Nothing here is imported at module scope by anything on the ``--help`` /
``--version`` path. ``requests`` and ``packaging.version`` are imported inside
the functions that need them, matching the lazy-import rule ADR-0008 holds
every command to.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fantasy_sports.core.errors import ErrorCode, FantasySportsError
from fantasy_sports.health.manifest import HealthManifest, ManifestError, parse_manifest

__all__ = [
    "CACHE_FILENAME",
    "CACHE_TTL_SECONDS",
    "DEFAULT_MANIFEST_URL",
    "NO_CHECK_ENV",
    "REQUEST_TIMEOUT_SECONDS",
    "TRIGGER_CODES",
    "UPGRADE_COMMAND",
    "build_health_block",
    "default_cache_path",
    "evaluate_failure",
    "get_manifest",
    "is_opted_out",
    "render_human_guidance",
    "upgrade_available",
]

DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/jwulff/fantasy-sports/main/health.json"
"""Served from ``raw.githubusercontent.com``: no hosting, CDN-cached, free."""

NO_CHECK_ENV = "FANTASY_SPORTS_NO_HEALTH_CHECK"

REQUEST_TIMEOUT_SECONDS = 2.0

CACHE_TTL_SECONDS = 6 * 60 * 60

CACHE_FILENAME = "health.json"

UPGRADE_COMMAND = "uv tool upgrade fantasy-sports"

TRIGGER_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.SCHEMA_DRIFT, ErrorCode.PROVIDER_UNAVAILABLE}
)
"""The only codes that may ever cause a network call (ARCHITECTURE §11.3).

``AUTH_EXPIRED`` and ``RATE_LIMITED`` are deliberately absent — their cause is
already known and local, so a health check would tell the user nothing they
do not already know. Everything :func:`~fantasy_sports.output.errors.classify`
cannot positively identify becomes ``PROVIDER_UNAVAILABLE`` before this module
ever sees it, which is what makes "unexpected exception" a member of this set
without a third code.
"""


# --------------------------------------------------------------------------- #
# Opt-out
# --------------------------------------------------------------------------- #

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})


def is_opted_out(
    *, environ: Mapping[str, str] | None = None, config_path: Path | None = None
) -> bool:
    """Whether the user has disabled the health check entirely.

    Checked before any network call is made, not used to discard a result
    afterward — an opted-out user's traffic should never reach GitHub in the
    first place, not merely go unreported.
    """
    env = os.environ if environ is None else environ
    if env.get(NO_CHECK_ENV, "").strip().lower() in _TRUE_STRINGS:
        return True
    # `False` means the key was present and explicitly disabled it; `None`
    # means absent, unreadable, or not a bool — every one of those fails open
    # to "not opted out". Only an explicit `health_check = false` counts.
    return _config_health_check_value(config_path) is False


def _config_health_check_value(config_path: Path | None) -> bool | None:
    """Read the top-level ``health_check`` key from ``config.toml``, if present.

    ``config.toml`` is a namespace shared across layers
    (``docs/memory/config-toml-is-a-shared-namespace.md``): this reads only its
    own key and tolerates everything else, including a file that does not
    parse at all — a broken config file must not be the reason a health check
    silently fails to fire when it should.
    """
    try:
        from fantasy_sports.config.paths import config_file

        path = config_path if config_path is not None else config_file()
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad or absent config file is not a failure here
        return None
    value = raw.get("health_check")
    return value if isinstance(value, bool) else None


# --------------------------------------------------------------------------- #
# Fetching and caching
# --------------------------------------------------------------------------- #


def default_cache_path() -> Path:
    from fantasy_sports.config.paths import cache_home

    return cache_home() / CACHE_FILENAME


def _fetch_raw(url: str, *, timeout: float) -> dict[str, Any] | None:
    """One unauthenticated ``GET``. ``None`` on anything but a clean 200.

    Every failure mode collapses to ``None`` on purpose: a timeout, a DNS
    failure, a 404 (the canary has not published yet), a 500, a body that is
    not JSON, a JSON body that is not an object. The caller cannot distinguish
    them and does not need to — "no information" is the only signal a
    fail-open check is allowed to produce.
    """
    try:
        import requests

        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001 - fail-open is absolute (rule 1)
        return None
    return data if isinstance(data, dict) else None


def _load_cache(path: Path) -> tuple[float, dict[str, Any]] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    fetched_at = document.get("fetched_at")
    manifest = document.get("manifest")
    if not isinstance(fetched_at, int | float) or not isinstance(manifest, dict):
        return None
    return float(fetched_at), manifest


def _save_cache(path: Path, raw: dict[str, Any], *, fetched_at: float) -> None:
    """Best-effort write. A cache that cannot be written just costs a refetch.

    Catches ``Exception`` broadly, not just ``OSError``: a permission error or
    a full disk is the expected failure, but "best effort" means exactly
    that — this function must never be the reason a caller two frames up
    (``doctor``, which does not wrap this call in its own try/except) sees an
    exception it did not ask for.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".health-", suffix=".tmp")
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump({"fetched_at": fetched_at, "manifest": raw}, stream)
            temp.replace(path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    except Exception:  # noqa: BLE001 - a cache write must never be able to fail loudly
        pass


def get_manifest(
    *,
    force: bool = False,
    url: str = DEFAULT_MANIFEST_URL,
    cache_path: Path | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    now: float | None = None,
) -> HealthManifest | None:
    """Fetch (or read a cached copy of) the health manifest.

    ``None`` means "no information available" — offline, GitHub down, the
    canary has not published yet, or a malformed response. It is never raised
    as an error; there is nothing a caller can do differently in any of those
    cases, which is the whole point of fail-open.

    Does **not** check :func:`is_opted_out` itself, so a caller that has
    already decided to check controls that policy in one place — see
    :func:`evaluate_failure` for the on-error path and
    ``fantasy_sports.commands.doctor`` for the forced one.
    """
    moment = time.time() if now is None else now
    path = default_cache_path() if cache_path is None else cache_path

    if not force:
        cached = _load_cache(path)
        if cached is not None:
            fetched_at, raw = cached
            if moment - fetched_at < CACHE_TTL_SECONDS:
                return _safe_parse(raw)

    raw = _fetch_raw(url, timeout=timeout)
    if raw is None:
        return None
    _save_cache(path, raw, fetched_at=moment)
    return _safe_parse(raw)


def _safe_parse(raw: dict[str, Any]) -> HealthManifest | None:
    try:
        return parse_manifest(raw)
    except ManifestError:
        return None


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #


def upgrade_available(current: str, latest: str | None) -> bool:
    """Whether ``latest`` is a newer release than ``current``, by PEP 440.

    Never a string compare — ``"0.1.10" < "0.1.9"`` lexicographically, which
    would tell a current user to "upgrade" to an older release. An
    unparseable version on either side answers ``False`` rather than
    guessing: claiming an upgrade is available on bad data is worse than
    staying quiet.
    """
    if not latest:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        return Version(current) < Version(latest)
    except InvalidVersion:
        return False


# --------------------------------------------------------------------------- #
# Building the payload
# --------------------------------------------------------------------------- #


def build_health_block(
    manifest: HealthManifest,
    *,
    current_version: str,
    provider: str | None,
    error_code: ErrorCode | str | None = None,
) -> dict[str, Any]:
    """The ``health`` object folded into an error envelope (ARCHITECTURE §11.3).

    ``known_issue`` is the first issue the named provider reports for
    ``error_code``, or ``None`` — there is deliberately no ranking beyond
    "first in the manifest's own list", which is the canary's to order.
    """
    later = manifest.latest_version
    upgraded = upgrade_available(current_version, later)
    provider_health = manifest.provider(provider)
    known_issue = None
    if provider_health is not None and error_code is not None:
        code = error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
        matches = provider_health.issues_for(code)
        known_issue = matches[0].to_health_payload() if matches else None

    return {
        "your_version": current_version,
        "latest_version": later,
        "upgrade_available": upgraded,
        "upgrade_command": UPGRADE_COMMAND if upgraded else None,
        "provider_status": provider_health.status if provider_health else None,
        "known_issue": known_issue,
    }


def render_human_guidance(
    block: Mapping[str, Any],
    *,
    provider: str | None,
) -> str | None:
    """Prose for a human at a terminal — appended to stderr, after the JSON error.

    ``None`` when there is nothing useful to add over the JSON already
    written, which keeps a script that only checks the exit code and a human
    reading the same run both getting exactly what they need.
    """
    lines: list[str] = []
    if block["upgrade_available"]:
        lines.append(
            f"  You are on {block['your_version']} — {block['latest_version']} is available."
        )
        issue = block.get("known_issue")
        if issue:
            lines.append(f"  This looks like a known issue, fixed in {issue['fixed_in']}:")
            lines.append(f"    #{issue['issue']}  {issue['summary']}")
        lines.append("")
        lines.append(f"  Fix:  {block['upgrade_command']}")
        return "\n".join(lines)

    status = block.get("provider_status")
    issue = block.get("known_issue")
    if status and status != "healthy":
        lines.append(f"  You are on the latest version ({block['your_version']}).")
        label = provider or "provider"
        if issue:
            lines.append(
                f"  {label} status: {status} — this is a known outage, "
                f"tracked in #{issue['issue']}."
            )
            lines.append(f"  Nothing to do but wait. Details: {issue['url']}")
        else:
            lines.append(f"  {label} status: {status}.")
        return "\n".join(lines)

    return None


def evaluate_failure(
    error: FantasySportsError,
    *,
    current_version: str,
    provider: str | None,
    url: str = DEFAULT_MANIFEST_URL,
    cache_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    now: float | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The whole on-error check: gate, fetch, build. Never raises.

    Returns ``(health_block, human_guidance)``. Both are ``None`` when the
    check does not apply (wrong code, opted out) or produced nothing usable
    (offline, malformed manifest) — the two are indistinguishable to a caller
    on purpose, because "say nothing extra" is the same action either way.
    """
    try:
        if error.code not in TRIGGER_CODES:
            return None, None
        if is_opted_out(environ=environ):
            return None, None
        manifest = get_manifest(url=url, cache_path=cache_path, timeout=timeout, now=now)
        if manifest is None:
            return None, None
        block = build_health_block(
            manifest, current_version=current_version, provider=provider, error_code=error.code
        )
        guidance = render_human_guidance(block, provider=provider)
        return block, guidance
    except Exception:  # noqa: BLE001 - the health check must never raise (rule 1)
        return None, None
