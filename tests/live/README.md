# Live tests

Everything here is marked `@pytest.mark.live`, hits real ESPN with real
credentials, and is excluded from the default selection by `tests/conftest.py`.

```bash
ESPN_S2=... ESPN_SWID='{...}' uv run pytest -m live
```

Never put a live call in `tests/unit/` — `pytest-socket` blocks the network
there, and a cassette miss must fail loudly rather than quietly reaching ESPN.
