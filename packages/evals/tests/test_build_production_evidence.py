from __future__ import annotations

import base64
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_evals import production_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_production_evidence.py"
SPEC = importlib.util.spec_from_file_location("build_production_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_EVALUATION_TIME = datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc)
_BUNDLE_SCHEMA = ROOT / "tests/fixtures/central-989681f6-public-trust-dsse-bundle.schema.json"


class _AcceptingBundleVerifier:
    def verify_unregistered_pair(
        self,
        *,
        kind: str,
        object_value: dict[str, object],
        object_reference: dict[str, object],
        object_raw: bytes,
        bundle_value: dict[str, object],
        evaluation_time: datetime,
    ) -> None:
        assert kind == object_reference["kind"]
        assert object_raw == evidence.canonical_json_bytes(object_value) + b"\n"
        assert bundle_value["mediaType"] == evidence.SIGSTORE_BUNDLE_MEDIA_TYPE
        assert evaluation_time == _EVALUATION_TIME


def _bundle_bytes() -> bytes:
    key_id = "oa-public-trust-p256-0123456789abcdef"
    return (
        evidence.canonical_json_bytes(
            {
                "mediaType": evidence.SIGSTORE_BUNDLE_MEDIA_TYPE,
                "verificationMaterial": {"publicKey": {"hint": key_id}},
                "dsseEnvelope": {
                    "payload": base64.b64encode(b"{}\n").decode("ascii"),
                    "payloadType": (
                        "application/vnd.openadapt.production-public-trust-signing-statement"
                        "+json;version=1"
                    ),
                    "signatures": [{"keyid": key_id, "sig": "MAYCAQECAQE="}],
                },
            }
        )
        + b"\n"
    )


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def test_pair_command_writes_exact_content_addressed_paths(tmp_path: Path) -> None:
    regular = tmp_path / "admission.json"
    raw_bundle = tmp_path / "admission.sigstore.json"
    output_root = tmp_path / "registry"
    references_output = tmp_path / "references.json"
    schema = tmp_path / "qualification-admission.schema.json"
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "verdict"],
                "properties": {
                    "schema_version": {"const": "openadapt.qualification-admission/v4"},
                    "verdict": {"const": "accepted"},
                },
            }
        ),
        encoding="utf-8",
    )
    regular.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.qualification-admission/v4",
                "verdict": "accepted",
            }
        ),
        encoding="utf-8",
    )
    bundle_bytes = _bundle_bytes()
    raw_bundle.write_bytes(bundle_bytes)

    regular_reference, bundle_reference = MODULE.build_pair(
        kind="qualification-admission",
        object_path=regular,
        sigstore_bundle_path=raw_bundle,
        registry_source_commit="a" * 40,
        registry_revision=12,
        registry_head_sha256=_digest("b"),
        output_root=output_root,
        references_output=references_output,
        object_schema_path=schema,
        bundle_schema_path=_BUNDLE_SCHEMA,
        bundle_verifier=_AcceptingBundleVerifier(),
        evaluation_time=_EVALUATION_TIME,
    )

    assert (output_root / regular_reference["object_path"]).read_bytes() == (
        evidence.canonical_json_bytes(json.loads(regular.read_text(encoding="utf-8"))) + b"\n"
    )
    assert (output_root / bundle_reference["object_path"]).read_bytes() == bundle_bytes
    assert json.loads(references_output.read_text(encoding="utf-8")) == [
        regular_reference,
        bundle_reference,
    ]
    evidence.validate_reference_pair(
        (regular_reference, bundle_reference),
        expected_regular_kind="qualification-admission",
    )


def test_pair_command_refuses_schema_invalid_object_before_write(tmp_path: Path) -> None:
    regular = tmp_path / "admission.json"
    bundle = tmp_path / "admission.sigstore.json"
    schema = tmp_path / "qualification-admission.schema.json"
    regular.write_text(
        '{"schema_version":"openadapt.qualification-admission/v4","verdict":"accepted"}',
        encoding="utf-8",
    )
    bundle.write_text("{}", encoding="utf-8")
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "admission_id_sha256"],
                "properties": {
                    "schema_version": {"const": "openadapt.qualification-admission/v4"},
                    "admission_id_sha256": {
                        "type": "string",
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "registry"
    with pytest.raises(evidence.ProductionEvidenceError, match="fails central schema"):
        MODULE.build_pair(
            kind="qualification-admission",
            object_path=regular,
            sigstore_bundle_path=bundle,
            registry_source_commit="a" * 40,
            registry_revision=12,
            registry_head_sha256=_digest("b"),
            output_root=output_root,
            references_output=tmp_path / "references.json",
            object_schema_path=schema,
            bundle_schema_path=_BUNDLE_SCHEMA,
            bundle_verifier=_AcceptingBundleVerifier(),
            evaluation_time=_EVALUATION_TIME,
        )
    assert not output_root.exists()


def test_pair_command_refuses_to_replace_different_evidence(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"old")
    with pytest.raises(evidence.ProductionEvidenceError, match="refusing to replace"):
        MODULE._write_atomic(path, b"new")


def test_pair_command_refuses_invalid_sigstore_bundle_before_write(tmp_path: Path) -> None:
    regular = tmp_path / "admission.json"
    bundle = tmp_path / "admission.sigstore.json"
    schema = tmp_path / "qualification-admission.schema.json"
    regular.write_text(
        '{"schema_version":"openadapt.qualification-admission/v4","verdict":"accepted"}',
        encoding="utf-8",
    )
    bundle.write_bytes(evidence.canonical_json_bytes({"mediaType": "wrong"}) + b"\n")
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["schema_version", "verdict"],
                "properties": {
                    "schema_version": {"const": "openadapt.qualification-admission/v4"},
                    "verdict": {"const": "accepted"},
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "registry"
    with pytest.raises(evidence.ProductionEvidenceError, match="fails central schema"):
        MODULE.build_pair(
            kind="qualification-admission",
            object_path=regular,
            sigstore_bundle_path=bundle,
            registry_source_commit="a" * 40,
            registry_revision=12,
            registry_head_sha256=_digest("b"),
            output_root=output_root,
            references_output=tmp_path / "references.json",
            object_schema_path=schema,
            bundle_schema_path=_BUNDLE_SCHEMA,
            bundle_verifier=_AcceptingBundleVerifier(),
            evaluation_time=_EVALUATION_TIME,
        )
    assert not output_root.exists()


def test_pair_command_refuses_noncanonical_bundle_before_write(tmp_path: Path) -> None:
    regular = tmp_path / "admission.json"
    bundle = tmp_path / "admission.sigstore.json"
    schema = tmp_path / "qualification-admission.schema.json"
    regular.write_text(
        '{"schema_version":"openadapt.qualification-admission/v4","verdict":"accepted"}',
        encoding="utf-8",
    )
    bundle.write_text(json.dumps(json.loads(_bundle_bytes()), indent=2), encoding="utf-8")
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["schema_version", "verdict"],
                "properties": {
                    "schema_version": {"const": "openadapt.qualification-admission/v4"},
                    "verdict": {"const": "accepted"},
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "registry"
    with pytest.raises(evidence.ProductionEvidenceError, match="canonical JSON"):
        MODULE.build_pair(
            kind="qualification-admission",
            object_path=regular,
            sigstore_bundle_path=bundle,
            registry_source_commit="a" * 40,
            registry_revision=12,
            registry_head_sha256=_digest("b"),
            output_root=output_root,
            references_output=tmp_path / "references.json",
            object_schema_path=schema,
            bundle_schema_path=_BUNDLE_SCHEMA,
            bundle_verifier=_AcceptingBundleVerifier(),
            evaluation_time=_EVALUATION_TIME,
        )
    assert not output_root.exists()


def test_pair_command_refuses_failed_bundle_verification_before_write(tmp_path: Path) -> None:
    regular = tmp_path / "admission.json"
    bundle = tmp_path / "admission.sigstore.json"
    schema = tmp_path / "qualification-admission.schema.json"
    regular.write_text(
        '{"schema_version":"openadapt.qualification-admission/v4","verdict":"accepted"}',
        encoding="utf-8",
    )
    bundle.write_bytes(_bundle_bytes())
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["schema_version", "verdict"],
                "properties": {
                    "schema_version": {"const": "openadapt.qualification-admission/v4"},
                    "verdict": {"const": "accepted"},
                },
            }
        ),
        encoding="utf-8",
    )

    class RejectingBundleVerifier:
        def verify_unregistered_pair(self, **kwargs: object) -> None:
            raise evidence.ProductionEvidenceError("test signature verification failed")

    output_root = tmp_path / "registry"
    with pytest.raises(evidence.ProductionEvidenceError, match="signature verification failed"):
        MODULE.build_pair(
            kind="qualification-admission",
            object_path=regular,
            sigstore_bundle_path=bundle,
            registry_source_commit="a" * 40,
            registry_revision=12,
            registry_head_sha256=_digest("b"),
            output_root=output_root,
            references_output=tmp_path / "references.json",
            object_schema_path=schema,
            bundle_schema_path=_BUNDLE_SCHEMA,
            bundle_verifier=RejectingBundleVerifier(),
            evaluation_time=_EVALUATION_TIME,
        )
    assert not output_root.exists()


def test_json_reader_rejects_duplicate_keys_and_floats(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"one","schema_version":"two"}', encoding="utf-8")
    with pytest.raises(evidence.ProductionEvidenceError, match="duplicate key"):
        MODULE._read_json_object(duplicate, "test object")

    floating = tmp_path / "float.json"
    floating.write_text('{"size":1.5}', encoding="utf-8")
    with pytest.raises(evidence.ProductionEvidenceError, match="floating-point"):
        MODULE._read_json_object(floating, "test object")


def test_summary_verifier_refuses_inactive_central_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "evidence-registry.json").write_text(
        json.dumps({"signer_registry": None}), encoding="utf-8"
    )
    monkeypatch.setattr(MODULE, "_require_pinned_central_checkout", lambda root: root)
    with pytest.raises(evidence.ProductionEvidenceError, match="signer registry is inactive"):
        MODULE.PinnedCentralDecisionReceiptVerifier(
            tmp_path,
            evaluation_time=datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc),
        )
