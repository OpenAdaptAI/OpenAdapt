"""End-to-end contract tests for the RFC 8252 Desktop browser login."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from engine.auth import pairing, store
from engine.auth.browser_pkce import (
    BrowserPkceProvider,
    _LoopbackReceiver,
    _write_callback_body,
    generate_pkce_pair,
)

HOST = "https://app.openadapt.ai"
SECRET = "oap_" + "A" * 43
TOKEN = "oai_ingest_" + "B" * 43
EXPIRES_AT = "2026-10-26T12:00:00+00:00"
EXPIRES_TS = 1793016000.0
REAL_HTTPX_GET = httpx.get


class _Response:
    def __init__(
        self,
        status_code: int,
        body: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body


def _lifetime() -> dict:
    return {
        "expires_at": EXPIRES_AT,
        "expires_in_days": 90,
        "expiring_soon": False,
        "legacy_non_expiring": False,
        "warning_days": 14,
    }


def _claim_body(pairing_id: str) -> dict:
    return {
        "paired": True,
        "pairing_id": pairing_id,
        "ingest_token_id": str(uuid4()),
        "ingest_token": TOKEN,
        "credential": _lifetime(),
    }


def _validation() -> _Response:
    return _Response(
        200,
        {"count": 0, "credential": _lifetime()},
        {
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "90",
        },
    )


def _desktop_available(monkeypatch) -> None:
    monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
    monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "darwin")
    monkeypatch.setattr("engine.auth.browser_pkce.secure_store_available", lambda: True)
    monkeypatch.setattr(pairing, "secure_store_available", lambda: True)


def _deliver_from_login_url(
    url: str,
    *,
    code: str | None = SECRET,
    state: str | None = None,
    error: str | None = None,
    include_state: bool = True,
) -> dict[str, list[str]]:
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(url).query,
        keep_blank_values=True,
    )
    redirect = query["redirect_to"][0]
    callback_state = query["state"][0] if state is None else state

    def _deliver() -> None:
        time.sleep(0.02)
        params: dict[str, str] = {}
        if code is not None:
            params["code"] = code
        if include_state and callback_state is not None:
            params["state"] = callback_state
        if error is not None:
            params["error"] = error
            params["error_description"] = "The user refused the connection."
        REAL_HTTPX_GET(redirect, params=params, timeout=5)

    threading.Thread(target=_deliver, daemon=True).start()
    return query


class TestPkce:
    def test_pair_is_s256_and_within_rfc_7636_bounds(self) -> None:
        verifier, challenge = generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert 43 <= len(verifier) <= 128
        assert len(challenge) == 43
        assert "=" not in verifier + challenge

    def test_pairs_are_unique(self) -> None:
        assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


class TestLoopbackReceiver:
    def test_captures_only_the_exact_callback(self) -> None:
        with _LoopbackReceiver() as receiver:
            redirect = receiver.redirect_uri
            assert redirect.startswith("http://127.0.0.1:")
            assert redirect.endswith("/callback")

            def _deliver() -> None:
                time.sleep(0.02)
                wrong = redirect.replace("/callback", "/")
                assert httpx.get(wrong, timeout=5).status_code == 404
                response = httpx.get(
                    redirect,
                    params={"code": SECRET, "state": "state_1234567890"},
                    timeout=5,
                )
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"

            threading.Thread(target=_deliver, daemon=True).start()
            receiver.serve_until_code(timeout=5)
            assert receiver.code == SECRET
            assert receiver.state == "state_1234567890"

    def test_close_before_server_start_does_not_call_blocking_shutdown(self) -> None:
        receiver = _LoopbackReceiver()
        receiver.close()
        receiver.close()

    def test_browser_disconnect_after_callback_still_signals_completion(self) -> None:
        event = threading.Event()

        class _DisconnectedStream:
            def write(self, body: bytes) -> None:
                raise BrokenPipeError("browser closed")

        with pytest.raises(BrokenPipeError):
            _write_callback_body(_DisconnectedStream(), b"done", event)
        assert event.is_set()


class TestIsAvailable:
    def test_headless_env_false(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_HEADLESS", "1")
        monkeypatch.setattr("engine.auth.browser_pkce.secure_store_available", lambda: True)
        assert BrowserPkceProvider().is_available() is False

    def test_linux_without_display_false(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
        monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "linux")
        monkeypatch.setattr("engine.auth.browser_pkce.secure_store_available", lambda: True)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert BrowserPkceProvider().is_available() is False

    def test_complete_cloud_contract_needs_no_supabase_client_configuration(
        self, monkeypatch
    ) -> None:
        _desktop_available(monkeypatch)
        monkeypatch.delenv("OPENADAPT_SUPABASE_URL", raising=False)
        monkeypatch.delenv("OPENADAPT_SUPABASE_ANON_KEY", raising=False)
        assert BrowserPkceProvider(open_browser=lambda _: None).is_available() is True
        source = Path("engine/auth/browser_pkce.py").read_text()
        assert "/auth/v1/token" not in source
        assert "OPENADAPT_SUPABASE_" not in source

    def test_missing_system_browser_is_unavailable(self, monkeypatch) -> None:
        import webbrowser

        _desktop_available(monkeypatch)
        monkeypatch.setattr(
            webbrowser,
            "get",
            lambda: (_ for _ in ()).throw(webbrowser.Error("no browser")),
        )
        assert BrowserPkceProvider().is_available() is False

    def test_locked_keychain_refuses_before_opening_browser(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_HEADLESS", raising=False)
        monkeypatch.setattr("engine.auth.browser_pkce.sys.platform", "darwin")
        monkeypatch.setattr("engine.auth.browser_pkce.secure_store_available", lambda: False)
        opened: list[str] = []
        with pytest.raises(RuntimeError, match="unavailable"):
            BrowserPkceProvider(open_browser=opened.append).login()
        assert opened == []

    def test_untrusted_cloud_origin_refuses_before_opening_browser(self, monkeypatch) -> None:
        _desktop_available(monkeypatch)
        opened: list[str] = []
        provider = BrowserPkceProvider(
            host="https://app.openadapt.ai.evil.example",
            open_browser=opened.append,
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            provider.login()
        assert opened == []


class TestLogin:
    def test_full_flow_uses_one_oap_claim_and_the_shared_durable_pipeline(
        self, fake_keyring, monkeypatch
    ) -> None:
        _desktop_available(monkeypatch)
        pairing_id = str(uuid4())
        events: list[str] = []
        sent_challenge: list[str] = []
        real_snapshot = pairing.snapshot_pairing_canonical
        real_stage = pairing.stage_pairing_credential
        real_commit = pairing.commit_pairing_stage
        real_clear = pairing.clear_pairing_stage

        def _open_browser(url: str) -> None:
            query = _deliver_from_login_url(url)
            assert set(query) == {
                "redirect_to",
                "code_challenge",
                "code_challenge_method",
                "state",
            }
            assert all(len(values) == 1 for values in query.values())
            assert query["code_challenge_method"] == ["S256"]
            assert len(query["code_challenge"][0]) == 43
            sent_challenge.append(query["code_challenge"][0])
            assert 16 <= len(query["state"][0]) <= 128

        def _post(url, *, json, headers=None, **kwargs):
            if url.endswith("/claim"):
                events.append("claim")
                assert headers is None
                assert set(json) == {"pairing_secret", "device_name", "code_verifier"}
                assert json["pairing_secret"] == SECRET
                assert 43 <= len(json["code_verifier"]) <= 128
                challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(json["code_verifier"].encode()).digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
                # It is the verifier for the challenge sent in the login URL.
                assert sent_challenge == [challenge]
                return _Response(
                    201,
                    _claim_body(pairing_id),
                    {"cache-control": "no-store", "referrer-policy": "no-referrer"},
                )
            events.append("confirm")
            assert url.endswith("/confirm")
            assert json == {"pairing_id": pairing_id}
            assert headers == {"Authorization": f"Bearer {TOKEN}"}
            return _Response(200, {"connected": True})

        def _snapshot(host):
            events.append("snapshot")
            return real_snapshot(host)

        def _stage(*args):
            events.append("stage")
            return real_stage(*args)

        def _validate(*args, **kwargs):
            events.append("validate")
            return _validation()

        def _commit(value):
            events.append("commit")
            return real_commit(value)

        def _clear(value):
            events.append("clear")
            return real_clear(value)

        monkeypatch.setattr(pairing, "_safe_device_name", lambda: "test-device")
        monkeypatch.setattr(pairing, "snapshot_pairing_canonical", _snapshot)
        monkeypatch.setattr(pairing, "stage_pairing_credential", _stage)
        monkeypatch.setattr(pairing, "commit_pairing_stage", _commit)
        monkeypatch.setattr(pairing, "clear_pairing_stage", _clear)
        monkeypatch.setattr(pairing.httpx, "post", _post)
        monkeypatch.setattr(pairing.httpx, "get", _validate)

        credential = BrowserPkceProvider(host=HOST, open_browser=_open_browser).login()

        assert credential == {
            "kind": "ingest_token",
            "token": TOKEN,
            "refresh_token": None,
            "org_id": None,
            "host": HOST,
            "expires_at": EXPIRES_TS,
        }
        assert store.auth_header() == {"Authorization": f"Bearer {TOKEN}"}
        assert events == [
            "snapshot",
            "claim",
            "stage",
            "validate",
            "commit",
            "confirm",
            "clear",
        ]

    def test_local_alias_uses_one_canonical_host_for_login_claim_and_readback(
        self, fake_keyring, monkeypatch
    ) -> None:
        _desktop_available(monkeypatch)
        pairing_id = str(uuid4())
        login_urls: list[str] = []

        def _open_browser(url: str) -> None:
            login_urls.append(url)
            _deliver_from_login_url(url)

        def _post(url, *, json, headers=None, **kwargs):
            if url.endswith("/claim"):
                assert url == "http://localhost/api/local-bridge/pairings/claim"
                return _Response(
                    201,
                    _claim_body(pairing_id),
                    {"cache-control": "no-store", "referrer-policy": "no-referrer"},
                )
            assert url == "http://localhost/api/local-bridge/pairings/confirm"
            return _Response(200, {"connected": True})

        monkeypatch.setattr(pairing.httpx, "post", _post)
        monkeypatch.setattr(pairing.httpx, "get", lambda *a, **k: _validation())

        credential = BrowserPkceProvider(
            host="http://LOCALHOST:80/",
            open_browser=_open_browser,
        ).login()

        assert login_urls[0].startswith("http://localhost/login?")
        assert credential["host"] == "http://localhost"
        assert store.load_credential("http://localhost") == credential

    @pytest.mark.parametrize(
        "wrong_code",
        [
            "supabase_authorization_code",
            "oai_ingest_" + "A" * 43,
            "oapp_" + "A" * 43,
            "oaps_" + "A" * 43,
        ],
    )
    def test_rejects_supabase_and_other_credential_prefixes_before_claim(
        self, wrong_code, monkeypatch
    ) -> None:
        _desktop_available(monkeypatch)
        claimed = False

        def _post(*args, **kwargs):
            nonlocal claimed
            claimed = True
            raise AssertionError("a wrong credential role must not reach claim")

        monkeypatch.setattr(pairing.httpx, "post", _post)
        provider = BrowserPkceProvider(
            open_browser=lambda url: _deliver_from_login_url(url, code=wrong_code)
        )
        with pytest.raises(RuntimeError, match="invalid OpenAdapt pairing code"):
            provider.login()
        assert claimed is False

    @pytest.mark.parametrize("callback_state", ["", "wrong_state_123456"])
    def test_requires_present_exact_state(self, callback_state, monkeypatch) -> None:
        _desktop_available(monkeypatch)
        provider = BrowserPkceProvider(
            open_browser=lambda url: _deliver_from_login_url(
                url,
                state=callback_state,
            )
        )
        with pytest.raises(RuntimeError, match="state mismatch"):
            provider.login()

    def test_access_denied_stops_without_a_claim(self, monkeypatch) -> None:
        _desktop_available(monkeypatch)
        provider = BrowserPkceProvider(
            open_browser=lambda url: _deliver_from_login_url(
                url,
                code=None,
                error="access_denied",
            )
        )
        with pytest.raises(RuntimeError, match="user refused"):
            provider.login()

    def test_access_denied_without_state_is_rejected_as_a_mismatch(self, monkeypatch) -> None:
        _desktop_available(monkeypatch)
        provider = BrowserPkceProvider(
            open_browser=lambda url: _deliver_from_login_url(
                url,
                code=None,
                error="access_denied",
                include_state=False,
            )
        )
        with pytest.raises(RuntimeError, match="state mismatch"):
            provider.login()

    def test_pairing_410_is_a_safe_login_failure(self, monkeypatch) -> None:
        _desktop_available(monkeypatch)
        monkeypatch.setattr(pairing.httpx, "post", lambda *a, **k: _Response(410, {}))
        provider = BrowserPkceProvider(open_browser=lambda url: _deliver_from_login_url(url))
        with pytest.raises(RuntimeError, match="expired.*already used"):
            provider.login()

    def test_headless_login_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_HEADLESS", "1")
        with pytest.raises(RuntimeError, match="unavailable"):
            BrowserPkceProvider().login()
