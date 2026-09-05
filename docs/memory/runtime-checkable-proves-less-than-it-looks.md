# `isinstance(x, Provider)` proves less than it looks

**Found:** 2026-09-05, building U2 (#3). **Applies to:** every adapter that
claims to satisfy `providers.base.Provider` — U7 (#8) first.

`Provider` is a `@runtime_checkable` `typing.Protocol`, so
`isinstance(EspnProvider(), Provider)` is legal and reads like a conformance
proof. It is not one. Two limits, neither of them obvious at the call site:

1. **`isinstance` checks that members *exist*, never their signatures.** A
   `fetch_matchups(self, league_id)` that forgot `season` and `week` passes.
   So does one returning `dict` instead of `list[Matchup]`. The only thing a
   passing check rules out is a *missing* method — real, but thin.
2. **`issubclass` raises `TypeError`, not `False`.** `Provider` has a data
   member (`name: str`), and CPython refuses `issubclass()` on any protocol
   with non-method members. Reaching for it as the "static" version of the
   same check gets an exception, not an answer. `tests/unit/test_provider_protocol.py`
   pins this so the failure is documented rather than discovered.

The corollary that matters: **an adapter's conformance test must call every
method with realistic arguments and assert on the returned types**, the way
`test_every_read_returns_normalized_objects_for_every_provider_shape` does.
An `isinstance` assertion alone is a smoke test wearing a contract's clothes.

One more sharp edge if you go looking: setting a method to `None` on a class
(`fetch_raw = None`) *does* make `isinstance` return `False`, because CPython
special-cases a `None` value for a member that is callable on the protocol.
That is the only way to remove an inherited method for a test — `del
Subclass.method` raises `AttributeError`, since the attribute lives on the
base.
