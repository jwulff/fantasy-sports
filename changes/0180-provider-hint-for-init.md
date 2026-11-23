# Name ESPN on `init` and `leagues discover` failures (#180)

A failure envelope carries `provider`, and two things key off it: the health
client folds an upgrade notice or a known-outage note into the error
(`_check_health`), and an agent reading the envelope can tell which provider
to go fix. `init` reported `provider: null` on every failure while its own
success envelope said `"espn"` — the same command, same run, two different
answers about who it was talking to.

`leagues discover` turned out to already be covered: `_provider_hint()`
answers for the whole `leagues` group, added when the command landed in #172.
The issue named both commands because it was filed from a pre-#179 preflight
review; only `init` was actually broken. Both now have a failure test so the
gap cannot reopen silently in either place.

## What changed

- **`cli/app.py::_provider_hint`** — `init` is registered without a `group`
  (it is the one command that composes across `auth`, `leagues` and
  `doctor`), so group matching cannot reach it and it is now matched by
  invocation name. The provider comes from `commands.init.PROVIDER`, the
  same single source the `auth` branch uses and the same constant
  `init()` passes to its own success envelope, so the two halves cannot
  drift apart again. The import stays inside the branch: `--help` must not
  pay for a module it will never call.
- **`tests/unit/test_cli_execution.py`** — four tests under
  `TestProviderHintOnFailure`. Two assert the hint itself; two drive the
  real CLI and assert `provider == "espn"` in the error envelope, one per
  command. `init`'s failure is `--season` with `--all-seasons`, which is
  rejected before any config read or credential lookup, so the test needs
  no fixtures and no network. `leagues discover`'s stubs
  `auth.chain.require_credentials` to raise `AUTH_MISSING`, the failure a
  user with no stored SWID actually sees, rather than depending on the
  host's Keychain happening to be empty.

## What did not change

No new error code, and no change to the taxonomy: the envelope still allows
`provider: null`, and every command that genuinely cannot know a provider
before a read still reports it. `doctor` — the other league-less command —
is deliberately left null, since it spans providers by design.
