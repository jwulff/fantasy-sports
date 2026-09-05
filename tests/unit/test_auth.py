"""Credential resolution, SWID repair, and staleness reporting (#5).

The redaction tests come first on purpose. Rule 5 in ``CLAUDE.md`` — never log,
print, or record a credential — is a security control, and a control that is
only asserted by *not* printing something is not asserted at all. Every
redaction test below renders a real string (a repr, a ``str(exc)``, a formatted
traceback, an error payload, the on-disk state file) and searches it for a
sentinel secret value.
"""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta

import pytest

from fantasy_sports.auth import chain, staleness

# A sentinel that cannot occur by accident anywhere in rendered output.
SECRET = "AEBqp7SENTINELs2cookievalue0123456789abcdefXYZ%2Fzz"
SWID_BARE = "1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D"
SWID_BRACED = "{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}"


@pytest.fixture(autouse=True)
def _forget_secrets():
    """Keep the process-wide scrub set from leaking between tests."""
    chain.forget_secrets()
    yield
    chain.forget_secrets()


# ---------------------------------------------------------------------------
# Redaction — the security control
# ---------------------------------------------------------------------------


def test_secret_never_renders_its_value():
    secret = chain.Secret(SECRET)
    rendered = [repr(secret), str(secret), f"{secret}", f"{secret!r}", format(secret)]
    for text in rendered:
        assert SECRET not in text
        assert chain.REDACTED in text


def test_secret_still_reveals_on_demand():
    assert chain.Secret(SECRET).reveal() == SECRET


def test_resolved_credential_repr_is_redacted():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    assert SECRET not in repr(resolved)
    assert SECRET not in str(resolved)


def test_credential_set_repr_is_redacted():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    credentials = chain.CredentialSet(resolved={"espn_s2": resolved}, missing=("swid",))
    assert SECRET not in repr(credentials)


def test_error_message_is_scrubbed_even_when_a_caller_interpolates_a_secret():
    """The failure mode this defends against is a *caller* mistake.

    Someone writes ``raise AuthError(f"bad cookie {value}")`` in a future unit.
    The value never reaches ``args``, ``str()``, or the payload.
    """
    chain.Secret(SECRET)  # registers the value with the scrubber
    err = chain.AuthError(f"rejected cookie {SECRET} for espn_s2")

    assert SECRET not in str(err)
    assert SECRET not in repr(err)
    assert SECRET not in "".join(str(a) for a in err.args)
    assert SECRET not in json.dumps(err.to_payload())
    assert chain.REDACTED in str(err)


def test_secret_is_absent_from_a_rendered_traceback():
    chain.Secret(SECRET)
    try:
        raise chain.AuthError(f"boom {SECRET}")
    except chain.AuthError as exc:
        rendered = "".join(traceback.format_exception(exc))
    assert SECRET not in rendered


def test_secret_is_absent_from_a_traceback_that_captures_local_variables():
    """``capture_locals=True`` renders every local by ``repr``.

    This is the strongest form of the claim: a raw ``str`` credential held in a
    frame *would* appear here, and a :class:`~fantasy_sports.auth.chain.Secret`
    does not. It is why credentials are carried in a wrapper rather than as
    plain strings.
    """

    def inner() -> None:
        held = chain.Secret(SECRET)  # noqa: F841 — deliberately live in the frame
        raise chain.AuthError("resolution failed")

    try:
        inner()
    except chain.AuthError as exc:
        rendered = "".join(
            traceback.TracebackException.from_exception(exc, capture_locals=True).format()
        )

    assert SECRET not in rendered
    assert chain.REDACTED in rendered


def test_a_raw_string_credential_would_have_leaked():
    """Control case: proves the previous test is measuring something real."""

    def inner() -> None:
        held = SECRET  # noqa: F841
        raise RuntimeError("resolution failed")

    try:
        inner()
    except RuntimeError as exc:
        rendered = "".join(
            traceback.TracebackException.from_exception(exc, capture_locals=True).format()
        )

    assert SECRET in rendered


def test_redact_scrubs_arbitrary_text_such_as_a_request_url():
    chain.Secret(SECRET)
    url = f"https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET}&swid={SWID_BRACED}"
    chain.Secret(SWID_BRACED)
    scrubbed = chain.redact(url)
    assert SECRET not in scrubbed
    assert SWID_BRACED not in scrubbed
    assert "fantasy.espn.com" in scrubbed


def test_redact_ignores_values_too_short_to_scrub_safely():
    """Scrubbing a 3-character value would corrupt unrelated text."""
    chain.Secret("abc")
    assert chain.redact("abcdef") == "abcdef"


def test_redact_is_a_no_op_when_nothing_is_registered():
    assert chain.redact("nothing to see") == "nothing to see"


def test_rejected_swid_error_does_not_echo_the_value():
    bad = "NOTAGUID" + SECRET
    with pytest.raises(chain.AuthError) as excinfo:
        chain.normalize_swid(bad)
    rendered = str(excinfo.value) + json.dumps(excinfo.value.to_payload())
    assert bad not in rendered
    assert SECRET not in rendered


def test_auth_status_payload_contains_no_credential_values():
    resolved = chain.ResolvedCredential(
        name="espn_s2", source=chain.CredentialSource.ENV, secret=chain.Secret(SECRET)
    )
    credentials = chain.CredentialSet(resolved={"espn_s2": resolved}, missing=("swid",))
    status = staleness.build_auth_status(credentials, environ={})
    assert SECRET not in json.dumps(status.to_payload())


def test_state_file_on_disk_never_contains_a_credential_value(tmp_path):
    path = tmp_path / "auth-state.json"
    chain.save_credentials(
        {"espn_s2": SECRET, "swid": SWID_BARE},
        writer=lambda name, value: None,
        state_path=path,
    )
    assert SECRET not in path.read_text()
    assert SWID_BARE not in path.read_text()
