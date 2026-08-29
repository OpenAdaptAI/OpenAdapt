"""Tests for the shared keychain credential store + auth_header()."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from engine.auth import store
from engine.auth.paste import PasteTokenProvider
from engine.auth.provider import AuthProvider, Credential


def _cred(host: str = "https://app.openadapt.ai", token: str = "oai_ingest_abc") -> Credential:
    return {
        "kind": "ingest_token",
        "token": token,
        "refresh_token": None,
        "org_id": "org_1",
        "host": host,
        "expires_at": None,
    }


_NEW_RUNNER_TOKEN = "oar_" + "d" * 64
_PRIOR_RUNNER_TOKEN = "oar_" + "e" * 64


def _runner_registration(token: str = _NEW_RUNNER_TOKEN) -> dict:
    return {
        "schema_version": "openadapt.hosted-runner-registration-result/v1",
        "runner_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "runner_session_id": "33333333-3333-4333-8333-333333333333",
        "runner_token": token,
        "token_expires_at": "2030-01-01T00:00:00Z",
        "local_runtime_release": {
            target: {
                "target": target,
                "admission_id": str(uuid5(NAMESPACE_URL, f"admission:{target}")),
                "admission_sha256": character * 64,
                "release_version": "1.0.0",
                "release_artifact_sha256": character * 64,
            }
            for target, character in (
                ("flow", "a"),
                ("desktop", "b"),
                ("capture", "c"),
            )
        },
    }


class TestCredentialStore:
    def test_store_and_load_roundtrip(self, fake_keyring) -> None:
        store.store_credential(_cred())
        loaded = store.load_credential("https://app.openadapt.ai")
        assert loaded is not None
        assert loaded["token"] == "oai_ingest_abc"
        assert loaded["org_id"] == "org_1"

    def test_load_missing_returns_none(self, fake_keyring) -> None:
        assert store.load_credential("https://nope.example") is None

    def test_store_sets_active_host(self, fake_keyring) -> None:
        store.store_credential(_cred(host="https://h1"))
        assert store.active_host() == "https://h1"
        store.store_credential(_cred(host="https://h2", token="oai_ingest_h2"))
        assert store.active_host() == "https://h2"

    def test_clear_credential(self, fake_keyring) -> None:
        store.store_credential(_cred())
        store.clear_credential("https://app.openadapt.ai")
        assert store.load_credential("https://app.openadapt.ai") is None

    def test_corrupt_credential_ignored(self, fake_keyring) -> None:
        # A corrupt companion JSON entry is ignored (and no raw token exists).
        fake_keyring.set_password(
            store.SERVICE_NAME, "https://bad" + store._CRED_SUFFIX, "{not json"
        )
        assert store.load_credential("https://bad") is None

    def test_raw_token_is_primary_value_for_tray(self, fake_keyring) -> None:
        # P0-5: the tray reads keyring value under account=host as a RAW bearer
        # token, so the primary value MUST be the token, not a JSON blob.
        store.store_credential(_cred(token="oai_ingest_raw"))
        primary = fake_keyring.get_password(store.SERVICE_NAME, "https://app.openadapt.ai")
        assert primary == "oai_ingest_raw"

    def test_load_from_bare_raw_token(self, fake_keyring) -> None:
        # A surface that stored only a raw token (no companion) still resolves.
        fake_keyring.set_password(store.SERVICE_NAME, "https://h", "oai_ingest_bare")
        loaded = store.load_credential("https://h")
        assert loaded is not None
        assert loaded["token"] == "oai_ingest_bare"
        assert loaded["kind"] == "ingest_token"

    def test_secure_store_writes_and_verifies_every_entry(self, fake_keyring) -> None:
        class _Backend:
            priority = 1

        fake_keyring.get_keyring = lambda: _Backend()
        assert store.secure_store_available() is True
        assert store.store_credential_secure(_cred()) is True
        assert store.load_credential("https://app.openadapt.ai") == _cred()

    def test_secure_store_cleans_partial_writes(self, fake_keyring) -> None:
        original = fake_keyring.set_password

        def _fail_companion(service, account, value):
            if account.endswith(store._CRED_SUFFIX):
                raise RuntimeError("locked")
            return original(service, account, value)

        fake_keyring.set_password = _fail_companion
        assert store.store_credential_secure(_cred()) is False
        assert fake_keyring.get_password(store.SERVICE_NAME, "https://app.openadapt.ai") is None
        assert fake_keyring.get_password(store.SERVICE_NAME, store._ACTIVE_HOST_ACCOUNT) is None

    def test_secure_store_restores_existing_credential_on_failure(self, fake_keyring) -> None:
        old = _cred(token="oai_ingest_old")
        store.store_credential(old)
        original = fake_keyring.set_password

        def _fail_new_companion(service, account, value):
            if account.endswith(store._CRED_SUFFIX) and "oai_ingest_new" in value:
                raise RuntimeError("locked")
            return original(service, account, value)

        fake_keyring.set_password = _fail_new_companion
        assert store.store_credential_secure(_cred(token="oai_ingest_new")) is False
        assert store.load_credential("https://app.openadapt.ai") == old

    def test_runner_registration_replaces_one_exact_keychain_entry(
        self, fake_keyring
    ) -> None:
        host = "https://app.openadapt.ai"

        assert store.store_runner_registration_secure(
            host, _runner_registration()
        ) is True

        assert store.load_runner_credential(host) == _runner_registration()
        assert fake_keyring.get_password(store.SERVICE_NAME, host) is None

    def test_runner_registration_restores_prior_entry_when_write_fails(
        self, fake_keyring
    ) -> None:
        host = "https://app.openadapt.ai"
        prior = _runner_registration(_PRIOR_RUNNER_TOKEN)
        assert store.store_runner_registration_secure(host, prior) is True
        original = fake_keyring.set_password

        def fail_replacement(service, account, value):
            if account.endswith(store._RUNNER_SUFFIX) and _NEW_RUNNER_TOKEN in value:
                raise RuntimeError("locked")
            return original(service, account, value)

        fake_keyring.set_password = fail_replacement

        assert store.store_runner_registration_secure(
            host, _runner_registration()
        ) is False
        assert store.load_runner_credential(host) == prior

    def test_runner_registration_rejects_partial_release_binding(
        self, fake_keyring
    ) -> None:
        registration = _runner_registration()
        del registration["local_runtime_release"]["capture"]

        assert store.store_runner_registration_secure(
            "https://app.openadapt.ai", registration
        ) is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("runner_token", "oar_not-canonical"),
            ("tenant_id", "tenant-not-a-uuid"),
            ("token_expires_at", "2030-01-01T00:00:00+00:00"),
        ],
    )
    def test_runner_registration_rejects_noncanonical_identity_fields(
        self,
        fake_keyring,
        field: str,
        value: str,
    ) -> None:
        registration = _runner_registration()
        registration[field] = value

        assert store.store_runner_registration_secure(
            "https://app.openadapt.ai", registration
        ) is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("admission_id", "admission-flow"),
            ("release_version", "release version with spaces"),
        ],
    )
    def test_runner_registration_rejects_noncanonical_release_fields(
        self,
        fake_keyring,
        field: str,
        value: str,
    ) -> None:
        registration = _runner_registration()
        registration["local_runtime_release"]["flow"][field] = value

        assert store.store_runner_registration_secure(
            "https://app.openadapt.ai", registration
        ) is False


class TestAuthHeader:
    def test_env_token_takes_precedence(self, fake_keyring, monkeypatch) -> None:
        store.store_credential(_cred(token="oai_ingest_stored"))
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "oai_ingest_env")
        assert store.auth_header() == {"Authorization": "Bearer oai_ingest_env"}

    def test_falls_back_to_active_credential(self, fake_keyring) -> None:
        store.store_credential(_cred(token="oai_ingest_stored"))
        assert store.auth_header() == {"Authorization": "Bearer oai_ingest_stored"}

    def test_empty_when_no_credential(self, fake_keyring) -> None:
        assert store.auth_header() == {}

    def test_host_bound_header_refuses_active_credential_for_other_origin(
        self, fake_keyring
    ) -> None:
        store.store_credential(_cred(host="https://app.openadapt.ai"))

        assert store.auth_header("https://customer.example") == {}
        assert store.auth_header("https://app.openadapt.ai/dashboard") == {
            "Authorization": "Bearer oai_ingest_abc"
        }

    def test_explicit_environment_token_is_operator_override(
        self, fake_keyring, monkeypatch
    ) -> None:
        store.store_credential(_cred(host="https://app.openadapt.ai"))
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "explicit-env-token")

        assert store.auth_header("https://customer.example") == {
            "Authorization": "Bearer explicit-env-token"
        }

    def test_host_bound_header_refuses_unsafe_destination_even_with_environment_token(
        self, fake_keyring, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "explicit-env-token")

        assert store.auth_header("http://customer.example") == {}
        assert store.auth_header("https://user:secret@customer.example") == {}
        assert store.auth_header("not-a-url") == {}

    def test_canonical_host_origin_allows_only_https_or_local_http(self) -> None:
        assert (
            store.canonical_host_origin("HTTPS://Customer.Example:443/dashboard")
            == "https://customer.example"
        )
        assert (
            store.canonical_host_origin("http://localhost:3000/path")
            == "http://localhost:3000"
        )
        assert store.canonical_host_origin("http://customer.example") == ""
        assert store.canonical_host_origin("https://user@example.com") == ""


class _RaisingKeyring:
    """A keyring backend with no usable store -- every call raises.

    Mimics ``keyring.errors.NoKeyringError`` on a headless box, where the
    ``keyring`` PACKAGE imports fine but no Secret Service backend exists.
    """

    class _NoBackend(Exception):
        pass

    def get_password(self, service, account):
        raise self._NoBackend("No recommended backend was available")

    def set_password(self, service, account, password):
        raise self._NoBackend("No recommended backend was available")

    def delete_password(self, service, account):
        raise self._NoBackend("No recommended backend was available")


class TestHeadlessDegrade:
    """P0-4: a missing keyring backend must DEGRADE, never raise.

    ``auth_header()`` runs on every outbound hosted call; a raise here crashed
    the whole cloud/BYOC lane on any headless box.
    """

    @pytest.fixture
    def raising_keyring(self, monkeypatch):
        kr = _RaisingKeyring()
        monkeypatch.setattr("engine.auth.store._keyring", lambda: kr)
        monkeypatch.delenv("OPENADAPT_INGEST_TOKEN", raising=False)
        return kr

    def test_auth_header_degrades_to_empty(self, raising_keyring) -> None:
        assert store.auth_header() == {}

    def test_store_credential_does_not_raise(self, raising_keyring) -> None:
        # Log-and-skip: persistence fails silently, no exception propagates.
        store.store_credential(_cred())

    def test_load_and_active_degrade_to_none(self, raising_keyring) -> None:
        assert store.load_credential("https://app.openadapt.ai") is None
        assert store.active_host() is None
        assert store.active_credential() is None

    def test_clear_credential_does_not_raise(self, raising_keyring) -> None:
        store.clear_credential("https://app.openadapt.ai")

    def test_env_token_still_works_without_backend(self, raising_keyring, monkeypatch) -> None:
        monkeypatch.setenv("OPENADAPT_INGEST_TOKEN", "oai_ingest_env")
        assert store.auth_header() == {"Authorization": "Bearer oai_ingest_env"}


class TestProtocolConformance:
    def test_paste_is_auth_provider(self) -> None:
        assert isinstance(PasteTokenProvider(), AuthProvider)

    def test_browser_is_auth_provider(self) -> None:
        from engine.auth.browser_pkce import BrowserPkceProvider

        assert isinstance(BrowserPkceProvider(), AuthProvider)

    def test_credential_shape(self) -> None:
        c = _cred()
        assert set(c.keys()) == {
            "kind", "token", "refresh_token", "org_id", "host", "expires_at"
        }
        with pytest.raises(KeyError):
            _ = c["nonexistent"]  # type: ignore[misc]
