"""Authoring a ProcessContract from two independently admitted capabilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.admitted_composition import (
    ProcessContractError,
    author_process_contract,
    is_process_contract_artifact,
    live_bundle_content_digest,
    topological_order,
)
from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import HandoffBinding, is_composition_artifact
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from openadapt_flow.qualification_admission import sign_qualification_admission
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr
from tests.test_qualification_admission import _payload, _private_key

INTAKE_ADMISSION = "11111111-1111-4111-8111-111111111111"
POSTING_ADMISSION = "77777777-7777-4777-8777-777777777777"
INTAKE_VERSION = "44444444-4444-4444-8444-444444444444"
POSTING_VERSION = "88888888-8888-4888-8888-888888888888"


def _writer(name: str = "intake") -> Workflow:
    return Workflow(
        name=name,
        surface="web",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            ),
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": ValueExpr(param="patient_id")},
                        expected_count=1,
                    )
                ],
            ),
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _reader(name: str = "posting") -> Workflow:
    return Workflow(
        name=name,
        surface="linux",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _save(tmp_path: Path, workflow: Workflow, folder: str) -> Path:
    path = tmp_path / folder
    path.mkdir()
    workflow.save(path)
    return path


def _envelope_for(
    bundle: Path,
    *,
    admission_id: str,
    workflow_version_id: str,
    digest: str | None = None,
) -> Path:
    workflow = Workflow.load(bundle)
    live = digest or live_bundle_content_digest(workflow, bundle)
    envelope = sign_qualification_admission(
        _payload(
            admission_id=admission_id,
            workflow_version_id=workflow_version_id,
            bundle_content_digest=live,
        ),
        _private_key(),
    )
    path = bundle / "qualification-admission.json"
    path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
    return path


def _two_admitted(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _reader(), "posting")
    intake_env = _envelope_for(
        intake, admission_id=INTAKE_ADMISSION, workflow_version_id=INTAKE_VERSION
    )
    posting_env = _envelope_for(
        posting, admission_id=POSTING_ADMISSION, workflow_version_id=POSTING_VERSION
    )
    return intake, intake_env, posting, posting_env


def test_two_admitted_children_author(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        name="claim-post",
        inputs=["patient_id"],
        out=out,
    )
    assert is_process_contract_artifact(out)
    assert not is_composition_artifact(out)
    assert contract.name == "claim-post"
    assert topological_order(contract) == ["intake", "posting"]
    assert contract.children[0].admission_id == INTAKE_ADMISSION
    assert contract.children[1].admission_id == POSTING_ADMISSION
    assert (out / "children").exists() is False
    reloaded = type(contract).load(out)
    assert reloaded.children[0].surface == "web"
    assert reloaded.children[1].surface == "linux"


def test_author_refuses_one_child(tmp_path: Path) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    env = _envelope_for(
        intake, admission_id=INTAKE_ADMISSION, workflow_version_id=INTAKE_VERSION
    )
    with pytest.raises(ProcessContractError, match="at least two"):
        author_process_contract([("intake", env, intake)], out=tmp_path / "process")


def test_author_refuses_unknown_handoff_target(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    with pytest.raises(ProcessContractError, match="unknown child"):
        author_process_contract(
            [
                ("intake", intake_env, intake),
                ("posting", posting_env, posting),
            ],
            handoffs=[
                HandoffBinding(
                    from_child="intake",
                    source="patient_id",
                    to_child="billing",
                    target="patient_id",
                )
            ],
            out=tmp_path / "process",
        )


def test_author_refuses_non_effect_bound_source(tmp_path: Path) -> None:
    first = _save(tmp_path, _reader("first"), "first")
    second = _save(tmp_path, _reader("second"), "second")
    first_env = _envelope_for(
        first, admission_id=INTAKE_ADMISSION, workflow_version_id=INTAKE_VERSION
    )
    second_env = _envelope_for(
        second, admission_id=POSTING_ADMISSION, workflow_version_id=POSTING_VERSION
    )
    with pytest.raises(ProcessContractError, match="not a parameter bound"):
        author_process_contract(
            [
                ("first", first_env, first),
                ("second", second_env, second),
            ],
            handoffs=[
                HandoffBinding(
                    from_child="first",
                    source="patient_id",
                    to_child="second",
                    target="patient_id",
                )
            ],
            out=tmp_path / "process",
        )


def test_author_refuses_cycle(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    with pytest.raises(ProcessContractError, match="cycle"):
        author_process_contract(
            [
                ("intake", intake_env, intake),
                ("posting", posting_env, posting),
            ],
            after={"intake": ["posting"], "posting": ["intake"]},
            out=tmp_path / "cycled",
        )


def test_author_refuses_digest_mismatch_against_envelope(tmp_path: Path) -> None:
    intake, _, posting, posting_env = _two_admitted(tmp_path)
    bad_env = _envelope_for(
        intake,
        admission_id=INTAKE_ADMISSION,
        workflow_version_id=INTAKE_VERSION,
        digest="a" * 64,
    )
    with pytest.raises(
        ProcessContractError, match="digest does not match the envelope"
    ):
        author_process_contract(
            [
                ("intake", bad_env, intake),
                ("posting", posting_env, posting),
            ],
            out=tmp_path / "mismatch",
        )


def test_compose_recordings_fail_admission_two_envelopes_pass(tmp_path: Path) -> None:
    """The point of the MVP: compose output is red; two envelopes are green.

    Pointing ProcessContract at PR 430 composition.json of copied recordings
    fails the admission check. The same two workflows with v1 envelopes author.
    """

    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _reader(), "posting")
    composed = tmp_path / "composed"
    author_composition(
        [("intake", intake), ("posting", posting)],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        out=composed,
    )
    assert is_composition_artifact(composed)
    assert not is_process_contract_artifact(composed)
    compose_intake = composed / "children" / "intake"
    compose_posting = composed / "children" / "posting"
    missing_env = compose_intake / "qualification-admission.json"
    with pytest.raises(ProcessContractError, match="admission"):
        author_process_contract(
            [
                ("intake", missing_env, compose_intake),
                (
                    "posting",
                    compose_posting / "qualification-admission.json",
                    compose_posting,
                ),
            ],
            out=tmp_path / "from-compose",
        )

    intake_env = _envelope_for(
        intake, admission_id=INTAKE_ADMISSION, workflow_version_id=INTAKE_VERSION
    )
    posting_env = _envelope_for(
        posting, admission_id=POSTING_ADMISSION, workflow_version_id=POSTING_VERSION
    )
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        out=out,
    )
    assert is_process_contract_artifact(out)
    assert len(contract.children) == 2
    assert {child.admission_id for child in contract.children} == {
        INTAKE_ADMISSION,
        POSTING_ADMISSION,
    }
