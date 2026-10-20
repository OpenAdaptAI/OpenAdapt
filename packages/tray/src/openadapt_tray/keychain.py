"""OS keychain access for the hosted ingest token.

Secrets are NEVER written to ``tray.json`` / ``config.toml``. The ingest token
lives in the OS secure store (macOS Keychain / Windows Credential Manager /
Linux Secret Service) via ``keyring``, under the SAME service name the desktop
app's ``engine/auth/store.py`` uses so both surfaces share one credential.

Resolution precedence for reads (mirrors the desktop ``push`` tool):
    ``OPENADAPT_INGEST_TOKEN`` env var → keychain entry for the host.
"""

import os

# Shared with the desktop app (spec §3a/§3e): service "ai.openadapt.desktop",
# account keyed by hosted host.
KEYCHAIN_SERVICE = "ai.openadapt.desktop"
ENV_INGEST_TOKEN = "OPENADAPT_INGEST_TOKEN"

try:
    import keyring

    KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover - keyring backend may be unavailable
    keyring = None  # type: ignore[assignment]
    KEYRING_AVAILABLE = False


def _account(host: str) -> str:
    """Normalize a hosted host into a keychain account key."""
    return (host or "").rstrip("/")


def get_ingest_token(host: str) -> str | None:
    """Resolve the ingest token for a hosted host.

    Args:
        host: The hosted base URL (e.g. ``https://app.openadapt.ai``).

    Returns:
        The bearer token, or ``None`` if unset.
    """
    env = os.environ.get(ENV_INGEST_TOKEN)
    if env:
        return env

    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, _account(host))
    except Exception as e:  # pragma: no cover - backend errors
        print(f"Keychain read failed: {e}")
        return None


def set_ingest_token(host: str, token: str) -> bool:
    """Store the ingest token for a hosted host in the OS keychain.

    Args:
        host: The hosted base URL.
        token: The bearer token to store.

    Returns:
        True if stored successfully.
    """
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.set_password(KEYCHAIN_SERVICE, _account(host), token)
        return True
    except Exception as e:  # pragma: no cover - backend errors
        print(f"Keychain write failed: {e}")
        return False


def clear_ingest_token(host: str) -> bool:
    """Remove the stored ingest token for a hosted host.

    Args:
        host: The hosted base URL.

    Returns:
        True if removed (or already absent).
    """
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, _account(host))
        return True
    except keyring.errors.PasswordDeleteError:
        # keyring raises this when the entry is absent. The contract is "removed
        # OR already absent", so this IS the success case -- returning False here
        # made a signed-out tray look like a failed sign-out.
        return True
    except Exception as e:  # pragma: no cover - backend errors
        print(f"Keychain delete failed: {e}")
        return False


def has_ingest_token(host: str) -> bool:
    """Return True if an ingest token is resolvable for the host."""
    return get_ingest_token(host) is not None
