"""Qualification evidence from a sealed run directory (openadapt_flow/sealed_set.py).

A run of a sealed bundle writes a sealed set, so a case's report and frames
exist on disk only as ciphertext under their logical names. Qualification
binds such a file by the digest of its plaintext. The sealed set
authenticates the ciphertext and decrypts it in memory, so a changed byte on
disk, a wrong digest or a missing file is never evidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openadapt_flow import crypto
from openadapt_flow.bundle_sealing import seal_bundle
from openadapt_flow.qualification import EvidenceRef, _read_evidence_bytes
from openadapt_flow.qualify.campaign import _evidence_bytes, _evidence_ref
from tests.test_cli_seal import _KEY, _source

PLAIN = b'{"note": "complete-bundle-marker"}'


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    seal_bundle(_source(tmp_path), tmp_path / "evidence" / "sealed")
    return tmp_path / "evidence"


def _ref(relative_path: str, payload: bytes) -> EvidenceRef:
    return EvidenceRef(
        kind="other",
        sha256=hashlib.sha256(payload).hexdigest(),
        relative_path=relative_path,
    )


def test_a_sealed_file_is_evidence_by_its_plaintext(sealed: Path) -> None:
    payload, error = _read_evidence_bytes(
        root=sealed, evidence=_ref("sealed/param_hygiene.json", PLAIN)
    )
    assert error is None and payload == PLAIN
    # qualify binds the same file the same way.
    path = sealed / "sealed" / "param_hygiene.json"
    assert _evidence_bytes(path) == PLAIN
    assert _evidence_ref(sealed, path, "other").sha256 == hashlib.sha256(
        PLAIN
    ).hexdigest()


def test_a_wrong_digest_or_a_missing_file_is_not_evidence(sealed: Path) -> None:
    _payload, error = _read_evidence_bytes(
        root=sealed, evidence=_ref("sealed/param_hygiene.json", b"other bytes")
    )
    assert error is not None and "hash mismatch" in error
    _payload, error = _read_evidence_bytes(
        root=sealed, evidence=_ref("sealed/absent.json", PLAIN)
    )
    assert error is not None and "missing" in error


def test_changed_ciphertext_is_not_evidence(sealed: Path) -> None:
    physical = sealed / "sealed" / "param_hygiene.json.enc"
    data = bytearray(physical.read_bytes())
    data[-1] ^= 0x01
    physical.write_bytes(bytes(data))
    _payload, error = _read_evidence_bytes(
        root=sealed, evidence=_ref("sealed/param_hygiene.json", PLAIN)
    )
    assert error is not None
