"""Export the versioned control-overlay JSON Schemas into the package."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from openadapt_types import (
    BusinessDecisionAnswerReceiptV1,
    BusinessDecisionAnswerV1,
    BusinessDecisionDeliveryPolicyV1,
    BusinessDecisionPresentationV1,
    BusinessDecisionTaskV1,
    ControlOverlayFrameV1,
    ControlOverlayFrameV2,
    ControlOverlayTimelineV1,
    ControlOverlayTimelineV2,
    ExecutionRequirementsV1,
    HumanDecisionReceiptV1,
    HumanDecisionTaskV1,
    HumanDecisionTaskV2,
    RunnerCapabilityManifestV1,
    match_runner_capabilities,
)
from openadapt_types.execute_openapi import execute_openapi_document

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "openadapt_types" / "schemas"
SCHEMAS = {
    "business-decision-task-v1.json": BusinessDecisionTaskV1,
    "business-decision-answer-v1.json": BusinessDecisionAnswerV1,
    "business-decision-answer-receipt-v1.json": BusinessDecisionAnswerReceiptV1,
    "business-decision-presentation-v1.json": BusinessDecisionPresentationV1,
    "business-decision-delivery-policy-v1.json": BusinessDecisionDeliveryPolicyV1,
    "control-overlay-frame-v1.json": ControlOverlayFrameV1,
    "control-overlay-timeline-v1.json": ControlOverlayTimelineV1,
    "control-overlay-frame-v2.json": ControlOverlayFrameV2,
    "control-overlay-timeline-v2.json": ControlOverlayTimelineV2,
    "human-decision-task-v1.json": HumanDecisionTaskV1,
    "human-decision-task-v2.json": HumanDecisionTaskV2,
    "human-decision-receipt-v1.json": HumanDecisionReceiptV1,
    "runner-capability-manifest-v1.json": RunnerCapabilityManifestV1,
    "execution-requirements-v1.json": ExecutionRequirementsV1,
}


def canonical_match_vector() -> dict[str, object]:
    """Return one fixed, PHI-free vector for non-Python implementations."""

    manifest = RunnerCapabilityManifestV1.model_validate(
        {
            "schema_version": "openadapt.runner-capability-manifest/v1",
            "installation_id": "install_reference_01",
            "runner_id": "runner_reference_01",
            "agent_version": "0.15.0",
            "flow_version": "1.26.0",
            "host_os": "macos",
            "architecture": "aarch64",
            "lanes": [
                {
                    "surface": "web",
                    "execution_mode": "in_session",
                    "capabilities": [
                        "playwright_dom",
                        "structural_resolution",
                        "playwright_actuation",
                        "application_identity",
                        "workflow_state_identity",
                        "settled_state_detection",
                        "durable_resume",
                        "effect_tier_1",
                    ],
                    "supported_profiles": ["standard"],
                },
                {
                    "surface": "citrix",
                    "execution_mode": "external",
                    "capabilities": [
                        "pixel_observation",
                        "ocr_relational_resolution",
                        "physical_input_actuation",
                        "application_identity",
                        "session_identity",
                        "workflow_state_identity",
                        "record_identity",
                        "governed_authorization",
                        "settled_state_detection",
                        "session_continuity",
                        "durable_resume",
                        "postcondition_verification",
                        "evidence_export",
                        "effect_verification",
                        "effect_tier_2",
                        "local_secret_resolution",
                        "parameter_by_reference",
                        "no_external_egress",
                    ],
                    "supported_profiles": ["regulated", "standard"],
                },
            ],
            "generated_at": "2026-07-28T12:00:00Z",
            "expires_at": "2026-07-28T12:15:00Z",
        }
    )
    requirements = ExecutionRequirementsV1.model_validate(
        {
            "schema_version": "openadapt.execution-requirements/v1",
            "workflow_family_id": "family_reference_01",
            "portable_intent_digest": "sha256:" + "1" * 64,
            "selected_plan_id": "plan_reference_01",
            "plan_digest": "sha256:" + "2" * 64,
            "qualification_digest": "sha256:" + "3" * 64,
            "binding_digest": "sha256:" + "4" * 64,
            "surface": "citrix",
            "execution_mode": "external",
            "profile": "regulated",
            "minimum_effect_tier": 2,
            "required_capabilities": [
                "pixel_observation",
                "ocr_relational_resolution",
                "record_identity",
                "governed_authorization",
                "settled_state_detection",
                "durable_resume",
                "local_secret_resolution",
                "parameter_by_reference",
                "no_external_egress",
            ],
            "permitted_runner_ids": ["runner_reference_01"],
            "minimum_runtime_version": "1.26.0",
            "maximum_runtime_version": "1.26.9",
            "authorization_digest": "sha256:" + "5" * 64,
            "runtime_input_digest": "sha256:" + "6" * 64,
        }
    )
    match = match_runner_capabilities(
        manifest,
        requirements,
        at="2026-07-28T12:05:00Z",
    )
    return {
        "schema_version": "openadapt.runner-capability-match-vector/v1",
        "match_at": "2026-07-28T12:05:00Z",
        "manifest": manifest.model_dump(mode="json"),
        "manifest_canonical_json": manifest.canonical_bytes().decode("ascii"),
        "manifest_digest": manifest.digest,
        "requirements": requirements.model_dump(mode="json"),
        "requirements_canonical_json": requirements.canonical_bytes().decode("ascii"),
        "requirements_digest": requirements.digest,
        "expected_match": match.model_dump(mode="json"),
    }


def negative_match_vectors() -> dict[str, object]:
    """Return fail-closed vectors for non-Python manifest consumers."""

    reference = cast(dict[str, Any], canonical_match_vector())
    duplicate_manifest = deepcopy(reference["manifest"])
    duplicate_lane = deepcopy(duplicate_manifest["lanes"][0])
    duplicate_lane["capabilities"] = ["pixel_observation"]
    duplicate_manifest["lanes"].append(duplicate_lane)

    borrowing_manifest = deepcopy(reference["manifest"])
    borrowing_requirements = deepcopy(reference["requirements"])
    borrowing_requirements["minimum_effect_tier"] = 1
    borrowing_requirements["required_capabilities"] = ["playwright_dom"]
    borrowing_match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(borrowing_manifest),
        ExecutionRequirementsV1.model_validate(borrowing_requirements),
        at=reference["match_at"],
    )

    return {
        "schema_version": "openadapt.runner-capability-negative-vectors/v1",
        "cases": [
            {
                "case_id": "duplicate_surface_mode_lane",
                "manifest": duplicate_manifest,
                "requirements": reference["requirements"],
                "match_at": reference["match_at"],
                "expected_manifest_accepted": False,
                "expected_rejection": "duplicate_surface_mode_lane",
                "expected_match": None,
            },
            {
                "case_id": "cross_lane_capability_and_effect_borrowing",
                "manifest": borrowing_manifest,
                "requirements": borrowing_requirements,
                "match_at": reference["match_at"],
                "expected_manifest_accepted": True,
                "expected_rejection": None,
                "expected_match": borrowing_match.model_dump(mode="json"),
            },
        ],
    }


def rendered_schemas() -> dict[str, str]:
    return {
        filename: json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for filename, model in SCHEMAS.items()
    }


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered_schemas().items():
        (SCHEMA_DIR / filename).write_text(text, encoding="utf-8")
    (SCHEMA_DIR / "runner-capability-match-v1.vector.json").write_text(
        json.dumps(canonical_match_vector(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (SCHEMA_DIR / "runner-capability-negative-v1.vector.json").write_text(
        json.dumps(negative_match_vectors(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (SCHEMA_DIR / "execute-v1-openapi.json").write_text(
        json.dumps(execute_openapi_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
