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

import pytest

from fantasy_sports.core.redaction import (
    CREDENTIAL_PATTERNS,
    CREDENTIAL_PLACEHOLDER,
    SWID_PLACEHOLDER,
    UnscrubbableResponseError,
    decode_body,
    scrub_body,
    scrub_credential_patterns,
)

FAKE_SWID = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
FAKE_SWID_BARE = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
FAKE_ESPN_S2 = "AEBnotarealcookie0123456789abcdefABCDEF%2Bnotareal%3D%3D"


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #


def test_a_brace_wrapped_swid_is_replaced():
    scrubbed = scrub_credential_patterns(f'{{"owners": ["{FAKE_SWID}"]}}')
    assert FAKE_SWID not in scrubbed
    assert SWID_PLACEHOLDER in scrubbed


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
    assert SWID_PLACEHOLDER in scrub_body(compressed)
