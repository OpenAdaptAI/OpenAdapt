"""Tests for BrowserPkceProvider + the loopback receiver."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse

import httpx
import pytest

from engine.auth import store
from engine.auth.browser_pkce import (
    BrowserPkceProvider,
    _LoopbackReceiver,
    generate_pkce_pair,
)


class TestPkce:
    def test_pair_is_s256(self) -> None:
        verifier, challenge = generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected
        assert "=" not in challenge  # base64url, unpadded

    def test_pairs_are_unique(self) -> None:
        assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


class TestLoopbackReceiver:
    def test_captures_code(self) -> None:
        receiver = _LoopbackReceiver()
        redirect = receiver.redirect_uri
        assert redirect.startswith("http://127.0.0.1:")
        assert redirect.endswith("/callback")

        def _deliver():
            time.sleep(0.1)
            httpx.get(redirect, params={"code": "abc", "state": "s1"}, timeout=5)

        threading.Thread(target=_deliver, daemon=True).start()
        receiver.serve_until_code(timeout=5)
        assert receiver.code == "abc"
        assert receiver.state == "s1"


class TestIsAvailable:
    def test_headless_env_false(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_HEADLESS", "1")
        assert BrowserPkceProvider().is_available() is False

    def test_linux_without_display_false(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
        monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert BrowserPkceProvider().is_available() is False

    def test_macos_true(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
        monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "darwin")
        assert BrowserPkceProvider().is_available() is True


class TestLogin:
    def test_full_flow(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
        monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "darwin")

        def _open_browser(url: str) -> None:
            # Extract the loopback redirect and deliver a code asynchronously.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = qs["redirect_to"][0]
            state = qs["state"][0]

            def _deliver():
                time.sleep(0.1)
                httpx.get(redirect, params={"code": "auth_code_1", "state": state}, timeout=5)

            threading.Thread(target=_deliver, daemon=True).start()

        provider = BrowserPkceProvider(host="https://app.openadapt.ai", open_browser=_open_browser)
        provider._exchange_code = lambda code, verifier, redirect_uri: {  # type: ignore[assignment]
            "access_token": "supabase_access",
            "refresh_token": "supabase_refresh",
            "expires_at": 1234.0,
        }
        provider._mint_ingest_token = lambda access_token: ("oai_ingest_minted", "org_9")  # type: ignore[assignment]

        cred = provider.login()
        assert cred["kind"] == "ingest_token"
        assert cred["token"] == "oai_ingest_minted"
        assert cred["refresh_token"] == "supabase_refresh"
        assert cred["org_id"] == "org_9"
        # The bearer path resolves the minted ingest token.
        assert store.auth_header() == {"Authorization": "Bearer oai_ingest_minted"}

    def test_headless_login_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_HEADLESS", "1")
        with pytest.raises(RuntimeError, match="unavailable"):
            BrowserPkceProvider().login()
