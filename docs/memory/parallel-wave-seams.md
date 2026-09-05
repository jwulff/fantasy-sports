# What a four-way parallel wave costs, and which seams are dangerous to close late

**Found:** 2026-09-05, reconciling wave 1 (#35). **Applies to:** any future wave
that branches several units off the same commit — the pattern that built #2,
#3, #5, #7 and half of #12 in parallel.

Five units branched off one commit and each defined what it needed rather than
importing a module that did not exist yet. Every PR was green alone, the merged
state was green, and the merged state was **incoherent**: two `CredentialSpec`
classes, two `LeagueNotFoundError`s, an `AuthError` parallel to the taxonomy,
and `auth/` parsing `config.toml` with its own `tomllib` call. Each author
flagged the seam in their PR body. That is what made the reconciliation
mechanical instead of archaeological, and it is the part worth repeating.

The general lesson is small: **the wave is worth it, and the reconciliation
issue is not optional.** File it before the wave lands, mark it `p0`, and block
the next wave on it — otherwise the units built on top import whichever
duplicate they happen to find, and the cost stops being one refactor.

Two specific seams were not mechanical, and both would have been silently lost.

## 1. A behaviour attached to a deleted class does not delete with it

`auth.chain.AuthError` scrubbed its message through `redact()` **at
construction**. Replacing it with `core.errors.AuthMissingError` deletes the
class — and, if nobody notices, the scrubbing with it. Nothing fails. No test
goes red. `AuthMissingError` renders a message it was handed, and the next
`raise SchemaDriftError(f"GET {url}")` with a cookie in the URL leaks it.

The protection had to move *down* to `FantasySportsError`, not sideways, which
is exactly what `credential-leak-channels.md` predicted when it said "if #3's
`core/errors.py` becomes the shared base, that base must scrub too."

The generalisation: **when deleting a class in favour of a shared one, list what
the deleted class *did* that the shared one does not, before looking at what it
*was*.** A signature that matches proves nothing about a constructor side
effect. The test that holds it is deliberately built out of a **non-auth**
error — `ProviderUnavailableError` constructed from a message containing a
credential — because a test that used an auth error would still pass if the
scrubbing had stayed behind in `auth/`.

That move forced a second one. The scrub registry lived in `auth/chain.py`, and
`core/` cannot import `auth/` — `chain.py` imports `core.models.CredentialSpec`,
so the dependency runs one way. `redact()` and the known-secret set moved to
`core/redaction.py`; `Secret`, which carries a credential rather than scrubbing
text, stayed in `auth/`. **When a guarantee has to be enforced by a lower
layer, the mechanism moves to that layer, not a re-import back up.**

## 2. Two classes with one name get merged, never wrapped

`core/models.py` had the provider-facing `CredentialSpec` (`secret`,
`required`, `staleness`); `auth/chain.py` had the resolution-facing one
(`env_vars`, `guidance`). The tempting fixes are both wrong: keeping both
(callers pick by import path, and they will disagree) or having one wrap the
other (a wrapper is a second shape wearing the first one's name).

They are one description of one thing — a provider that *declares* a credential
and a chain that *resolves* it must not be able to disagree about what it is
called. The fields merged into one class, with the resolution fields defaulted
so a provider describing its needs abstractly is not obliged to invent an
environment variable.

The trap that falls out of defaulting: `env_vars` became optional, so
`require_credentials`' `spec.env_vars[0]` became an `IndexError` waiting for
the first provider that declares a credential with no environment fallback.
**Making a field optional makes every unguarded index into it a latent bug** —
grep for the accesses in the same change, not the next one.

Related: [[credential-leak-channels]], [[config-toml-is-a-shared-namespace]]
