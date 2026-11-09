"""TLS + certificate-pinning for the in-guest win_agent PHI channel.

The win_agent HTTP channel carries PHI in the clear: ``/screenshot`` returns a
PNG of the live patient chart and ``/execute_windows`` carries the commands that
read/write it. The 2026 HIPAA Security Rule makes **encryption in transit**
mandatory, so before any PHI lane (self-hosted included) this channel must be
**encrypted AND authenticated**. This module provides the small, dependency-honest
pieces both ends use.

Trust model (per-run self-signed cert + certificate pinning)
------------------------------------------------------------
The **control plane** (the harness that launches the agent) mints a fresh
self-signed certificate for the run with :func:`generate_self_signed_cert`,
provisions the cert + key **into the guest** (the agent serves HTTPS with them),
and hands the **client** the cert's SHA-256 **fingerprint**. The client
(:class:`~openadapt_flow.backends.windows_backend.WindowsBackend`) **pins** that
exact fingerprint via :func:`pinned_session`: the TLS session is accepted only if
the server presents the one certificate the control plane provisioned. An
attacker who intercepts the LAN and offers *any* other certificate — including a
valid CA-signed one — presents a different fingerprint and the handshake is
**rejected**.

Why pinning and not a CA / mutual-TLS:

* **vs. plain CA validation** — the cert is self-signed and per-run, so there is
  no CA to trust; pinning is what makes a self-signed cert safe (it removes the
  MITM window a self-signed cert would otherwise open).
* **vs. mutual-TLS** — mTLS also works, but it requires provisioning *and*
  verifying a **second** (client) certificate. Pinning reuses the exact channel
  that already carries the per-run bearer token to carry one 64-hex-char
  fingerprint, so it is the **simpler robust option** for a per-run trust root.

The per-run bearer token is retained as an independent **second factor**:
encryption + server-identity from TLS/pinning, caller-authorization from the
token. Losing either does not silently open the channel.

Dependency posture
-------------------
Certificate *minting* uses the audited :mod:`cryptography` library and happens on
the **control plane** (which already depends on it — see
:mod:`openadapt_flow.crypto`), not necessarily in the guest. The **guest agent**
only needs stdlib :mod:`ssl` to wrap its socket with the provisioned cert/key
files, so ``server.py`` stays framework-free. The **client** pin uses
:mod:`requests`/:mod:`urllib3` (already the Windows client dependency). Every
heavy import here is lazy so this module imports on any OS.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ssl

    import requests


# Default subject-alternative names a per-run cert is valid for: the loopback
# tunnel endpoint the client usually reaches, plus the two loopback spellings.
_DEFAULT_SANS: tuple[str, ...] = ("127.0.0.1", "localhost")


@dataclass(frozen=True)
class CertBundle:
    """A provisioned per-run certificate: file paths + pin fingerprint.

    Args:
        certfile: Path to the PEM certificate the agent serves.
        keyfile: Path to the PEM private key (written ``0600``).
        fingerprint: Lower-case hex SHA-256 of the **DER** certificate — the
            value the client pins (matches what :mod:`urllib3` computes over the
            peer certificate).
    """

    certfile: str
    keyfile: str
    fingerprint: str


def normalize_fingerprint(fingerprint: str) -> str:
    """Return ``fingerprint`` as lower-case hex with any ``:`` separators removed.

    Accepts both the colon-grouped form (``AA:BB:...``) openssl prints and the
    bare hex :mod:`urllib3` expects, so a control plane may hand either.

    Raises:
        ValueError: If the result is not valid hex of a supported digest length
            (SHA-256/64, SHA-1/40, MD5/32 hex chars).
    """
    cleaned = fingerprint.replace(":", "").replace(" ", "").strip().lower()
    try:
        bytes.fromhex(cleaned)
    except ValueError as e:
        raise ValueError(f"not a hex fingerprint: {fingerprint!r}") from e
    if len(cleaned) not in (32, 40, 64):
        raise ValueError(
            f"unexpected fingerprint length {len(cleaned)} (want SHA-256/40/32 hex)"
        )
    return cleaned


def fingerprint_from_der(der: bytes) -> str:
    """SHA-256 of a DER certificate, as lower-case hex (the pin value)."""
    return hashlib.sha256(der).hexdigest()


def fingerprint_from_pem_file(certfile: str) -> str:
    """SHA-256 pin fingerprint of the certificate in a PEM file."""
    import ssl  # noqa: PLC0415 - stdlib, lazy to keep module import light

    with open(certfile, encoding="ascii") as fh:
        pem = fh.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    return fingerprint_from_der(der)


def generate_self_signed_cert(
    hostnames: Optional[list[str]] = None,
    *,
    dirpath: Optional[str] = None,
    valid_days: int = 1,
) -> CertBundle:
    """Mint a fresh per-run self-signed cert/key and return its :class:`CertBundle`.

    Runs on the **control plane** (uses :mod:`cryptography`). The cert is valid
    for a short window (``valid_days``, default 1) because it is minted per run,
    and its SAN covers ``hostnames`` plus the loopback spellings so the client
    can reach the agent by IP or name. IP-literal SANs are emitted as
    ``IPAddress`` entries and hostnames as ``DNSName`` entries.

    Args:
        hostnames: Extra names/IPs the cert must be valid for (e.g. the guest's
            LAN IP when the agent binds ``0.0.0.0``). Loopback is always added.
        dirpath: Directory to write ``agent-cert.pem`` / ``agent-key.pem`` into.
            When omitted a fresh ``mkdtemp`` directory is used (caller owns
            cleanup). Never commit these files.
        valid_days: Certificate lifetime in days.

    Returns:
        A :class:`CertBundle` with the written file paths and the pin fingerprint.
    """
    import datetime  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415

    names: list[str] = list(_DEFAULT_SANS)
    for h in hostnames or []:
        if h and h not in names:
            names.append(h)

    san_entries: list[x509.GeneralName] = []
    for name in names:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "openadapt-flow win_agent")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    out_dir = dirpath or tempfile.mkdtemp(prefix="oaflow-agent-tls-")
    os.makedirs(out_dir, exist_ok=True)
    certfile = os.path.join(out_dir, "agent-cert.pem")
    keyfile = os.path.join(out_dir, "agent-key.pem")

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Write the key private (0600) BEFORE its bytes land, so the secret never
    # exists world-readable even momentarily.
    key_fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(key_fd, "wb") as fh:
        fh.write(key_pem)
    with open(certfile, "wb") as fh:
        fh.write(cert_pem)

    return CertBundle(
        certfile=certfile,
        keyfile=keyfile,
        fingerprint=fingerprint_from_der(cert.public_bytes(serialization.Encoding.DER)),
    )


def server_ssl_context(certfile: str, keyfile: str) -> "ssl.SSLContext":
    """Build a TLS **server** context from a provisioned cert/key pair.

    Uses stdlib :mod:`ssl` only (the guest needs no extra dependency). TLS 1.2 is
    the floor; the client authenticates the server by **pinned fingerprint**, so
    no client certificate is requested here.

    Raises:
        FileNotFoundError: If ``certfile`` / ``keyfile`` do not exist.
    """
    import ssl  # noqa: PLC0415

    for path in (certfile, keyfile):
        if not os.path.exists(path):
            raise FileNotFoundError(f"TLS material not found: {path}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def pinned_session(
    fingerprint: str,
    *,
    session: "Optional[requests.Session]" = None,
) -> "requests.Session":
    """Return a :mod:`requests` session that pins the server cert ``fingerprint``.

    The mounted HTTPS adapter accepts the connection **only** when the peer
    certificate's SHA-256 fingerprint equals ``fingerprint`` (via
    :mod:`urllib3`'s ``assert_fingerprint``). Because identity is proven by the
    pin, ordinary CA-chain / hostname verification is intentionally not required
    (the self-signed per-run cert has no chain and often no matching hostname) —
    the pin is a **stronger**, exact-match check than either.

    Args:
        fingerprint: The hex SHA-256 fingerprint provisioned by the control
            plane (colon-grouped or bare; normalized here).
        session: An existing session to harden in place (else a fresh one).

    Returns:
        The session with the pinning adapter mounted on ``https://``.
    """
    import requests  # noqa: PLC0415
    from requests.adapters import HTTPAdapter  # noqa: PLC0415

    pin = normalize_fingerprint(fingerprint)

    class _FingerprintPinnedAdapter(HTTPAdapter):
        """HTTPAdapter that pins the server cert fingerprint on every request.

        The pin is applied in :meth:`cert_verify` (requests' own per-request
        hook), NOT just ``init_poolmanager``: requests would otherwise re-set
        ``cert_reqs`` to ``CERT_REQUIRED`` here from the default ``verify=True``
        and a per-run self-signed cert would fail chain validation before the
        pin is ever checked. Setting the pin here makes ``urllib3`` authenticate
        the peer by exact fingerprint match instead -- a strictly stronger,
        MITM-proof check for a per-run trust root.
        """

        def cert_verify(  # type: ignore[override]
            self, conn: object, url: str, verify: object, cert: object
        ) -> None:
            conn.assert_fingerprint = pin  # type: ignore[attr-defined]
            # Identity comes from the exact-match pin; skip chain + hostname
            # checks a per-run self-signed cert cannot satisfy.
            conn.cert_reqs = "CERT_NONE"  # type: ignore[attr-defined]
            conn.assert_hostname = False  # type: ignore[attr-defined]

    sess = session if session is not None else requests.Session()
    sess.mount("https://", _FingerprintPinnedAdapter())
    return sess
