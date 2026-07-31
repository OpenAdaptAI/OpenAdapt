from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytest.importorskip("openadapt_flow.ir")

from engine.qualification_capabilities import (
    collect_qualification_capabilities,
    current_signed_capability_observations,
    sign_qualification_capability_observation,
    verify_qualification_capability_observation,
)
from engine.qualification_lifecycle import retain_capability_observation


def _sha() -> str:
    return hashlib.sha256(b"exact run report").hexdigest()


def _report(
    *,
    target_kind: str = "web",
    profile: str | None = None,
    results: list[dict] | None = None,
    evidence_classes: list[str] | None = None,
) -> dict:
    report = {
        "workflow_name": "qualification-test",
        "started_at": "2026-07-27T12:00:00+00:00",
        "execution_target_kind": target_kind,
        "results": results or [],
    }
    if profile is not None:
        classes = evidence_classes or ["authorization"]
        contracts = {
            "authorization": 1,
            "identity": int("identity" in classes),
            "postcondition": int("postcondition" in classes),
            "effect": int(any(item.startswith("effect_tier_") for item in classes)),
        }
        report.update(
            {
                "execution_profile": profile,
                "execution_outcome": "VERIFIED",
                "production_eligible": True,
                "execution_completed": True,
                "success": True,
                "external_network_calls": "none",
                "outcome_envelope": {
                    "outcome": "VERIFIED",
                    "profile": profile,
                    "production_eligible": True,
                    "execution_completed": True,
                    "required_contracts": contracts,
                    "passed_contracts": contracts,
                    "evidence_classes": classes,
                    "model_calls": 0,
                    "external_network_calls": "none",
                    "compensation_actions": 0,
                },
            }
        )
        if "postcondition" in classes:
            from openadapt_flow.ir import (
                postcondition_contract_sha256,
                postcondition_step_contract_sha256,
            )

            workflow_contract_sha256 = "a" * 64
            step_contract_sha256 = postcondition_step_contract_sha256(
                workflow_contract_sha256=workflow_contract_sha256,
                step_index=0,
                action_kind="click",
            )
            report["outcome_envelope"]["workflow_contract_sha256"] = workflow_contract_sha256
            report["outcome_envelope"]["postcondition_evidence"] = [
                {
                    "result_index": 0,
                    "workflow_contract_sha256": workflow_contract_sha256,
                    "step_index": 0,
                    "step_contract_sha256": step_contract_sha256,
                    "action_kind": "click",
                    "contract_kind": "explicit_predicate",
                    "contract_index": 0,
                    "contract_sha256": postcondition_contract_sha256(
                        workflow_contract_sha256=workflow_contract_sha256,
                        step_contract_sha256=step_contract_sha256,
                        action_kind="click",
                        contract_kind="explicit_predicate",
                        contract_index=0,
                    ),
                    "verdict": "passed",
                }
            ]
    return report


def test_capabilities_are_derived_from_exact_run_evidence() -> None:
    observation = collect_qualification_capabilities(
        _report(
            profile="standard",
            evidence_classes=[
                "authorization",
                "identity",
                "postcondition",
                "effect_tier_1",
            ],
            results=[
                {
                    "step_id": "submit",
                    "intent": "Submit",
                    "ok": True,
                    "resolution": {
                        "rung": "structural",
                        "point": [10, 10],
                        "confidence": 1.0,
                        "elapsed_ms": 1.0,
                    },
                    "before_png": "steps/submit.before.png",
                    "delivery_receipt": {
                        "receipt_id": "receipt-1",
                        "operation": "dom_click",
                        "native": True,
                        "delivered_at": "2026-07-27T12:00:01+00:00",
                    },
                    "identity": {
                        "status": "verified",
                        "mode": "signal_quorum",
                        "signal_evidence": [
                            {
                                "signal": "session",
                                "source": "session",
                                "verdict": "verified",
                                "evidence_class": "session_identity",
                                "match": "exact",
                            },
                            {
                                "signal": "application",
                                "source": "application",
                                "verdict": "verified",
                                "evidence_class": "application_identity",
                                "match": "exact",
                            },
                            {
                                "signal": "workflow_state",
                                "source": "workflow_state",
                                "verdict": "verified",
                                "evidence_class": "workflow_state_identity",
                                "match": "exact",
                            },
                        ],
                    },
                    "postconditions_ok": True,
                    "effect_evidence": [
                        {
                            "effect_contract_hash": "sha256:" + "1" * 64,
                            "substrate": "rest",
                            "verification_tier": 1,
                            "initial_verdict": "confirmed",
                            "final_verdict": "confirmed",
                        }
                    ],
                }
            ],
        ),
        expected_target_kind="web",
        runtime_version="1.24.0",
        report_sha256=_sha(),
        action_kinds={"submit": "click"},
    )

    assert observation.observed_capabilities == [
        "actuation",
        "application_identity",
        "effect_verification",
        "governed_authorization",
        "identity_verification",
        "independent_system_of_record",
        "pixel_observation",
        "playwright_dom",
        "postcondition_verification",
        "session_continuity",
        "settled_state_detection",
        "structural_observation",
        "workflow_state_identity",
    ]


def test_wrong_substrate_cannot_satisfy_declared_capabilities() -> None:
    observation = collect_qualification_capabilities(
        _report(
            target_kind="windows",
            profile="standard",
            evidence_classes=["authorization", "effect_tier_1"],
            results=[
                {
                    "step_id": "submit",
                    "intent": "Submit",
                    "ok": True,
                    "resolution": {
                        "rung": "structural",
                        "point": [10, 10],
                        "confidence": 1.0,
                        "elapsed_ms": 1.0,
                    },
                    "postconditions_ok": True,
                    "effect_evidence": [
                        {
                            "effect_contract_hash": "sha256:" + "1" * 64,
                            "substrate": "rest",
                            "verification_tier": 1,
                            "initial_verdict": "confirmed",
                            "final_verdict": "confirmed",
                        }
                    ],
                }
            ],
        ),
        expected_target_kind="citrix",
        runtime_version="1.24.0",
        report_sha256=_sha(),
        action_kinds={"submit": "click"},
    )

    assert observation.target_kind_matches is False
    assert observation.observed_capabilities == []


def test_retained_capability_receipt_is_typed_bounded_and_hash_bound(
    tmp_path: Path,
) -> None:
    observation = collect_qualification_capabilities(
        _report(
            results=[
                {
                    "step_id": "submit",
                    "intent": "Submit",
                    "ok": True,
                    "resolution": {
                        "rung": "structural",
                        "point": [10, 10],
                        "confidence": 1.0,
                        "elapsed_ms": 1.0,
                    },
                    "patient_name": "Sensitive Person",
                }
            ],
        ),
        expected_target_kind="web",
        runtime_version="1.24.0",
        report_sha256=_sha(),
        action_kinds={"submit": "click"},
    )

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    signed = sign_qualification_capability_observation(
        observation,
        project_id="project-1",
        project_revision=1,
        project_contract_sha256="1" * 64,
        workflow_contract_sha256="2" * 64,
        environment_contract_sha256="3" * 64,
        environment_digest="4" * 64,
        case_id="representative",
        run_id="run-1",
        attestation_key_id="desktop-local-v1",
        private_key=private_raw,
    )
    reference = retain_capability_observation(
        tmp_path,
        case_id="representative",
        run_id="run-1",
        observation=signed.model_dump(mode="json"),
    )
    receipt_path = tmp_path / "qualification-evidence" / reference["relative_path"]
    body = receipt_path.read_text(encoding="utf-8")
    payload = json.loads(body)

    assert "Sensitive Person" not in body
    assert verify_qualification_capability_observation(
        signed,
        public_key_base64=b64encode(
            private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii"),
    )
    assert payload["observations"] == [
        {"name": "actuation", "source": "actuation"},
        {"name": "playwright_dom", "source": "resolution"},
        {"name": "structural_observation", "source": "resolution"},
    ]
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == reference["sha256"]


def test_current_capability_receipt_rejects_tampering(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    environment = SimpleNamespace(
        environment_digest="4" * 64,
        runtime_version="1.24.0",
        target_kind="web",
        required_capabilities=[],
        contract_sha256=lambda: "3" * 64,
    )
    project = SimpleNamespace(
        project_id="project-1",
        revision=1,
        environment=environment,
        trusted_runner_keys={"desktop-local-v1": public_key},
        contract_sha256=lambda: "1" * 64,
    )
    observation = collect_qualification_capabilities(
        _report(
            results=[
                {
                    "step_id": "submit",
                    "intent": "Submit",
                    "ok": True,
                    "resolution": {
                        "rung": "structural",
                        "point": [10, 10],
                        "confidence": 1.0,
                        "elapsed_ms": 1.0,
                    },
                }
            ],
        ),
        expected_target_kind="web",
        runtime_version="1.24.0",
        report_sha256=_sha(),
        action_kinds={"submit": "click"},
    )
    signed = sign_qualification_capability_observation(
        observation,
        project_id="project-1",
        project_revision=1,
        project_contract_sha256="1" * 64,
        workflow_contract_sha256="2" * 64,
        environment_contract_sha256="3" * 64,
        environment_digest="4" * 64,
        case_id="representative",
        run_id="run-1",
        attestation_key_id="desktop-local-v1",
        private_key=private_raw,
    )
    reference = retain_capability_observation(
        tmp_path,
        case_id="representative",
        run_id="run-1",
        observation=signed.model_dump(mode="json"),
    )
    receipt_path = tmp_path / "qualification-evidence" / reference["relative_path"]
    receipt_path.with_name("run-report-receipt.json").write_text(
        json.dumps(
            {
                "schema": "openadapt.qualification-run-receipt/v1",
                "run_id": "run-1",
                "report_sha256": _sha(),
            }
        ),
        encoding="utf-8",
    )

    current = current_signed_capability_observations(
        tmp_path,
        workflow_contract_sha256="2" * 64,
        project=project,
    )
    assert current == {"representative": signed}

    environment.target_kind = "citrix"
    assert (
        current_signed_capability_observations(
            tmp_path,
            workflow_contract_sha256="2" * 64,
            project=project,
        )
        == {}
    )
    environment.target_kind = "web"
    environment.required_capabilities = ["actuation"]
    assert (
        current_signed_capability_observations(
            tmp_path,
            workflow_contract_sha256="2" * 64,
            project=project,
        )
        == {}
    )
    environment.required_capabilities = []

    tampered = json.loads(receipt_path.read_text())
    tampered["observations"].append({"name": "session_continuity", "source": "identity"})
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert (
        current_signed_capability_observations(
            tmp_path,
            workflow_contract_sha256="2" * 64,
            project=project,
        )
        == {}
    )


def test_untyped_step_evidence_cannot_mint_capabilities() -> None:
    with pytest.raises(ValueError):
        collect_qualification_capabilities(
            _report(results=[{"step_id": "submit", "ok": True, "identity": {}}]),
            expected_target_kind="web",
            runtime_version="1.24.0",
            report_sha256=_sha(),
            action_kinds={"submit": "click"},
        )
