"""``auth status`` and ``auth login`` — projections over ``auth/``.

Both are thin on purpose. The credential chain, the staleness heuristic, the
SWID normalizer, and the Keychain write all landed with
jwulff/fantasy-sports#5; nothing here re-implements any of it. What this module
adds is the envelope around the result and the one rule those functions cannot
enforce on their own: **no credential value ever reaches an output stream.**

Three consequences of that rule are visible below.

* ``login`` reads values with :func:`getpass.getpass`, so a value is not echoed
  to the terminal and does not land in shell history.
* Values are never accepted as command-line options. An argument is visible in
  ``ps``, in shell history, and in any process listing a co-tenant can read;
  making that impossible is worth more than the convenience of scripting a
  cookie paste.
* The envelope reports credential *names* — stored, repaired, unchanged — and
  never a value, a length, or a prefix.

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

__all__ = ["login", "status"]

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
