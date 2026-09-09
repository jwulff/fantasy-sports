"""The shape of ``health.json`` — ADR-0005, ``docs/ARCHITECTURE.md`` §11.2.

The canary (jwulff/fantasy-sports#11, not yet built) publishes this file; this
module only reads it. Because a client on an old release may parse a manifest
written by a newer canary, and because the file lives outside this repo's own
release cadence, parsing here is **defensive by construction**: an unrecognized
or missing field is dropped rather than raising, and only a document that is
not even a JSON object fails to parse at all. A version of this tool that
cannot understand next year's manifest schema should degrade to "no
information", never crash on someone else's file.

Nothing here does I/O. :mod:`fantasy_sports.health.client` fetches the bytes;
this module only turns an already-decoded ``dict`` into typed data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HealthManifest",
    "KnownIssue",
    "ManifestError",
    "ProviderHealth",
    "parse_manifest",
]


class ManifestError(ValueError):
    """``raw`` is not even shaped like a manifest (not a JSON object).

    Every other malformed field is tolerated by :func:`parse_manifest`; this is
    the one thing that is not recoverable enough to guess at.
    """


@dataclass(frozen=True)
class KnownIssue:
    """One entry in a provider's ``known_issues`` list."""

    code: str | None = None
    endpoint: str | None = None
    affects: str | None = None
    fixed_in: str | None = None
    issue: int | None = None
    url: str | None = None
    summary: str | None = None

    def to_health_payload(self) -> dict[str, Any]:
        """The subset folded into an error envelope's ``health.known_issue``.

        ARCHITECTURE §11.3's example carries exactly these four keys — an agent
        acting on ``upgrade_available`` needs the issue number and the fixed
        release, not the internal ``code``/``endpoint``/``affects`` matching
        criteria that got it selected.
        """
        return {
            "issue": self.issue,
            "url": self.url,
            "fixed_in": self.fixed_in,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ProviderHealth:
    """One provider's entry under ``providers`` in the manifest."""

    name: str
    status: str | None = None
    checked_at: str | None = None
    known_issues: tuple[KnownIssue, ...] = ()

    def issues_for(self, code: str) -> tuple[KnownIssue, ...]:
        """Known issues whose ``code`` matches the taxonomy code that fired.

        A blank ``code`` on the manifest entry is never a match — an
        unclassified known issue should not be silently attached to every
        error this provider can raise.
        """
        return tuple(issue for issue in self.known_issues if issue.code == code)


@dataclass(frozen=True)
class HealthManifest:
    """The parsed ``health.json`` document (schema ``fantasy-sports-health/v1``)."""

    schema: str | None = None
    latest_version: str | None = None
    min_supported_version: str | None = None
    yanked_versions: tuple[str, ...] = ()
    providers: Mapping[str, ProviderHealth] = field(default_factory=dict)
    updated_at: str | None = None

    def provider(self, name: str | None) -> ProviderHealth | None:
        return None if name is None else self.providers.get(name)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    # `bool` is an `int` in Python (docs/memory/config-toml-is-a-shared-namespace.md
    # §3 covers the same trap for TOML); a manifest is JSON, but the same
    # defensive guard costs nothing here.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _known_issue(raw: Any) -> KnownIssue | None:
    if not isinstance(raw, Mapping):
        return None
    return KnownIssue(
        code=_text(raw.get("code")),
        endpoint=_text(raw.get("endpoint")),
        affects=_text(raw.get("affects")),
        fixed_in=_text(raw.get("fixed_in")),
        issue=_int(raw.get("issue")),
        url=_text(raw.get("url")),
        summary=_text(raw.get("summary")),
    )


def _provider_health(name: Any, raw: Any) -> ProviderHealth | None:
    if not isinstance(name, str) or not name or not isinstance(raw, Mapping):
        return None
    issues_raw = raw.get("known_issues")
    issues = (
        tuple(issue for issue in (_known_issue(item) for item in issues_raw) if issue is not None)
        if isinstance(issues_raw, Sequence) and not isinstance(issues_raw, str | bytes)
        else ()
    )
    return ProviderHealth(
        name=name,
        status=_text(raw.get("status")),
        checked_at=_text(raw.get("checked_at")),
        known_issues=issues,
    )


def parse_manifest(raw: Any) -> HealthManifest:
    """Turn a decoded JSON document into a :class:`HealthManifest`.

    Every field is optional and every malformed one is dropped silently,
    except the document itself: something that is not a JSON object cannot be
    a manifest at all, and that is the one condition worth naming.

    :raises ManifestError: if ``raw`` is not a mapping.
    """
    if not isinstance(raw, Mapping):
        raise ManifestError(f"health manifest must be a JSON object, got {type(raw).__name__}")

    providers_raw = raw.get("providers")
    providers: dict[str, ProviderHealth] = {}
    if isinstance(providers_raw, Mapping):
        for name, entry in providers_raw.items():
            parsed = _provider_health(name, entry)
            if parsed is not None:
                providers[parsed.name] = parsed

    return HealthManifest(
        schema=_text(raw.get("schema")),
        latest_version=_text(raw.get("latest_version")),
        min_supported_version=_text(raw.get("min_supported_version")),
        yanked_versions=_string_tuple(raw.get("yanked_versions")),
        providers=providers,
        updated_at=_text(raw.get("updated_at")),
    )
