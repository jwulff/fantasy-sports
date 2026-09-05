"""Shared test configuration.

Unit tests never touch the network: ``pytest-socket`` is enabled through
``addopts`` in ``pyproject.toml``, so a cassette miss or a stray live call
fails loudly instead of quietly reaching ESPN.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect ``live`` tests unless they were asked for by name or marker."""
    if config.getoption("-m"):
        return
    skip_live = pytest.mark.skip(reason="live test; run with -m live and real credentials")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
