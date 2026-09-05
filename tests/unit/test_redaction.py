"""The pattern half of ``core/redaction.py``.

``core/redaction.py`` now holds two mechanisms, and they answer different
questions:

* the **registry** (``remember_secret`` / ``redact``) blanks values this
  process was handed. Right for *our* credentials.
* the **patterns** (``scrub_credential_patterns``) match the credential
  *shape*. Right for values we never held — and that is the cache's case,
  because ESPN echoes other league members' SWID GUIDs inside roster payloads.
  You cannot register a secret you have never seen.

``tests/unit/test_scrubbing.py`` exercises the patterns through the cassette
hook. These are the direct tests: transitive exercise gives line coverage, but
ADR-0008's mutation gate on ``core/`` needs assertions that bite.

Every credential-shaped string in this file is synthetic.
"""

from __future__ import annotations

import gzip
import json

import pytest

from fantasy_sports.core.redaction import (
    CASSETTE_SWID_SALT,
    CREDENTIAL_PATTERNS,
    CREDENTIAL_PLACEHOLDER,
    SWID_PLACEHOLDER,
    SWID_PSEUDONYM_SENTINEL,
    UnscrubbableResponseError,
    decode_body,
    is_swid_pseudonym,
    new_swid_salt,
    scrub_body,
    scrub_credential_patterns,
    swid_pseudonym,
)

FAKE_SWID = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
FAKE_SWID_BARE = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
OTHER_SWID = "{99887766-5544-3322-1100-AABBCCDDEEFF}"
FAKE_ESPN_S2 = "AEBnotarealcookie0123456789abcdefABCDEF%2Bnotareal%3D%3D"

#: One payload shaped the way ESPN shapes it: the owner list and the member
#: list are joined on the SWID and nothing else. ``members[]`` carries no
#: display name, which is why this join is the only path from a team to a
#: person (the probe on #29).
TWO_TEAM_PAYLOAD = (
    '{"teams": [{"id": 1, "owners": ["' + FAKE_SWID + '"]},'
    ' {"id": 2, "owners": ["' + OTHER_SWID + '"]}],'
    ' "members": [{"id": "' + FAKE_SWID + '"}, {"id": "' + OTHER_SWID + '"}]}'
)


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #


def test_a_brace_wrapped_swid_is_replaced():
    scrubbed = json.loads(scrub_credential_patterns(f'{{"owners": ["{FAKE_SWID}"]}}'))
    assert scrubbed["owners"][0] != FAKE_SWID
    assert is_swid_pseudonym(scrubbed["owners"][0])


def test_a_bare_guid_under_a_swid_key_is_replaced():
    scrubbed = scrub_credential_patterns(f'"swid": "{FAKE_SWID_BARE}"')
    assert FAKE_SWID_BARE not in scrubbed


def test_an_espn_s2_value_is_replaced():
    scrubbed = scrub_credential_patterns(f"Cookie: espn_s2={FAKE_ESPN_S2}")
    assert FAKE_ESPN_S2 not in scrubbed
    assert CREDENTIAL_PLACEHOLDER in scrubbed


def test_scrubbing_is_idempotent():
    """The cache re-scrubs on refresh paths; a second pass must change nothing."""
    once = scrub_credential_patterns(f"espn_s2={FAKE_ESPN_S2}; SWID={FAKE_SWID}")
    assert scrub_credential_patterns(once) == once


def test_scrubbing_a_pseudonym_shaped_body_is_idempotent():
    """The trap the sentinel exists for.

    A pseudonym is a well-formed GUID, so without the reserved first group the
    SWID pattern would match it and a second pass would rewrite it to a
    *different* value — silently, and only on the refresh path.
    """
    once = scrub_credential_patterns(TWO_TEAM_PAYLOAD)
    assert scrub_credential_patterns(once) == once
    assert scrub_credential_patterns(once, swid_salt=new_swid_salt()) == once, (
        "a pseudonym must survive a pass under a different salt, or a cache "
        "refresh would disagree with the rows already in the file"
    )


def test_a_pseudonym_is_not_a_credential_scan_finding():
    """The other half of the sentinel.

    The scrubber and the repo-wide scan share ``CREDENTIAL_PATTERNS``. If a
    pseudonym matched, every already-scrubbed cassette would become a finding
    the moment this landed.
    """
    scrubbed = scrub_credential_patterns(TWO_TEAM_PAYLOAD)
    for name, pattern in CREDENTIAL_PATTERNS.items():
        assert not pattern.search(scrubbed), name
    assert not CREDENTIAL_PATTERNS["SWID GUID"].search(
        swid_pseudonym(FAKE_SWID, salt=CASSETTE_SWID_SALT)
    )


def test_a_ci_secret_expression_is_not_a_finding():
    """``$``, ``{`` and ``}`` are excluded from the value class on purpose."""
    expression = "ESPN_S2: ${{ secrets.ESPN_S2 }}"
    assert scrub_credential_patterns(expression) == expression


def test_ordinary_text_is_left_alone():
    body = '{"teams": [{"id": 1, "abbrev": "WULF"}]}'
    assert scrub_credential_patterns(body) == body


def test_every_named_pattern_stops_matching_after_a_scrub():
    text = f'{{"id": "{FAKE_SWID}", "swid": "{FAKE_SWID_BARE}", "espn_s2": "{FAKE_ESPN_S2}"}}'
    scrubbed = scrub_credential_patterns(text)
    for name, pattern in CREDENTIAL_PATTERNS.items():
        assert not pattern.search(scrubbed), name


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #


def test_decode_body_gunzips():
    assert decode_body(gzip.compress(b'{"ok": true}')) == '{"ok": true}'


def test_decode_body_passes_plain_text_through():
    assert decode_body(b'{"ok": true}') == '{"ok": true}'
    assert decode_body('{"ok": true}') == '{"ok": true}'


def test_decode_body_refuses_bytes_it_cannot_read():
    """Bytes that cannot be read cannot be proven clean."""
    with pytest.raises(UnscrubbableResponseError):
        decode_body(b"\xff\xfe\x00 neither gzip nor utf-8")


def test_decode_body_refuses_a_truncated_gzip_stream():
    with pytest.raises(UnscrubbableResponseError):
        decode_body(gzip.compress(b'{"ok": true}')[:12])


def test_scrub_body_decodes_before_it_scrubs():
    """The gzip trap: scrub-then-decode is a no-op that reports success."""
    compressed = gzip.compress(f'{{"id": "{FAKE_SWID}"}}'.encode())
    assert FAKE_SWID not in scrub_body(compressed)
    assert is_swid_pseudonym(json.loads(scrub_body(compressed))["id"])


def test_scrub_body_forwards_the_salt():
    body = f'{{"id": "{FAKE_SWID}"}}'
    mine, yours = new_swid_salt(), new_swid_salt()
    assert scrub_body(body, swid_salt=mine) != scrub_body(body, swid_salt=yours)


# --------------------------------------------------------------------------- #
# Pseudonyms — the owner-to-member join (jwulff/fantasy-sports#38)
# --------------------------------------------------------------------------- #


def test_the_owner_to_member_join_survives_the_scrub():
    """The bug this exists for.

    ESPN's ``members[]`` carries no display name, so the SWID join is the only
    path from a team to a person. One shared placeholder turns two teams and
    two members into a two-by-two ambiguity.
    """
    payload = json.loads(scrub_credential_patterns(TWO_TEAM_PAYLOAD))
    owners = [team["owners"][0] for team in payload["teams"]]
    members = [member["id"] for member in payload["members"]]

    assert owners[0] != owners[1], "two distinct members collapsed onto one token"
    assert owners == members, "the join key no longer joins"
    assert all(is_swid_pseudonym(value) for value in owners + members)


def test_a_pseudonym_is_stable_across_payloads():
    """Stability across payloads is what a counter cannot give.

    An encounter-order counter is stable within one payload and wrong between
    two recorded in different request orders.
    """
    first = scrub_credential_patterns(f'{{"a": "{FAKE_SWID}", "b": "{OTHER_SWID}"}}')
    second = scrub_credential_patterns(f'{{"a": "{OTHER_SWID}", "b": "{FAKE_SWID}"}}')

    assert json.loads(first)["a"] == json.loads(second)["b"]
    assert json.loads(first)["b"] == json.loads(second)["a"]


def test_a_pseudonym_ignores_the_case_of_the_guid_it_came_from():
    assert swid_pseudonym(FAKE_SWID.lower(), salt=CASSETTE_SWID_SALT) == swid_pseudonym(
        FAKE_SWID, salt=CASSETTE_SWID_SALT
    )
    assert swid_pseudonym(FAKE_SWID_BARE, salt=CASSETTE_SWID_SALT) == swid_pseudonym(
        FAKE_SWID, salt=CASSETTE_SWID_SALT
    )


def test_a_pseudonym_keeps_guid_shape_under_the_reserved_sentinel():
    pseudonym = swid_pseudonym(FAKE_SWID, salt=CASSETTE_SWID_SALT)
    assert pseudonym.startswith("{" + SWID_PSEUDONYM_SENTINEL + "-")
    assert len(pseudonym) == len(FAKE_SWID)
    assert is_swid_pseudonym(pseudonym)


def test_the_salt_changes_the_pseudonym():
    """The cache's random per-store salt has to actually buy something."""
    assert swid_pseudonym(FAKE_SWID, salt="one") != swid_pseudonym(FAKE_SWID, salt="two")


def test_the_cassette_salt_is_deterministic():
    """A committed fixture must re-record byte-identically, so this cannot move."""
    assert swid_pseudonym(FAKE_SWID, salt=CASSETTE_SWID_SALT) == swid_pseudonym(
        FAKE_SWID, salt=CASSETTE_SWID_SALT
    )
    assert scrub_credential_patterns(TWO_TEAM_PAYLOAD) == scrub_credential_patterns(
        TWO_TEAM_PAYLOAD
    )


def test_new_swid_salt_is_random():
    assert new_swid_salt() != new_swid_salt()


def test_is_swid_pseudonym_rejects_a_real_guid():
    assert not is_swid_pseudonym(FAKE_SWID)
    assert not is_swid_pseudonym(SWID_PLACEHOLDER)
    assert not is_swid_pseudonym("")
