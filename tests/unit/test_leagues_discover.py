"""``leagues discover`` (jwulff/fantasy-sports#157), offline.

The read replays ``tests/cassettes/espn/fan_2026.yaml`` through ``requests.get``,
the seam the production code uses. The cassette is **synthetic** (built from the
documented fan.api shape by ``scripts/build_fan_cassette.py``), so what these
tests prove is that the reader handles that shape, not that ESPN still sends it.
Shape variations no recording can honestly hold are injected by editing the
recorded body.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from _harness import FAKE_SWID, FakeFanApi, isolate_home

from fantasy_sports.cli.app import run
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands import leagues as leagues_commands
from fantasy_sports.core.errors import (
    AuthMissingError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
)
from fantasy_sports.core.models import DiscoveredLeague
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.providers import espn_fan

SWID_BARE = FAKE_SWID.strip("{}")


recorded_body = FakeFanApi.recorded_body


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    isolate_home(tmp_path, monkeypatch)
    yield
    forget_secrets()


def serve(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> FakeFanApi:
    import requests

    api = FakeFanApi(**kwargs)
    monkeypatch.setattr(requests, "get", api)
    return api


def rows(**kwargs: Any) -> list[dict[str, Any]]:
    return leagues_commands.discover(**kwargs).data


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_one_row_per_type_9_preference_and_the_decoy_is_ignored(monkeypatch):
    serve(monkeypatch)
    found = rows()
    assert [(r["league_id"], r["name"]) for r in found] == [
        ("99", "Synthetic Gridiron"),
        ("1234", "Synthetic Ice"),
    ]


def test_row_fields_football(monkeypatch):
    serve(monkeypatch)
    row = rows()[0]
    assert {k: v for k, v in row.items() if k != "raw"} == {
        "provider": "espn",
        "provider_id": "99",
        "league_id": "99",
        "season": 2026,
        "sport": "football",
        "name": "Synthetic Gridiron",
        "team_id": "3",
        "is_manager": True,
    }
    assert row["raw"]["type"]["id"] == 9


def test_row_fields_non_football_and_non_manager(monkeypatch):
    serve(monkeypatch)
    row = rows()[1]
    assert (row["sport"], row["team_id"], row["is_manager"]) == ("hockey", "7", False)


def test_an_unmapped_abbreviation_is_passed_through_and_kept_in_raw(monkeypatch):
    body = recorded_body()
    body["preferences"][0]["metaData"]["entry"]["abbrev"] = "xyz"
    serve(monkeypatch, body=body)
    row = rows()[0]
    assert row["sport"] == "xyz"
    assert row["raw"]["metaData"]["entry"]["abbrev"] == "xyz"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: (e.pop("abbrev"), e.update(gameAbbrev="FFL")),
        lambda e: (e.pop("abbrev"), e.update(game={"abbrev": "FFL"})),
    ],
)
def test_the_abbreviation_is_found_wherever_the_entry_carries_it(monkeypatch, mutate):
    body = recorded_body()
    mutate(body["preferences"][0]["metaData"]["entry"])
    serve(monkeypatch, body=body)
    assert rows()[0]["sport"] == "football"


def test_an_entry_with_no_abbreviation_is_sport_unknown(monkeypatch):
    body = recorded_body()
    del body["preferences"][0]["metaData"]["entry"]["abbrev"]
    serve(monkeypatch, body=body)
    assert rows()[0]["sport"] == "unknown"


def test_every_group_in_an_entry_is_a_row(monkeypatch):
    body = recorded_body()
    groups = body["preferences"][0]["metaData"]["entry"]["groups"]
    groups.append({"groupId": 99, "groupName": "Second", "groupManager": False})
    serve(monkeypatch, body=body)
    assert [r["name"] for r in rows()] == ["Synthetic Gridiron", "Second", "Synthetic Ice"]


def test_the_envelope_is_a_league_less_collection_naming_its_source(monkeypatch):
    serve(monkeypatch)
    envelope = leagues_commands.discover()
    payload = envelope.to_dict()
    assert payload["provider"] == "espn"
    assert payload["league_id"] is None and payload["season"] is None
    assert [s["name"] for s in payload["sources"]] == ["fan.api:fans"]
    assert payload["untrusted"] == {"[0].name": "Synthetic Gridiron", "[1].name": "Synthetic Ice"}
    assert REGISTRY["leagues discover"].shape is DataShape.COLLECTION
    assert REGISTRY["leagues discover"].takes_league is False


def test_the_request_carries_the_encoded_swid_in_the_path_and_no_cookies(monkeypatch):
    api = serve(monkeypatch)
    rows()
    (call,) = api.calls
    assert urlsplit(call["url"]).path == f"/apis/v2/fans/%7B{SWID_BARE}%7D"
    assert unquote(call["url"].rsplit("/", 1)[-1]) == FAKE_SWID
    assert call["params"] == {"featureFlags": "fantasy"}
    assert not {"cookies", "headers"} & call["kwargs"].keys()


def test_discover_needs_only_the_swid(monkeypatch):
    monkeypatch.delenv("FANTASY_SPORTS_ESPN_S2")
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    serve(monkeypatch)
    assert len(rows()) == 2


# --------------------------------------------------------------------------- #
# --config-snippet
# --------------------------------------------------------------------------- #


def test_config_snippet_is_valid_toml_that_the_config_loader_accepts(monkeypatch, tmp_path):
    import tomllib

    serve(monkeypatch)
    found = rows(config_snippet=True)
    assert found[0]["config_snippet"] == (
        "[leagues.synthetic-gridiron]\n"
        'provider = "espn"\n'
        'league_id = "99"\n'
        "season = 2026\n"
        'sport = "football"\n'
    )
    pasted = "\n".join(r["config_snippet"] for r in found)
    assert tomllib.loads(pasted)["leagues"]["synthetic-gridiron"]["league_id"] == "99"

    from fantasy_sports.config.leagues import list_leagues

    config = tmp_path / "pasted.toml"
    config.write_text(pasted, encoding="utf-8")
    assert [(p.name, p.league_id, p.sport) for p in list_leagues(config)] == [
        ("synthetic-gridiron", "99", "football"),
    ]


def test_a_league_in_a_sport_the_adapter_cannot_read_gets_a_comment_not_a_profile(monkeypatch):
    """EspnProvider ignores ``sport`` and builds the football client, so a pasted
    hockey profile would query the nfl endpoint with a hockey league id."""
    import tomllib

    serve(monkeypatch)
    hockey = rows(config_snippet=True)[1]
    assert hockey["sport"] == "hockey"
    assert hockey["config_snippet"].startswith("# synthetic-ice:")
    assert "1234" in hockey["config_snippet"]
    assert tomllib.loads(hockey["config_snippet"]) == {}


def test_snippets_are_absent_without_the_flag_and_nothing_is_written(monkeypatch, tmp_path):
    serve(monkeypatch)
    before = (tmp_path / "config" / "fantasy-sports" / "config.toml").read_text()
    assert all("config_snippet" not in r for r in rows())
    rows(config_snippet=True)
    assert (tmp_path / "config" / "fantasy-sports" / "config.toml").read_text() == before


def _league(name: str, league_id: str = "5") -> DiscoveredLeague:
    return DiscoveredLeague(
        provider="espn", provider_id=league_id, league_id=league_id, season=2026,
        sport="football", name=name, team_id="1", is_manager=False, raw={},
    )  # fmt: skip


def test_slugs_are_lowercase_dash_separated_and_deduplicated():
    found = [_league("The  Big League!"), _league("the big league"), _league("THE BIG LEAGUE")]
    assert leagues_commands.slugs(found) == [
        "the-big-league",
        "the-big-league-2",
        "the-big-league-3",
    ]


def test_a_suffixed_slug_never_collides_with_another_leagues_natural_slug():
    import tomllib

    found = [_league("Foo"), _league("Foo"), _league("Foo 2"), _league("foo-2")]
    names = leagues_commands.slugs(found)
    assert len(set(names)) == 4 and names[:3] == ["foo", "foo-2", "foo-2-2"]
    row = {"provider": "espn", "league_id": "5", "season": 2026, "sport": "football"}
    tomllib.loads("\n".join(leagues_commands.snippet(n, row) for n in names))


def test_a_name_with_nothing_sluggable_falls_back_to_the_league_id():
    assert leagues_commands.slugs([_league("!!!", "77"), _league("", "78")]) == [
        "league-77",
        "league-78",
    ]


def test_hostile_values_cannot_break_out_of_the_snippet():
    import tomllib

    hostile = 'x"\n[evil]\na = 1'
    football = {"provider": "espn", "league_id": hostile, "season": 2026, "sport": "football"}
    document = tomllib.loads(leagues_commands.snippet("ok", football))
    assert set(document) == {"leagues"}
    assert document["leagues"]["ok"]["league_id"] == hostile

    other = {**football, "sport": hostile}  # league_id is hostile here too
    assert set(tomllib.loads(leagues_commands.snippet("ok", other))) == set(), "a comment only"


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_no_swid_is_auth_missing_and_nothing_is_requested(monkeypatch):
    for name in ("FANTASY_SPORTS_SWID", "FANTASY_SPORTS_ESPN_S2"):
        monkeypatch.delenv(name)
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    api = serve(monkeypatch)
    with pytest.raises(AuthMissingError):
        rows()
    assert api.calls == []


def test_zero_type_9_preferences_is_an_empty_success_with_a_stderr_warning(monkeypatch, capsys):
    body = recorded_body()
    body["preferences"] = [p for p in body["preferences"] if p["type"]["id"] != 9]
    serve(monkeypatch, body=body)
    envelope = leagues_commands.discover()
    assert envelope.data == [] and envelope.error is None
    captured = capsys.readouterr()
    assert "no fantasy leagues" in captured.err
    assert captured.out == ""


def test_an_empty_preferences_list_is_not_drift(monkeypatch):
    serve(monkeypatch, body={"preferences": []})
    assert rows() == []


@pytest.mark.parametrize("body", [{}, {"preferences": None}, {"preferences": {}}, []])
def test_a_response_without_a_preferences_list_is_schema_drift(monkeypatch, body):
    serve(monkeypatch, body=body)
    with pytest.raises(SchemaDriftError) as caught:
        rows()
    assert caught.value.path == ("preferences",)


def test_a_body_that_is_not_json_is_schema_drift(monkeypatch):
    serve(monkeypatch, body=b"<html>")
    with pytest.raises(SchemaDriftError):
        rows()


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda e: e.pop("groups"), "groups"),
        (lambda e: e.pop("seasonId"), "seasonId"),
        (lambda e: e.update(seasonId="soon"), "seasonId"),
        (lambda e: e.pop("entryId"), "entryId"),
        (lambda e: e["groups"][0].pop("groupId"), "groupId"),
        (lambda e: e["groups"].__setitem__(0, "x"), "groups[0]"),
    ],
)
def test_a_type_9_entry_that_lost_a_field_is_schema_drift(monkeypatch, mutate, path):
    body = recorded_body()
    mutate(body["preferences"][0]["metaData"]["entry"])
    serve(monkeypatch, body=body)
    with pytest.raises(SchemaDriftError) as caught:
        rows()
    assert path in caught.value.path[-1]


@pytest.mark.parametrize("meta", [None, {}, {"entry": None}])
def test_a_type_9_preference_without_metadata_is_schema_drift(monkeypatch, meta):
    body = recorded_body()
    body["preferences"][0]["metaData"] = meta
    serve(monkeypatch, body=body)
    with pytest.raises(SchemaDriftError):
        rows()


def test_a_malformed_preference_that_is_not_type_9_is_ignored(monkeypatch):
    body = recorded_body()
    body["preferences"] += ["junk", {"type": None}, {"type": {"id": 2}, "metaData": 5}]
    serve(monkeypatch, body=body)
    assert len(rows()) == 2


def test_http_429_is_rate_limited_and_keeps_retry_after(monkeypatch):
    serve(monkeypatch, status=429, headers={"Retry-After": "30"})
    with pytest.raises(RateLimitedError) as caught:
        rows()
    assert caught.value.to_dict()["details"] == {"status": 429, "retry_after": 30.0}


def test_http_429_without_retry_after_reports_none(monkeypatch):
    serve(monkeypatch, status=429)
    with pytest.raises(RateLimitedError) as caught:
        rows()
    assert caught.value.retry_after is None


def test_other_http_failures_are_provider_unavailable(monkeypatch):
    serve(monkeypatch, status=503)
    with pytest.raises(ProviderUnavailableError) as caught:
        rows()
    assert caught.value.details["status"] == 503


def test_a_transport_failure_never_carries_the_url_or_the_swid(monkeypatch):
    serve(monkeypatch, raises=ConnectionError(f"HTTPSConnectionPool /fans/%7B{SWID_BARE}%7D"))
    with pytest.raises(ProviderUnavailableError) as caught:
        rows()
    text = json.dumps(caught.value.to_dict())
    assert SWID_BARE not in text and "ConnectionError" in text


# --------------------------------------------------------------------------- #
# The SWID never leaves
# --------------------------------------------------------------------------- #


def test_a_swid_riding_in_the_raw_payload_is_scrubbed(monkeypatch):
    body = recorded_body()
    body["preferences"][0]["metaData"]["entry"]["owner"] = FAKE_SWID
    serve(monkeypatch, body=body)
    assert SWID_BARE not in json.dumps(rows())


def test_the_cli_prints_rows_and_never_the_swid(monkeypatch, capsys):
    serve(monkeypatch)
    assert run(["leagues", "discover", "--config-snippet"]) == 0
    out = capsys.readouterr().out
    assert SWID_BARE not in out
    payload = json.loads(out)
    assert [r["league_id"] for r in payload["data"]] == ["99", "1234"]
    assert payload["league_id"] is None


def test_the_cli_reports_a_missing_swid_as_a_typed_failure(monkeypatch, capsys):
    for name in ("FANTASY_SPORTS_SWID", "FANTASY_SPORTS_ESPN_S2"):
        monkeypatch.delenv(name)
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    serve(monkeypatch)
    assert run(["leagues", "discover"]) != 0
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "AUTH_MISSING"


def test_a_transport_error_interpolating_the_url_is_redacted_by_the_registry(monkeypatch):
    """The encoded spelling is registered, so a caller three layers away that
    puts the fan.api URL in a message cannot leak the SWID."""
    from fantasy_sports.core.errors import ConfigInvalidError

    serve(monkeypatch)
    rows()
    leaked = f"GET {espn_fan.BASE_URL}/%7B{SWID_BARE}%7D failed"
    assert SWID_BARE not in str(ConfigInvalidError(leaked))
