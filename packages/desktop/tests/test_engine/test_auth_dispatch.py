"""Tests for the provider-dispatch helpers in engine.auth."""

from __future__ import annotations

import pytest

from engine import auth


class TestAvailableProviders:
    def test_order_browser_first(self) -> None:
        providers = auth.available_providers()
        assert [p.name for p in providers] == ["browser_pkce", "paste"]


class TestLoginDispatch:
    def test_skips_unavailable_provider(self, monkeypatch) -> None:
        """When browser is unavailable, dispatch falls through to paste."""
        marker = {"kind": "ingest_token", "token": "t", "refresh_token": None,
                  "org_id": None, "host": "https://app.openadapt.ai", "expires_at": None}

        class _Browser:
            name = "browser_pkce"

            def is_available(self):
                return False

            def login(self):
                raise AssertionError("unavailable provider must not be called")

        class _Paste:
            name = "paste"

            def is_available(self):
                return True

            def login(self):
                return marker

        monkeypatch.setattr(auth, "available_providers", lambda host="": [_Browser(), _Paste()])
        cred = auth.login()
        assert cred is marker

    def test_prefer_filters(self, monkeypatch) -> None:
        called = []

        class _Paste:
            name = "paste"

            def is_available(self):
                return True

            def login(self):
                called.append("paste")
                return {"kind": "ingest_token", "token": "t", "refresh_token": None,
                        "org_id": None, "host": "h", "expires_at": None}

        class _Browser:
            name = "browser_pkce"

            def is_available(self):
                return True

            def login(self):
                called.append("browser")
                return {}

        monkeypatch.setattr(auth, "available_providers", lambda host="": [_Browser(), _Paste()])
        auth.login(prefer="paste")
        assert called == ["paste"]

    def test_no_provider_raises(self, monkeypatch) -> None:
        class _Unavailable:
            name = "browser_pkce"

            def is_available(self):
                return False

            def login(self):
                return {}

        monkeypatch.setattr(auth, "available_providers", lambda host="": [_Unavailable()])
        with pytest.raises(RuntimeError, match="No auth provider"):
            auth.login()
