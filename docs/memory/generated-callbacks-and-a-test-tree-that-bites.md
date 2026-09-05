# Callbacks are generated, and three ways the test tree bit back

**Found:** 2026-09-05, building U8 (#9). **Applies to:** `cli/app.py`, the
future MCP projection, and anyone adding a test file or a `conftest.py`.

## 1. A typer callback is built from declared parameters, not from the handler

The tempting shape for ADR-0003 is `target.command(name)(spec.resolve())` —
hand typer the registered function and let it introspect the signature. It is
what `cli/app.py` did before this unit, and it does not survive contact with a
real registry, for two reasons:

* **Introspection means import.** `build_app()` runs for *every* non-fast-path
  invocation, including one that is about to print a usage error, and
  resolving all ten handlers pulls in `espn_api`, `requests`, and `keyring`.
* **A handler would then have to hold `typer.Option(...)` defaults**, which is
  precisely the typer dependency `commands/` may not have.

So `CommandSpec.params` is the description both surfaces read, and a callback
is assembled from it:

```python
callback.__signature__ = inspect.Signature(parameters)   # inspect.signature
callback.__annotations__ = {p.name: p.annotation for p in parameters}  # get_type_hints
```

Both are needed: typer reads defaults through `inspect.signature` and
annotations through `get_type_hints`, and neither falls back to the other. The
function itself is `def callback(**values)`, so any keyword typer passes is
accepted, `typer.Context` included.

Three things that were checked rather than assumed, against typer 0.27.2:
`X | None` works as an annotation (no `typing.Optional` needed), a `list[str]`
annotation gives a repeatable option, and `typer.Option(...)` as the default
makes it required. `spec.resolve()` is called at *dispatch* time, which is what
keeps `--help` at 17 ms.

The test that holds the whole thing together is in `tests/unit/test_registry.py`:
every spec's declared parameters must equal its handler's keyword-only
parameters. Without it, a declaration and an implementation can disagree until
an agent invokes the command.

## 2. Duplicate test-module basenames are a collection error, not a shadowing

The plan names `tests/integration/test_cli.py`, and `tests/unit/test_cli.py`
already exists. Under pytest's default `prepend` import mode with no
`__init__.py`, both want the module name `test_cli`, and the second one to be
collected fails the **entire run** with `import file mismatch`. Renaming is the
cheap fix (`test_cli_end_to_end.py`); adding `__init__.py` files is not,
because `from conftest import ...` — which several test modules rely on — works
precisely *because* `tests/` is put on `sys.path` by the rootdir conftest.

The same rule bites harder for `conftest.py` itself: adding
`tests/live/conftest.py` made `from conftest import CASSETTE_SWID_SALT`
resolve to the **live** conftest and broke two unrelated test modules. There
can be exactly one `conftest` basename in this tree, so anything a
subdirectory needs goes in that directory's test modules instead.

Shared fixtures live in `tests/_harness.py` for the same reason: it sits beside
`conftest.py`, so it is importable everywhere, and it keeps ordinary
scaffolding out of a file whose other contents are a security control.

## 3. The live suite could not reach the network at all

`addopts` in `pyproject.toml` carries `--disable-socket`, and `addopts` applies
to **every** run, `-m live` included. So `uv run pytest -m live` failed every
test with `A test tried to use socket.getaddrinfo` — not skipped, not
inconclusive: red, with a message about sockets and nothing about ESPN. The
canary that ADR-0005 relies on had never actually run.

The fix is `pytest.mark.enable_socket` (pytest-socket's own marker) alongside
`pytest.mark.live` in each live module. It cannot live in a
`tests/live/conftest.py`, for the reason in §2.

Related: [[import-budget-and-the-fastpath]], [[typer-vendors-click]]
