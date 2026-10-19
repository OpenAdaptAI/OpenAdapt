"""Tests for OS keychain access to the ingest token."""

from unittest.mock import patch

from keyring.errors import PasswordDeleteError

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


class TestClearIngestToken:
    """``clear_ingest_token`` promises "removed (or already absent)"."""

    def test_removed_returns_true(self):
        with patch.object(keychain, "KEYRING_AVAILABLE", True), patch.object(
            keychain, "keyring"
        ) as mock_keyring:
            mock_keyring.errors.PasswordDeleteError = PasswordDeleteError
            assert keychain.clear_ingest_token("https://example.test") is True

    def test_absent_entry_is_success(self):
        """Deleting a token that was never stored is a no-op success.

        keyring raises ``PasswordDeleteError`` when the entry is absent. This
        used to be caught and reported as failure, so signing out on a machine
        that had never signed in looked like a failed sign-out.
        """
        with patch.object(keychain, "KEYRING_AVAILABLE", True), patch.object(
            keychain, "keyring"
        ) as mock_keyring:
            mock_keyring.errors.PasswordDeleteError = PasswordDeleteError
            mock_keyring.delete_password.side_effect = PasswordDeleteError("absent")
            assert keychain.clear_ingest_token("https://example.test") is True

    def test_backend_error_returns_false(self):
        """A real backend failure is still a failure."""
        with patch.object(keychain, "KEYRING_AVAILABLE", True), patch.object(
            keychain, "keyring"
        ) as mock_keyring:
            mock_keyring.errors.PasswordDeleteError = PasswordDeleteError
            mock_keyring.delete_password.side_effect = RuntimeError("backend down")
            assert keychain.clear_ingest_token("https://example.test") is False

    def test_no_keyring_backend_returns_false(self):
        with patch.object(keychain, "KEYRING_AVAILABLE", False):
            assert keychain.clear_ingest_token("https://example.test") is False
