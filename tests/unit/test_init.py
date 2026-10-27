"""``init`` (jwulff/fantasy-sports#158), offline.

Every seam is a fake: the browser search, ``fetch_leagues``, the TTY check, the
Keychain write. The config is a real file under a ``tmp_path`` XDG tree, because
what ``init`` must not do to it is the point (``docs/memory/config-toml-is-a-
shared-namespace.md``). The platform is never read: the browser search is
replaced whole, so these tests mean the same on a Linux runner.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from _harness import FAKE_S2, FAKE_SWID, isolate_home

from fantasy_sports.auth import browser
from fantasy_sports.auth.chain import Secret, forget_secrets
from fantasy_sports.cli.app import run
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands import init as init_command
from fantasy_sports.core.errors import AuthMissingError, ConfigInvalidError
from fantasy_sports.core.models import DiscoveredLeague
from fantasy_sports.providers import espn_fan


def league(
    league_id: str, name: str, *, season: int = 2026, sport: str = "football"
) -> DiscoveredLeague:
    return DiscoveredLeague(
        provider="espn",
        provider_id=league_id,
        league_id=league_id,
        season=season,
        sport=sport,
        name=name,
        team_id="1",
        is_manager=False,
        raw={},
    )


GRIDIRON = league("99", "Synthetic Gridiron")
OTHER = league("55", "Office League")
LAST_YEAR = league("99", "Synthetic Gridiron", season=2025)
ICE = league("1234", "Synthetic Ice", season=2027, sport="hockey")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    isolate_home(tmp_path, monkeypatch)
    config_file().unlink()  # a fresh install has no config.toml
    yield
    forget_secrets()


def config_file() -> Path:
    from fantasy_sports.config.paths import config_file as path

    return path()


class Fakes:
    """The four seams, recorded."""

    def __init__(self) -> None:
        self.keychain: dict[str, str] = {}
        self.swids: list[str] = []
        self.searches = 0
        self.leagues: list[DiscoveredLeague] = [GRIDIRON]
        self.profiles: tuple[browser.ProfileCookies, ...] = (
            browser.ProfileCookies(
                "Chrome", "Default", espn_s2=Secret(FAKE_S2), swid=Secret(FAKE_SWID)
            ),
        )
        self.tty = False
        self.answers: list[str] = []


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fake = Fakes()

    def search(*args: Any, **kwargs: Any) -> browser.BrowserSearch:
        fake.searches += 1
        return browser.BrowserSearch(
            profiles=fake.profiles, searched=tuple(p.label for p in fake.profiles)
        )

    def fetch(swid: str, **kwargs: Any) -> list[DiscoveredLeague]:
        fake.swids.append(swid)
        return list(fake.leagues)

    monkeypatch.setattr(browser, "find_espn_cookies", search)
    monkeypatch.setattr(espn_fan, "fetch_leagues", fetch)
    monkeypatch.setattr(init_command, "_stdin_is_tty", lambda: fake.tty)
    monkeypatch.setattr("fantasy_sports.auth.chain.write_to_keychain", fake.keychain.__setitem__)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": fake.answers.pop(0))
    return fake


def written_config() -> dict[str, Any]:
    return tomllib.loads(config_file().read_text(encoding="utf-8"))


def seed(text: str) -> None:
    config_file().write_text(text, encoding="utf-8")


def test_init_is_a_top_level_object_command_with_its_own_params():
    spec = REGISTRY["init"]
    assert spec.group is None and spec.shape is DataShape.OBJECT and not spec.takes_league
    assert {p.name for p in spec.handler_params} == {"season", "all_seasons", "dry_run"}


# --------------------------------------------------------------------------- #
# Fresh file
# --------------------------------------------------------------------------- #


def test_a_fresh_install_imports_discovers_writes_and_runs_the_doctor(fakes: Fakes):
    data = init_command.init().data
    assert fakes.keychain == {"espn_s2": FAKE_S2, "swid": FAKE_SWID}
    assert data["credentials"] == {
        "source": "browser",
        "browser": "Chrome",
        "profile": "Default",
        "stored": True,
    }
    assert data["leagues"]["written"] == [
        {
            "slug": "synthetic-gridiron",
            "provider": "espn",
            "league_id": "99",
            "season": 2026,
            "sport": "football",
        }
    ]
    assert data["leagues"]["skipped"] == []
    assert data["default"] == "synthetic-gridiron"
    assert data["config_path"] == str(config_file())
    assert {"status", "ok", "checks"} <= set(data["doctor"])
    assert written_config() == {
        "default": "synthetic-gridiron",
        "leagues": {
            "synthetic-gridiron": {
                "provider": "espn",
                "league_id": "99",
                "season": 2026,
                "sport": "football",
            }
        },
    }
    # The profile it wrote is one the league layer reads back.
    from fantasy_sports.config.leagues import load

    assert load().resolve().league_id == "99"


def test_the_swid_reaches_discovery_and_no_envelope_field(fakes: Fakes):
    envelope = init_command.init()
    assert fakes.swids == [FAKE_SWID]
    rendered = json.dumps(envelope.data)
    assert FAKE_SWID not in rendered and FAKE_SWID.strip("{}") not in rendered
    assert FAKE_S2 not in rendered


def test_two_leagues_are_written_and_no_default_is_guessed(fakes: Fakes):
    fakes.leagues = [GRIDIRON, OTHER]
    data = init_command.init().data
    assert [r["slug"] for r in data["leagues"]["written"]] == [
        "synthetic-gridiron",
        "office-league",
    ]
    assert data["default"] is None
    assert "default" not in written_config()
    assert 'default = "synthetic-gridiron"' in data["next_step"]


def test_the_current_season_is_the_latest_listed_and_past_ones_wait_for_the_flag(fakes: Fakes):
    fakes.leagues = [LAST_YEAR, GRIDIRON]
    assert [r["season"] for r in init_command.init(dry_run=True).data["leagues"]["written"]] == [
        2026
    ]
    data = init_command.init(all_seasons=True).data
    assert [(r["slug"], r["season"]) for r in data["leagues"]["written"]] == [
        ("synthetic-gridiron", 2025),
        ("synthetic-gridiron-2", 2026),
    ]


def test_season_overrides_the_current_season(fakes: Fakes):
    fakes.leagues = [LAST_YEAR, GRIDIRON]
    data = init_command.init(season=2025).data
    assert [r["season"] for r in data["leagues"]["written"]] == [2025]
    assert written_config()["leagues"]["synthetic-gridiron"]["season"] == 2025


def test_season_and_all_seasons_together_are_refused(fakes: Fakes):
    with pytest.raises(ConfigInvalidError):
        init_command.init(season=2025, all_seasons=True)
    assert fakes.searches == 0 and not config_file().exists()


def test_a_league_in_a_sport_the_adapter_cannot_read_is_skipped_not_written(fakes: Fakes):
    fakes.leagues = [GRIDIRON, ICE]
    data = init_command.init().data
    assert [r["league_id"] for r in data["leagues"]["written"]] == ["99"]
    assert data["leagues"]["skipped"] == [
        {
            "slug": None,
            "provider": "espn",
            "league_id": "1234",
            "season": 2027,
            "sport": "hockey",
            "reason": "unsupported-sport",
        }
    ]
    assert "hockey" not in config_file().read_text()


def test_an_account_with_no_leagues_writes_nothing_and_still_reports(fakes: Fakes):
    fakes.leagues = []
    data = init_command.init().data
    assert data["leagues"] == {"written": [], "skipped": []}
    assert not config_file().exists()
    assert data["doctor"]["checks"]


# --------------------------------------------------------------------------- #
# Never clobbers
# --------------------------------------------------------------------------- #


def test_an_overlapping_league_is_left_untouched_and_reported_skipped(fakes: Fakes):
    seed(
        'default = "mine"\n\n[leagues.mine]\nprovider = "espn"\nleague_id = "99"\n'
        'season = 2026\nsport = "football"\n'
    )
    fakes.leagues = [GRIDIRON, OTHER]
    data = init_command.init().data
    assert data["leagues"]["skipped"] == [
        {
            "slug": "mine",
            "provider": "espn",
            "league_id": "99",
            "season": 2026,
            "sport": "football",
            "reason": "already-configured",
        }
    ]
    assert [r["slug"] for r in data["leagues"]["written"]] == ["office-league"]
    config = written_config()
    assert config["leagues"]["mine"] == {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "sport": "football",
    }
    assert config["default"] == "mine"


def test_everything_already_configured_leaves_the_file_byte_identical(fakes: Fakes):
    seed('[leagues.mine]\nprovider = "espn"\nleague_id = "99"\nseason = 2026\n')
    before = config_file().read_bytes()
    data = init_command.init().data
    assert data["leagues"]["written"] == []
    assert config_file().read_bytes() == before


def test_unrelated_top_level_keys_survive(fakes: Fakes):
    seed('[auth]\nnote = "keep me"\n\n[notify]\nchannel = "ops"\n')
    init_command.init()
    config = written_config()
    assert config["auth"] == {"note": "keep me"}
    assert config["notify"] == {"channel": "ops"}
    assert "synthetic-gridiron" in config["leagues"]


def test_an_existing_default_is_never_changed(fakes: Fakes):
    seed(
        'default = "office"\n\n[leagues.office]\nprovider = "espn"\nleague_id = "55"\n'
        "season = 2026\n"
    )
    data = init_command.init().data
    assert data["default"] is None
    assert written_config()["default"] == "office"


def test_a_lone_existing_league_stays_the_default_when_a_second_arrives(fakes: Fakes):
    seed('[leagues.office]\nprovider = "espn"\nleague_id = "55"\nseason = 2026\n')
    data = init_command.init().data
    assert data["default"] == "office"
    assert written_config()["default"] == "office"
    from fantasy_sports.config.leagues import load

    assert load().resolve().name == "office"
    assert [r["slug"] for r in data["leagues"]["written"]] == ["synthetic-gridiron"]


def test_nothing_new_written_leaves_a_lone_implicit_default_implicit(fakes: Fakes):
    seed('[leagues.mine]\nprovider = "espn"\nleague_id = "99"\nseason = 2026\n')
    assert init_command.init().data["default"] is None
    assert "default" not in written_config()


def test_a_slug_already_taken_by_another_league_is_suffixed(fakes: Fakes):
    seed('[leagues.synthetic-gridiron]\nprovider = "espn"\nleague_id = "7"\nseason = 2026\n')
    data = init_command.init().data
    assert [r["slug"] for r in data["leagues"]["written"]] == ["synthetic-gridiron-2"]
    assert written_config()["leagues"]["synthetic-gridiron"]["league_id"] == "7"


def test_an_unreadable_config_is_config_invalid_and_not_overwritten(fakes: Fakes):
    seed("this is [not toml")
    with pytest.raises(ConfigInvalidError):
        init_command.init()
    assert config_file().read_text() == "this is [not toml"
    # ...and it failed before the Keychain, the browser or ESPN was touched.
    assert fakes.keychain == {} and fakes.searches == 0 and fakes.swids == []


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def test_dry_run_reports_without_writing_the_config_or_the_keychain(fakes: Fakes):
    data = init_command.init(dry_run=True).data
    assert data["dry_run"] is True
    assert data["credentials"]["stored"] is False
    assert [r["slug"] for r in data["leagues"]["written"]] == ["synthetic-gridiron"]
    assert data["default"] == "synthetic-gridiron"
    assert not config_file().exists()
    assert fakes.keychain == {}
    assert data["doctor"] is None


def test_dry_run_leaves_every_xdg_tree_byte_for_byte_unchanged(fakes: Fakes, tmp_path: Path):
    def snapshot() -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    seed('[auth]\nnote = "x"\n')
    before = snapshot()
    init_command.init(dry_run=True)
    assert snapshot() == before


def test_dry_run_with_no_browser_uses_stored_credentials_and_never_prompts(fakes: Fakes):
    fakes.profiles = ()
    fakes.tty = True  # a terminal is available, and still must not be prompted
    data = init_command.init(dry_run=True).data
    assert data["credentials"] == {"source": "stored", "stored": False}
    assert fakes.answers == [] and fakes.keychain == {}


def test_dry_run_with_no_credentials_anywhere_is_auth_missing(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
):
    fakes.profiles = ()
    for name in ("FANTASY_SPORTS_ESPN_S2", "FANTASY_SPORTS_SWID"):
        monkeypatch.delenv(name)
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    with pytest.raises(AuthMissingError):
        init_command.init(dry_run=True)


# --------------------------------------------------------------------------- #
# No browser
# --------------------------------------------------------------------------- #


def test_no_browser_and_no_terminal_fails_auth_missing_and_writes_nothing(fakes: Fakes):
    fakes.profiles = ()
    with pytest.raises(AuthMissingError) as caught:
        init_command.init()
    assert "no terminal" in str(caught.value)
    assert "auth login" in (caught.value.remediation or "")
    assert "--from-clipboard" in (caught.value.remediation or "")
    assert fakes.keychain == {} and fakes.swids == []
    assert not config_file().exists()


def test_no_browser_on_a_terminal_falls_back_to_the_prompt(fakes: Fakes):
    fakes.profiles = ()
    fakes.tty = True
    fakes.answers = [FAKE_S2, FAKE_SWID, ""]  # both cookies, then the optional key skipped
    data = init_command.init().data
    assert data["credentials"] == {"source": "prompt", "stored": True}
    assert fakes.keychain == {"espn_s2": FAKE_S2, "swid": FAKE_SWID}
    assert [r["slug"] for r in data["leagues"]["written"]] == ["synthetic-gridiron"]


def test_a_failure_exits_with_the_auth_missing_code_through_the_cli(
    fakes: Fakes, capsys: pytest.CaptureFixture[str]
):
    fakes.profiles = ()
    assert run(["init"]) != 0
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "AUTH_MISSING"


def test_the_cli_projection_runs_init_end_to_end(fakes: Fakes, capsys: pytest.CaptureFixture[str]):
    assert run(["init", "--dry-run", "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["leagues"]["written"][0]["league_id"] == "99"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_result_does_not_depend_on_the_interpreter_platform(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, platform: str
):
    monkeypatch.setattr("sys.platform", platform)
    assert init_command.init(dry_run=True).data["leagues"]["written"][0]["league_id"] == "99"


def test_the_real_tty_check_asks_stdin(monkeypatch: pytest.MonkeyPatch):
    class Stdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Stdin())
    assert init_command._stdin_is_tty() is True


def test_dry_run_discovers_with_the_browsers_swid_not_the_chains(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
):
    """Fresh install: nothing stored anywhere, the browser is the only source."""
    for name in ("FANTASY_SPORTS_ESPN_S2", "FANTASY_SPORTS_SWID"):
        monkeypatch.delenv(name)
    monkeypatch.setattr("fantasy_sports.auth.chain.read_from_keychain", lambda spec: None)
    data = init_command.init(dry_run=True).data
    assert fakes.swids == [FAKE_SWID]
    assert data["credentials"]["source"] == "browser"
    assert FAKE_SWID not in json.dumps(data)


def test_dry_run_does_not_discover_as_an_older_stored_account(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FANTASY_SPORTS_SWID", "{FFFFFFFF-0000-0000-0000-000000000000}")
    init_command.init(dry_run=True)
    assert fakes.swids == [FAKE_SWID]


def test_the_envelope_names_the_discovery_fetch_as_its_source(fakes: Fakes):
    envelope = init_command.init()
    payload = envelope.to_dict()
    assert [source["name"] for source in payload["sources"]] == [espn_fan.SOURCE_NAME]
    assert payload["data_as_of"] is not None and payload["data_age_seconds"] is not None


def test_a_same_id_profile_in_another_sport_does_not_shadow_the_football_league(fakes: Fakes):
    seed('[leagues.puck]\nprovider = "espn"\nleague_id = "99"\nseason = 2026\nsport = "hockey"\n')
    data = init_command.init().data
    assert [r["slug"] for r in data["leagues"]["written"]] == ["synthetic-gridiron"]
    assert data["leagues"]["skipped"] == []
    assert written_config()["leagues"]["puck"]["sport"] == "hockey"


def test_several_existing_leagues_without_a_default_are_left_for_the_user(fakes: Fakes):
    seed(
        '[leagues.a]\nprovider = "espn"\nleague_id = "1"\nseason = 2026\n'
        '[leagues.b]\nprovider = "espn"\nleague_id = "2"\nseason = 2026\n'
    )
    data = init_command.init().data
    assert data["default"] is None and data["next_step"] is None
    assert "default" not in written_config()


@pytest.mark.parametrize("failure", [ValueError("closed"), OSError("bad fd")])
def test_a_stdin_whose_isatty_raises_counts_as_no_terminal(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
):
    class Stdin:
        def isatty(self) -> bool:
            raise failure

    monkeypatch.setattr("sys.stdin", Stdin())
    assert init_command._stdin_is_tty() is False
