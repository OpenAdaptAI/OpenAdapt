"""Synthetic structure controls, plus a real public wrong-publisher proof.

Positive publisher verification is explicitly injected in unit controls. No
private key is made/read and no test claims a genuine registry publication.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow import registry, registry_attest
from openadapt_flow import registry_catalog as catalog
from openadapt_flow.ir import ActionKind, Step, Workflow

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
SAN = "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/release-registry.yml@refs/tags/registry-synthetic"


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))


def _b64(payload):
    return base64.b64encode(payload).decode("ascii")


def _proof(tmp_path):
    bundle = tmp_path / "bundle"
    Workflow(
        name="held-out",
        steps=[Step(id="wait", intent="Observe", action=ActionKind.WAIT)],
    ).save(bundle)
    entry = {
        "id": "synthetic/held-out",
        "version": "1.2.0",
        "status": "example",
        "path": "bundle",
        "files": registry.build_entry_files(bundle),
    }
    index = registry.stamp_index({"schema_version": 2, "workflows": [entry]}, now=NOW)
    raw = json.dumps(index).encode()
    return {
        "schema": catalog.PROOF_SCHEMA,
        "index_base64": _b64(raw),
        "attestation_base64": _b64(b'{"synthetic_verifier_fixture":true}'),
        "programs": [
            {
                "ref": "synthetic/held-out@1.2.0",
                "workflow_base64": _b64((bundle / "workflow.json").read_bytes()),
                "manifest_base64": _b64((bundle / "manifest.json").read_bytes()),
            }
        ],
    }


def _inject_publisher(monkeypatch, proof, *, digest=None, san=SAN):
    raw = base64.b64decode(proof["index_base64"])
    expected = digest or hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        registry_attest,
        "verify",
        lambda *a, **k: (san, {registry_attest.INDEX_SUBJECT: expected}),
    )


def test_synthetic_projection_rederives_separate_program_identities(
    tmp_path, monkeypatch
):
    proof = _proof(tmp_path)
    _inject_publisher(monkeypatch, proof)
    summary = catalog.verify_catalog(proof, now=NOW)
    [entry] = summary["entries"]
    assert summary["release_tag"] == "registry-synthetic"
    assert entry["ref"] == "synthetic/held-out@1.2.0"
    assert (
        len(
            {
                entry["workflow_sha256"],
                entry["bundle_content_digest"],
                entry["workflow_contract_sha256"],
            }
        )
        == 3
    )
    assert "qualification" not in summary and "certified" not in summary


@pytest.mark.parametrize(
    "fault",
    [
        "entry_ref",
        "workflow_bytes",
        "missing_manifest",
        "extra_projection",
        "extra_program",
        "stolen_digest",
        "unindexed_asset",
    ],
)
def test_program_and_proof_projection_faults_refuse(tmp_path, monkeypatch, fault):
    proof = _proof(tmp_path)
    _inject_publisher(monkeypatch, proof)
    if fault == "entry_ref":
        proof["programs"][0]["ref"] = "synthetic/other@1.2.0"
    elif fault == "workflow_bytes":
        proof["programs"][0]["workflow_base64"] = _b64(b"{}")
    elif fault in {"missing_manifest", "stolen_digest", "unindexed_asset"}:
        material = proof["programs"][0]
        raw = json.loads(base64.b64decode(material["workflow_base64"]))
        if fault == "missing_manifest":
            raw.pop("manifest", None)
            material["manifest_base64"] = None
        elif fault == "stolen_digest":
            raw["manifest"]["content_digest"] = "0" * 64
        else:
            from openadapt_flow.bundle_validation import source_canonical_payload

            raw["manifest"]["file_hashes"]["templates/unindexed.png"] = "a" * 64
            raw["manifest"]["content_digest"] = hashlib.sha256(
                source_canonical_payload(raw, raw["manifest"]["file_hashes"])
            ).hexdigest()
        payload = json.dumps(raw).encode()
        material["workflow_base64"] = _b64(payload)
        index = json.loads(base64.b64decode(proof["index_base64"]))
        index["workflows"][0]["files"]["workflow.json"] = hashlib.sha256(
            payload
        ).hexdigest()
        proof["index_base64"] = _b64(json.dumps(index).encode())
        _inject_publisher(monkeypatch, proof)
    elif fault == "extra_projection":
        proof["verified"] = True
    else:
        proof["programs"].append(dict(proof["programs"][0]))
    with pytest.raises(registry.RegistryError):
        catalog.verify_catalog(proof, now=NOW)


def test_a_wrong_index_subject_refuses_before_program_metadata(tmp_path, monkeypatch):
    proof = _proof(tmp_path)
    _inject_publisher(monkeypatch, proof, digest="0" * 64)
    with pytest.raises(registry.RegistryError, match="does not name this index"):
        catalog.verify_catalog(proof, now=NOW)


@pytest.mark.parametrize("fault", ["expired", "interval", "boolean_serial", "rollback"])
def test_every_catalog_freshness_fault_refuses(tmp_path, monkeypatch, fault):
    proof = _proof(tmp_path)
    data = json.loads(base64.b64decode(proof["index_base64"]))
    if fault == "expired":
        now = NOW + timedelta(days=31)
    else:
        now = NOW
    if fault == "interval":
        data["expires_at"] = "2027-11-20T00:00:00Z"
    if fault == "boolean_serial":
        data["serial"] = True
    proof["index_base64"] = _b64(json.dumps(data).encode())
    _inject_publisher(monkeypatch, proof)
    if fault == "rollback":
        registry.record_serial(
            registry.identity_key(registry_attest.REGISTRY_IDENTITY, SAN),
            data["serial"] + 1,
        )
    with pytest.raises(registry.RegistryError):
        catalog.verify_catalog(proof, now=now)


def test_real_public_wrong_workflow_sigstore_fixture_is_not_a_registry_proof(tmp_path):
    proof = _proof(tmp_path)
    fixture = (
        Path(__file__).parent
        / "fixtures/registry/desktop-0.24.1-SHA256SUMS.sigstore.json"
    )
    proof["attestation_base64"] = _b64(fixture.read_bytes())
    with pytest.raises(registry.RegistryError, match="attestation"):
        catalog.verify_catalog(proof, now=NOW)


def test_embedded_manifest_keeps_runtime_precedence(tmp_path, monkeypatch):
    proof = _proof(tmp_path)
    proof["programs"][0]["manifest_base64"] = None
    _inject_publisher(monkeypatch, proof)
    assert (
        catalog.verify_catalog(proof, now=NOW)["entries"][0]["ref"]
        == "synthetic/held-out@1.2.0"
    )


def test_local_test_signer_never_becomes_a_public_catalog_publisher(
    tmp_path, monkeypatch
):
    proof = _proof(tmp_path)
    _inject_publisher(monkeypatch, proof, san="test:synthetic")
    with pytest.raises(registry.RegistryError, match="production registry publisher"):
        catalog.verify_catalog(proof, now=NOW)


def test_catalog_archive_subject_is_checked_without_trusting_projection(
    tmp_path, monkeypatch
):
    proof = _proof(tmp_path)
    index = json.loads(base64.b64decode(proof["index_base64"]))
    index["workflows"][0]["archive"] = {
        "url": "https://registry.example/bundle.tar.gz",
        "sha256": "a" * 64,
    }
    proof["index_base64"] = _b64(json.dumps(index).encode())
    _inject_publisher(monkeypatch, proof)
    with pytest.raises(registry.RegistryError, match="archive"):
        catalog.verify_catalog(proof, now=NOW)
