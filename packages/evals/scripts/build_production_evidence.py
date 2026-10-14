#!/usr/bin/env python3
"""Build public production summaries and content-addressed evidence pairs."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from openadapt_evals.production_evidence import (
    CENTRAL_TRUST_CONTRACT_COMMIT,
    DecisionReceiptVerifier,
    ProductionEvidenceError,
    build_evidence_object_pair,
    build_production_acceptance_summary,
    canonical_json_bytes,
)

_CENTRAL_SCHEMA_PATHS = {
    "public-trust-dsse-bundle": "production-public-trust-dsse-bundle.schema.json",
    "production-acceptance-manifest": "production-lifecycle-evidence-manifest.schema.json",
    "production-acceptance-summary": "production-lifecycle-evidence-summary.schema.json",
    "production-cloud-deploy-authorization": "production-cloud-deploy-authorization.schema.json",
    "production-cloud-deployment-result": "production-cloud-deployment-result.schema.json",
    "production-current-default": "production-current-default.schema.json",
    "production-deployment-observation": "production-deployment-observation.schema.json",
    "production-lifecycle-checkpoint": "production-lifecycle-checkpoint.schema.json",
    "qualification-admission": "qualification-admission.schema.json",
    "qualification-authority-state-receipt": "qualification-authority-state-receipt.schema.json",
    "qualification-evidence-decision-receipt": (
        "qualification-evidence-decision-receipt.schema.json"
    ),
    "qualification-release": "qualification-release.schema.json",
    "qualification-revocation-state-receipt": (
        "qualification-revocation-state-receipt.schema.json"
    ),
    "support-release-admission": "support-release-admission.schema.json",
}

_SUMMARY_INPUT_KEYS = frozenset(
    {
        "target",
        "claim_scope",
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "release_identity",
        "release_sha256",
        "artifact_inventory_sha256",
        "publication_staging",
        "expected_publication_assets",
        "qualification_evidence_decision_receipt",
        "qualification_evidence_decision_receipt_references",
        "qualification_admission",
        "qualification_admission_references",
        "production_acceptance_manifest",
        "production_acceptance_manifest_references",
        "authority_state_sha256",
        "revocation_state_sha256",
        "signer_registry_sha256",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer",
    }
)


class PublicTrustBundleVerifier(Protocol):
    def verify_unregistered_pair(
        self,
        *,
        kind: str,
        object_value: Mapping[str, Any],
        object_reference: Mapping[str, Any],
        object_raw: bytes,
        bundle_value: Mapping[str, Any],
        evaluation_time: datetime,
    ) -> None: ...


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProductionEvidenceError(f"JSON contains duplicate key: {key}")
        value[key] = item
    return value


def _read_json_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=lambda value: (_ for _ in ()).throw(
                ProductionEvidenceError(f"{context} contains a floating-point value: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionEvidenceError(f"cannot read {context}: {path}") from exc
    if not isinstance(value, dict):
        raise ProductionEvidenceError(f"{context} must be one JSON object")
    return value


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise ProductionEvidenceError(f"refusing to replace different evidence: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_pinned_central_checkout(root: Path) -> Path:
    resolved = root.resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(resolved), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProductionEvidenceError("central trust root is not a readable Git checkout") from exc
    if head != CENTRAL_TRUST_CONTRACT_COMMIT or dirty:
        raise ProductionEvidenceError(
            "central trust root must be a clean checkout of " + CENTRAL_TRUST_CONTRACT_COMMIT
        )
    return resolved


def _validate_object_schema(value: Mapping[str, Any], schema_path: Path) -> None:
    try:
        import jsonschema
        from referencing import Registry, Resource

        schema = _read_json_object(schema_path, "central JSON schema")

        def retrieve(uri: str) -> Resource[dict[str, Any]]:
            name = uri.rsplit("/", 1)[-1]
            if not name or name != Path(name).name:
                raise ProductionEvidenceError("central schema reference is not a local file")
            referenced = _read_json_object(
                schema_path.parent / name, f"central referenced JSON schema {name}"
            )
            return Resource.from_contents(referenced)

        resource = Resource.from_contents(schema)
        registry = Registry(retrieve=retrieve).with_resource(
            schema_path.resolve().as_uri(), resource
        )
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        errors = sorted(validator.iter_errors(dict(value)), key=lambda item: list(item.path))
    except ImportError as exc:
        raise ProductionEvidenceError(
            "jsonschema is required for evidence object validation"
        ) from exc
    except (OSError, jsonschema.SchemaError) as exc:
        raise ProductionEvidenceError("central evidence schema cannot be resolved") from exc
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "object"
        raise ProductionEvidenceError(
            f"regular production evidence object fails central schema at {location}: "
            f"{errors[0].message}"
        )


class PinnedCentralDecisionReceiptVerifier(DecisionReceiptVerifier):
    """Verify registered evidence against the trust state at the pinned central root."""

    def __init__(self, root: Path, *, evaluation_time: datetime) -> None:
        self.root = _require_pinned_central_checkout(root)
        self.evaluation_time = evaluation_time
        self._load_current_trust()

    def _load_current_trust(self) -> None:
        registry_document = _read_json_object(
            self.root / "evidence-registry.json", "central evidence registry"
        )
        pointer = registry_document.get("signer_registry")
        if not isinstance(pointer, dict):
            raise ProductionEvidenceError("central qualification signer registry is inactive")

        scripts = self.root / "scripts"
        sys.path.insert(0, str(scripts))
        previous_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            for name in (
                "production_trust",
                "public_trust_kms",
                "public_trust_resolver",
                "validate_evidence_registry",
            ):
                sys.modules.pop(name, None)
            evidence = importlib.import_module("validate_evidence_registry")
            trust = importlib.import_module("production_trust")
            kms = importlib.import_module("public_trust_kms")
            resolver = importlib.import_module("public_trust_resolver")
        except Exception as exc:
            raise ProductionEvidenceError("central trust verifier cannot be loaded") from exc
        finally:
            sys.dont_write_bytecode = previous_bytecode_setting
            sys.path.remove(str(scripts))

        try:
            entries = evidence.validate_registry(registry_document, root=self.root)
            signer_registry_raw = (self.root / pointer["object_path"]).read_bytes()
            signer_registry = _read_json_object(
                self.root / pointer["object_path"], "central qualification signer registry"
            )
            signer_registry = evidence.validate_signer_registry(signer_registry)
        except Exception as exc:
            raise ProductionEvidenceError("central signer registry validation failed") from exc

        state_entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        validators = {
            "qualification-authority-state-receipt": trust.validate_authority_state,
            "qualification-revocation-state-receipt": trust.validate_revocation_state,
        }
        for index, entry in enumerate(entries):
            kind = entry["kind"]
            if kind not in validators:
                continue
            if index + 1 >= len(entries):
                raise ProductionEvidenceError("central current trust state bundle is absent")
            state_entries[kind] = (entry, entries[index + 1])
        if set(state_entries) != set(validators):
            raise ProductionEvidenceError("central current authority or revocation state is absent")
        states: dict[str, dict[str, Any]] = {}
        try:
            for kind, (entry, _) in state_entries.items():
                candidate = _read_json_object(
                    self.root / entry["object_path"], f"central current {kind}"
                )
                states[kind] = validators[kind](candidate, now=self.evaluation_time)
        except Exception as exc:
            raise ProductionEvidenceError("central current trust state is invalid") from exc
        authority = states["qualification-authority-state-receipt"]
        revocation = states["qualification-revocation-state-receipt"]
        registry_identity = evidence.signer_registry_identity_digest(signer_registry)
        registry_raw = (
            "sha256:" + hashlib.sha256(evidence.canonical(signer_registry) + b"\n").hexdigest()
        )
        if (
            authority["signer_registry_sha256"] != registry_raw
            or authority["signer_registry_identity_sha256"] != registry_identity
            or authority["signer_registry_revision"] != signer_registry["revision"]
            or revocation["signer_registry_sha256"] != registry_identity
            or revocation["authority_state_sha256"] != authority["authority_state_sha256"]
        ):
            raise ProductionEvidenceError("central current trust state bindings differ")
        try:
            for kind, value in states.items():
                regular_entry, bundle_entry = state_entries[kind]
                resolved = resolver.verify_registered_public_trust_pair(
                    object_raw=(self.root / regular_entry["object_path"]).read_bytes(),
                    object_reference=self._reference(
                        regular_entry,
                        registry_document=registry_document,
                        evidence=evidence,
                    ),
                    bundle_raw=(self.root / bundle_entry["object_path"]).read_bytes(),
                    bundle_reference=self._reference(
                        bundle_entry,
                        registry_document=registry_document,
                        evidence=evidence,
                    ),
                    signer_registry_raw=signer_registry_raw,
                    expected_signer_registry_sha256=registry_identity,
                    expected_authority_state_sha256=authority["authority_state_sha256"],
                    expected_revocation_state_sha256=revocation["revocation_state_sha256"],
                    now=self.evaluation_time,
                )
                if resolved["object"] != value:
                    raise ProductionEvidenceError(
                        "central current trust object differs after verification"
                    )
            trust.verify_embedded_signature(
                authority,
                signer_registry=signer_registry,
                object_schema_version="openadapt.qualification-authority-state-receipt/v2",
                signature_domain=trust.AUTHORITY_STATE_SIGNATURE_DOMAIN,
                usage="qualification-authority-state-receipt",
                now=self.evaluation_time,
            )
            trust.verify_embedded_signature(
                revocation,
                signer_registry=signer_registry,
                object_schema_version="openadapt.qualification-revocation-state-receipt/v1",
                signature_domain=trust.REVOCATION_STATE_SIGNATURE_DOMAIN,
                usage="qualification-revocation-state-receipt",
                now=self.evaluation_time,
            )
            revoked = {
                (item["subject_kind"], item["subject_id"]) for item in revocation["revocations"]
            }
            if any(
                signer["status"] == "active"
                and ("qualification-signer-key", signer["public_key_sha256"]) in revoked
                for signer in signer_registry["signers"]
            ):
                raise ProductionEvidenceError("a current active signer key is revoked")
        except Exception as exc:
            raise ProductionEvidenceError("central current trust signatures are invalid") from exc
        self.evidence = evidence
        self.trust = trust
        self.kms = kms
        self.resolver = resolver
        self.registry_document = registry_document
        self.registry_entries = entries
        self.signer_registry_raw = signer_registry_raw
        self.signer_registry = signer_registry
        self.authority = authority
        self.revocation = revocation

    @staticmethod
    def _reference(
        entry: Mapping[str, Any],
        *,
        registry_document: Mapping[str, Any],
        evidence: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": evidence.REFERENCE_SCHEMA,
            "repository": evidence.REPOSITORY,
            "repository_id": evidence.REPOSITORY_ID,
            "repository_owner_id": evidence.REPOSITORY_OWNER_ID,
            "registry_source_commit": CENTRAL_TRUST_CONTRACT_COMMIT,
            "registry_revision": registry_document["revision"],
            "registry_head_sha256": registry_document["registry_head_sha256"],
            **dict(entry),
        }

    def _verify_registered_pair(
        self,
        value: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        bundle_reference: Mapping[str, Any],
        kind: str,
        evaluation_time: datetime,
    ) -> None:
        if evaluation_time != self.evaluation_time:
            raise ProductionEvidenceError("evidence evaluation time differs from pinned verifier")
        try:
            regular_entry = self.evidence.require_registered(
                self.registry_entries,
                reference=reference,
                label=kind,
            )
            regular_index = self.registry_entries.index(regular_entry)
            bundle_entry = self.registry_entries[regular_index + 1]
            expected_regular = self._reference(
                regular_entry,
                registry_document=self.registry_document,
                evidence=self.evidence,
            )
            expected_bundle = self._reference(
                bundle_entry,
                registry_document=self.registry_document,
                evidence=self.evidence,
            )
        except Exception as exc:
            raise ProductionEvidenceError(f"{kind} is not currently registered") from exc
        if (
            dict(reference) != expected_regular
            or dict(bundle_reference) != expected_bundle
            or expected_regular["kind"] != kind
            or expected_bundle["kind"] != kind + "-sigstore-bundle"
        ):
            raise ProductionEvidenceError(f"{kind} current registry reference differs")
        try:
            resolved = self.resolver.verify_registered_public_trust_pair(
                object_raw=canonical_json_bytes(dict(value)) + b"\n",
                object_reference=expected_regular,
                bundle_raw=(self.root / bundle_entry["object_path"]).read_bytes(),
                bundle_reference=expected_bundle,
                signer_registry_raw=self.signer_registry_raw,
                expected_signer_registry_sha256=self.evidence.signer_registry_identity_digest(
                    self.signer_registry
                ),
                expected_authority_state_sha256=self.authority["authority_state_sha256"],
                expected_revocation_state_sha256=self.revocation["revocation_state_sha256"],
                now=evaluation_time,
            )
        except Exception as exc:
            raise ProductionEvidenceError(
                f"{kind} current registered signature verification failed"
            ) from exc
        if resolved["object"] != dict(value):
            raise ProductionEvidenceError(f"{kind} registered object differs")

    def verify_registered_object(
        self,
        value: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        bundle_reference: Mapping[str, Any],
        kind: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None:
        if (
            authority_state_sha256 != self.authority["authority_state_sha256"]
            or signer_registry_sha256
            != self.evidence.signer_registry_identity_digest(self.signer_registry)
            or revocation_state_sha256 != self.revocation["revocation_state_sha256"]
        ):
            raise ProductionEvidenceError(f"{kind} current trust bindings differ")
        self._verify_registered_pair(
            value,
            reference=reference,
            bundle_reference=bundle_reference,
            kind=kind,
            evaluation_time=evaluation_time,
        )
        revoked = {
            (item["subject_kind"], item["subject_id"]) for item in self.revocation["revocations"]
        }
        if (kind, reference["semantic_identity_sha256"]) in revoked:
            raise ProductionEvidenceError(f"{kind} is revoked")

    def verify_unregistered_pair(
        self,
        *,
        kind: str,
        object_value: Mapping[str, Any],
        object_reference: Mapping[str, Any],
        object_raw: bytes,
        bundle_value: Mapping[str, Any],
        evaluation_time: datetime,
    ) -> None:
        if evaluation_time != self.evaluation_time:
            raise ProductionEvidenceError("pair evaluation time differs from pinned verifier")
        try:
            statement = self.kms.statement_from_bundle(dict(bundle_value))
            matches = [
                signer
                for signer in self.signer_registry["signers"]
                if signer.get("key_id") == statement["key_id"]
                and signer.get("algorithm") == "ecdsa-p256-sha256"
            ]
            if len(matches) != 1:
                raise ProductionEvidenceError(
                    "public-trust bundle does not select one current signer"
                )
            statement = self.kms.validate_statement_object_binding(
                statement,
                object_raw=object_raw,
                object_value=object_value,
                object_kind=kind,
                object_schema_version=object_reference["object_schema_version"],
                object_media_type=object_reference["object_media_type"],
                semantic_identity_sha256=object_reference["semantic_identity_sha256"],
                expected_signer_registry_sha256=(
                    self.evidence.signer_registry_identity_digest(self.signer_registry)
                ),
                expected_authority_state_sha256=self.authority["authority_state_sha256"],
                expected_revocation_state_sha256=self.revocation["revocation_state_sha256"],
            )
            self.kms.verify_bundle(
                dict(bundle_value),
                expected_statement=statement,
                signer=matches[0],
                now=evaluation_time,
            )
        except ProductionEvidenceError:
            raise
        except Exception as exc:
            raise ProductionEvidenceError(
                "public-trust bundle cryptographic verification failed"
            ) from exc

    def verify(
        self,
        receipt: Mapping[str, Any],
        *,
        reference: Mapping[str, Any],
        bundle_reference: Mapping[str, Any],
        object_sha256: str,
        semantic_identity_sha256: str,
        authority_state_sha256: str,
        signer_registry_sha256: str,
        revocation_state_sha256: str,
        evaluation_time: datetime,
    ) -> None:
        if evaluation_time != self.evaluation_time:
            raise ProductionEvidenceError("receipt evaluation time differs from pinned verifier")
        self._verify_registered_pair(
            receipt,
            reference=reference,
            bundle_reference=bundle_reference,
            kind="qualification-evidence-decision-receipt",
            evaluation_time=evaluation_time,
        )
        try:
            verified = self.trust.validate_receipt(
                dict(receipt),
                signer_registry=self.signer_registry,
                now=evaluation_time,
            )
        except Exception as exc:
            raise ProductionEvidenceError(
                "decision receipt cryptographic verification failed"
            ) from exc
        expected_object_sha256 = (
            "sha256:" + hashlib.sha256(self.evidence.canonical(verified) + b"\n").hexdigest()
        )
        if (
            object_sha256 != expected_object_sha256
            or authority_state_sha256 != self.authority["authority_state_sha256"]
            or signer_registry_sha256
            != self.evidence.signer_registry_identity_digest(self.signer_registry)
            or revocation_state_sha256 != self.revocation["revocation_state_sha256"]
            or verified["evidence_authority_contract_sha256"]
            != self.authority["evidence_authority_sha256"]
        ):
            raise ProductionEvidenceError("decision receipt current trust bindings differ")
        revoked = {
            (item["subject_kind"], item["subject_id"]) for item in self.revocation["revocations"]
        }
        revoked_subjects = (
            (
                "qualification-evidence-decision-receipt",
                semantic_identity_sha256,
            ),
            ("qualification-campaign-permit", verified["campaign_permit_sha256"]),
            (
                "qualification-evidence-authority",
                verified["evidence_authority_contract_sha256"],
            ),
        )
        if any(subject in revoked for subject in revoked_subjects):
            raise ProductionEvidenceError("decision receipt or its authority is revoked")
        signing_keys = [
            signer
            for signer in self.signer_registry["signers"]
            if signer.get("key_id") == verified["issuer_key_id"]
        ]
        if len(signing_keys) != 1:
            raise ProductionEvidenceError("decision receipt signing key is invalid")
        if verified["evidence_class"] == "remote-safe-synthetic" and (
            signing_keys[0].get("key_origin") != "aws-kms" or not signing_keys[0].get("kms_key_arn")
        ):
            raise ProductionEvidenceError(
                "synthetic decision receipt does not use the current AWS KMS signer"
            )
        if any(
            signer["status"] == "active"
            and ("qualification-signer-key", signer["public_key_sha256"]) in revoked
            for signer in self.signer_registry["signers"]
        ):
            raise ProductionEvidenceError("decision receipt signing key is revoked")


def build_summary(
    input_path: Path,
    output_path: Path,
    *,
    decision_receipt_verifier: DecisionReceiptVerifier,
    evaluation_time: datetime,
    object_schema_path: Path,
) -> dict[str, Any]:
    """Build one public v3 lifecycle summary from public inputs only."""

    payload = _read_json_object(input_path, "production summary input")
    if set(payload) != _SUMMARY_INPUT_KEYS:
        missing = sorted(_SUMMARY_INPUT_KEYS - set(payload))
        extra = sorted(set(payload) - _SUMMARY_INPUT_KEYS)
        raise ProductionEvidenceError(
            f"production summary input keys differ: missing={missing}, extra={extra}"
        )
    summary = build_production_acceptance_summary(
        **payload,
        decision_receipt_verifier=decision_receipt_verifier,
        evaluation_time=evaluation_time,
    )
    _validate_object_schema(summary, object_schema_path)
    _write_atomic(output_path, canonical_json_bytes(summary) + b"\n")
    return summary


def build_pair(
    *,
    kind: str,
    object_path: Path,
    sigstore_bundle_path: Path,
    registry_source_commit: str,
    registry_revision: int,
    registry_head_sha256: str,
    output_root: Path,
    references_output: Path,
    object_schema_path: Path,
    bundle_schema_path: Path,
    bundle_verifier: PublicTrustBundleVerifier,
    evaluation_time: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write one regular object and its bundle to their exact public paths."""

    object_value = _read_json_object(object_path, "regular production evidence object")
    _validate_object_schema(object_value, object_schema_path)
    try:
        raw_bundle = sigstore_bundle_path.read_bytes()
    except OSError as exc:
        raise ProductionEvidenceError(
            f"cannot read raw Sigstore bundle: {sigstore_bundle_path}"
        ) from exc
    try:
        bundle_value = json.loads(
            raw_bundle.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=lambda value: (_ for _ in ()).throw(
                ProductionEvidenceError(
                    f"raw Sigstore bundle contains a floating-point value: {value}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionEvidenceError("raw Sigstore bundle is not one JSON object") from exc
    if not isinstance(bundle_value, dict):
        raise ProductionEvidenceError("raw Sigstore bundle is not one JSON object")
    if raw_bundle != canonical_json_bytes(bundle_value) + b"\n":
        raise ProductionEvidenceError(
            "raw Sigstore bundle must be canonical JSON followed by one LF"
        )
    _validate_object_schema(bundle_value, bundle_schema_path)
    pair = build_evidence_object_pair(
        kind=kind,
        object_value=object_value,
        sigstore_bundle=raw_bundle,
        registry_source_commit=registry_source_commit,
        registry_revision=registry_revision,
        registry_head_sha256=registry_head_sha256,
    )
    bundle_verifier.verify_unregistered_pair(
        kind=kind,
        object_value=object_value,
        object_reference=pair.references[0],
        object_raw=pair.objects[0],
        bundle_value=bundle_value,
        evaluation_time=evaluation_time,
    )
    for payload, reference in zip(pair.objects, pair.references, strict=True):
        _write_atomic(output_root / reference["object_path"], payload)
    references = [dict(reference) for reference in pair.references]
    _write_atomic(references_output, canonical_json_bytes(references))
    return pair.references


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    summary = commands.add_parser("summary", help="build a public lifecycle summary")
    summary.add_argument("--input", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    summary.add_argument("--central-trust-root", type=Path, required=True)

    pair = commands.add_parser("pair", help="build a content-addressed object pair")
    pair.add_argument("--kind", required=True)
    pair.add_argument("--object", type=Path, required=True)
    pair.add_argument("--sigstore-bundle", type=Path, required=True)
    pair.add_argument("--registry-source-commit", required=True)
    pair.add_argument("--registry-revision", type=int, required=True)
    pair.add_argument("--registry-head-sha256", required=True)
    pair.add_argument("--output-root", type=Path, required=True)
    pair.add_argument("--references-output", type=Path, required=True)
    pair.add_argument("--central-trust-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evaluation_time = datetime.now(timezone.utc).replace(microsecond=0)
        verifier = PinnedCentralDecisionReceiptVerifier(
            args.central_trust_root,
            evaluation_time=evaluation_time,
        )
        if args.command == "summary":
            build_summary(
                args.input,
                args.output,
                decision_receipt_verifier=verifier,
                evaluation_time=evaluation_time,
                object_schema_path=(
                    verifier.root
                    / "schemas"
                    / _CENTRAL_SCHEMA_PATHS["production-acceptance-summary"]
                ),
            )
        else:
            central_root = verifier.root
            if (
                args.registry_source_commit != CENTRAL_TRUST_CONTRACT_COMMIT
                or args.registry_revision != verifier.registry_document["revision"]
                or args.registry_head_sha256 != verifier.registry_document["registry_head_sha256"]
            ):
                raise ProductionEvidenceError(
                    "pair registry metadata differs from the pinned current registry"
                )
            schema_name = _CENTRAL_SCHEMA_PATHS.get(args.kind)
            if schema_name is None:
                raise ProductionEvidenceError(
                    f"pair kind {args.kind!r} has no final central JSON schema"
                )
            build_pair(
                kind=args.kind,
                object_path=args.object,
                sigstore_bundle_path=args.sigstore_bundle,
                registry_source_commit=args.registry_source_commit,
                registry_revision=args.registry_revision,
                registry_head_sha256=args.registry_head_sha256,
                output_root=args.output_root,
                references_output=args.references_output,
                object_schema_path=central_root / "schemas" / schema_name,
                bundle_schema_path=(
                    central_root / "schemas" / _CENTRAL_SCHEMA_PATHS["public-trust-dsse-bundle"]
                ),
                bundle_verifier=verifier,
                evaluation_time=evaluation_time,
            )
    except ProductionEvidenceError as exc:
        raise SystemExit(f"production evidence refused: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
