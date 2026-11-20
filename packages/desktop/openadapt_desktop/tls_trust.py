"""Give the installed engine a certificate bundle when Python has none.

A frozen Python keeps the certificate paths of the machine that built it. On
macOS and Linux those paths don't exist on the user's computer, so every HTTPS
download through ``ssl``'s default context (the vision runtime, the FFmpeg
runtime) fails with CERTIFICATE_VERIFY_FAILED. Windows is unaffected: Python
reads the Windows certificate store there.

``ensure_tls_trust_store`` runs once at engine start. When OpenSSL's default
paths are missing it sets ``SSL_CERT_FILE`` for this process and, through the
inherited environment, for every child the engine starts. Verification stays
on; this only tells OpenSSL where the trusted roots are.
"""

from __future__ import annotations

import os
import ssl
import sys
from collections.abc import MutableMapping
from pathlib import Path

# The operating system's own bundle comes first, so roots an administrator
# added or removed are honored. certifi's bundle is the fallback.
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine, OpenBSD
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
)


def _certifi_bundle() -> str | None:
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def _has_entries(directory: str | None) -> bool:
    if not directory:
        return False
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def ensure_tls_trust_store(
    environ: MutableMapping[str, str] | None = None,
    *,
    platform: str | None = None,
    candidates: tuple[str, ...] | None = None,
) -> str | None:
    """Set ``SSL_CERT_FILE`` when OpenSSL's default paths are missing.

    Returns the bundle it chose, or None when nothing needed to change: the
    operator already named a bundle, Python's defaults exist, or this is
    Windows.
    """

    env = os.environ if environ is None else environ
    if (platform or sys.platform) == "win32":
        return None
    if env.get("SSL_CERT_FILE") or env.get("SSL_CERT_DIR"):
        return None
    defaults = ssl.get_default_verify_paths()
    # ``cafile`` and ``capath`` are None when the built-in paths don't exist.
    # A directory that exists but holds nothing is no trust store either.
    if defaults.cafile or _has_entries(defaults.capath):
        return None
    options = list(SYSTEM_CA_BUNDLES if candidates is None else candidates)
    bundled = _certifi_bundle()
    if bundled:
        options.append(bundled)
    for option in options:
        try:
            if Path(option).is_file() and Path(option).stat().st_size > 0:
                env["SSL_CERT_FILE"] = option
                return option
        except OSError:
            continue
    return None
