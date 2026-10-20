"""Real public Sigstore records, verified offline with the shipped trust roots.

``tests/fixtures/rekor_real`` holds the landing demo's referral r3 Seal
(synthetic patient, redacted report) and the run authorization it was
sealed under, as anchored in the public Rekor log (rekor.sigstore.dev) and
timestamped by the Sigstore TSA on 2026-10-20. Rekor log indexes: Seal
3339635766, authorization 3339630782.

Nothing here touches the network: these tests check that real Rekor entries
(Ed25519ph ``hashedrekord``) and real Sigstore TSA tokens verify against the
Rekor key and TSA root built into OpenAdapt, and that a change to any of
them is refused.
"""

from __future__ import annotations

import copy
import json
import urllib.request
from pathlib import Path

import pytest
from openadapt_types.seal import parse_key_registry

from openadapt_flow import ssh_sig
from openadapt_flow.offline_seal import LocalSealV1, seal_sha256, verify_local_seal
from openadapt_flow.run_authorization import NAMESPACE, verify, verify_anchor
from openadapt_flow.seal_anchor import verify_anchor as verify_seal_anchor

FIXTURES = Path(__file__).parent / "fixtures" / "rekor_real"
AUTH = FIXTURES / "run-authorization"
# The founder's key as github.com/abrichr.keys listed it when the fixture was made.
FOUNDER = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEqSb2q1hQenZ7L7Ae78iAbYWEU4xmIrj3wuvNwasrgv"
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("these checks must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _registry():
    return parse_key_registry(_json(FIXTURES / "seal-key.json"))


def test_the_real_seal_and_its_rekor_entry_verify_offline():
    seal = LocalSealV1.model_validate(_json(FIXTURES / "seal.json")).wire()
    registry = _registry()
    assert verify_local_seal(seal, seal["subject_sha256"], registry).ok
    anchor = _json(FIXTURES / "seal.anchor.json")
    assert anchor["entry"]["logIndex"] == 3339635766
    raw = __import__("base64").b64decode(registry[0].public_key)
    result = verify_seal_anchor(seal, anchor, raw)  # the built-in public Rekor key
    assert result.ok, result.reason
    assert result.log_index == 3339635766


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: a["entry"]["verification"]["inclusionProof"]["hashes"].__setitem__(
            0, "00" * 32
        ),
        lambda a: a["entry"].__setitem__(
            "integratedTime", a["entry"]["integratedTime"] + 1
        ),
        lambda a: a.__setitem__("nonce", "00" * 32),
    ],
)
def test_a_changed_real_entry_is_refused(edit):
    seal = LocalSealV1.model_validate(_json(FIXTURES / "seal.json")).wire()
    raw = __import__("base64").b64decode(_registry()[0].public_key)
    anchor = copy.deepcopy(_json(FIXTURES / "seal.anchor.json"))
    edit(anchor)
    assert not verify_seal_anchor(seal, anchor, raw).ok


def test_the_real_tsa_tokens_verify_against_the_shipped_sigstore_root():
    pytest.importorskip("asn1crypto")
    from openadapt_flow.seal_timestamp import (
        authorization_fields,
        seal_fields,
        verify_record_timestamp,
    )

    stamp = verify_record_timestamp(
        seal_fields(FIXTURES / "seal.json"), FIXTURES / "seal.tsa.json"
    )
    assert stamp.ok, stamp.reason
    assert stamp.imprint_sha256 == seal_sha256(_json(FIXTURES / "seal.json"))
    auth_stamp = verify_record_timestamp(
        authorization_fields(AUTH / "run-authorization.json"),
        AUTH / "run-authorization.tsa.json",
    )
    assert auth_stamp.ok, auth_stamp.reason


def test_the_real_authorization_and_its_anchor_verify_offline(tmp_path):
    pinned = tmp_path / "allowed_signers"
    founder_raw = ssh_sig.parse_key_line(FOUNDER)
    pinned.write_text(
        ssh_sig.allowed_signers_line("github:abrichr", NAMESPACE, founder_raw) + "\n"
    )
    result = verify(
        AUTH, allowed_signers=pinned, registry=_registry(), require_anchor=True
    )
    assert result.ok, (result.reason, result.detail)
    assert result.identity_verified
    assert (
        result.signer_fingerprint
        == "SHA256:x+1MwCSAsNNHpc+UcIdbktY/Z+/FByyqfhuwiRzeO9A"
    )
    assert result.anchor.log_index == 3339630782
    # The Seal references exactly this authorization.
    assert _json(FIXTURES / "seal.json")["authorization_sha256"] == result.sha256
    # Without a pinned key the same files verify, but the identity is self-asserted.
    offline = verify(AUTH)
    assert offline.ok and not offline.identity_verified


def test_the_real_authorization_anchor_is_refused_for_another_record():
    anchor_file = AUTH / "run-authorization.anchor.json"
    result = verify_anchor("ab" * 32, "cd" * 32, anchor_file, _registry())
    assert result.reason == "anchor_mismatch"
