"""
Pytest configuration for the offline test suite.

- Adds project root and tests/ to sys.path.
- Blocks all network calls (socket-level guard) — tests must use ReplayProvider.
"""
import socket
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parent

sys.path.insert(0, str(_PROJECT_ROOT))  # providers, tools, observe
sys.path.insert(0, str(_TESTS_DIR))     # helpers


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail immediately if any test attempts a real network call."""
    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Network call blocked in tests! Tests must use fixture-based replay providers."
        )

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
