"""Durable contract tests for runner capability admission."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    CapabilityMatchV1,
    CapabilityMismatchCode,
    ExecutionRequirementsV1,
    RunnerCapability,
    RunnerCapabilityManifestV1,
    match_runner_capabilities,
)


def _manifest_payload() -> dict[str, object]:
    return {
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
                    "record_identity",
                    "governed_authorization",
                    "settled_state_detection",
                    "durable_resume",
                    "effect_tier_2",
                    "local_secret_resolution",
                    "parameter_by_reference",
                    "no_external_egress",
                ],
                "supported_profiles": ["standard", "regulated"],
            },
        ],
        "generated_at": "2026-07-28T12:00:00Z",
        "expires_at": "2026-07-28T12:15:00Z",
    }


def _requirements_payload() -> dict[str, object]:
    return {
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
        "permitted_executor_ids": [],
        "minimum_runtime_version": "1.26.0",
        "maximum_runtime_version": "1.26.9",
        "authorization_digest": "sha256:" + "5" * 64,
        "runtime_input_digest": "sha256:" + "6" * 64,
    }


def test_closed_models_reject_unknown_fields_and_capabilities() -> None:
    manifest = _manifest_payload()
    manifest["application"] = "must not enter the public interface"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunnerCapabilityManifestV1.model_validate(manifest)

    manifest = _manifest_payload()
    manifest["lanes"][0]["capabilities"] = ["customer_specific_locator"]
    with pytest.raises(ValidationError, match="Input should be"):
        RunnerCapabilityManifestV1.model_validate(manifest)

    requirements = _requirements_payload()
    requirements["required_capabilities"] = ["unknown_capability"]
    with pytest.raises(ValidationError, match="Input should be"):
        ExecutionRequirementsV1.model_validate(requirements)


def test_closed_vocabulary_contains_the_existing_desktop_capabilities() -> None:
    existing_desktop_capabilities = {
        "actuation",
        "application_identity",
        "effect_verification",
        "governed_authorization",
        "identity_verification",
        "immediate_screen_confirmation",
        "independent_session",
        "independent_system_of_record",
        "persisted_state_reacquisition",
        "pixel_observation",
        "playwright_dom",
        "postcondition_verification",
        "session_continuity",
        "settled_state_detection",
        "structural_observation",
        "workflow_state_identity",
    }
    assert existing_desktop_capabilities <= {
        capability.value for capability in RunnerCapability
    }


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("capabilities", "pixel_observation"),
        ("supported_profiles", "regulated"),
    ],
)
def test_lane_rejects_duplicate_set_members(field: str, duplicate: str) -> None:
    payload = _manifest_payload()
    lane = payload["lanes"][1]
    values = list(lane[field])
    values.append(duplicate)
    lane[field] = values
    with pytest.raises(ValidationError):
        RunnerCapabilityManifestV1.model_validate(payload)


def test_manifest_rejects_duplicate_surface_mode_lanes() -> None:
    payload = _manifest_payload()
    payload["lanes"].append(dict(payload["lanes"][0]))
    with pytest.raises(ValidationError):
        RunnerCapabilityManifestV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("required_capabilities", "pixel_observation"),
        ("permitted_runner_ids", "runner_reference_01"),
        ("permitted_executor_ids", "executor_reference_01"),
    ],
)
def test_requirements_reject_duplicate_set_members(field: str, duplicate: str) -> None:
    payload = _requirements_payload()
    values = list(payload[field])
    if not values:
        values.append(duplicate)
    values.append(duplicate)
    payload[field] = values
    with pytest.raises(ValidationError):
        ExecutionRequirementsV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "openadapt.runner-capability-manifest/v2"),
        ("agent_version", "0.15"),
        ("flow_version", "1.26.0rc1"),
        ("flow_version", "01.26.0"),
    ],
)
def test_manifest_rejects_invalid_contract_or_runtime_versions(
    field: str, value: str
) -> None:
    payload = _manifest_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        RunnerCapabilityManifestV1.model_validate(payload)


def test_contract_versions_are_required_and_version_components_are_bounded() -> None:
    payload = _manifest_payload()
    del payload["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        RunnerCapabilityManifestV1.model_validate(payload)

    payload = _manifest_payload()
    payload["flow_version"] = "1.1234567890.0"
    with pytest.raises(ValidationError):
        RunnerCapabilityManifestV1.model_validate(payload)

    requirements = _requirements_payload()
    del requirements["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        ExecutionRequirementsV1.model_validate(requirements)


def test_requirements_reject_invalid_or_reversed_runtime_ranges() -> None:
    for minimum, maximum in (
        ("1.26", "1.26.9"),
        ("1.27.0", "1.26.9"),
    ):
        payload = _requirements_payload()
        payload["minimum_runtime_version"] = minimum
        payload["maximum_runtime_version"] = maximum
        with pytest.raises(ValidationError):
            ExecutionRequirementsV1.model_validate(payload)

    payload = _requirements_payload()
    payload["minimum_effect_tier"] = "2"
    with pytest.raises(ValidationError):
        ExecutionRequirementsV1.model_validate(payload)


def test_manifest_rejects_an_invalid_lifetime() -> None:
    payload = _manifest_payload()
    payload["expires_at"] = payload["generated_at"]
    with pytest.raises(ValidationError, match="later than generated_at"):
        RunnerCapabilityManifestV1.model_validate(payload)

    payload = _manifest_payload()
    payload["generated_at"] = "2026-07-28T12:00:00.1234567Z"
    with pytest.raises(ValidationError):
        RunnerCapabilityManifestV1.model_validate(payload)


def test_canonicalization_sorts_sets_and_produces_stable_digests() -> None:
    first_payload = _manifest_payload()
    second_payload = _manifest_payload()
    second_payload["lanes"] = list(reversed(second_payload["lanes"]))
    for lane in second_payload["lanes"]:
        lane["capabilities"] = list(reversed(lane["capabilities"]))

    first = RunnerCapabilityManifestV1.model_validate(first_payload)
    second = RunnerCapabilityManifestV1.model_validate(second_payload)
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert first.canonical_bytes().decode("ascii")
    assert b", " not in first.canonical_bytes()


def test_matcher_accepts_an_exact_compatible_runner() -> None:
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(_manifest_payload()),
        ExecutionRequirementsV1.model_validate(_requirements_payload()),
        at="2026-07-28T12:05:00Z",
    )
    assert match.matched is True
    assert match.mismatch_codes == ()
    assert match.missing_capabilities == ()


def test_matcher_reports_expiry_surface_mode_profile_version_pin_and_capability() -> (
    None
):
    manifest_payload = _manifest_payload()
    manifest_payload.update(
        {
            "runner_id": None,
            "flow_version": "1.25.9",
            "lanes": [
                {
                    "surface": "citrix",
                    "execution_mode": "external",
                    "supported_profiles": ["demo"],
                    "capabilities": ["pixel_observation", "effect_tier_4"],
                }
            ],
        }
    )
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(manifest_payload),
        ExecutionRequirementsV1.model_validate(_requirements_payload()),
        at="2026-07-28T12:15:00Z",
    )

    assert match.matched is False
    assert set(match.mismatch_codes) == {
        CapabilityMismatchCode.MANIFEST_EXPIRED,
        CapabilityMismatchCode.PROFILE_UNSUPPORTED,
        CapabilityMismatchCode.RUNTIME_VERSION_BELOW_MINIMUM,
        CapabilityMismatchCode.RUNNER_ID_UNASSIGNED,
        CapabilityMismatchCode.MINIMUM_EFFECT_TIER_UNSUPPORTED,
        CapabilityMismatchCode.REQUIRED_CAPABILITY_MISSING,
    }
    assert RunnerCapability.OCR_RELATIONAL_RESOLUTION in match.missing_capabilities


def test_matcher_rejects_an_unsupported_surface_or_mode() -> None:
    manifest = RunnerCapabilityManifestV1.model_validate(_manifest_payload())
    requirements_payload = _requirements_payload()
    requirements_payload["surface"] = "rdp"
    surface_match = match_runner_capabilities(
        manifest,
        ExecutionRequirementsV1.model_validate(requirements_payload),
        at="2026-07-28T12:05:00Z",
    )
    assert surface_match.mismatch_codes == (CapabilityMismatchCode.SURFACE_UNSUPPORTED,)

    requirements_payload = _requirements_payload()
    requirements_payload["execution_mode"] = "in_session"
    mode_match = match_runner_capabilities(
        manifest,
        ExecutionRequirementsV1.model_validate(requirements_payload),
        at="2026-07-28T12:05:00Z",
    )
    assert mode_match.mismatch_codes == (
        CapabilityMismatchCode.EXECUTION_MODE_UNSUPPORTED,
    )


def test_matcher_never_borrows_capabilities_or_effect_tiers_between_lanes() -> None:
    requirements_payload = _requirements_payload()
    requirements_payload["minimum_effect_tier"] = 1
    requirements_payload["required_capabilities"] = ["playwright_dom"]
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(_manifest_payload()),
        ExecutionRequirementsV1.model_validate(requirements_payload),
        at="2026-07-28T12:05:00Z",
    )
    assert match.matched is False
    assert set(match.mismatch_codes) == {
        CapabilityMismatchCode.MINIMUM_EFFECT_TIER_UNSUPPORTED,
        CapabilityMismatchCode.REQUIRED_CAPABILITY_MISSING,
    }
    assert match.missing_capabilities == (RunnerCapability.PLAYWRIGHT_DOM,)


def test_matcher_rejects_runtime_above_the_qualified_maximum() -> None:
    manifest_payload = _manifest_payload()
    manifest_payload["flow_version"] = "1.27.0"
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(manifest_payload),
        ExecutionRequirementsV1.model_validate(_requirements_payload()),
        at="2026-07-28T12:05:00Z",
    )
    assert match.matched is False
    assert match.mismatch_codes == (
        CapabilityMismatchCode.RUNTIME_VERSION_ABOVE_MAXIMUM,
    )


def test_matcher_rejects_a_manifest_before_its_generation_instant() -> None:
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(_manifest_payload()),
        ExecutionRequirementsV1.model_validate(_requirements_payload()),
        at="2026-07-28T11:59:59Z",
    )
    assert match.matched is False
    assert match.mismatch_codes == (CapabilityMismatchCode.MANIFEST_NOT_YET_VALID,)


def test_runner_matcher_fails_closed_on_a_pinned_external_executor() -> None:
    requirements_payload = _requirements_payload()
    requirements_payload["permitted_executor_ids"] = ["executor_reference_01"]
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(_manifest_payload()),
        ExecutionRequirementsV1.model_validate(requirements_payload),
        at="2026-07-28T12:05:00Z",
    )
    assert match.matched is False
    assert match.mismatch_codes == (
        CapabilityMismatchCode.EXECUTOR_ID_REQUIREMENT_UNSUPPORTED,
    )


def test_match_result_cannot_claim_success_with_a_mismatch() -> None:
    with pytest.raises(ValidationError, match="matched must be true"):
        CapabilityMatchV1(
            schema_version="openadapt.capability-match/v1",
            manifest_digest="sha256:" + "a" * 64,
            requirements_digest="sha256:" + "b" * 64,
            matched=True,
            mismatch_codes=(CapabilityMismatchCode.MANIFEST_EXPIRED,),
        )


def test_packaged_schemas_and_canonical_match_vector_are_exact() -> None:
    packaged = files("openadapt_types.schemas")
    assert (
        json.loads(packaged.joinpath("runner-capability-manifest-v1.json").read_text())
        == RunnerCapabilityManifestV1.model_json_schema()
    )
    assert (
        json.loads(packaged.joinpath("execution-requirements-v1.json").read_text())
        == ExecutionRequirementsV1.model_json_schema()
    )

    vector = json.loads(
        packaged.joinpath("runner-capability-match-v1.vector.json").read_text()
    )
    manifest = RunnerCapabilityManifestV1.model_validate(vector["manifest"])
    requirements = ExecutionRequirementsV1.model_validate(vector["requirements"])
    match = match_runner_capabilities(
        manifest,
        requirements,
        at=vector["match_at"],
    )
    assert (
        manifest.canonical_bytes().decode("ascii") == vector["manifest_canonical_json"]
    )
    assert (
        requirements.canonical_bytes().decode("ascii")
        == vector["requirements_canonical_json"]
    )
    assert manifest.digest == vector["manifest_digest"]
    assert requirements.digest == vector["requirements_digest"]
    assert match.model_dump(mode="json") == vector["expected_match"]


def test_packaged_negative_vectors_reject_duplicate_and_cross_lane_borrowing() -> None:
    packaged = files("openadapt_types.schemas")
    vector = json.loads(
        packaged.joinpath("runner-capability-negative-v1.vector.json").read_text()
    )
    cases = {case["case_id"]: case for case in vector["cases"]}

    duplicate = cases["duplicate_surface_mode_lane"]
    with pytest.raises(ValidationError, match="unique surface and execution-mode"):
        RunnerCapabilityManifestV1.model_validate(duplicate["manifest"])
    assert duplicate["expected_manifest_accepted"] is False
    assert duplicate["expected_rejection"] == "duplicate_surface_mode_lane"
    assert duplicate["expected_match"] is None

    borrowing = cases["cross_lane_capability_and_effect_borrowing"]
    match = match_runner_capabilities(
        RunnerCapabilityManifestV1.model_validate(borrowing["manifest"]),
        ExecutionRequirementsV1.model_validate(borrowing["requirements"]),
        at=borrowing["match_at"],
    )
    assert borrowing["expected_manifest_accepted"] is True
    assert borrowing["expected_rejection"] is None
    assert match.model_dump(mode="json") == borrowing["expected_match"]
