"""Tests must never reach live Google or other production network services."""
import socket

import pytest


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("live_network_forbidden_in_tests_use_a_fake")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
