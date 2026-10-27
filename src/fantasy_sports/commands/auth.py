"""``auth status``, ``auth login``, and ``auth logout`` — projections over ``auth/``.

All three are thin on purpose. The credential chain, the staleness heuristic,
the SWID normalizer, and the Keychain write all landed with
jwulff/fantasy-sports#5, and the removal walk with jwulff/fantasy-sports#62;
nothing here re-implements any of it. What this module adds is the envelope
around the result and the one rule those functions cannot enforce on their
own: **no credential value ever reaches an output stream.**

Three consequences of that rule are visible below.

* ``login`` reads values with :func:`getpass.getpass`, so a value is not echoed
  to the terminal and does not land in shell history.
* Values are never accepted as command-line options. An argument is visible in
  ``ps``, in shell history, and in any process listing a co-tenant can read;
  making that impossible is worth more than the convenience of scripting a
  cookie paste.
* The envelope reports credential *names* — stored, repaired, removed, still
  set — and never a value, a length, or a prefix.

``auth status`` never contacts ESPN. It reports what is configured, where it
came from, and how old it is; it cannot report whether ESPN still accepts it,
because ESPN answers 401 identically for an expired cookie, a malformed one,
and a league the account was never in
(``docs/memory/espn-401-tells-you-nothing.md``).

All three cover every credential in ``auth.chain.ALL_CREDENTIALS``: ESPN's two
cookies and the optional FantasyPros API key (ADR-0011). The key is optional
*to this tool*, so ``status`` reports it with ``required: false`` and leaves
``complete`` to the cookies, ``login`` lets its prompt be skipped with Enter,
and ``logout`` clears it wherever it is stored.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from fantasy_sports.commands.context import require_shape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["login", "logout", "status"]

PROVIDER = "espn"
"""The only provider v0.1 ships. ``auth`` is not league-scoped, so there is no
profile to read one from. The FantasyPros key rides along in the same report
because it is one credential chain, not a second provider (ADR-0011)."""


def status() -> Envelope:
    """Report which credentials are configured, from where, and how old.

    ``data`` is a mapping: ``complete``, a ``credentials`` list (one entry per
    declared credential, carrying presence, source, age, and freshness), the
    staleness threshold and its provenance, and any warnings.

    ``staleness_threshold.verified`` is always false. No ESPN documentation,
    library source, or community post states a cookie lifetime, so the
    threshold is an admitted heuristic rather than a prediction.
    """
    from fantasy_sports.auth.chain import ALL_CREDENTIALS, resolve_credentials
    from fantasy_sports.auth.staleness import build_auth_status
    from fantasy_sports.output.envelope import Envelope

    report = build_auth_status(
        resolve_credentials(ALL_CREDENTIALS), optional=_optional_names(ALL_CREDENTIALS)
    )
    data = report.to_payload()
    require_shape(data, command="auth status")
    return Envelope.success(provider=PROVIDER, data=data)


def login(*, from_browser: str | None = None, profile: str | None = None) -> Envelope:
    """Store ESPN's two cookies in the Keychain: prompted for, or read from a browser.

    With ``from_browser`` the cookies are read from a local browser profile
    (:func:`_login_from_browser`) and the prompt is never shown. Without it:

    Prompt for ESPN's two cookies and store them in the Keychain.

    Each value is read without echo. Nothing is written until every value has
    normalized successfully: a half-saved cookie pair is worse than an unsaved
    one, because ``espn-api`` sends cookies only when it has both and one alone
    reaches ESPN as an unauthenticated request.

    ``data`` is a mapping naming which credentials were ``stored`` and which
    were ``repaired`` on the way in — a SWID pasted without its braces is
    repaired rather than rejected. It contains no values.

    The optional FantasyPros API key is prompted for last; an empty answer
    skips it and stores the cookies alone. Only a *required* credential left
    blank refuses the whole login.
    """
    from fantasy_sports.auth.chain import ALL_CREDENTIALS, save_credentials
    from fantasy_sports.core.errors import AuthMissingError, ConfigInvalidError
    from fantasy_sports.output.envelope import Envelope

    if from_browser is not None:
        return _login_from_browser(from_browser, profile)
    if profile is not None:
        raise ConfigInvalidError(
            "--profile only applies with --from-browser.",
            kind="argument",
            remediation="Add --from-browser, or drop --profile to be prompted for the cookies.",
        )

    values: dict[str, str] = {}
    for spec in ALL_CREDENTIALS:
        entered = _prompt(spec)
        if entered:
            values[spec.name] = entered

    missing = [spec.name for spec in ALL_CREDENTIALS if spec.required and spec.name not in values]
    if missing:
        raise AuthMissingError(
            f"Nothing entered for: {', '.join(missing)}. Nothing was stored.",
            remediation="Run `fantasy-sports auth login` again and paste both cookies.",
        )

    repaired = save_credentials(values, specs=ALL_CREDENTIALS)
    data = {
        "stored": sorted(values),
        "repaired": sorted(repaired),
        "keychain_service": _service(),
    }
    require_shape(data, command="auth login")
    return Envelope.success(provider=PROVIDER, data=data)


def _login_from_browser(from_browser: str, profile: str | None) -> Envelope:
    """Import the cookie pair from a local browser profile and store it.

    Picks the single profile holding both cookies; if several do, the one whose
    ``espn_s2`` expires last, and ``profiles_seen`` names the others. Storage
    goes through :func:`~fantasy_sports.auth.chain.save_credentials`, so SWID
    normalization and ``record_stored`` happen exactly as for a typed login, and
    the cookies' real expiry is recorded beside ``stored_at``.

    ``data`` carries where the cookies came from and when they expire — never a
    value, a length, or a prefix.
    """
    from datetime import UTC, datetime

    from fantasy_sports.auth.browser import BROWSER_NAMES, find_espn_cookies
    from fantasy_sports.auth.chain import ALL_CREDENTIALS, save_credentials
    from fantasy_sports.commands import FROM_BROWSER_ANY
    from fantasy_sports.core.errors import ConfigInvalidError
    from fantasy_sports.output.envelope import Envelope

    name = None if from_browser == FROM_BROWSER_ANY else from_browser
    try:
        search = find_espn_cookies(name, profile)
    except ValueError:
        raise ConfigInvalidError(
            f"Unknown browser {from_browser!r}.",
            kind="argument",
            remediation=f"Pass one of: {', '.join(BROWSER_NAMES)}; or no name to search them all.",
        ) from None

    candidates = search.complete
    if not candidates:
        raise _no_browser_cookies(search, name, profile)
    floor = datetime.min.replace(tzinfo=UTC)
    chosen = max(candidates, key=lambda found: found.espn_s2_expires_at or floor)
    assert chosen.espn_s2 is not None and chosen.swid is not None  # `complete`

    repaired = save_credentials(
        {"espn_s2": chosen.espn_s2.reveal(), "swid": chosen.swid.reveal()},
        specs=ALL_CREDENTIALS,
        expires_at=chosen.expires_at(),
    )
    data = {
        "stored": ["espn_s2", "swid"],
        "repaired": sorted(repaired),
        "keychain_service": _service(),
        "source": {"browser": chosen.browser, "profile": chosen.profile},
        "expires_at": {key: _iso(moment) for key, moment in chosen.expires_at().items()},
        "profiles_seen": [
            {
                "browser": found.browser,
                "profile": found.profile,
                "complete": found.complete,
                "chosen": found is chosen,
            }
            for found in search.profiles
        ],
        "searched": list(search.searched),
        "skipped": list(search.skipped),
    }
    require_shape(data, command="auth login")
    return Envelope.success(provider=PROVIDER, data=data)


def _iso(moment: object) -> str | None:
    from datetime import UTC, datetime

    if not isinstance(moment, datetime):
        return None
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _no_browser_cookies(search: object, name: str | None, profile: str | None):
    """The ``AUTH_MISSING`` for "no profile holds both cookies".

    The remediation lists every profile that was opened and every store that
    could not be, so "I am logged in to ESPN in Chrome" and "Chrome was never
    searched" are distinguishable from the error alone.
    """
    from fantasy_sports.auth.browser import BROWSER_NAMES, BrowserSearch
    from fantasy_sports.core.errors import AuthMissingError

    assert isinstance(search, BrowserSearch)
    scope = name or "any browser"
    if profile:
        scope += f", profile {profile}"
    searched = (
        ", ".join(search.searched) or f"no profiles found (looked for {', '.join(BROWSER_NAMES)})"
    )
    parts = [f"Searched: {searched}."]
    if search.skipped:
        parts.append("Skipped: " + "; ".join(search.skipped) + ".")
    if search.keychain_denied:
        parts.append(
            "macOS Keychain access was denied, so an encrypted cookie store could not be read; "
            "run the command again and choose Allow (not Always Allow) at the prompt."
        )
    parts.append(
        "Log into fantasy.espn.com in one of these browsers and try again, or run "
        "`fantasy-sports auth login` without --from-browser to paste the cookies."
    )
    return AuthMissingError(
        f"No browser profile holds both ESPN cookies ({scope}). Nothing was stored.",
        remediation=" ".join(parts),
        details={"searched": list(search.searched), "skipped": list(search.skipped)},
    )


def logout() -> Envelope:
    """Remove the stored ESPN cookies from every link the chain reads.

    The remediation ``SECURITY.md`` could not offer before this existed: a
    leaked cookie that stays in the Keychain or in ``config.toml`` until
    ``auth login`` happens to overwrite it has not been remediated. The
    Keychain entries are deleted and the two keys are removed from the
    ``[credentials]`` table; every other key in the file survives.

    The environment is **reported, not changed** — a process cannot unset a
    variable in its parent's shell, a launchd plist, or a CI secret store, and
    claiming success while one is still set would be worse than doing nothing.

    ``data`` is a mapping: ``removed`` and ``still_set`` name lists, one
    ``credentials`` row per declared credential with the outcome of each link
    (``removed`` / ``absent`` / ``still-set`` / ``unavailable``) and the env
    vars found set, the Keychain service, the config path, and warnings for
    every link that still holds the credential. It contains no values.

    A link that cannot be reached — locked Keychain, no backend, an unreadable
    config file — is reported ``unavailable``, and the other links are still
    cleared first. Then the command **fails**: ``CONFIG_INVALID`` with
    ``kind: "credential_store"`` and the whole per-link report under
    ``details.report``. A value may still be on the machine, and exit 0 is
    the one answer a script or an agent branching on the status must not get
    while that is true (raised on the launch thread, jwulff/fantasy-sports#89).
    The code is the taxonomy's "a human must change something; retrying
    unchanged cannot work" (ADR-0009): unlock the Keychain or fix the file,
    run it again, and the second run finishes the job and exits 0. The
    environment is different: ``still-set`` is reported, not failed, because
    the command named the variables and there is nothing it could have done.

    Nothing stored anywhere is a success, not an error: the outcome the user
    wanted is the one they have.
    """
    from fantasy_sports.auth.chain import ALL_CREDENTIALS
    from fantasy_sports.auth.logout import clear_credentials
    from fantasy_sports.core.errors import ConfigInvalidError
    from fantasy_sports.output.envelope import Envelope

    report = clear_credentials(ALL_CREDENTIALS)
    data = report.to_payload()
    require_shape(data, command="auth logout")
    if report.unavailable:
        names = ", ".join(report.unavailable)
        raise ConfigInvalidError(
            f"auth logout could not reach every stored link for {names}; a value may "
            "still be on this machine. Removed: "
            f"{', '.join(report.removed) or 'nothing'}.",
            kind="credential_store",
            remediation=(
                "Unlock the Keychain or fix the config file named in the warnings, then "
                "run `fantasy-sports auth logout` again; it is idempotent."
            ),
            details={"report": data},
        )
    return Envelope.success(provider=PROVIDER, data=data)


def _prompt(spec: object) -> str:
    """Read one credential without echoing it, printing guidance to stderr.

    Guidance goes to **stderr** so that ``auth login`` piped to a JSON parser
    still writes only the envelope to stdout, exactly like every other command.
    """
    import getpass

    label = getattr(spec, "label", "credential")
    guidance = getattr(spec, "guidance", "")
    if guidance:
        print(f"{label}: {guidance}", file=sys.stderr)
    return getpass.getpass(f"{label}: ").strip()


def _service() -> str:
    from fantasy_sports.auth.chain import SERVICE

    return SERVICE


def _optional_names(specs: object) -> frozenset[str]:
    return frozenset(spec.name for spec in specs if not spec.required)  # type: ignore[attr-defined]
