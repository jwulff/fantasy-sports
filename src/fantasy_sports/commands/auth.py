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
profile to read one from."""


def status() -> Envelope:
    """Report which credentials are configured, from where, and how old.

    ``data`` is a mapping: ``complete``, a ``credentials`` list (one entry per
    declared credential, carrying presence, source, age, and freshness), the
    staleness threshold and its provenance, and any warnings.

    ``staleness_threshold.verified`` is always false. No ESPN documentation,
    library source, or community post states a cookie lifetime, so the
    threshold is an admitted heuristic rather than a prediction.
    """
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
    from fantasy_sports.auth.staleness import build_auth_status
    from fantasy_sports.output.envelope import Envelope

    report = build_auth_status(resolve_credentials(ESPN_CREDENTIALS))
    data = report.to_payload()
    require_shape(data, command="auth status")
    return Envelope.success(provider=PROVIDER, data=data)


def login() -> Envelope:
    """Prompt for ESPN's two cookies and store them in the Keychain.

    Each value is read without echo. Nothing is written until every value has
    normalized successfully: a half-saved cookie pair is worse than an unsaved
    one, because ``espn-api`` sends cookies only when it has both and one alone
    reaches ESPN as an unauthenticated request.

    ``data`` is a mapping naming which credentials were ``stored`` and which
    were ``repaired`` on the way in — a SWID pasted without its braces is
    repaired rather than rejected. It contains no values.
    """
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, save_credentials
    from fantasy_sports.core.errors import AuthMissingError
    from fantasy_sports.output.envelope import Envelope

    values: dict[str, str] = {}
    for spec in ESPN_CREDENTIALS:
        entered = _prompt(spec)
        if entered:
            values[spec.name] = entered

    missing = [spec.name for spec in ESPN_CREDENTIALS if spec.required and spec.name not in values]
    if missing:
        raise AuthMissingError(
            f"Nothing entered for: {', '.join(missing)}. Nothing was stored.",
            remediation="Run `fantasy-sports auth login` again and paste both cookies.",
        )

    repaired = save_credentials(values, specs=ESPN_CREDENTIALS)
    data = {
        "stored": sorted(values),
        "repaired": sorted(repaired),
        "keychain_service": _service(),
    }
    require_shape(data, command="auth login")
    return Envelope.success(provider=PROVIDER, data=data)


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
    config file — is reported ``unavailable`` rather than failing the command,
    so the other links are still cleared. Nothing stored anywhere is a
    success, not an error: the outcome the user wanted is the one they have.
    """
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS
    from fantasy_sports.auth.logout import clear_credentials
    from fantasy_sports.output.envelope import Envelope

    data = clear_credentials(ESPN_CREDENTIALS).to_payload()
    require_shape(data, command="auth logout")
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
