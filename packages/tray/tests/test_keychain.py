"""Tests for OS keychain access to the ingest token."""

from unittest.mock import patch


from openadapt_tray import keychain


class TestIngestToken:
    def test_env_var_takes_precedence(self, monkeypatch):
        """OPENADAPT_INGEST_TOKEN env var wins over the keychain."""
        monkeypatch.setenv(keychain.ENV_INGEST_TOKEN, "env-token")
        with patch.object(keychain, "keyring") as mock_keyring:
            token = keychain.get_ingest_token("https://example.test")
        assert token == "env-token"
        mock_keyring.get_password.assert_not_called()

    def test_reads_from_keyring(self, monkeypatch):
        """Falls back to the keychain when no env var is set."""
        monkeypatch.delenv(keychain.ENV_INGEST_TOKEN, raising=False)
        with patch.object(keychain, "KEYRING_AVAILABLE", True), patch.object(
            keychain, "keyring"
        ) as mock_keyring:
            mock_keyring.get_password.return_value = "kc-token"
            token = keychain.get_ingest_token("https://example.test/")
        assert token == "kc-token"
        # Account key is the host with any trailing slash stripped.
        mock_keyring.get_password.assert_called_once_with(
            keychain.KEYCHAIN_SERVICE, "https://example.test"
        )

    def test_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv(keychain.ENV_INGEST_TOKEN, raising=False)
        with patch.object(keychain, "KEYRING_AVAILABLE", True), patch.object(
            keychain, "keyring"
        ) as mock_keyring:
            mock_keyring.get_password.return_value = None
            assert keychain.get_ingest_token("https://example.test") is None

    def test_shared_service_name_matches_desktop(self):
        """The keychain service must match the desktop app's store (§3a)."""
        assert keychain.KEYCHAIN_SERVICE == "ai.openadapt.desktop"

    def test_has_ingest_token(self, monkeypatch):
        monkeypatch.setenv(keychain.ENV_INGEST_TOKEN, "x")
        assert keychain.has_ingest_token("https://example.test") is True
