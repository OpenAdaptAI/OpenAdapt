"""Propose, accept, and locally admit a qualification contract from a demo.

These tests pin the fail-closed contract: a missing system-of-record oracle
HALTs, the --break-it class is always in the starter matrix, and the
local-dev signer cannot enter a production trust map. They do not claim
Production and they do not invent trial counts.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.__main__ import main
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.qualification import IdentityEnforcement, QualificationOutcome
from openadapt_flow.qualification_admission import (
    QualificationAdmissionError,
    expected_from_payload,
    load_qualification_signer_trust,
    verify_qualification_admission,
)
from openadapt_flow.qualification_dev_signer import (
    LOCAL_DEV_ISSUER_WORKFLOW,
    LOCAL_DEV_KEY_ID,
    LOCAL_DEV_PUBLIC_KEY_B64,
    LOCAL_DEV_REF_PREFIX,
    LOCAL_DEV_SCHEMA,
    production_shaped_local_payload,
    sign_production_shaped_local_admission,
)
from openadapt_flow.qualification_proposal import (
    QualificationProposalError,
    accept_proposal,
    admit_local_dev,
    propose_qualification,
    refuse_pin,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from tests.test_qualification_admission import _trust


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _write_workflow(
    tmp_path: Path,
    workflow: Workflow,
    *,
    app_url: str = "http://127.0.0.1:9/",
) -> tuple[Workflow, Path, Path]:
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "app_url": app_url,
                "application": "MockMed",
                "application_version": "tutorial",
                "viewport": [1280, 800],
                "surface": "web",
                "params": dict(workflow.params),
            }
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_png())
    workflow.surface = "web"
    workflow.save(bundle)
    return Workflow.load(bundle), recording, bundle


def _workflow(*, effects: bool = True) -> Workflow:
    effect_list = []
    if effects:
        effect_list = [
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"id": ValueExpr(param="record_id")},
                field="note",
                value=ValueExpr(param="note"),
                idempotency_key=ValueExpr(param="record_id"),
                risk="irreversible",
            )
        ]
    return Workflow(
        name="qualified-write",
        params={"record_id": "example", "note": "example"},
        steps=[
            Step(
                id="save",
                intent="Save the record",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Save",
                    structured_identity="record identity",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                    )
                ],
                effects=effect_list,
                risk="irreversible",
                identity_armed=True,
            )
        ],
    )


def test_proposal_from_a_tiny_fixture_fills_pins_and_break_it(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow())
    proposal = propose_qualification(
        workflow, recording_dir=recording, policy_pack="community"
    )
    assert proposal.status == "draft"
    assert [pin["kind"] for pin in proposal.pins] == [
        "application",
        "environment",
        "identity",
        "effect",
    ]
    assert all(pin["status"] == "proposed" for pin in proposal.pins)
    application = proposal.pin("application")["payload"]
    assert application["application"] == "MockMed"
    assert application["application_identity"] == "http://127.0.0.1:9"
    assert application["application_version"] == "tutorial"
    identity = proposal.pin("identity")["payload"]["policies"]
    assert identity[0]["enforcement"] == "canonical_ladder"
    effects = proposal.pin("effect")["payload"]["effects"]
    assert effects[0]["kind"] == "field_equals"
    classes = [case["case_class"] for case in proposal.failure_matrix]
    assert "break_it" in classes
    assert "identity_swap" in classes
    assert proposal.suggestions == []
    gate = proposal.oracle_gate or {}
    assert gate.get("passed") is True
    assert gate.get("break_it_executed") is True


def test_missing_system_of_record_halts(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow(effects=False))
    proposal = propose_qualification(
        workflow, recording_dir=recording, policy_pack="community"
    )
    assert proposal.status == "halted"
    assert proposal.halt_reason is not None
    assert "effect pin is missing" in proposal.halt_reason
    assert "invent" in proposal.halt_reason
    with pytest.raises(QualificationProposalError, match="effect pin is missing"):
        accept_proposal(workflow, proposal)


def test_refusing_a_pin_halts_instead_of_guessing(tmp_path: Path) -> None:
    workflow, recording, _bundle = _write_workflow(tmp_path, _workflow())
    proposal = propose_qualification(workflow, recording_dir=recording)
    halted = refuse_pin(proposal, "effect")
    assert halted.status == "halted"
    assert halted.halt_reason is not None
    assert "refused the effect pin" in halted.halt_reason
    with pytest.raises(QualificationProposalError, match="refused"):
        accept_proposal(workflow, halted)


def test_accept_confirms_pins_and_keeps_break_it(tmp_path: Path) -> None:
    workflow, recording, bundle = _write_workflow(tmp_path, _workflow())
    proposal = propose_qualification(workflow, recording_dir=recording)
    accepted = accept_proposal(workflow, proposal)
    assert accepted.status == "accepted"
    assert workflow.qualification is not None
    assert workflow.qualification.environment.application == "MockMed"
    policy = workflow.qualification.identity_policies["save"]
    assert policy.enforcement is IdentityEnforcement.CANONICAL_LADDER
    assert workflow.qualification.effect_policies[0].tier == 1
    case_ids = {case.id for case in workflow.qualification.cases}
    assert "fault-break-it" in case_ids
    break_it = next(
        case for case in workflow.qualification.cases if case.id == "fault-break-it"
    )
    assert break_it.expected_outcome is QualificationOutcome.HALTED
    local = admit_local_dev(workflow, accepted, bundle_dir=bundle)
    assert local.schema_version == LOCAL_DEV_SCHEMA
    assert local.purpose == "local-dev"
    assert local.issuer_workflow == LOCAL_DEV_ISSUER_WORKFLOW


def test_local_dev_key_cannot_enter_a_production_trust_map() -> None:
    registry = {
        LOCAL_DEV_KEY_ID: {
            "public_key": LOCAL_DEV_PUBLIC_KEY_B64,
            "allowed_workflows": [LOCAL_DEV_ISSUER_WORKFLOW],
            "allowed_ref_prefixes": [LOCAL_DEV_REF_PREFIX],
        }
    }
    with pytest.raises(
        QualificationAdmissionError,
        match="cannot enter a production trust map",
    ):
        load_qualification_signer_trust(json.dumps(registry))


def test_production_verify_rejects_a_local_dev_signed_envelope() -> None:
    payload = production_shaped_local_payload(
        bundle_content_digest="2" * 64,
        environment_digest="b" * 64,
    )
    envelope = sign_production_shaped_local_admission(payload)
    with pytest.raises(
        QualificationAdmissionError,
        match="cannot enter a production trust map",
    ):
        verify_qualification_admission(
            envelope,
            trusted_signers=_trust(),
            expected=expected_from_payload(payload),
        )


def test_cli_propose_and_accept(tmp_path: Path) -> None:
    _workflow_obj, recording, bundle = _write_workflow(tmp_path, _workflow())
    proposal_path = tmp_path / "proposal.json"
    assert (
        main(
            [
                "qualify",
                "propose",
                str(bundle),
                "--recording",
                str(recording),
                "--out",
                str(proposal_path),
            ]
        )
        == 0
    )
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert payload["status"] == "draft"
    assert any(case["case_class"] == "break_it" for case in payload["failure_matrix"])
    assert (
        main(
            [
                "qualify",
                "accept",
                str(bundle),
                "--proposal",
                str(proposal_path),
                "--admit-local",
            ]
        )
        == 0
    )
    admission = bundle / "qualification-admission.local-dev.json"
    assert admission.is_file()
    body = json.loads(admission.read_text(encoding="utf-8"))
    assert body["purpose"] == "local-dev"
    assert body["schema_version"] == LOCAL_DEV_SCHEMA


def test_cli_propose_qualification_alias_and_missing_sor(tmp_path: Path) -> None:
    _workflow_obj, recording, bundle = _write_workflow(
        tmp_path, _workflow(effects=False)
    )
    assert (
        main(
            [
                "propose-qualification",
                str(bundle),
                "--recording",
                str(recording),
            ]
        )
        == 2
    )


def test_cli_refuse_pin_halts(tmp_path: Path) -> None:
    _workflow_obj, recording, bundle = _write_workflow(tmp_path, _workflow())
    proposal_path = tmp_path / "proposal.json"
    main(
        [
            "qualify",
            "propose",
            str(bundle),
            "--recording",
            str(recording),
            "--out",
            str(proposal_path),
        ]
    )
    assert (
        main(
            [
                "qualify",
                "accept",
                str(bundle),
                "--proposal",
                str(proposal_path),
                "--refuse-pin",
                "identity",
            ]
        )
        == 2
    )


def test_extra_field_case_when_params_are_not_identity(tmp_path: Path) -> None:
    workflow = _workflow()
    workflow.params = {"note": "example"}
    workflow, recording, _bundle = _write_workflow(tmp_path, workflow)
    proposal = propose_qualification(workflow, recording_dir=recording)
    classes = [case["case_class"] for case in proposal.failure_matrix]
    assert "break_it" in classes
    assert "extra_field" in classes
    assert "identity_swap" not in classes
