"""Pure, offline-testable shape-diff and classification logic for the ESPN canary.

Nothing in this module touches the network. ``scripts/canary/run.py`` is the
only thing that does; everything here operates on a JSON payload already in
hand, which is exactly what makes it unit-testable with fixtures instead of
live ESPN (jwulff/fantasy-sports#11's acceptance criteria).

## The three-way classification, and why it exists

``docs/research/03-espn-api-surface.md`` §4.2 documents a live case study:
``espn-api``'s own canary went red for 12 consecutive days
(2026-08-07 → 2026-08-18) from a transitive ``idna`` release breaking
*import*, not from ESPN changing anything. If a canary cannot tell "our own
build broke" from "ESPN's response shape changed," it trains everyone to
ignore it — the exact failure the 2026-08-28 review comment on #11 warns
about.

So a run lands in exactly one of:

* :data:`Classification.BUILD_ERROR` — an exception before any HTTP
  request was even attempted (an import failure, a dependency-resolution
  break). Never touches this module; ``run.py`` builds the report directly,
  because nothing here could have been reached yet.
* :data:`Classification.CANARY_INFRA` — a request was attempted and failed
  before a parseable JSON payload came back (connection error, 401, 404,
  429, or anything else ``fantasy_sports``'s own error taxonomy maps to
  something other than success). Also built directly by ``run.py``, for the
  same reason: there is no payload for this module to inspect yet.
* :data:`Classification.SCHEMA_DRIFT` — a payload came back and this
  module's own assertions found something wrong with it: a required path
  from §4.4 is gone, an enum value has no entry in ``espn-api``'s own maps,
  or a key this project reads has disappeared relative to the committed
  manifest.
* :data:`Classification.OK` — none of the above.

Only :func:`classify` (and the report it returns) is exercised by this
module's own logic; the first two classifications are recorded here as an
enum only so ``run.py`` and the report renderer share one vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "REQUIRED_PATHS",
    "CheckReport",
    "Classification",
    "EnumGap",
    "SignatureDiff",
    "classify",
    "diff_signature",
    "enum_coverage_gaps",
    "extract_signature",
    "missing_required_paths",
    "report_from_dict",
    "resolve_path",
]


class Classification(StrEnum):
    """The vocabulary a canary run's result is reported in. See module docstring."""

    OK = "ok"
    BUILD_ERROR = "build_error"
    CANARY_INFRA = "canary_infra"
    SCHEMA_DRIFT = "schema_drift"


#: docs/research/03-espn-api-surface.md §4.4 — "Required top-level keys
#: present on each view's response... These are the exact paths every
#: constructor in §3.1 does unguarded direct access on -- they are the
#: fields whose absence produces a raw KeyError today." Verified against the
#: real committed recording, tests/cassettes/espn/canary_2018.yaml.
REQUIRED_PATHS: tuple[str, ...] = (
    "status.currentMatchupPeriod",
    "status.finalScoringPeriod",
    "settings.scoringSettings",
    "settings.rosterSettings.lineupSlotCounts",
    "teams[].record.overall.wins",
    "teams[].record.overall.losses",
    "teams[].record.overall.ties",
    "teams[].record.overall.pointsFor",
    "teams[].record.overall.pointsAgainst",
    "teams[].roster.entries[].playerPoolEntry.player.id",
    "teams[].roster.entries[].playerPoolEntry.player.fullName",
    "teams[].roster.entries[].playerPoolEntry.player.defaultPositionId",
    "teams[].roster.entries[].playerPoolEntry.player.eligibleSlots",
)


def resolve_path(payload: Any, path: str) -> list[Any]:
    """Resolve a dotted path with ``[]`` list-fanout segments to the values found.

    ``"teams[].record.overall.wins"`` walks into ``payload["teams"]``, fans
    out across the list, and returns one value per team that has the full
    chain. A segment missing on *any* branch simply drops that branch; the
    path resolves to an empty list only when *no* branch has it, which is
    exactly the "this field is gone" signal callers check for.
    """
    segments = path.split(".")
    current: list[Any] = [payload]
    for segment in segments:
        fanout = segment.endswith("[]")
        key = segment[:-2] if fanout else segment
        next_values: list[Any] = []
        for item in current:
            if not isinstance(item, Mapping) or key not in item:
                continue
            value = item[key]
            if fanout:
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    next_values.extend(value)
            else:
                next_values.append(value)
        current = next_values
        if not current:
            return []
    return current


def missing_required_paths(payload: Mapping[str, Any]) -> list[str]:
    """Every path in :data:`REQUIRED_PATHS` that resolves to nothing in ``payload``."""
    return [path for path in REQUIRED_PATHS if not resolve_path(payload, path)]


@dataclass(frozen=True)
class EnumGap:
    """One id seen in the payload with no entry in ``espn-api``'s own map."""

    field: str
    value: int
    context: str


def enum_coverage_gaps(
    payload: Mapping[str, Any],
    *,
    position_map: Mapping[int, str],
    pro_team_map: Mapping[int, str],
) -> list[EnumGap]:
    """IDs observed in the payload that ``espn-api``'s own maps don't cover.

    docs/research/03-espn-api-surface.md §4.4: "the cheapest, highest-signal
    canary assertion available" — an unmapped id degrades silently
    (``POSITION_MAP.get(x, '')``) rather than raising, so nothing except an
    explicit assertion like this one catches it. It's exactly how #662's new
    hockey position id was found: manually, by a user, after the fact.
    """
    gaps: list[EnumGap] = []
    for player in resolve_path(payload, "teams[].roster.entries[].playerPoolEntry.player"):
        if not isinstance(player, Mapping):
            continue
        name = str(player.get("fullName", "<unknown>"))
        position = player.get("defaultPositionId")
        if isinstance(position, int) and position not in position_map:
            gaps.append(EnumGap("defaultPositionId", position, name))
        for slot in player.get("eligibleSlots") or ():
            if isinstance(slot, int) and slot not in position_map:
                gaps.append(EnumGap("eligibleSlots", slot, name))
        pro_team = player.get("proTeamId")
        if isinstance(pro_team, int) and pro_team not in pro_team_map:
            gaps.append(EnumGap("proTeamId", pro_team, name))
    return gaps


#: The fixed, load-bearing locations `extract_signature` fingerprints.
#: Deliberately shallow and deliberately *not* every field ESPN sends:
#: roster/player sub-objects carry hundreds of stat fields that turn over
#: often and are not load-bearing for this project (docs/testing.md's
#: fixture-honesty rule applies here too — a diff nobody reads is worse than
#: no diff). This is exactly the set of object shapes providers/espn.py's
#: constructors read.
_SIGNATURE_PATHS: tuple[str, ...] = (
    "status",
    "settings",
    "settings.rosterSettings",
    "teams[]",
    "teams[].record.overall",
    "teams[].roster.entries[]",
    "teams[].roster.entries[].playerPoolEntry.player",
)


def extract_signature(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """A shallow, stable structural fingerprint: sorted key sets at :data:`_SIGNATURE_PATHS`."""

    def keys_at(path: str) -> list[str]:
        found = resolve_path(payload, path)
        if not found or not isinstance(found[0], Mapping):
            return []
        return sorted(found[0].keys())

    signature = {"root": sorted(payload.keys()) if isinstance(payload, Mapping) else []}
    signature.update({path: keys_at(path) for path in _SIGNATURE_PATHS})
    return signature


@dataclass(frozen=True)
class SignatureDiff:
    """The result of comparing two :func:`extract_signature` outputs."""

    added: dict[str, list[str]]
    removed: dict[str, list[str]]

    @property
    def has_removals(self) -> bool:
        return any(self.removed.values())

    @property
    def has_additions(self) -> bool:
        return any(self.added.values())

    @property
    def is_empty(self) -> bool:
        return not self.has_removals and not self.has_additions


def diff_signature(
    expected: Mapping[str, list[str]], observed: Mapping[str, list[str]]
) -> SignatureDiff:
    """Compare two signatures produced by :func:`extract_signature`.

    A removed key is the drift signal ADR-0005/§4.3 cares about most — a
    field this project reads has disappeared. An added key is reported too
    (visibility matters, and it is how additive drift like #662 gets
    noticed before it crashes anything) but is never on its own the reason a
    run is classified :data:`Classification.SCHEMA_DRIFT` — ESPN adding a
    field is common and is not, by itself, a break.
    """
    keys = set(expected) | set(observed)
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for key in keys:
        expected_keys = set(expected.get(key, ()))
        observed_keys = set(observed.get(key, ()))
        gained = sorted(observed_keys - expected_keys)
        lost = sorted(expected_keys - observed_keys)
        if gained:
            added[key] = gained
        if lost:
            removed[key] = lost
    return SignatureDiff(added=added, removed=removed)


@dataclass
class CheckReport:
    """One canary run's full findings, independent of how it gets rendered."""

    classification: Classification
    missing_paths: list[str] = field(default_factory=list)
    enum_gaps: list[EnumGap] = field(default_factory=list)
    signature_diff: SignatureDiff | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.classification is Classification.OK

    def render_summary(self) -> str:
        """Markdown, suitable for both stdout and ``$GITHUB_STEP_SUMMARY``."""
        lines = [f"## Canary result: {self.classification.value.upper()}"]
        if self.detail:
            lines.append(self.detail)
        if self.missing_paths:
            lines.append("\n**Missing required paths:**")
            lines.extend(f"- `{path}`" for path in self.missing_paths)
        if self.enum_gaps:
            lines.append("\n**Unmapped enum values:**")
            lines.extend(
                f"- `{gap.field}={gap.value}` (seen on {gap.context})" for gap in self.enum_gaps
            )
        if self.signature_diff and not self.signature_diff.is_empty:
            if self.signature_diff.removed:
                lines.append("\n**Keys that disappeared (breaking):**")
                for path, keys in sorted(self.signature_diff.removed.items()):
                    lines.append(f"- `{path}`: {', '.join(keys)}")
            if self.signature_diff.added:
                lines.append("\n**New keys observed (informational, not a failure):**")
                for path, keys in sorted(self.signature_diff.added.items()):
                    lines.append(f"- `{path}`: {', '.join(keys)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable projection, the inverse of :func:`report_from_dict`.

        jwulff/fantasy-sports#64's two-job workflow needs this: the detection
        job (``contents: read``) writes one of these to ``--report-json`` so
        the publish job (``contents: write``, ``issues: write``) can act on
        the *structured* findings — the fingerprint the dedup search keys on
        (:mod:`scripts.canary.issue_filer`) needs the actual missing paths and
        removed keys, not a re-parse of :meth:`render_summary`'s prose.
        """
        return {
            "classification": self.classification.value,
            "missing_paths": list(self.missing_paths),
            "enum_gaps": [
                {"field": gap.field, "value": gap.value, "context": gap.context}
                for gap in self.enum_gaps
            ],
            "signature_diff": (
                {"added": self.signature_diff.added, "removed": self.signature_diff.removed}
                if self.signature_diff is not None
                else None
            ),
            "detail": self.detail,
        }


def report_from_dict(data: Mapping[str, Any]) -> CheckReport:
    """The inverse of :meth:`CheckReport.to_dict`. See that method for why."""
    signature_diff_raw = data.get("signature_diff")
    signature_diff = (
        SignatureDiff(
            added=dict(signature_diff_raw.get("added", {})),
            removed=dict(signature_diff_raw.get("removed", {})),
        )
        if signature_diff_raw is not None
        else None
    )
    return CheckReport(
        classification=Classification(data["classification"]),
        missing_paths=list(data.get("missing_paths", [])),
        enum_gaps=[
            EnumGap(field=gap["field"], value=gap["value"], context=gap["context"])
            for gap in data.get("enum_gaps", [])
        ],
        signature_diff=signature_diff,
        detail=data.get("detail", ""),
    )


def classify(
    payload: Mapping[str, Any] | None,
    *,
    manifest_signature: Mapping[str, list[str]],
    position_map: Mapping[int, str],
    pro_team_map: Mapping[int, str],
) -> CheckReport:
    """The shape-assertion pass. Only ever called once a real payload is in hand.

    ``run.py`` never calls this for a connection failure or a non-200 —
    those are :data:`Classification.CANARY_INFRA`, built without this
    function, precisely because there is no payload here to inspect.
    """
    if payload is None:
        return CheckReport(
            classification=Classification.CANARY_INFRA, detail="No payload to inspect."
        )

    missing = missing_required_paths(payload)
    gaps = enum_coverage_gaps(payload, position_map=position_map, pro_team_map=pro_team_map)
    observed_signature = extract_signature(payload)
    signature_diff = diff_signature(manifest_signature, observed_signature)

    if missing or gaps or signature_diff.has_removals:
        return CheckReport(
            classification=Classification.SCHEMA_DRIFT,
            missing_paths=missing,
            enum_gaps=gaps,
            signature_diff=signature_diff,
            detail="ESPN's response shape no longer matches what providers/espn.py expects.",
        )

    detail = "All required paths present, all enum values mapped, no keys disappeared."
    if signature_diff.has_additions:
        detail += " New keys were observed (informational; see below)."
    return CheckReport(
        classification=Classification.OK,
        signature_diff=signature_diff if not signature_diff.is_empty else None,
        detail=detail,
    )
