"""The cassette scrub-before-write hook, and the repo-wide credential scan.

Every credential-shaped string in this file is synthetic. The GUIDs are
well-formed but arbitrary; the ``espn_s2`` blobs spell out that they are fake.
A real ESPN credential must never appear in this repository, including in a
file somebody intends to delete.

The tests that matter most read the cassette back **off disk**. A scrubber that
is only asserted through its own return value proves nothing about the bytes
that were persisted, and the bytes are what leaks.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    REDACTED,
    REPO_ROOT,
    SWID_PLACEHOLDER,
    CredentialFinding,
    UnscrubbableResponseError,
    build_vcr_config,
    format_findings,
    iter_fixture_paths,
    pytest_collection_modifyitems,
    scan_file,
    scan_paths,
    scan_text,
    scrub_request,
    scrub_response,
    scrub_text,
)

# --- synthetic credentials, shaped like the real thing --------------------- #

FAKE_SWID = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
FAKE_SWID_BARE = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
FAKE_ESPN_S2 = "AEBnotarealcookie0123456789abcdefABCDEF%2Bnotareal%3D%3D"
FAKE_COOKIE = f"espn_s2={FAKE_ESPN_S2}; SWID={FAKE_SWID}"
ESPN_HOST = "https://lm-api-reads.fantasy.espn.com"


def _request(uri: str = f"{ESPN_HOST}/apis/v3/games/ffl/seasons/2026", **kwargs: Any) -> Any:
    from vcr.request import Request

    headers = kwargs.pop("headers", {"Cookie": FAKE_COOKIE, "Accept": "application/json"})
    return Request(method="GET", uri=uri, body=kwargs.pop("body", None), headers=headers)


def _response(body: bytes | str = b'{"teams": []}', **kwargs: Any) -> dict[str, Any]:
    headers = kwargs.pop("headers", {"Content-Type": ["application/json"]})
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": headers,
        "body": {"string": body},
    }


def _record(
    path: Path,
    interactions: list[tuple[Any, dict[str, Any]]],
    record_mode: str = "all",
) -> str:
    """Drive a real vcrpy cassette through the configured hooks and save it.

    Nothing here touches the network: interactions are appended directly, which
    is the same code path (``Cassette.append``) a live recording takes.
    """
    import vcr

    config = build_vcr_config()
    config["cassette_library_dir"] = str(path.parent)
    with vcr.VCR(**config).use_cassette(path.name, record_mode=record_mode) as cassette:
        for request, response in interactions:
            cassette.append(request, response)
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The scan. Written first: it is the audit that the control was in force.
# --------------------------------------------------------------------------- #


def test_the_scan_actually_inspects_committed_files() -> None:
    """A scan over zero files would pass vacuously and prove nothing."""
    paths = iter_fixture_paths(REPO_ROOT)
    assert paths, f"no fixture-shaped files found under {REPO_ROOT}; the scan is vacuous"


def test_no_committed_fixture_contains_a_credential() -> None:
    findings = scan_paths(iter_fixture_paths(REPO_ROOT))
    assert not findings, "credential pattern in committed fixtures:\n" + format_findings(findings)


@pytest.mark.parametrize(
    ("poison", "pattern"),
    [
        (f'      string: \'{{"owner": "{FAKE_SWID}"}}\'', "SWID GUID"),
        (f'      string: \'{{"swid": "{FAKE_SWID_BARE}"}}\'', "GUID under a swid key"),
        (f"      Cookie: [espn_s2={FAKE_ESPN_S2}]", "espn_s2 value"),
    ],
)
def test_scan_fails_loudly_on_a_poisoned_fixture(tmp_path: Path, poison: str, pattern: str) -> None:
    """The counterpart to the clean-tree run: a scan that has never failed is
    not evidence that it can fail."""
    poisoned = tmp_path / "poisoned.yaml"
    poisoned.write_text(f"interactions:\n  - response:\n    body:\n{poison}\n", encoding="utf-8")

    findings = scan_paths([poisoned])

    assert findings, f"scan missed a {pattern} in {poisoned}"
    assert any(finding.pattern == pattern for finding in findings)
    assert all(finding.path == poisoned for finding in findings)


def test_scan_sees_inside_a_base64_encoded_binary_body(tmp_path: Path) -> None:
    """vcrpy writes a non-UTF-8 body as a ``!!binary`` scalar, which base64
    hides from a grep-style scan. The structural pass must still catch it."""
    payload = f'{{"owner": "{FAKE_SWID}"}}'.encode()
    poisoned = tmp_path / "binary.yaml"
    poisoned.write_bytes(
        yaml.dump({"interactions": [{"response": {"body": {"string": payload}}}]}).encode()
    )

    assert "!!binary" in poisoned.read_text(encoding="utf-8")
    assert not scan_text(poisoned.read_text(encoding="utf-8"), path=poisoned), (
        "the base64 body should be invisible to the plain text scan"
    )

    findings = scan_file(poisoned)
    assert findings, "structural scan missed a credential hidden in a !!binary body"
    assert all("binary value" in finding.location for finding in findings)


def test_scan_findings_never_echo_the_credential() -> None:
    """Assertion output lands in CI logs; it must not carry the secret."""
    finding = CredentialFinding(Path("tests/cassettes/x.yaml"), "SWID GUID", "line 12")
    rendered = f"{finding}\n{format_findings([finding])}"
    assert FAKE_SWID not in rendered
    assert FAKE_ESPN_S2 not in rendered
    assert "SWID GUID" in rendered
    assert "line 12" in rendered


def test_scan_covers_anything_under_a_cassettes_directory(tmp_path: Path) -> None:
    """Cassettes are in scope by location, not only by suffix, so a recording
    saved under an unexpected extension is still audited."""
    cassettes = tmp_path / "tests" / "cassettes" / "espn"
    cassettes.mkdir(parents=True)
    (cassettes / "standings.cassette").write_text(f"cookie: {FAKE_COOKIE}\n", encoding="utf-8")

    findings = scan_paths(iter_fixture_paths(tmp_path))

    assert [finding.pattern for finding in findings] == ["SWID GUID", "espn_s2 value"]


def test_scan_ignores_deliberately_unscrubbed_recordings(tmp_path: Path) -> None:
    """``tests/cassettes/**/*.unscrubbed.yaml`` is gitignored by design, so it
    is not a committed fixture and must not fail the scan."""
    (tmp_path / "roster.unscrubbed.yaml").write_text(f"cookie: {FAKE_COOKIE}\n", encoding="utf-8")
    (tmp_path / "roster.yaml").write_text("cookie: REDACTED\n", encoding="utf-8")

    names = {path.name for path in iter_fixture_paths(tmp_path)}

    assert names == {"roster.yaml"}


# --------------------------------------------------------------------------- #
# Scrub before write
# --------------------------------------------------------------------------- #


def test_cookie_header_is_written_to_disk_with_its_value_replaced(tmp_path: Path) -> None:
    """Replaced, not dropped. A cassette missing ``Cookie`` entirely is
    indistinguishable from one recorded without credentials, so a future reader
    cannot tell whether the scrub ever ran."""
    written = _record(tmp_path / "cookie.yaml", [(_request(), _response())])

    assert FAKE_ESPN_S2 not in written
    assert FAKE_SWID not in written
    # vcrpy's replacement filter rewrites the header under the name it was
    # registered with, so the key comes back lowercased.
    assert "cookie" in written.lower(), "the header must survive as evidence the scrub ran"
    assert REDACTED in written

    recorded = yaml.safe_load(written)["interactions"][0]["request"]["headers"]
    cookies = {key.lower(): value for key, value in recorded.items()}["cookie"]
    assert cookies == [REDACTED]


def test_authorization_header_is_written_to_disk_redacted(tmp_path: Path) -> None:
    request = _request(headers={"Authorization": f"Bearer {FAKE_ESPN_S2}"})
    written = _record(tmp_path / "auth.yaml", [(request, _response())])

    assert FAKE_ESPN_S2 not in written
    assert REDACTED in written


def test_espn_s2_and_swid_query_parameters_are_scrubbed_from_the_uri(tmp_path: Path) -> None:
    uri = f"{ESPN_HOST}/apis/v3/games/ffl?espn_s2={FAKE_ESPN_S2}&SWID={FAKE_SWID}&view=mTeam"
    written = _record(tmp_path / "query.yaml", [(_request(uri, headers={}), _response())])

    assert FAKE_ESPN_S2 not in written
    assert FAKE_SWID not in written
    recorded_uri = yaml.safe_load(written)["interactions"][0]["request"]["uri"]
    assert f"espn_s2={REDACTED}" in recorded_uri
    assert f"SWID={REDACTED}" in recorded_uri
    assert "view=mTeam" in recorded_uri, "non-credential parameters must survive"


def test_swid_guid_echoed_in_a_response_body_is_redacted(tmp_path: Path) -> None:
    """Header filtering does not catch this: ESPN returns owner SWIDs inline in
    roster payloads, in the response body."""
    body = f'{{"teams": [{{"id": 3, "owners": ["{FAKE_SWID}"]}}]}}'.encode()
    written = _record(tmp_path / "roster.yaml", [(_request(headers={}), _response(body))])

    assert FAKE_SWID not in written
    assert SWID_PLACEHOLDER in written
    assert '"id": 3' in written, "the rest of the payload must be preserved"


def test_gzipped_response_body_is_decoded_before_it_is_scrubbed(tmp_path: Path) -> None:
    """ESPN serves gzip. Without ``decode_compressed_response`` the recorded
    body is opaque bytes, the body scrubber matches nothing, and the SWID is
    persisted base64-encoded where no text scan can see it."""
    plain = f'{{"owners": ["{FAKE_SWID}"]}}'.encode()
    response = _response(
        gzip.compress(plain),
        headers={"Content-Type": ["application/json"], "Content-Encoding": ["gzip"]},
    )
    path = tmp_path / "gzip.yaml"
    written = _record(path, [(_request(headers={}), response)])

    assert FAKE_SWID not in written
    assert SWID_PLACEHOLDER in written
    assert not scan_file(path)


def test_rerecording_an_existing_fixture_reapplies_scrubbing(tmp_path: Path) -> None:
    """The prior pass is never trusted. A fixture that arrived on disk with a
    credential in it -- recorded before this hook existed, or by a contributor
    who bypassed it -- is re-scrubbed when it is re-recorded."""
    path = tmp_path / "stale.yaml"
    path.write_text(
        yaml.dump(
            {
                "version": 1,
                "interactions": [
                    {
                        "request": {
                            "method": "GET",
                            "uri": f"{ESPN_HOST}/stale?SWID={FAKE_SWID}",
                            "body": None,
                            "headers": {"Cookie": [FAKE_COOKIE]},
                        },
                        "response": {
                            "status": {"code": 200, "message": "OK"},
                            "headers": {"Content-Type": ["application/json"]},
                            "body": {"string": f'{{"owner": "{FAKE_SWID}"}}'},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert scan_file(path), "the fixture under test must start out poisoned"

    fresh_body = f'{{"owner": "{FAKE_SWID}"}}'.encode()
    written = _record(path, [(_request(), _response(fresh_body))], record_mode="all")

    assert FAKE_SWID not in written, "the stale interaction was trusted instead of re-scrubbed"
    assert FAKE_ESPN_S2 not in written
    assert not scan_file(path)
    assert SWID_PLACEHOLDER in written


def test_a_recorded_cassette_passes_the_scan(tmp_path: Path) -> None:
    """The hook and the audit agree: what the hook writes, the scan accepts."""
    path = tmp_path / "clean.yaml"
    body = f'{{"owners": ["{FAKE_SWID}"], "swid": "{FAKE_SWID_BARE}"}}'.encode()
    _record(path, [(_request(f"{ESPN_HOST}/x?espn_s2={FAKE_ESPN_S2}"), _response(body))])

    assert not scan_file(path)


# --- unit-level behaviour of the hooks ------------------------------------- #


def test_scrub_text_redacts_every_credential_shape() -> None:
    scrubbed = scrub_text(f"espn_s2={FAKE_ESPN_S2}; swid: {FAKE_SWID_BARE}; owner={FAKE_SWID}")
    assert FAKE_ESPN_S2 not in scrubbed
    assert FAKE_SWID_BARE not in scrubbed
    assert SWID_PLACEHOLDER in scrubbed


def test_scrub_text_leaves_an_already_scrubbed_value_alone() -> None:
    already = f"espn_s2={REDACTED}; SWID={SWID_PLACEHOLDER}"
    assert scrub_text(already) == already


def test_scrub_text_does_not_flag_a_ci_secret_expression() -> None:
    """``ESPN_S2: ${{ secrets.ESPN_S2 }}`` in a workflow is a reference, not a
    credential; treating it as one would make the scan cry wolf on every PR."""
    expression = "ESPN_S2: ${{ secrets.ESPN_S2 }}"
    assert scrub_text(expression) == expression
    assert not scan_text(expression, path=Path("ci.yml"))


def test_a_credential_in_an_unlisted_header_is_still_scrubbed() -> None:
    request = _request(headers={"X-Fantasy-Filter": f'{{"owner": "{FAKE_SWID}"}}'})
    scrubbed = scrub_request(request)
    assert FAKE_SWID not in scrubbed.headers["X-Fantasy-Filter"]


def test_a_response_body_that_cannot_be_read_is_refused_rather_than_recorded() -> None:
    """Opaque bytes cannot be proven clean, and a regex over them silently
    finds nothing. Refusing is the only honest outcome."""
    with pytest.raises(UnscrubbableResponseError, match="cannot be scanned"):
        scrub_response(_response(b"\xff\xfe\x00 not utf-8"))


def test_scrub_response_tolerates_a_response_without_a_body() -> None:
    scrubbed = scrub_response({"status": {"code": 204}, "headers": {"Set-Cookie": [FAKE_COOKIE]}})
    assert scrubbed["headers"]["Set-Cookie"] == [REDACTED]


def test_scrub_hooks_pass_none_through() -> None:
    assert scrub_request(None) is None
    assert scrub_response(None) is None


def test_vcr_config_replaces_rather_than_deletes_filtered_headers() -> None:
    config = build_vcr_config()
    assert all(isinstance(entry, tuple) for entry in config["filter_headers"])
    assert config["decode_compressed_response"] is True
    assert config["before_record_request"] is scrub_request
    assert config["before_record_response"] is scrub_response


# --------------------------------------------------------------------------- #
# The live marker keeps behaving as it did
# --------------------------------------------------------------------------- #


class _StubConfig:
    def __init__(self, marker_expression: str) -> None:
        self._marker_expression = marker_expression

    def getoption(self, name: str) -> str:
        assert name == "-m"
        return self._marker_expression


class _StubItem:
    def __init__(self, *keywords: str) -> None:
        self.keywords = set(keywords)
        self.added_markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.added_markers.append(marker)


def test_live_tests_are_skipped_in_a_default_run() -> None:
    live, offline = _StubItem("live"), _StubItem("unit")
    pytest_collection_modifyitems(_StubConfig(""), [live, offline])  # type: ignore[arg-type]

    assert [marker.name for marker in live.added_markers] == ["skip"]
    assert offline.added_markers == []


def test_an_explicit_marker_selection_leaves_live_tests_alone() -> None:
    live = _StubItem("live")
    pytest_collection_modifyitems(_StubConfig("live"), [live])  # type: ignore[arg-type]

    assert live.added_markers == []
