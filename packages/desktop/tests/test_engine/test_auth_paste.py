"""Tests for PasteTokenProvider."""

from __future__ import annotations

import httpx
import pytest

from engine.auth import store
from engine.auth.paste import PasteTokenProvider, TokenValidationError

from .conftest import FakeResponse

TOKEN = "oai_ingest_" + "A" * 43
ENV_TOKEN = "oai_ingest_" + "B" * 43
PASTED_TOKEN = "oai_ingest_" + "C" * 43
EXPIRES_AT = "2026-10-26T12:00:00+00:00"
EXPIRES_TS = 1793016000.0


def _lifetime_response(org_id: str = "org_42") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "org_id": org_id,
            "credential": {
                "expires_at": EXPIRES_AT,
                "expires_in_days": 90,
                "expiring_soon": False,
                "legacy_non_expiring": False,
                "warning_days": 14,
            },
        },
        headers={
            "cache-control": "no-store",
            "x-openadapt-credential-warning-days": "14",
            "x-openadapt-credential-expires-in-days": "90",
        },
    )


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
            lambda *a, **k: _lifetime_response(),
        )
        provider = PasteTokenProvider(host="https://app.openadapt.ai")
        cred = provider.login(token=TOKEN)
        assert cred["kind"] == "ingest_token"
        assert cred["token"] == TOKEN
        assert cred["org_id"] == "org_42"
        assert cred["expires_at"] == EXPIRES_TS
        # Persisted + resolvable via auth_header.
        assert store.auth_header() == {"Authorization": f"Bearer {TOKEN}"}

    def test_login_reads_env_when_headless(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", ENV_TOKEN)
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(200, {"org_id": "org_env"}),
        )

        def _no_prompt(_):
            raise AssertionError("should not prompt when env is set")

        cred = PasteTokenProvider(prompt=_no_prompt).login()
        assert cred["token"] == ENV_TOKEN

    def test_login_prompts_interactively(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(200, {}),
        )
        cred = PasteTokenProvider(prompt=lambda _: f"  {PASTED_TOKEN}  ").login()
        assert cred["token"] == PASTED_TOKEN

    def test_working_token_fallback_keeps_unknown_expiry_from_an_older_cloud(
        self, fake_keyring, monkeypatch
    ) -> None:
        response = _lifetime_response()
        response.headers.pop("cache-control")
        monkeypatch.setattr("engine.auth.paste.httpx.get", lambda *a, **k: response)

        cred = PasteTokenProvider().login(token=TOKEN)

        assert cred["token"] == TOKEN
        assert cred["expires_at"] is None

    def test_login_rejects_bad_token(self, fake_keyring, monkeypatch) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: FakeResponse(401),
        )
        with pytest.raises(TokenValidationError, match="rejected"):
            PasteTokenProvider().login(token=TOKEN)

    def test_login_network_error(self, fake_keyring, monkeypatch) -> None:
        def _raise(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr("engine.auth.paste.httpx.get", _raise)
        with pytest.raises(TokenValidationError, match="Could not reach"):
            PasteTokenProvider().login(token=TOKEN)

    @pytest.mark.parametrize(
        "wrong_role",
        [
            "supabase_authorization_code",
            "oap_" + "A" * 43,
            "oapp_" + "A" * 43,
            "oaps_" + "A" * 43,
        ],
    )
    def test_rejects_every_non_ingest_credential_before_network(
        self, wrong_role, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "engine.auth.paste.httpx.get",
            lambda *a, **k: pytest.fail("wrong credential role reached Cloud validation"),
        )
        with pytest.raises(TokenValidationError, match="pairing code or portal"):
            PasteTokenProvider().login(token=wrong_role)

    def test_login_no_token_raises(self, fake_keyring) -> None:
        with pytest.raises(TokenValidationError, match="No ingest token"):
            PasteTokenProvider(prompt=lambda _: "").login()
