"""Unit tests are offline. A cassette miss must fail loudly, never call ESPN."""

from __future__ import annotations

import socket

import pytest


def test_outbound_connections_are_blocked():
    with pytest.raises(Exception) as err:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("example.com", 80))
    assert "socket" in type(err.value).__name__.lower() or "Socket" in str(err.value)
