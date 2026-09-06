"""Offline standard suite; only reference/storage tests request a catalog."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION", raising=False)
    original = socket.socket.connect

    def local_only(stream, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                local = host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if not local:
                raise AssertionError("Standard tests cannot access external networks; use a mock transport.")
        return original(stream, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


@pytest.fixture
def isolated_catalog(tmp_path, monkeypatch):
    # Own only this fixture directory. Never remove real catalog data.
    test_home = tmp_path / "collection"
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(test_home))
    return test_home
