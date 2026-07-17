"""Tests for PasteTokenProvider."""

from __future__ import annotations

import httpx
import pytest

from engine.auth import store
from engine.auth.paste import PasteTokenProvider, TokenValidationError

from .conftest import FakeResponse


class TestPasteTokenProvider:
    def test_is_available_always_true(self) -> None:
        assert PasteTokenProvider().is_available() is True

    def test_name(self) -> None:
        assert PasteTokenProvider().name == "paste"

    def test_settings_url(self) -> None:
        p = PasteTokenProvider(host="https://app.openadapt.ai")
        assert p.settings_url == "https://app.openadapt.ai/dashboard/settings/ingest"

    def test_login_with_explicit_token(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(200, {"org_id": "org_42"}),
        )
        provider = PasteTokenProvider(host="https://app.openadapt.ai")
        cred = provider.login(token="oai_ingest_xyz")
        assert cred["kind"] == "ingest_token"
        assert cred["token"] == "oai_ingest_xyz"
        assert cred["org_id"] == "org_42"
        # Persisted + resolvable via auth_header.
        assert store.auth_header() == {"Authorization": "Bearer oai_ingest_xyz"}

    def test_login_reads_env_when_headless(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "oai_ingest_env")
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(200, {"org_id": "org_env"}),
        )

        def _no_prompt(_):
            raise AssertionError("should not prompt when env is set")

        cred = PasteTokenProvider(prompt=_no_prompt).login()
        assert cred["token"] == "oai_ingest_env"

    def test_login_prompts_interactively(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(200, {}),
        )
        cred = PasteTokenProvider(prompt=lambda _: "  oai_ingest_pasted  ").login()
        assert cred["token"] == "oai_ingest_pasted"

    def test_login_rejects_bad_token(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(401),
        )
        with pytest.raises(TokenValidationError, match="rejected"):
            PasteTokenProvider().login(token="oai_ingest_bad")

    def test_login_network_error(self, fake_keyring, monkeypatch) -> None:
        def _raise(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr("engine.auth.paste.httpx.get", _raise)
        with pytest.raises(TokenValidationError, match="Could not reach"):
            PasteTokenProvider().login(token="oai_ingest_x")

    def test_login_no_token_raises(self, fake_keyring) -> None:
        with pytest.raises(TokenValidationError, match="No ingest token"):
            PasteTokenProvider(prompt=lambda _: "").login()
