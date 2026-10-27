"""The adapter's browser re-import retry (jwulff/fantasy-sports#159).

A transport that gates on the cookie it is sent stands in for ESPN, which is
the whole point: the trigger is "the browser holds a different pair than the one
we sent", and the only honest way to test that is a server whose answer depends
on the cookie. The 401 body is ESPN's real, constant one, so nothing here lets
the classification depend on a reason type
(``docs/memory/espn-401-tells-you-nothing.md``).

Nothing touches the real Keychain, a real browser, or the network. The
browser read is a fake ``reimport`` or a patched ``find_espn_cookies``; the
platform is never consulted, so CI's Linux sees the same thing as a Mac.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    FAKE_S2,
    FAKE_SWID,
    LEAGUE_ID,
    SEASON,
    FakeResponse,
    RecordedEspn,
    WritableEspn,
    install_writable_espn,
    isolate_home,
)

from fantasy_sports.auth import reimport
from fantasy_sports.auth.browser import BrowserSearch, ProfileCookies
from fantasy_sports.auth.chain import (
    CredentialSet,
    CredentialSource,
    ResolvedCredential,
    Secret,
)
from fantasy_sports.commands import REGISTRY
from fantasy_sports.core.errors import AuthExpiredError, AuthMissingError, LeagueNotFoundError
from fantasy_sports.core.journal import Journal
from fantasy_sports.core.models import League
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.providers.espn import AUTH_REASONS, AuthReason, EspnProvider

STALE_S2 = "AEB" + "s" * 200
FRESH_S2 = FAKE_S2
SWID = FAKE_SWID
WHY = "Johnson is out for the season; Liskov starts this week (sanity-gate: PROCEED)"
OTHER_SWID = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"
SECRETS = (STALE_S2, FRESH_S2, SWID)


@pytest.fixture(autouse=True)
def _clean_scrub_registry() -> Iterator[None]:
    yield
    forget_secrets()


def _denied() -> dict[str, Any]:
    """ESPN's real, constant 401 body (probed 2026-09-05)."""
    return {
        "messages": ["You are not authorized to view this League."],
        "details": [{"type": "AUTH_LEAGUE_NOT_VISIBLE", "message": "nope"}],
    }


class Gate:
    """Serve ``inner`` only to ``good``; answer everyone else with the real 401."""

    def __init__(self, inner: Any, good: str) -> None:
        self.inner = inner
        self.good = good
        self.sent: list[str | None] = []

    def __call__(self, url: str, params=None, headers=None, cookies=None) -> Any:
        s2 = (cookies or {}).get("espn_s2")
        self.sent.append(s2)
        if s2 != self.good:
            return FakeResponse(_denied(), 401)
        return self.inner(url, params=params, headers=headers, cookies=cookies)


@pytest.fixture
def alternate_probe_fails(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``espn-api``'s own alternate-shape retry, which the transport never sees."""
    from espn_api.requests import espn_requests

    attempted: list[str] = []

    class _Requests:
        @staticmethod
        def get(url, params=None, headers=None, cookies=None):
            attempted.append(url)
            return FakeResponse(_denied(), 401)

    monkeypatch.setattr(espn_requests, "requests", _Requests)
    return attempted


def stored(source: CredentialSource = CredentialSource.KEYCHAIN, s2: str = STALE_S2):
    values = {"espn_s2": s2, "swid": SWID}
    return CredentialSet(
        resolved={
            name: ResolvedCredential(name=name, source=source, secret=Secret(value))
            for name, value in values.items()
        }
    )


class Reimporter:
    """A fake ``auth.reimport.attempt``: records its calls, returns a canned pair."""

    def __init__(self, result: reimport.Reimported | None) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    def __call__(self, sent: Any, credential_set: Any) -> reimport.Reimported | None:
        self.calls.append(dict(sent))
        return self.result


class Persister:
    """A fake ``auth.reimport.commit``: records what ESPN accepted and was stored."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[reimport.Reimported] = []

    def __call__(self, found: reimport.Reimported) -> bool:
        self.calls.append(found)
        return self.ok


def fresh_pair() -> reimport.Reimported:
    return reimport.Reimported(
        browser="Chrome", profile="Default", espn_s2=Secret(FRESH_S2), swid=Secret(SWID)
    )


def provider(
    good: str,
    reimporter: Any,
    *,
    persist: Persister | None = None,
    source=CredentialSource.KEYCHAIN,
    **served: Any,
):
    gate = Gate(RecordedEspn(overrides=served or None), good)
    persist = Persister() if persist is None else persist
    espn = EspnProvider(stored(source), http=gate, reimport=reimporter, persist=persist)
    return espn, gate


def assert_no_secret(value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text


# --------------------------------------------------------------------------- #
# The retry
# --------------------------------------------------------------------------- #


def test_a_different_browser_pair_is_saved_and_the_read_retried_once(
    alternate_probe_fails: list[str],
):
    reimporter, persist = Reimporter(fresh_pair()), Persister()
    espn, gate = provider(FRESH_S2, reimporter, persist=persist)

    league = espn.fetch_league(LEAGUE_ID, SEASON)

    assert isinstance(league, League)
    assert len(reimporter.calls) == 1
    # Saved once, and only after ESPN accepted it.
    [saved] = persist.calls
    assert saved.source == {"browser": "Chrome", "profile": "Default"}
    assert reimporter.calls[0] == {"espn_s2": STALE_S2, "swid": SWID}
    # The first request went out stale; every later one fresh.
    assert gate.sent[0] == STALE_S2 and set(gate.sent[1:]) == {FRESH_S2}
    assert len(alternate_probe_fails) == 1, "the library's own probe ran once, before the retry"


def test_success_after_a_reimport_carries_one_warning_naming_the_profile_and_no_value(
    alternate_probe_fails: list[str],
):
    espn, _ = provider(FRESH_S2, Reimporter(fresh_pair()))
    espn.fetch_league(LEAGUE_ID, SEASON)
    espn.fetch_teams(LEAGUE_ID, SEASON)

    [notice] = espn.warnings
    assert "Chrome/Default" in notice and "saved to the Keychain" in notice
    assert_no_secret(notice)


def test_a_pair_that_works_but_cannot_be_saved_says_so_and_claims_no_stored_age(
    alternate_probe_fails: list[str],
):
    espn, _ = provider(FRESH_S2, Reimporter(fresh_pair()), persist=Persister(ok=False))
    espn.fetch_league(LEAGUE_ID, SEASON)

    [notice] = espn.warnings
    assert "could not be saved" in notice
    assert espn.credential_report()["espn_s2_age_seconds"] is None
    assert_no_secret(notice)


def test_the_credential_age_block_reads_the_fresh_keychain_entry(
    alternate_probe_fails: list[str], monkeypatch: pytest.MonkeyPatch
):
    """After the swap the age is attributed to the Keychain copy just stored."""
    seen: list[CredentialSource] = []
    from fantasy_sports.providers import espn as module

    def spy(credential_set, **kwargs):
        seen.append(credential_set.resolved["espn_s2"].source)
        return {"espn_s2_age_seconds": 0, "warning": None}

    monkeypatch.setattr(module, "_credentials_block", spy)
    espn, _ = provider(FRESH_S2, Reimporter(fresh_pair()), source=CredentialSource.CONFIG)
    espn.fetch_league(LEAGUE_ID, SEASON)
    espn.credential_report()
    assert seen == [CredentialSource.KEYCHAIN]
    assert espn._credentials["espn_s2"] == FRESH_S2


def test_fetch_raw_is_retried_too(alternate_probe_fails: list[str]):
    reimporter = Reimporter(fresh_pair())
    espn, gate = provider(FRESH_S2, reimporter, mSettings={"settings": {"name": "x"}})
    payload = espn.fetch_raw(LEAGUE_ID, SEASON, view="mSettings")
    assert payload == {"settings": {"name": "x"}}
    assert len(reimporter.calls) == 1
    assert gate.sent[0] == STALE_S2 and gate.sent[-1] == FRESH_S2
    assert espn.warnings


def test_the_same_pair_or_no_browser_pair_raises_the_original_error_unchanged(
    alternate_probe_fails: list[str],
):
    reimporter, persist = Reimporter(None), Persister()
    espn, gate = provider(FRESH_S2, reimporter, persist=persist)

    with pytest.raises(LeagueNotFoundError) as err:
        espn.fetch_league(LEAGUE_ID, SEASON)

    assert len(reimporter.calls) == 1 and persist.calls == []
    assert gate.sent == [STALE_S2], "no retry was sent"
    assert "reimported_from_browser" not in err.value.details
    assert err.value.details["reason"] == "credentials_or_membership"
    assert espn.warnings == []


def test_a_browser_with_nothing_to_offer_is_asked_once_not_once_per_refusal(
    alternate_probe_fails: list[str],
):
    reimporter = Reimporter(None)
    espn, _ = provider(FRESH_S2, reimporter)
    for _ in range(2):
        with pytest.raises(LeagueNotFoundError):
            espn.fetch_league(LEAGUE_ID, SEASON)
    assert len(reimporter.calls) == 1


def test_credentials_without_a_chain_record_still_swap_when_a_reimporter_is_injected(
    alternate_probe_fails: list[str],
):
    gate = Gate(RecordedEspn(), FRESH_S2)
    espn = EspnProvider(
        {"espn_s2": STALE_S2, "swid": SWID},
        http=gate,
        reimport=Reimporter(fresh_pair()),
        persist=Persister(),
    )
    assert isinstance(espn.fetch_league(LEAGUE_ID, SEASON), League)
    assert espn.credential_report()["espn_s2_age_seconds"] is None


def test_a_second_refusal_carries_reimported_from_browser_and_never_asks_again(
    alternate_probe_fails: list[str],
):
    reimporter = Reimporter(fresh_pair())
    persist = Persister()
    espn, gate = provider("a cookie nobody holds", reimporter, persist=persist)

    with pytest.raises(LeagueNotFoundError) as err:
        espn.fetch_league(LEAGUE_ID, SEASON)

    assert persist.calls == [], "a pair ESPN refused is never stored"
    assert espn._credentials["espn_s2"] == STALE_S2, "and the sent pair is back in use"
    assert (LEAGUE_ID, SEASON) not in espn._leagues, "the half-built league was not kept"

    assert err.value.to_dict()["code"] == "LEAGUE_NOT_FOUND", "the classification is unchanged"
    assert err.value.details["reimported_from_browser"] == {
        "browser": "Chrome",
        "profile": "Default",
    }
    assert [s for s in gate.sent if s == FRESH_S2], "the fresh pair really was tried"
    assert espn.warnings == [], "a failed retry is not a success to announce"
    assert_no_secret(err.value.to_dict())

    # Once per process: a second read refuses again without another import.
    with pytest.raises(LeagueNotFoundError) as again:
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert len(reimporter.calls) == 1
    assert again.value.details["reimported_from_browser"]["browser"] == "Chrome"


def test_an_auth_expired_refusal_triggers_it_too(
    alternate_probe_fails: list[str], monkeypatch: pytest.MonkeyPatch
):
    """The branch that exists for a future reason type, same trigger as LEAGUE_NOT_FOUND."""
    monkeypatch.setitem(
        AUTH_REASONS,
        "AUTH_LEAGUE_NOT_VISIBLE",
        AuthReason(explanation="Pretend ESPN told us.", proves_credentials_bad=True),
    )
    reimporter = Reimporter(fresh_pair())

    espn, _ = provider(FRESH_S2, reimporter)
    assert isinstance(espn.fetch_league(LEAGUE_ID, SEASON), League)
    assert len(reimporter.calls) == 1

    refused, _ = provider("nobody", Reimporter(fresh_pair()))
    with pytest.raises(AuthExpiredError) as err:
        refused.fetch_league(LEAGUE_ID, SEASON)
    assert err.value.details["reimported_from_browser"]["profile"] == "Default"


def test_no_credentials_means_auth_missing_and_the_browser_is_not_consulted(
    alternate_probe_fails: list[str],
):
    reimporter = Reimporter(fresh_pair())
    espn = EspnProvider(None, http=Gate(RecordedEspn(), FRESH_S2), reimport=reimporter)
    with pytest.raises(AuthMissingError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert reimporter.calls == []


def test_a_404_is_not_a_trigger(monkeypatch: pytest.MonkeyPatch):
    """Only the 401 path: a 404 is a different question (``_not_found``)."""
    from espn_api.requests import espn_requests

    class _Requests:
        @staticmethod
        def get(*args: Any, **kwargs: Any):
            return FakeResponse({}, 404)

    monkeypatch.setattr(espn_requests, "requests", _Requests)
    reimporter = Reimporter(fresh_pair())
    espn = EspnProvider(stored(), http=lambda *a, **k: FakeResponse({}, 404), reimport=reimporter)
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert reimporter.calls == []


# --------------------------------------------------------------------------- #
# Through the real module: who may trigger it, and the opt-outs
# --------------------------------------------------------------------------- #


class Reads(list):
    """Browser reads so far; ``writes`` are the Keychain writes ``commit`` made."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, str]] = []


@pytest.fixture
def browser_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Reads:
    """Patch the browser and the Keychain write; count browser reads."""
    from fantasy_sports.auth import browser, chain, staleness

    reads = Reads()
    pair = ProfileCookies(
        browser="Chrome",
        profile="Default",
        espn_s2=Secret(FRESH_S2),
        swid=Secret(SWID),
    )

    def find(*args: Any, **kwargs: Any) -> BrowserSearch:
        reads.append("read")
        return BrowserSearch(profiles=(pair,))

    monkeypatch.setattr(browser, "find_espn_cookies", find)
    monkeypatch.setattr(chain, "write_to_keychain", lambda name, v: reads.writes.append((name, v)))
    monkeypatch.setattr(chain, "read_from_keychain", lambda spec: None)
    monkeypatch.setattr(chain, "delete_from_keychain", lambda name: False)
    monkeypatch.setattr(staleness, "record_stored", lambda *a, **k: None)
    for name in ("XDG_CONFIG_HOME", "FANTASY_SPORTS_NO_BROWSER_REIMPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return reads


def real_module_provider(source: CredentialSource):
    gate = Gate(RecordedEspn(), FRESH_S2)
    return EspnProvider(stored(source), http=gate), gate


def test_the_real_module_retries_a_keychain_credential(
    alternate_probe_fails: list[str], browser_reads: Reads
):
    espn, _ = real_module_provider(CredentialSource.KEYCHAIN)
    assert isinstance(espn.fetch_league(LEAGUE_ID, SEASON), League)
    assert browser_reads == ["read"]
    assert browser_reads.writes == [("espn_s2", FRESH_S2), ("swid", SWID)]


def test_the_real_module_stores_nothing_when_the_browser_pair_is_refused_too(
    alternate_probe_fails: list[str], browser_reads: Reads
):
    espn = EspnProvider(stored(), http=Gate(RecordedEspn(), "a cookie nobody holds"))
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert browser_reads == ["read"] and browser_reads.writes == []


def test_environment_credentials_never_trigger_it(
    alternate_probe_fails: list[str], browser_reads: Reads
):
    espn, gate = real_module_provider(CredentialSource.ENV)
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert browser_reads == [] and gate.sent == [STALE_S2]


def test_a_plain_mapping_of_credentials_never_triggers_it(
    alternate_probe_fails: list[str], browser_reads: Reads
):
    espn = EspnProvider({"espn_s2": STALE_S2, "swid": SWID}, http=Gate(RecordedEspn(), FRESH_S2))
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert browser_reads == []


def test_the_environment_opt_out_is_honoured(
    alternate_probe_fails: list[str], browser_reads: Reads, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FANTASY_SPORTS_NO_BROWSER_REIMPORT", "1")
    espn, _ = real_module_provider(CredentialSource.KEYCHAIN)
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert browser_reads == []


def test_the_config_opt_out_is_honoured(
    alternate_probe_fails: list[str], browser_reads: Reads, tmp_path: Path
):
    config = tmp_path / "config" / "fantasy-sports"
    config.mkdir(parents=True)
    (config / "config.toml").write_text("[auth]\nauto_reimport = false\n", encoding="utf-8")
    espn, _ = real_module_provider(CredentialSource.CONFIG)
    with pytest.raises(LeagueNotFoundError):
        espn.fetch_league(LEAGUE_ID, SEASON)
    assert browser_reads == []


# --------------------------------------------------------------------------- #
# The cache: a cached bootstrap must not hide a refusal on a later live request
# --------------------------------------------------------------------------- #


def test_a_cached_bootstrap_does_not_hide_a_refusal_on_a_later_live_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alternate_probe_fails: list[str]
):
    """Cache entries are credential-independent, so the bootstrap can come from the
    store while the stale cookie is first used by a live view (here ``mNav``)."""
    from fantasy_sports.cache.store import CacheStore

    isolate_home(tmp_path, monkeypatch)
    store = CacheStore()
    nav = {"members": [], "teams": []}
    warm = EspnProvider(stored(s2=FRESH_S2), http=RecordedEspn(), cache=store)
    warm.fetch_league(LEAGUE_ID, SEASON)

    gate = Gate(RecordedEspn(overrides={"mNav": nav}), FRESH_S2)
    persist = Persister()
    espn = EspnProvider(
        stored(), http=gate, cache=store, reimport=Reimporter(fresh_pair()), persist=persist
    )
    assert isinstance(espn.fetch_league(LEAGUE_ID, SEASON), League)
    assert gate.sent == [], "the whole bootstrap came from the cache"

    espn._league(LEAGUE_ID, SEASON).espn_request.league_get(params={"view": "mNav"})

    assert gate.sent[0] == STALE_S2 and gate.sent[-1] == FRESH_S2
    assert len(persist.calls) == 1 and len(espn.warnings) == 1


def test_a_refused_replacement_does_not_stay_installed_on_the_request(
    alternate_probe_fails: list[str],
):
    espn, _ = provider("a cookie nobody holds", Reimporter(fresh_pair()), mSettings={"ok": True})
    from espn_api.requests.espn_requests import ESPNAccessDenied

    with espn._raw_league(LEAGUE_ID, SEASON) as (request, _transport):
        with pytest.raises(ESPNAccessDenied):
            request.league_get(params={"view": "mSettings"})
        assert request.cookies["espn_s2"] == STALE_S2
        assert request.cookies["SWID"] == SWID


def test_a_transport_built_before_the_swap_reuses_the_pair_now_in_force(
    alternate_probe_fails: list[str],
):
    """No second browser read, no second save: the other league's transport just retries."""
    reimporter, persist = Reimporter(fresh_pair()), Persister()
    espn, gate = provider(FRESH_S2, reimporter, persist=persist, mSettings={"ok": True})
    espn.fetch_league(LEAGUE_ID, SEASON)

    stale_request = espn._transport(LEAGUE_ID, SEASON)._request
    stale_request.cookies = {"espn_s2": STALE_S2, "SWID": SWID}
    stale_request.league_get(params={"view": "mSettings"})

    assert len(reimporter.calls) == 1 and len(persist.calls) == 1
    assert stale_request.cookies["espn_s2"] == FRESH_S2


# --------------------------------------------------------------------------- #
# Writes: one journal entry, one accepted POST
# --------------------------------------------------------------------------- #


class World:
    """A keychain with a stale ``espn_s2`` and a browser with the fresh one."""

    def __init__(self, espn: WritableEspn, reads: Gate | None, posts: list[str | None]) -> None:
        self.espn = espn
        self.reads = reads
        self.posts = posts


def stale_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_reads: bool,
    gate_posts: bool,
    browser_swid: str = SWID,
) -> World:
    import requests

    from fantasy_sports.auth import browser, chain

    isolate_home(tmp_path, monkeypatch)
    for name in ("FANTASY_SPORTS_ESPN_S2", "FANTASY_SPORTS_SWID"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        chain,
        "read_from_keychain",
        lambda spec: {"espn_s2": STALE_S2, "swid": SWID}.get(spec.name),
    )
    pair = ProfileCookies(
        browser="Chrome", profile="Default", espn_s2=Secret(FRESH_S2), swid=Secret(browser_swid)
    )
    monkeypatch.setattr(
        browser, "find_espn_cookies", lambda *a, **k: BrowserSearch(profiles=(pair,))
    )
    espn = install_writable_espn(monkeypatch)
    gate = None
    if gate_reads:
        gate = Gate(espn, FRESH_S2)
        monkeypatch.setattr(requests, "get", gate)
    posts: list[str | None] = []
    if gate_posts:

        def post(url: str, **kwargs: Any) -> Any:
            posts.append(kwargs["cookies"].get("espn_s2"))
            if posts[-1] != FRESH_S2:
                return FakeResponse(_denied(), 401)
            return espn.post(url, **kwargs)

        monkeypatch.setattr(requests, "post", post)
    return World(espn, gate, posts)


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from fantasy_sports.auth import chain, staleness

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(chain, "write_to_keychain", lambda name, v: writes.append((name, v)))
    monkeypatch.setattr(staleness, "record_stored", lambda *a, **k: None)
    return writes


def add() -> dict[str, Any]:
    envelope = REGISTRY["add"].resolve()(
        team="1", player="Barbara Liskov", acknowledge_irreversible=WHY
    )
    return envelope.to_dict()


def test_a_write_whose_preflight_read_was_retried_is_journaled_and_sent_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
):
    world = stale_world(tmp_path, monkeypatch, gate_reads=True, gate_posts=False)

    payload = add()

    assert payload["error"] is None
    assert len(world.espn.posts) == 1, "the write was sent exactly once"
    with Journal.open() as journal:
        entries = journal.list(limit=10)
    assert len(entries) == 1, "the retry did not duplicate the journal entry"
    assert entries[0].status == "executed"
    assert payload["data"]["journal_entry_id"] == entries[0].id
    assert written == [("espn_s2", FRESH_S2), ("swid", SWID)]
    assert world.reads is not None
    assert world.reads.sent[0] == STALE_S2 and world.reads.sent[-1] == FRESH_S2


def test_another_accounts_browser_pair_is_never_adopted_even_for_the_preflight_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
):
    """Ownership comes from the SWID, so a pair for another one must not slip in
    through the read that precedes the write and let it execute as that account."""
    world = stale_world(
        tmp_path, monkeypatch, gate_reads=True, gate_posts=True, browser_swid=OTHER_SWID
    )

    with pytest.raises(LeagueNotFoundError):
        add()

    assert world.posts == [] and world.espn.posts == [], "no POST was ever made"
    assert written == []
    with Journal.open() as journal:
        assert journal.list(limit=10) == []


def test_a_write_401_saves_a_fresh_same_account_pair_but_sends_nothing_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
):
    """Reads pass (the cache can see to that); the POST is refused. No retry could
    prove the pair, but the next invocation needs it, so it is saved now."""
    from fantasy_sports.core.errors import WriteRejectedError

    world = stale_world(tmp_path, monkeypatch, gate_reads=False, gate_posts=True)

    with pytest.raises(WriteRejectedError) as err:
        add()

    payload = err.value.to_dict()
    assert payload["code"] == "WRITE_REJECTED" and payload["details"]["kind"] == "auth"
    assert payload["details"]["reimported_from_browser"] == {
        "browser": "Chrome",
        "profile": "Default",
    }
    assert "Chrome/Default" in payload["remediation"]
    assert "Nothing was sent twice" in payload["remediation"]
    assert_no_secret(payload)
    assert world.posts == [STALE_S2], "exactly one POST, and it never carried the new pair"
    assert world.espn.posts == []
    assert written == [("espn_s2", FRESH_S2), ("swid", SWID)]
    with Journal.open() as journal:
        [entry] = journal.list(limit=10)
    assert entry.status == "rejected"


def test_a_write_401_after_a_preflight_reimport_says_the_fresh_pair_was_refused_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
):
    import requests

    from fantasy_sports.core.errors import WriteRejectedError

    world = stale_world(tmp_path, monkeypatch, gate_reads=True, gate_posts=False)
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(_denied(), 401))

    with pytest.raises(WriteRejectedError) as err:
        add()

    payload = err.value.to_dict()
    assert payload["details"]["reimported_from_browser"]["browser"] == "Chrome"
    assert "Nothing was sent twice" not in payload["remediation"]
    assert world.espn.posts == []
    assert written == [("espn_s2", FRESH_S2), ("swid", SWID)], "saved once, by the read"


def test_a_write_401_with_no_fresh_pair_is_exactly_the_error_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
):
    from fantasy_sports.core.errors import WriteRejectedError

    world = stale_world(
        tmp_path, monkeypatch, gate_reads=False, gate_posts=True, browser_swid=OTHER_SWID
    )

    with pytest.raises(WriteRejectedError) as err:
        add()

    payload = err.value.to_dict()
    assert "reimported_from_browser" not in payload["details"]
    assert "Nothing was sent twice" not in payload["remediation"]
    assert world.posts == [STALE_S2] and written == []


def test_a_write_401_is_not_imported_when_the_provider_finder_offers_another_account():
    """Defence in depth: the SWID is re-checked even for an injected finder."""
    from fantasy_sports.core.errors import WriteRejectedError
    from fantasy_sports.providers.espn import _Write

    stranger = reimport.Reimported(
        browser="Chrome", profile="Other", espn_s2=Secret(FRESH_S2), swid=Secret(OTHER_SWID)
    )
    persist = Persister()
    espn = EspnProvider(
        stored(),
        http=RecordedEspn(),
        http_post=lambda url, **kw: FakeResponse(_denied(), 401),
        reimport=Reimporter(stranger),
        persist=persist,
    )
    write = _Write(league_id=LEAGUE_ID, season=SEASON, team_id=1, scoring_period=1, credentials={})
    with pytest.raises(WriteRejectedError) as err:
        espn._post(write, {"type": "ROSTER", "executionType": "EXECUTE"})
    assert persist.calls == [] and "reimported_from_browser" not in err.value.details


def test_a_write_401_whose_save_fails_reports_no_import():
    from fantasy_sports.core.errors import WriteRejectedError
    from fantasy_sports.providers.espn import _Write

    espn = EspnProvider(
        stored(),
        http=RecordedEspn(),
        http_post=lambda url, **kw: FakeResponse(_denied(), 401),
        reimport=Reimporter(fresh_pair()),
        persist=Persister(ok=False),
    )
    write = _Write(league_id=LEAGUE_ID, season=SEASON, team_id=1, scoring_period=1, credentials={})
    with pytest.raises(WriteRejectedError) as err:
        espn._post(write, {"type": "ROSTER", "executionType": "EXECUTE"})
    assert "reimported_from_browser" not in err.value.details
    assert espn._credentials["espn_s2"] == STALE_S2


def test_a_non_401_write_answer_never_consults_the_browser():
    from fantasy_sports.core.errors import FantasySportsError
    from fantasy_sports.providers.espn import _Write

    reimporter = Reimporter(fresh_pair())
    espn = EspnProvider(
        stored(),
        http=RecordedEspn(),
        http_post=lambda url, **kw: FakeResponse({"details": [{"type": "TRAN_X"}]}, 409),
        reimport=reimporter,
        persist=Persister(),
    )
    write = _Write(league_id=LEAGUE_ID, season=SEASON, team_id=1, scoring_period=1, credentials={})
    with contextlib.suppress(FantasySportsError):
        espn._post(write, {"type": "ROSTER", "executionType": "EXECUTE"})
    assert reimporter.calls == []


def test_the_command_envelope_and_stderr_carry_the_warning_and_no_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alternate_probe_fails: list[str],
    written: list[tuple[str, str]],
    capsys: pytest.CaptureFixture[str],
):
    from fantasy_sports.cli.app import run

    stale_world(tmp_path, monkeypatch, gate_reads=True, gate_posts=False)

    assert run(["teams"]) == 0
    captured = capsys.readouterr()

    body = json.loads(captured.out)
    [notice] = body["warnings"]
    assert "Chrome/Default" in notice and "saved to the Keychain" in notice
    assert f"warning: {notice}" in captured.err
    for secret in SECRETS:
        assert secret not in captured.out and secret not in captured.err
