"""Shared fixtures for engine unit tests (auth, hosted, flow bridge)."""

from __future__ import annotations

import pytest


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module (never touches the OS store)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self._store:
            raise KeyError("no such password")
        del self._store[(service, account)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch) -> FakeKeyring:
    """Patch the auth store to use an in-memory keyring backend.

    Autouse (review 2.2 P0-1): no engine test may silently reach the real OS
    keychain -- on a headless CI box that raises ``NoKeyringError`` and reddened
    every OS job. Tests that assert on stored credentials request this fixture
    by name to grab the in-memory backend; the rest just get isolation for free.
    """
    fake = FakeKeyring()
    monkeypatch.setattr("engine.auth.store._keyring", lambda: fake)
    # Ensure no ambient ingest token leaks in from the environment.
    monkeypatch.delenv("OPENADAPT_INGEST_TOKEN", raising=False)
    return fake


class FakeResponse:
    """Minimal httpx.Response stand-in for monkeypatched requests."""

    def __init__(self, status_code: int = 200, json_body: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._json
