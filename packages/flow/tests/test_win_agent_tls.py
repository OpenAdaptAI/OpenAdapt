"""TLS + certificate-pinning tests for the win_agent PHI channel.

Proves the in-transit encryption controls end to end against a REAL HTTPS
listener on loopback (no live VM/desktop, fake PNG grabber):

* a client that pins the server's per-run cert fingerprint completes the TLS
  handshake and drives ``/screenshot`` + ``/execute_windows``;
* a client that pins the WRONG fingerprint is rejected at the handshake;
* the fail-closed switch refuses plaintext when TLS is required (no silent
  downgrade);
* the bearer token remains an independent second factor over TLS.

Certs are minted per-test into a tmp dir and never committed.
"""

from __future__ import annotations

import struct
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests

from openadapt_flow.backends import WindowsBackend
from openadapt_flow.backends.win_agent import (
    AgentConfig,
    CertBundle,
    create_server,
    fingerprint_from_pem_file,
    generate_self_signed_cert,
    normalize_fingerprint,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _fake_png() -> bytes:
    ihdr = struct.pack(">II", 4, 2)
    return _PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 8


class RunningTLSAgent:
    """A started HTTPS agent server plus its base URL (context-managed)."""

    def __init__(self, config: AgentConfig, *, uia_fn=None) -> None:
        kwargs = {"grab_fn": _fake_png}
        if uia_fn is not None:
            kwargs["uia_fn"] = uia_fn
        self.server = create_server(config, **kwargs)
        host, port = self.server.server_address[:2]
        self.url = f"https://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def cert(tmp_path: Path) -> CertBundle:
    return generate_self_signed_cert(dirpath=str(tmp_path / "certs"))


@pytest.fixture()
def tls_agent(cert: CertBundle) -> Iterator[RunningTLSAgent]:
    a = RunningTLSAgent(
        AgentConfig(
            host="127.0.0.1", port=0, certfile=cert.certfile, keyfile=cert.keyfile
        ),
        uia_fn=lambda operation, payload: {"status": "ok", "text": None},
    )
    yield a
    a.close()


# -- cert minting -------------------------------------------------------------


def test_generated_cert_is_on_disk_and_key_is_private(cert: CertBundle) -> None:
    assert Path(cert.certfile).exists()
    key_mode = Path(cert.keyfile).stat().st_mode & 0o777
    assert key_mode == 0o600  # private key never world-readable
    assert len(cert.fingerprint) == 64  # SHA-256 hex


def test_fingerprint_helpers_agree(cert: CertBundle) -> None:
    assert fingerprint_from_pem_file(cert.certfile) == cert.fingerprint
    # colon-grouped == bare hex after normalization
    grouped = ":".join(
        cert.fingerprint[i : i + 2] for i in range(0, len(cert.fingerprint), 2)
    )
    assert normalize_fingerprint(grouped) == cert.fingerprint


def test_normalize_rejects_non_hex() -> None:
    with pytest.raises(ValueError):
        normalize_fingerprint("not-a-fingerprint")


# -- handshake + pinning success ----------------------------------------------


def test_pinned_client_completes_handshake_and_gets_screenshot(
    tls_agent: RunningTLSAgent, cert: CertBundle
) -> None:
    # The PHI screenshot travels over the pinned TLS session and validates as a
    # PNG -- this is the encrypted-in-transit + pinned-identity assertion. (No
    # pyautogui: the /screenshot path uses the injected fake grabber.)
    backend = WindowsBackend(tls_agent.url, pin_fingerprint=cert.fingerprint)
    assert backend.probe() is True
    assert backend.screenshot().startswith(_PNG_SIGNATURE)


def test_pinned_typed_observation_channel_works_over_tls(
    tls_agent: RunningTLSAgent, cert: CertBundle
) -> None:
    # A PHI-bearing typed observation also round-trips over the pinned TLS
    # session. The arbitrary-exec compatibility route remains disabled.
    from openadapt_flow.backends.win_agent import pinned_session

    sess = pinned_session(cert.fingerprint)
    resp = sess.post(
        f"{tls_agent.url}/uia/text-at-point",
        json={"x": 1, "y": 1},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "text": None}
    legacy = sess.post(
        f"{tls_agent.url}/execute_windows",
        json={"command": "print('must not execute')"},
        timeout=5,
    )
    assert legacy.status_code == 404


def test_plaintext_client_cannot_talk_to_tls_server(
    tls_agent: RunningTLSAgent, cert: CertBundle
) -> None:
    # Point a plaintext client at the HTTPS port -> transport failure, never a
    # silent success. (Loopback http:// is allowed to construct; the call fails.)
    http_url = tls_agent.url.replace("https://", "http://")
    backend = WindowsBackend(http_url, require_tls=False)
    assert backend.probe() is False


# -- wrong / unpinned cert is REJECTED ----------------------------------------


def test_wrong_pinned_fingerprint_is_rejected(
    tls_agent: RunningTLSAgent, tmp_path: Path
) -> None:
    # A DIFFERENT per-run cert -> different fingerprint. Pinning it must reject
    # the handshake with the server's real cert (MITM defense).
    other = generate_self_signed_cert(dirpath=str(tmp_path / "other"))
    backend = WindowsBackend(tls_agent.url, pin_fingerprint=other.fingerprint)
    # The action path must FAIL LOUDLY (never a silent no-op) on a bad cert.
    with pytest.raises(RuntimeError):
        backend.click(1, 1)
    # And the screenshot path raises rather than returning bytes.
    with pytest.raises(RuntimeError):
        backend.screenshot()
    # probe() swallows it into a clean False (still never a false "ok").
    assert backend.probe() is False


def test_wrong_fingerprint_raises_sslerror_at_transport(
    tls_agent: RunningTLSAgent, tmp_path: Path
) -> None:
    # Prove the rejection is a TLS/cert failure specifically, at the requests layer.
    from openadapt_flow.backends.win_agent import pinned_session

    other = generate_self_signed_cert(dirpath=str(tmp_path / "other2"))
    sess = pinned_session(other.fingerprint)
    with pytest.raises(requests.exceptions.SSLError):
        sess.get(f"{tls_agent.url}/screenshot", timeout=5)


# -- fail closed: plaintext refused when require_tls --------------------------


def test_require_tls_refuses_plaintext_nonloopback() -> None:
    # Default (require_tls=None) infers required for a non-loopback host.
    with pytest.raises(ValueError, match="refusing plaintext"):
        WindowsBackend("http://10.0.0.5:5000")
    # Explicit require_tls=True refuses even for loopback.
    with pytest.raises(ValueError, match="refusing plaintext"):
        WindowsBackend("http://127.0.0.1:5000", require_tls=True)


def test_loopback_plaintext_allowed_with_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        WindowsBackend("http://127.0.0.1:5000")
    assert any("plaintext" in str(w.message).lower() for w in caught)


def test_nonloopback_https_no_pin_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        WindowsBackend("https://10.0.0.5:5000")  # require_tls satisfied (https)
    assert any("pin_fingerprint" in str(w.message) for w in caught)


# -- token remains an independent second factor over TLS ----------------------


def test_tls_plus_token_enforces_both(cert: CertBundle) -> None:
    agent = RunningTLSAgent(
        AgentConfig(
            host="127.0.0.1",
            port=0,
            token="s3cret",
            certfile=cert.certfile,
            keyfile=cert.keyfile,
        )
    )
    try:
        # Right cert, right token -> ok.
        good = WindowsBackend(
            agent.url, pin_fingerprint=cert.fingerprint, auth_token="s3cret"
        )
        assert good.probe() is True
        # Right cert (TLS ok), NO token -> rejected by auth (401 -> loud raise).
        no_token = WindowsBackend(agent.url, pin_fingerprint=cert.fingerprint)
        assert no_token.probe() is False
        with pytest.raises(RuntimeError):
            no_token.click(1, 1)
    finally:
        agent.close()


# -- half-configured TLS pair fails closed ------------------------------------


def test_half_configured_tls_pair_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BOTH certfile and keyfile"):
        AgentConfig(certfile=str(tmp_path / "c.pem"))  # keyfile missing
    with pytest.raises(ValueError, match="BOTH certfile and keyfile"):
        AgentConfig(keyfile=str(tmp_path / "k.pem"))  # certfile missing
