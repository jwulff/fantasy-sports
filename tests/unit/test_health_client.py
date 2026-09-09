"""The client health check (ADR-0005 §11.3, jwulff/fantasy-sports#10).

Every test that reaches the network stubs ``requests.get`` rather than
calling it — ``pytest-socket`` blocks the real thing, and a stub proves the
*request* this module makes (URL, timeout) as well as its handling of the
response.

``tests/conftest.py``'s ``_no_health_check_by_default`` autouse fixture sets
``FANTASY_SPORTS_NO_HEALTH_CHECK=1`` for every test in this suite; the tests
here that mean to exercise the check itself clear it explicitly, which is the
point of writing that fixture's override in this file rather than anywhere
else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fantasy_sports.core.errors import (
    AuthExpiredError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.health import client as health_client
from fantasy_sports.health.manifest import (
    HealthManifest,
    parse_manifest,
)

MANIFEST_RAW = {
    "schema": "fantasy-sports-health/v1",
    "latest_version": "0.1.4",
    "min_supported_version": "0.1.2",
    "yanked_versions": [],
    "providers": {
        "espn": {
            "status": "degraded",
            "checked_at": "2026-09-03T06:00:00Z",
            "known_issues": [
                {
                    "code": "SCHEMA_DRIFT",
                    "fixed_in": "0.1.4",
                    "issue": 42,
                    "url": "https://github.com/jwulff/fantasy-sports/issues/42",
                    "summary": "ESPN changed mRoster player-entry shape",
                }
            ],
        }
    },
    "updated_at": "2026-09-03T06:00:00Z",
}


class FakeResponse:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeGet:
    """Records every call and answers from a queue, one response per call."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, *, timeout: float | None = None) -> Any:
        self.calls.append({"url": url, "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeGet called more times than it has responses queued")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _install(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> FakeGet:
    import requests

    fake = FakeGet(*responses)
    monkeypatch.setattr(requests, "get", fake)
    return fake


@pytest.fixture(autouse=True)
def _health_check_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the repo-wide opt-out for this file only."""
    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)


# --------------------------------------------------------------------------- #
# Opt-out
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_env_var_opts_out(value, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(health_client.NO_CHECK_ENV, value)
    assert health_client.is_opted_out(config_path=tmp_path / "absent.toml") is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_a_falsy_env_var_does_not_opt_out(value, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(health_client.NO_CHECK_ENV, value)
    assert health_client.is_opted_out(config_path=tmp_path / "absent.toml") is False


def test_config_toml_health_check_false_opts_out(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("health_check = false\n")
    assert health_client.is_opted_out(config_path=path) is True


def test_config_toml_health_check_true_does_not_opt_out(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("health_check = true\n")
    assert health_client.is_opted_out(config_path=path) is False


def test_an_absent_or_unparseable_config_file_does_not_opt_out(tmp_path: Path):
    """A broken config file must not be the reason the check silently never fires."""
    assert health_client.is_opted_out(config_path=tmp_path / "nope.toml") is False

    broken = tmp_path / "broken.toml"
    broken.write_text("this is not [ valid toml")
    assert health_client.is_opted_out(config_path=broken) is False


# --------------------------------------------------------------------------- #
# Version comparison — PEP 440, never a string compare
# --------------------------------------------------------------------------- #


def test_upgrade_available_is_pep440_not_lexicographic():
    """Lexicographically ``"0.1.10" < "0.1.9"``; PEP 440 says the opposite."""
    assert health_client.upgrade_available("0.1.9", "0.1.10") is True
    assert health_client.upgrade_available("0.1.10", "0.1.9") is False


def test_upgrade_available_is_false_when_current_or_equal():
    assert health_client.upgrade_available("0.1.4", "0.1.4") is False
    assert health_client.upgrade_available("0.1.5", "0.1.4") is False


def test_upgrade_available_is_false_with_no_latest_version():
    assert health_client.upgrade_available("0.1.4", None) is False
    assert health_client.upgrade_available("0.1.4", "") is False


def test_upgrade_available_fails_open_on_an_unparseable_version():
    assert health_client.upgrade_available("0.1.4", "not-a-version-at-all-!!") is False


# --------------------------------------------------------------------------- #
# Fetching and caching
# --------------------------------------------------------------------------- #


def test_get_manifest_fetches_the_documented_url_within_the_2s_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    manifest = health_client.get_manifest(cache_path=tmp_path / "health.json", now=1000.0)
    assert manifest is not None
    assert manifest.latest_version == "0.1.4"
    assert fake.calls == [{"url": health_client.DEFAULT_MANIFEST_URL, "timeout": 2.0}]


def test_a_fresh_cache_entry_is_served_without_a_second_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    cache_path = tmp_path / "health.json"
    first = health_client.get_manifest(cache_path=cache_path, now=1000.0)
    second = health_client.get_manifest(cache_path=cache_path, now=1000.0 + 60)
    assert first == second
    assert len(fake.calls) == 1


def test_a_cache_entry_older_than_6_hours_is_refetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    stale = dict(MANIFEST_RAW, latest_version="0.1.5")
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW), FakeResponse(stale))
    cache_path = tmp_path / "health.json"
    first = health_client.get_manifest(cache_path=cache_path, now=1000.0)
    second = health_client.get_manifest(
        cache_path=cache_path, now=1000.0 + health_client.CACHE_TTL_SECONDS + 1
    )
    assert first.latest_version == "0.1.4"
    assert second.latest_version == "0.1.5"
    assert len(fake.calls) == 2


def test_force_ignores_a_fresh_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``doctor`` (ARCHITECTURE §11.3's trigger table) always refetches."""
    updated = dict(MANIFEST_RAW, latest_version="0.1.9")
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW), FakeResponse(updated))
    cache_path = tmp_path / "health.json"
    health_client.get_manifest(cache_path=cache_path, now=1000.0)
    forced = health_client.get_manifest(cache_path=cache_path, force=True, now=1000.5)
    assert forced.latest_version == "0.1.9"
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({}, status_code=500),
        FakeResponse({}, status_code=404),
        FakeResponse(ValueError("not json")),
        FakeResponse(["not", "an", "object"]),
        TimeoutError("timed out"),
        ConnectionError("no route to host"),
    ],
)
def test_every_failure_mode_fails_open_to_none(
    response, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install(monkeypatch, response)
    assert health_client.get_manifest(cache_path=tmp_path / "health.json", now=1000.0) is None


def test_a_write_protected_cache_directory_does_not_stop_a_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The cache is an optimisation; a fetch must still succeed without it."""
    _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    # A file where the cache directory should be: `mkdir` inside `_save_cache`
    # fails, which must not prevent `get_manifest` from returning the fetch.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    manifest = health_client.get_manifest(cache_path=blocked / "health.json", now=1000.0)
    assert manifest is not None
    assert manifest.latest_version == "0.1.4"


def test_a_save_failure_that_is_not_an_oserror_still_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """``_save_cache`` is best-effort against *any* failure, not only ``OSError``."""
    import json as json_module

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("not disk-related at all")

    monkeypatch.setattr(json_module, "dump", _boom)
    _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    manifest = health_client.get_manifest(cache_path=tmp_path / "health.json", now=1000.0)
    assert manifest is not None
    assert manifest.latest_version == "0.1.4"


def test_a_corrupt_cache_file_is_treated_as_a_cache_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    cache_path = tmp_path / "health.json"
    cache_path.write_text("not json at all")
    manifest = health_client.get_manifest(cache_path=cache_path, now=1000.0)
    assert manifest is not None
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# Building the block and the human guidance
# --------------------------------------------------------------------------- #


def _manifest() -> HealthManifest:
    return parse_manifest(MANIFEST_RAW)


def test_build_health_block_with_an_upgrade_and_a_matching_known_issue():
    block = health_client.build_health_block(
        _manifest(), current_version="0.1.2", provider="espn", error_code="SCHEMA_DRIFT"
    )
    assert block == {
        "your_version": "0.1.2",
        "latest_version": "0.1.4",
        "upgrade_available": True,
        "upgrade_command": health_client.UPGRADE_COMMAND,
        "provider_status": "degraded",
        "known_issue": {
            "issue": 42,
            "url": "https://github.com/jwulff/fantasy-sports/issues/42",
            "fixed_in": "0.1.4",
            "summary": "ESPN changed mRoster player-entry shape",
        },
    }


def test_build_health_block_when_already_current():
    block = health_client.build_health_block(
        _manifest(), current_version="0.1.4", provider="espn", error_code="PROVIDER_UNAVAILABLE"
    )
    assert block["upgrade_available"] is False
    assert block["upgrade_command"] is None
    assert block["known_issue"] is None  # no known issue is filed under this code
    assert block["provider_status"] == "degraded"


def test_build_health_block_with_no_provider_named():
    block = health_client.build_health_block(
        _manifest(), current_version="0.1.2", provider=None, error_code="SCHEMA_DRIFT"
    )
    assert block["provider_status"] is None
    assert block["known_issue"] is None
    assert block["upgrade_available"] is True


def test_render_human_guidance_on_an_upgrade():
    block = health_client.build_health_block(
        _manifest(), current_version="0.1.2", provider="espn", error_code="SCHEMA_DRIFT"
    )
    text = health_client.render_human_guidance(block, provider="espn")
    assert "0.1.2" in text
    assert "0.1.4" in text
    assert "#42" in text
    assert health_client.UPGRADE_COMMAND in text


def test_render_human_guidance_on_a_known_outage_while_current():
    block = health_client.build_health_block(
        _manifest(), current_version="0.1.4", provider="espn", error_code="SCHEMA_DRIFT"
    )
    text = health_client.render_human_guidance(block, provider="espn")
    assert "latest version" in text
    assert "degraded" in text


def test_render_human_guidance_on_a_degraded_provider_with_no_matching_known_issue():
    manifest = parse_manifest(
        {
            "latest_version": "0.1.4",
            "providers": {"espn": {"status": "degraded", "known_issues": []}},
        }
    )
    block = health_client.build_health_block(
        manifest, current_version="0.1.4", provider="espn", error_code="SCHEMA_DRIFT"
    )
    text = health_client.render_human_guidance(block, provider="espn")
    assert "espn status: degraded." in text
    assert "#" not in text  # no known issue to cite


def test_render_human_guidance_is_none_when_current_and_healthy():
    manifest = parse_manifest({"latest_version": "0.1.4", "providers": {}})
    block = health_client.build_health_block(
        manifest, current_version="0.1.4", provider=None, error_code="SCHEMA_DRIFT"
    )
    assert health_client.render_human_guidance(block, provider=None) is None


# --------------------------------------------------------------------------- #
# evaluate_failure — the on-error entry point
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("error", [AuthExpiredError("expired"), RateLimitedError("throttled")])
def test_evaluate_failure_never_touches_the_network_for_a_non_trigger_code(
    error, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    block, guidance = health_client.evaluate_failure(
        error, current_version="0.1.2", provider="espn", cache_path=tmp_path / "health.json"
    )
    assert (block, guidance) == (None, None)
    assert fake.calls == []


@pytest.mark.parametrize("error", [SchemaDriftError("drift"), ProviderUnavailableError("down")])
def test_evaluate_failure_fires_for_the_two_trigger_codes(
    error, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    block, guidance = health_client.evaluate_failure(
        error,
        current_version="0.1.2",
        provider="espn",
        cache_path=tmp_path / "health.json",
        now=1000.0,
    )
    assert block is not None
    assert block["latest_version"] == "0.1.4"
    assert guidance is not None


def test_evaluate_failure_respects_the_opt_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    monkeypatch.setenv(health_client.NO_CHECK_ENV, "1")
    block, guidance = health_client.evaluate_failure(
        SchemaDriftError("drift"),
        current_version="0.1.2",
        provider="espn",
        cache_path=tmp_path / "health.json",
    )
    assert (block, guidance) == (None, None)
    assert fake.calls == []


def test_evaluate_failure_never_raises_even_if_get_manifest_blows_up(
    monkeypatch: pytest.MonkeyPatch,
):
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("a bug in the health check itself")

    monkeypatch.setattr(health_client, "get_manifest", _boom)
    block, guidance = health_client.evaluate_failure(
        SchemaDriftError("drift"), current_version="0.1.2", provider="espn"
    )
    assert (block, guidance) == (None, None)


def test_evaluate_failure_with_no_manifest_available_produces_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install(monkeypatch, FakeResponse({}, status_code=404))
    block, guidance = health_client.evaluate_failure(
        ProviderUnavailableError("down"),
        current_version="0.1.2",
        provider="espn",
        cache_path=tmp_path / "health.json",
    )
    assert (block, guidance) == (None, None)


# --------------------------------------------------------------------------- #
# Cache file shape
# --------------------------------------------------------------------------- #


def test_the_cache_file_records_fetched_at_and_the_raw_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install(monkeypatch, FakeResponse(MANIFEST_RAW))
    cache_path = tmp_path / "health.json"
    health_client.get_manifest(cache_path=cache_path, now=1234.0)
    document = json.loads(cache_path.read_text())
    assert document["fetched_at"] == 1234.0
    assert document["manifest"] == MANIFEST_RAW


@pytest.mark.parametrize(
    "contents",
    [
        "[1, 2, 3]",  # valid JSON, but not an object
        '{"fetched_at": "not-a-number", "manifest": {}}',
        '{"fetched_at": 1000.0, "manifest": "not-an-object"}',
        '{"fetched_at": 1000.0}',  # manifest missing entirely
    ],
)
def test_load_cache_rejects_a_malformed_but_valid_json_document(contents, tmp_path: Path):
    from fantasy_sports.health.client import _load_cache

    path = tmp_path / "health.json"
    path.write_text(contents)
    assert _load_cache(path) is None


def test_safe_parse_fails_open_on_something_manifest_parsing_refuses():
    from fantasy_sports.health.client import _safe_parse

    assert _safe_parse(["not", "a", "mapping"]) is None  # type: ignore[arg-type]


def test_default_cache_path_is_under_the_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = health_client.default_cache_path()
    assert path == tmp_path / ".cache" / "fantasy-sports" / health_client.CACHE_FILENAME
