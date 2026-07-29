"""Exact retained identity-policy proof for qualification evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    BundleManifest,
    IdentityCheck,
    IdentitySignalEvidence,
    PixelIdentityEvidence,
    Step,
    Workflow,
)
from openadapt_flow.qualification import (
    IdentityEnforcement,
    IdentityEvidenceSource,
    IdentityMatchMode,
    IdentityNormalizer,
    IdentityPolicy,
    IdentitySignalKey,
    IdentitySignalPolicy,
)
from openadapt_flow.qualification_identity_evidence import (
    qualification_identity_evidence_error,
)
from openadapt_flow.runtime.identity import (
    verify_pixel_identity,
    verify_structured_identity,
    verify_target_identity,
)
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_structured_template,
    verify_template_identity,
)
from openadapt_flow.runtime.replayer import Replayer


def _step() -> Step:
    return Step(
        id="save",
        intent="Save",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/save.png",
            region=(0, 0, 100, 20),
            click_point=(50, 10),
            structured_identity="Record: A123",
            context_text="DOB: 2000-01-01",
        ),
    )


def _canonical_policy() -> IdentityPolicy:
    return IdentityPolicy(
        step_id="save",
        enforcement=IdentityEnforcement.CANONICAL_LADDER,
    )


def _quorum_policy() -> IdentityPolicy:
    return IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            ),
            IdentitySignalPolicy(
                key=IdentitySignalKey.SECONDARY_IDENTIFIER,
                source=IdentityEvidenceSource.CAPTURED_CONTEXT,
                match=IdentityMatchMode.NORMALIZED,
                normalizers=[IdentityNormalizer.CASEFOLD],
                region=(0, 0, 100, 20),
                extract_pattern=r"DOB: (?P<value>[0-9/-]+)",
            ),
        ],
        quorum=1,
    )


def _quorum_check() -> IdentityCheck:
    return IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=0.5,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="record_id",
                source="structured",
                verdict="verified",
                evidence_class="application_structured_text",
                match="exact",
            ),
            IdentitySignalEvidence(
                signal="secondary_identifier",
                source="captured_context",
                verdict="unverifiable",
                evidence_class="captured_context_ocr",
                match="normalized",
            ),
        ],
        quorum_required=1,
        quorum_verified=1,
    )


def test_canonical_identity_requires_exact_runtime_structured_comparison() -> None:
    valid = verify_structured_identity("Record: A123", " record:   a123 ")
    assert valid is not None
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=_step(),
            actuation_path="gui",
        )
        is None
    )

    for mutation in (
        {"expected": "Other record"},
        {"observed": "Other record"},
        {"coverage": 0.01},
    ):
        assert qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid.model_copy(update=mutation),
            step=_step(),
            actuation_path="gui",
        )


def test_canonical_identity_accepts_exact_runtime_context_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = "Patient Alice Smith date March Seven"
    valid = verify_target_identity(
        step.anchor.context_text,
        "Patient Alice Smith date March Seven",
    )

    assert valid.mode == "context"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_accepts_exact_template_structured_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    template = build_identity_template(
        None,
        structured_identity=step.anchor.structured_identity,
    )
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    valid = verify_structured_template(template, " record:   a123 ")
    assert valid is not None

    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_accepts_exact_template_context_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    context_text = "Patient Alice Smith date March Seven"
    template = build_identity_template(context_text)
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    valid = verify_template_identity(template, context_text)

    assert valid.mode == "context"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_binds_exact_runtime_parameter() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = "Patient Alice Smith account ABCXYZ"
    recorded_params = {"record_id": "ABCXYZ"}
    runtime_params = {"record_id": "QWERTY"}
    valid = verify_target_identity(
        step.anchor.context_text,
        "Patient Alice Smith account QWERTY",
        params=runtime_params,
        param_examples=recorded_params,
    )

    assert valid.mode == "param"
    assert valid.param == "record_id"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
            runtime_params=runtime_params,
            recorded_params=recorded_params,
        )
        is None
    )
    assert qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=valid.model_copy(update={"param": "other_record"}),
        step=step,
        actuation_path="gui",
        runtime_params=runtime_params,
        recorded_params=recorded_params,
    )


def test_canonical_template_identity_binds_exact_runtime_parameter() -> None:
    step = _step()
    assert step.anchor is not None
    context_text = "Patient Alice Smith account ABCXYZ"
    recorded_params = {"record_id": "ABCXYZ"}
    runtime_params = {"record_id": "QWERTY"}
    template = build_identity_template(
        context_text,
        param_examples=recorded_params,
    )
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    valid = verify_template_identity(
        template,
        "Patient Alice Smith account QWERTY",
        params=runtime_params,
        param_examples=recorded_params,
    )

    assert valid.mode == "param"
    assert valid.param == "record_id"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
            runtime_params=runtime_params,
            recorded_params=recorded_params,
        )
        is None
    )
    assert qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=valid,
        step=step,
        actuation_path="gui",
        runtime_params={"other_record": "QWERTY"},
        recorded_params=recorded_params,
    )


def _pixel_crop() -> bytes:
    image = np.full((48, 240, 3), 255, dtype=np.uint8)
    cv2.putText(
        image,
        "A123-987",
        (8, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _retained_pixel_check(
    tmp_path: Path,
) -> tuple[Step, IdentityCheck, str, Path, Path]:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    step.anchor.identifier_crop = "templates/identifiers/save.png"
    step.anchor.identifier_region = (0, 0, 100, 20)
    recorded = _pixel_crop()
    # A trailing byte gives the live crop a distinct content address without
    # changing the exact decoded pixels evaluated by OpenCV.
    live = recorded + b"\x00"
    workflow = Workflow(name="pixel-evidence", steps=[step], manifest=BundleManifest())
    recorded_sha256 = hashlib.sha256(recorded).hexdigest()
    workflow.manifest.file_hashes[step.anchor.identifier_crop] = recorded_sha256
    evidence = Replayer._retain_pixel_identity_evidence(
        workflow=workflow,
        anchor=step.anchor,
        recorded_png=recorded,
        live_png=live,
        run_dir=tmp_path,
    )
    check = verify_pixel_identity(recorded, live, enable_verify=True)
    assert check is not None and check.status == "verified"
    check.pixel_evidence = evidence
    recorded_path = tmp_path / evidence.recorded_crop_inventory_ref
    live_path = tmp_path / evidence.live_crop_inventory_ref
    return step, check, recorded_sha256, recorded_path, live_path


def test_canonical_identity_rejects_fabricated_pixel_diagnostic() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    step.anchor.identifier_crop = "templates/identifiers/save.png"
    step.anchor.identifier_region = (0, 0, 100, 20)
    fabricated = IdentityCheck(
        status="verified",
        mode="pixel",
        coverage=1.0,
        expected="recorded identifier crop",
        observed="live identifier crop matches after alignment (worst window 0.000)",
    )

    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=fabricated,
            step=step,
            actuation_path="gui",
        )
        == "pixel identity verdict lacks exact retained crop evidence"
    )


def test_canonical_identity_accepts_exact_retained_pixel_crops(
    tmp_path: Path,
) -> None:
    step, valid, recorded_sha256, _recorded_path, _live_path = _retained_pixel_check(
        tmp_path
    )

    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
            evidence_root=tmp_path,
            recorded_asset_sha256=recorded_sha256,
        )
        is None
    )


def test_canonical_identity_rejects_mutated_pixel_crop(tmp_path: Path) -> None:
    step, check, recorded_sha256, _recorded_path, live_path = _retained_pixel_check(
        tmp_path
    )
    live_path.write_bytes(live_path.read_bytes() + b"mutated")

    error = qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=check,
        step=step,
        actuation_path="gui",
        evidence_root=tmp_path,
        recorded_asset_sha256=recorded_sha256,
    )
    assert error == "pixel identity crop evidence is missing or does not match its hash"


def test_canonical_identity_rejects_missing_pixel_crop(tmp_path: Path) -> None:
    step, check, recorded_sha256, _recorded_path, live_path = _retained_pixel_check(
        tmp_path
    )
    live_path.unlink()

    error = qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=check,
        step=step,
        actuation_path="gui",
        evidence_root=tmp_path,
        recorded_asset_sha256=recorded_sha256,
    )
    assert error == "pixel identity crop evidence is missing or does not match its hash"


def test_canonical_identity_rejects_symlinked_pixel_crop(tmp_path: Path) -> None:
    step, check, recorded_sha256, recorded_path, live_path = _retained_pixel_check(
        tmp_path
    )
    live_path.unlink()
    os.symlink(recorded_path, live_path)

    error = qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=check,
        step=step,
        actuation_path="gui",
        evidence_root=tmp_path,
        recorded_asset_sha256=recorded_sha256,
    )
    assert error == "pixel identity crop evidence is missing or does not match its hash"


def test_pixel_identity_evidence_rejects_traversal_reference() -> None:
    digest = "0" * 64
    with pytest.raises(ValueError, match="not content-addressed"):
        PixelIdentityEvidence(
            recorded_crop_sha256=digest,
            live_crop_sha256=digest,
            recorded_crop_inventory_ref=f"../identity-crops/{digest}.png",
            live_crop_inventory_ref=f"private/identity-crops/{digest}.png",
            evaluator_contract_sha256=digest,
        )


def test_canonical_identity_rejects_other_evaluator_contract(tmp_path: Path) -> None:
    step, check, recorded_sha256, _recorded_path, _live_path = _retained_pixel_check(
        tmp_path
    )
    assert check.pixel_evidence is not None
    other_contract = check.pixel_evidence.model_copy(
        update={"evaluator_contract_sha256": "0" * 64}
    )
    changed = check.model_copy(update={"pixel_evidence": other_contract})

    error = qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=changed,
        step=step,
        actuation_path="gui",
        evidence_root=tmp_path,
        recorded_asset_sha256=recorded_sha256,
    )
    assert error == "pixel identity evaluator contract does not match this runtime"


@pytest.mark.parametrize(
    "mutation",
    [
        {"quorum_required": 2},
        {"quorum_verified": 2},
        {"coverage": 1.0},
        {"mode": "structured"},
        {
            "signal_evidence": [
                IdentitySignalEvidence(
                    signal="record_id",
                    source="structured",
                    verdict="verified",
                    evidence_class="application_structured_text",
                    match="exact",
                )
            ]
        },
        {
            "signal_evidence": [
                IdentitySignalEvidence(
                    signal="record_id",
                    source="structured",
                    verdict="verified",
                    evidence_class="application_structured_text",
                    match="exact",
                ),
                IdentitySignalEvidence(
                    signal="secondary_identifier",
                    source="captured_context",
                    verdict="unverifiable",
                    evidence_class="captured_context_ocr",
                    match="exact",
                ),
            ]
        },
    ],
)
def test_signal_quorum_refuses_forged_policy_shape(
    mutation: dict[str, object],
) -> None:
    assert qualification_identity_evidence_error(
        policy=_quorum_policy(),
        check=_quorum_check().model_copy(update=mutation),
        step=_step(),
        actuation_path="gui",
    )


def test_signal_quorum_accepts_exact_policy_with_one_unreadable_signal() -> None:
    assert (
        qualification_identity_evidence_error(
            policy=_quorum_policy(),
            check=_quorum_check(),
            step=_step(),
            actuation_path="gui",
        )
        is None
    )


def test_api_identity_requires_exact_policy_signal_set() -> None:
    step = _step()
    step.api_binding = ApiBinding(
        url_template="/records/{record_id}",
        identity=[
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/url/record_id"],
            )
        ],
    )
    policy = IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            )
        ],
        quorum=1,
    )
    check = IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=1.0,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="record_id",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            )
        ],
        quorum_required=1,
        quorum_verified=1,
    )
    assert (
        qualification_identity_evidence_error(
            policy=policy,
            check=check,
            step=step,
            actuation_path="api",
        )
        is None
    )
    wrong_key = check.model_copy(deep=True)
    wrong_key.signal_evidence[0].signal = "subject_name"
    assert qualification_identity_evidence_error(
        policy=policy,
        check=wrong_key,
        step=step,
        actuation_path="api",
    )


def test_api_identity_refuses_reordered_policy_bindings() -> None:
    step = _step()
    step.api_binding = ApiBinding(
        url_template="/records/{record_id}/{secondary_id}",
        identity=[
            ApiIdentityBinding(
                key="secondary_identifier",
                param="secondary_id",
                effect_field="secondary_id",
                request_pointers=["/url/secondary_id"],
            ),
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/url/record_id"],
            ),
        ],
    )
    policy = IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            ),
            IdentitySignalPolicy(
                key=IdentitySignalKey.SECONDARY_IDENTIFIER,
                source=IdentityEvidenceSource.CAPTURED_CONTEXT,
                region=(0, 0, 100, 20),
                extract_pattern=r"DOB: (?P<value>[0-9/-]+)",
            ),
        ],
        quorum=2,
    )
    check = IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=1.0,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="secondary_identifier",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            ),
            IdentitySignalEvidence(
                signal="record_id",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            ),
        ],
        quorum_required=2,
        quorum_verified=2,
    )

    assert qualification_identity_evidence_error(
        policy=policy,
        check=check,
        step=step,
        actuation_path="api",
    )
