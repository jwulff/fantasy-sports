"""The cassette harness itself: matching, misses, offline-ness, and the corpus.

``test_scrubbing.py`` covers the scrub-before-write hook and the credential
scan. This file covers everything around them — the parts of U9 (#12) that
decide *which* recorded response a test gets, and what happens when there is
none.

Three properties are asserted here that nothing else can assert:

1. **Two recordings that differ only in ``x-fantasy-filter`` replay their own
   responses.** With a control that shows vcrpy's default matcher collides
   them, so the test cannot quietly stop measuring anything.
2. **A cassette miss raises.** It never falls through to a live call, and the
   error is vcrpy's, raised before a socket is opened rather than by
   ``pytest-socket`` afterwards.
3. **The corpus does not cover the compressed-body path**, which is why that
   path has direct unit tests rather than a fixture. Same class of guard as
   ``test_the_scan_actually_inspects_committed_files``: an assertion about what
   the evidence does *not* show.
"""

from __future__ import annotations

import gzip
import socket
import zlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    CASSETTE_LIBRARY_DIR,
    FILTER_HEADER,
    FILTER_MATCHER,
    REPO_ROOT,
    UnscrubbableResponseError,
    build_vcr,
    build_vcr_config,
    canonical_filter,
    filter_of,
    iter_fixture_paths,
    match_fantasy_filter,
    scrub_response,
)

ESPN_HOST = "https://lm-api-reads.fantasy.espn.com"
FREE_AGENTS_URL = f"{ESPN_HOST}/apis/v3/games/ffl/seasons/2026/segments/0/leagues/99"

#: Two real ``kona_player_info`` filters, differing only in ``filterSlotIds``.
WIDE_RECEIVERS = '{"players": {"filterSlotIds": {"value": [4]}, "limit": 50}}'
TIGHT_ENDS = '{"players": {"filterSlotIds": {"value": [6]}, "limit": 50}}'


def _request(uri: str = FREE_AGENTS_URL, fantasy_filter: str | None = None) -> Any:
    from vcr.request import Request

    headers = {"Accept": "application/json"}
    if fantasy_filter is not None:
        headers[FILTER_HEADER] = fantasy_filter
    return Request(method="GET", uri=uri, body=None, headers=headers)


def _response(body: bytes, headers: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": headers or {"Content-Type": ["application/json"]},
        "body": {"string": body},
    }


def _two_filtered_recordings(path: Path) -> None:
    """One cassette, one URL, two filters, two different bodies."""
    recorder = build_vcr(cassette_library_dir=str(path.parent))
    with recorder.use_cassette(path.name, record_mode="all") as cassette:
        cassette.append(_request(fantasy_filter=WIDE_RECEIVERS), _response(b'{"slot": "WR"}'))
        cassette.append(_request(fantasy_filter=TIGHT_ENDS), _response(b'{"slot": "TE"}'))


def _replay(path: Path, fantasy_filter: str | None, match_on: list[str] | None = None) -> Any:
    recorder = build_vcr(cassette_library_dir=str(path.parent))
    overrides = {"match_on": match_on} if match_on is not None else {}
    with recorder.use_cassette(
        path.name, record_mode="none", allow_playback_repeats=True, **overrides
    ) as cassette:
        return cassette.play_response(_request(fantasy_filter=fantasy_filter))


# --------------------------------------------------------------------------- #
# 1. The x-fantasy-filter matcher
# --------------------------------------------------------------------------- #


def test_two_recordings_differing_only_in_the_filter_replay_their_own_response(
    tmp_path: Path,
) -> None:
    """The scenario U9 exists for.

    ESPN scopes free agents, transactions and the activity feed by this header
    rather than by the URL, so two such reads are the same method, scheme, host,
    port, path and query — every dimension vcrpy matches on by default.
    """
    path = tmp_path / "filters.yaml"
    _two_filtered_recordings(path)

    assert _replay(path, WIDE_RECEIVERS)["body"]["string"] == b'{"slot": "WR"}'
    assert _replay(path, TIGHT_ENDS)["body"]["string"] == b'{"slot": "TE"}'


def test_the_default_matcher_collides_them(tmp_path: Path) -> None:
    """The control. Without this the test above could pass for another reason.

    vcrpy's default ``match_on`` ignores headers entirely, so the tight-end
    read is served the wide-receiver body — no error, no warning, and a
    filter-gated assertion that passes against the wrong payload.
    """
    path = tmp_path / "filters.yaml"
    _two_filtered_recordings(path)
    default = ["method", "scheme", "host", "port", "path", "query"]

    assert _replay(path, WIDE_RECEIVERS, match_on=default)["body"]["string"] == b'{"slot": "WR"}'
    assert _replay(path, TIGHT_ENDS, match_on=default)["body"]["string"] == b'{"slot": "WR"}'


def test_the_configured_match_on_includes_the_filter_matcher() -> None:
    """A future edit that drops it should fail here, not silently pass tests."""
    assert build_vcr_config()["match_on"][-1] == FILTER_MATCHER


def test_a_bare_vcr_refuses_the_configuration_rather_than_matching_on_less() -> None:
    """``match_on`` entries are resolved by *name* against ``VCR.matchers``.

    Constructing ``vcr.VCR(**build_vcr_config())`` directly therefore raises
    instead of quietly falling back to the default matcher. :func:`build_vcr`
    is the only supported entry point, and this is what enforces it.
    """
    import vcr

    with pytest.raises(KeyError, match=FILTER_MATCHER):
        vcr.VCR(**build_vcr_config()).use_cassette("nope.yaml").__enter__()


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ('{"a": 1, "b": 2}', '{"b": 2, "a": 1}'),
        ('{"value": ["A", "B"]}', '{"value": ["B", "A"]}'),
        ('{"v": {"x": [2, 1]}}', '{"v": {"x": [1, 2]}}'),
        ('{"a":1}', '{"a": 1}'),
    ],
)
def test_the_matcher_is_order_and_whitespace_independent(left: str, right: str) -> None:
    """Not a nicety — the header is not stable across processes.

    ``espn-api`` builds the transactions filter as
    ``{"filterType": {"value": list(types)}}`` over a Python *set*, and ``str``
    hashing is randomised per interpreter. A literal string comparison would
    make the committed ``mTransactions2`` interactions replay or miss depending
    on ``PYTHONHASHSEED``, which is a worse bug than the one the matcher fixes.
    """
    match_fantasy_filter(_request(fantasy_filter=left), _request(fantasy_filter=right))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (WIDE_RECEIVERS, TIGHT_ENDS),
        ('{"value": [4]}', '{"value": [4, 6]}'),
        (None, WIDE_RECEIVERS),
        (WIDE_RECEIVERS, None),
        ("not json at all", "also not json"),
    ],
)
def test_the_matcher_rejects_genuinely_different_filters(
    left: str | None, right: str | None
) -> None:
    with pytest.raises(AssertionError, match=FILTER_HEADER):
        match_fantasy_filter(_request(fantasy_filter=left), _request(fantasy_filter=right))


def test_a_matcher_failure_does_not_echo_the_filter() -> None:
    """A filter is league data, and this message lands in a CI log."""
    with pytest.raises(AssertionError) as err:
        match_fantasy_filter(_request(fantasy_filter=WIDE_RECEIVERS), _request())
    assert "filterSlotIds" not in str(err.value)


def test_an_unparseable_filter_falls_back_to_a_literal_comparison() -> None:
    """Unparseable is not a licence to treat two different headers as one."""
    assert canonical_filter("<html>nope</html>") == "<html>nope</html>"
    match_fantasy_filter(_request(fantasy_filter="same"), _request(fantasy_filter="same"))


def test_the_filter_is_read_case_insensitively_and_through_a_yaml_list() -> None:
    """A cassette stores headers as ``{name: [value]}``; a live request does not."""
    from vcr.request import Request

    live = Request("GET", FREE_AGENTS_URL, None, {"X-Fantasy-Filter": WIDE_RECEIVERS})
    stored = Request("GET", FREE_AGENTS_URL, None, {FILTER_HEADER: [WIDE_RECEIVERS]})

    assert filter_of(live) == WIDE_RECEIVERS
    match_fantasy_filter(live, stored)


def test_filter_of_tolerates_a_request_without_headers() -> None:
    assert filter_of(object()) is None
    assert canonical_filter(None) is None
    assert canonical_filter([]) is None


# --------------------------------------------------------------------------- #
# 2. A miss raises; it never becomes a live call
# --------------------------------------------------------------------------- #


def test_a_cassette_miss_raises_rather_than_calling_espn(tmp_path: Path) -> None:
    """The rule ``CLAUDE.md`` states as "a cassette miss must fail loudly".

    ``requests`` is driven for real here — this is the whole stack, not
    ``play_response`` in isolation — and the error must be vcrpy refusing to
    record over a write-protected cassette, *not* ``pytest-socket`` catching
    the call after the fact. Both fail the test run; only one of them fails
    before anything leaves the process.
    """
    import requests
    from vcr.errors import CannotOverwriteExistingCassetteException

    path = tmp_path / "miss.yaml"
    _two_filtered_recordings(path)
    recorder = build_vcr(cassette_library_dir=str(path.parent))

    with (
        recorder.use_cassette(path.name, record_mode="none"),
        pytest.raises(CannotOverwriteExistingCassetteException),
    ):
        requests.get(f"{ESPN_HOST}/apis/v3/games/ffl/seasons/2026/nothing-recorded", timeout=1)


def test_a_request_that_differs_only_in_the_filter_is_a_miss_not_a_wrong_body(
    tmp_path: Path,
) -> None:
    """The two halves of the matcher, together.

    A filter with no recording must miss loudly. Before the matcher it matched
    the *unfiltered* recording and returned a plausible, wrong answer — which
    is exactly the failure mode ``fetch_free_agents`` cannot detect, because
    ESPN's default player set looks like the answer to any filter.
    """
    import requests
    from vcr.errors import CannotOverwriteExistingCassetteException

    path = tmp_path / "one-filter.yaml"
    recorder = build_vcr(cassette_library_dir=str(path.parent))
    with recorder.use_cassette(path.name, record_mode="all") as cassette:
        cassette.append(_request(fantasy_filter=WIDE_RECEIVERS), _response(b'{"slot": "WR"}'))

    with (
        recorder.use_cassette(path.name, record_mode="none"),
        pytest.raises(CannotOverwriteExistingCassetteException),
    ):
        requests.get(FREE_AGENTS_URL, headers={FILTER_HEADER: TIGHT_ENDS}, timeout=1)


def test_the_playback_path_never_opens_a_socket(tmp_path: Path) -> None:
    """A cassette hit must not touch the network either.

    Asserted by replaying with ``socket.socket`` replaced by something that
    raises: if playback opened a connection at all, this would fail.
    """
    path = tmp_path / "hit.yaml"
    _two_filtered_recordings(path)

    real = socket.socket
    socket.socket = lambda *a, **k: pytest.fail("playback opened a socket")  # type: ignore[assignment]
    try:
        assert _replay(path, WIDE_RECEIVERS)["body"]["string"] == b'{"slot": "WR"}'
    finally:
        socket.socket = real  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 3. `pytest -m "not live"` is offline, by configuration and in fact
# --------------------------------------------------------------------------- #


def test_the_running_session_has_sockets_disabled(pytestconfig: pytest.Config) -> None:
    """Asserted against the *live* config object, not a copy of the ini file.

    ``tests/unit/test_no_network.py`` proves a connection is refused; this
    proves the refusal comes from configuration that is always in force rather
    than from a sandbox that happens to have no route out. A CI runner with
    working DNS would pass the first test only because of this setting.
    """
    assert pytestconfig.getoption("disable_socket") is True
    assert "--disable-socket" in pytestconfig.getini("addopts")


def test_ci_runs_the_offline_selection() -> None:
    """The workflow must run ``-m "not live"``.

    Offline CI is a property of the command CI runs, and nothing else in the
    test suite can observe it. A workflow edited to drop the marker expression
    would start running the live suite against real ESPN on every pull request.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'pytest -m "not live"' in workflow


def test_the_live_marker_is_declared_so_the_selection_is_not_a_typo() -> None:
    """``-m "not live"`` against an undeclared marker is a silent no-op filter."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "live: hits real ESPN" in pyproject


def test_every_live_test_module_is_marked_live() -> None:
    """A file under ``tests/live/`` without ``pytestmark`` would run in CI."""
    for module in sorted((REPO_ROOT / "tests" / "live").glob("test_*.py")):
        assert "pytestmark = pytest.mark.live" in module.read_text(encoding="utf-8"), module


# --------------------------------------------------------------------------- #
# 4. The compressed-body path, which the corpus cannot cover
# --------------------------------------------------------------------------- #


def test_no_committed_cassette_carries_a_compressed_body() -> None:
    """The honest statement of what the corpus proves, asserted rather than claimed.

    ``decode_compressed_response=True`` means every recording lands as plain
    text, so a green run over the corpus is *not* evidence that the scrubber
    survives a gzipped body — the corpus contains none. That path is covered by
    the direct tests below and in ``test_scrubbing.py``, and this assertion is
    what keeps the claim true: if a compressed body ever does reach a committed
    cassette, this goes red and the documentation is wrong.
    """
    for path in iter_fixture_paths(CASSETTE_LIBRARY_DIR):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for interaction in document.get("interactions", []):
            headers = {k.lower(): v for k, v in interaction["response"]["headers"].items()}
            assert "content-encoding" not in headers, f"{path.name} recorded a compressed body"
            assert isinstance(interaction["response"]["body"]["string"], str), (
                f"{path.name} holds a !!binary body; it cannot be proven clean by eye"
            )


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
    ],
)
def test_a_compressed_body_is_decoded_before_the_scrubber_sees_it(
    encoding: str, compress: Any
) -> None:
    """ESPN answers ``Accept-Encoding: gzip, deflate`` with a compressed body.

    vcrpy composes its ``decode_response`` filter *ahead* of
    ``before_record_response`` when ``decode_compressed_response`` is set, so
    the scrubber gets text. Without it the scrubber runs a regex over a
    compressed stream, matches nothing, reports success, and the credential is
    on disk. Exercised through :func:`build_vcr_config`'s real composed hook
    rather than by calling :func:`scrub_response` directly, because the
    composition order is the thing under test.
    """
    import vcr.config

    body = b'{"owners": ["{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"]}'
    hook = vcr.config.VCR(**build_vcr_config())._build_before_record_response(build_vcr_config())
    recorded = hook(_response(compress(body), {"Content-Encoding": [encoding]}))

    assert b"0F1E2D3C" not in recorded["body"]["string"]
    assert b"{00000000-" in recorded["body"]["string"], "the SWID pseudonym is gone too"


def test_an_encoding_vcrpy_cannot_decode_is_refused_rather_than_recorded() -> None:
    """``decode_response`` knows gzip, deflate and brotli — and nothing else.

    A ``Content-Encoding`` it does not recognise is returned untouched, so the
    scrubber is handed opaque bytes. Bytes that cannot be read cannot be proven
    clean, so the only honest outcome is a refusal. This is the hole
    ``decode_compressed_response`` does *not* close, and no cassette can cover
    it: recording one would require ESPN to start serving zstd.
    """
    zstd_shaped = bytes([0x28, 0xB5, 0x2F, 0xFD]) + b"\xff\xfe not utf-8"

    with pytest.raises(UnscrubbableResponseError, match="cannot be scanned"):
        scrub_response(_response(zstd_shaped, {"Content-Encoding": ["zstd"]}))


# --------------------------------------------------------------------------- #
# 5. PII: the part of #12 the credential scan cannot reach
# --------------------------------------------------------------------------- #
#
# The scan proves a committed cassette holds no *credential*. It says nothing
# about whose league the payload came from, and a real private league is nine
# other people's team names, display names, and — since #38 — pseudonymised
# member ids that are a confirmable mapping back to their SWIDs. Scrubbing does
# not make that committable, so the enforcement is on provenance instead of on
# content: only leagues that are already public may reach a commit.

#: The only leagues whose payloads may be committed. ``1234`` is ESPN's public
#: test league, hit daily by ``espn-api``'s own CI; ``99`` does not exist and is
#: invented by ``scripts/build_synthetic_cassette.py``.
PUBLIC_LEAGUES = frozenset({"1234", "99"})


def _committed_cassettes() -> list[Path]:
    return [
        path
        for path in iter_fixture_paths(CASSETTE_LIBRARY_DIR)
        if path.suffix in {".yaml", ".yml"}
    ]


def test_every_committed_cassette_comes_from_a_public_league() -> None:
    """The enforceable half of the issue's PII clause.

    A recording of John's private league would fail here even though it passes
    the credential scan, which is the point: the scan and this test answer
    different questions, and only this one can answer "whose data is it?".
    """
    assert _committed_cassettes(), "no committed cassettes found; this check is vacuous"
    for path in _committed_cassettes():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for interaction in document["interactions"]:
            uri = interaction["request"]["uri"]
            assert uri.startswith(ESPN_HOST), f"{path.name} recorded a non-ESPN host"
            _, _, tail = uri.partition("/leagues/")
            if tail:
                league = tail.split("/")[0].split("?")[0]
                assert league in PUBLIC_LEAGUES, f"{path.name} records private league {league}"


def test_private_recordings_are_gitignored() -> None:
    """The recording script's default output path for a non-canary league.

    Without this line the script's own refusal is the only guard, and a
    contributor who passes ``--out`` past it commits somebody's roster.
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tests/cassettes/private/" in ignored


# --------------------------------------------------------------------------- #
# 6. The documented re-record procedure
# --------------------------------------------------------------------------- #
#
# `scripts/record_espn_cassettes.py` is the only thing in the repo that talks to
# ESPN, so nothing here runs it. What is testable offline is its two guards --
# where a recording is allowed to land, and the verification that re-reads the
# finished bytes -- and those are the parts that make the procedure safe.


def _script() -> Any:
    import importlib.util

    path = REPO_ROOT / "scripts" / "record_espn_cassettes.py"
    spec = importlib.util.spec_from_file_location("record_espn_cassettes", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_a_private_league_defaults_to_the_gitignored_directory() -> None:
    script = _script()
    assert script.cassette_name("1234", 2018, None) == "espn/canary_2018.yaml"
    assert script.cassette_name("55501", 2026, None) == "private/espn_55501_2026.yaml"


def test_recording_a_private_league_into_the_committed_path_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--out`` is an override, not an escape hatch.

    Returns before a single request is made, so this needs no network and no
    cassette — which is also why the refusal has to be checked here rather than
    discovered during a recording session.
    """
    assert _script().main(["--league", "55501", "--season", "2026", "--out", "espn/leak.yaml"]) == 2
    assert "only the public canary is committable" in capsys.readouterr().err


def test_the_verification_pass_reads_the_finished_bytes_back_off_disk(tmp_path: Path) -> None:
    """The end-to-end guarantee: nothing is trusted until it is re-read.

    A scrubber asserted through its own return value proves nothing about what
    was persisted, and the bytes are what leaks. Both halves fire here — the
    credential-pattern scan, and the literal-value grep that does not depend on
    a pattern being right.
    """
    script = _script()
    swid = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
    poisoned = tmp_path / "leaked.yaml"
    poisoned.write_text(f'interactions: [{{"owner": "{swid}"}}]\n', encoding="utf-8")

    problems = script.verify(poisoned, [swid])

    assert len(problems) == 2, problems
    assert not any(swid in problem for problem in problems), "the value must not be echoed"

    clean = tmp_path / "clean.yaml"
    clean.write_text("interactions: []\n", encoding="utf-8")
    assert script.verify(clean, [swid]) == []


def test_the_verification_grep_covers_the_percent_encoded_swid() -> None:
    """``%7B...%7D`` matches neither the scrub patterns nor the scan.

    It is the shape a SWID takes in a URL *path* — the ``fan.api`` membership
    probe in ``docs/memory/espn-401-tells-you-nothing.md``, which is not wired
    in precisely because of this. Checking the literal value we actually sent
    is the one check that survives a pattern being wrong.
    """
    swid = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
    variants = _script().credential_variants([swid])

    assert b"%7B0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0%7D" in variants
    assert b"0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0" in variants
    assert swid.encode() in variants
    assert not _script().credential_variants([""]), "an absent credential is not a pattern"
