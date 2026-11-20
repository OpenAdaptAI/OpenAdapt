"""A changed authored contract retains source integrity, never its old approval."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import bundle_validation as bundles
from openadapt_flow.compiler.annotate import (
    FakeStepAnnotator,
    RiskProposal,
    StepAnnotation,
    WorkflowProposals,
    apply_annotations,
)
from openadapt_flow.compiler.api_proposal import admit_api_step, propose_api_step
from openadapt_flow.compiler.disambiguation import apply_answers, detect_ambiguities
from openadapt_flow.compiler.induction import OptionalPoint
from openadapt_flow.compiler.loop_authoring import author_data_driven_loop
from openadapt_flow.compiler.probing import ProbeTrace, interpretation_workflow
from openadapt_flow.compiler.readback_authoring import admit_check, apply_check
from openadapt_flow.ir import ActionKind, BundleManifest, ParamSpec, Step, Workflow
from openadapt_flow.qualification import (
    QualificationError,
    build_governed_authorization_template,
)
from tests import test_api_tier_web_form as api
from tests import test_readback_authoring as check
from tests.test_certification_sealed_omissions import FIXTURE

PIPELINES = (
    "loop",
    "probe",
    "annotation",
    "answers",
    "api-proposal",
    "api-admission",
    "check",
    "check-admission",
)


def _files(folder):
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


def _case(kind, root):
    if kind.startswith("api-"):
        recording = api._recording(root)
        workflow = api._workflow()
        if kind == "api-admission":
            workflow, _, reasons = propose_api_step(workflow, recording)
            assert not reasons
            return workflow, lambda source: admit_api_step(
                source, admitted_by="reviewer"
            )[0]
        return workflow, lambda source: propose_api_step(source, recording)[0]
    if kind.startswith("check"):
        recording = check._recording(root)
        vision = check._marked_screen(check.SHOWS)
        workflow = check._workflow()
        if kind == "check-admission":
            workflow, _ = apply_check(workflow, recording, vision=vision)
            return workflow, lambda source: admit_check(source, admitted_by="reviewer")[
                0
            ]
        return workflow, lambda source: apply_check(source, recording, vision=vision)[0]
    workflow = Workflow(
        name="synthetic authoring source",
        steps=[
            Step(
                id="type_value",
                intent="type value",
                action=ActionKind.TYPE,
                text="example",
                param="value",
            )
        ],
        param_specs={"value": ParamSpec(name="value", example="example")},
    )
    if kind == "loop":
        return workflow, lambda source: author_data_driven_loop(
            source, [{"value": "first"}, {"value": "second"}]
        )
    if kind == "probe":
        point = OptionalPoint(
            location="optional_0",
            kind="optional",
            column=0,
            source_trace=0,
            step=workflow.steps[0],
            index_in_trace={0: 0},
        )
        return workflow, lambda source: interpretation_workflow(
            [ProbeTrace(source, root / "bundle", "synthetic")], point, 0, "conditional"
        )[0]
    if kind == "annotation":
        annotator = FakeStepAnnotator(
            WorkflowProposals(
                steps=[
                    StepAnnotation(
                        step_id="type_value",
                        risk=RiskProposal(proposed_risk="irreversible"),
                    )
                ]
            )
        )
        return workflow, lambda source: apply_annotations(source, annotator).workflow
    workflow.steps[0].param = None
    workflow.param_specs = {}

    def answers(source):
        question = detect_ambiguities(source)[0]
        return apply_answers(source, {question.id: "param"}).workflow

    return workflow, answers


@pytest.mark.parametrize("kind", PIPELINES)
def test_each_authoring_entry_point_expires_a_previous_decision(tmp_path, kind):
    workflow, transform = _case(kind, tmp_path)
    source = tmp_path / "source"
    workflow.save(source)
    loaded = Workflow.load(source)
    # A planted stale metadata flag, not a real certification or approval.
    loaded.manifest.provenance.certified = True
    snapshot = loaded.model_dump()
    original = _files(source)
    candidate = transform(loaded)
    assert candidate.manifest is not None
    assert not candidate.manifest.provenance.certified
    assert candidate.manifest.provenance.certification_status == "expired"
    assert loaded.model_dump() == snapshot
    assert bundles.has_verified_load_capability(candidate)
    target = tmp_path / "derived"
    candidate.save(target)
    reopened = Workflow.load(target)
    assert not reopened.manifest.provenance.certified
    assert reopened.manifest.content_digest != loaded.manifest.content_digest
    assert _files(source) == original


@pytest.mark.parametrize("kind", PIPELINES)
@pytest.mark.parametrize(
    "fault", ("unchecked", "manifest-dropped", "raw-clone", "changed-seal")
)
def test_authoring_cannot_launder_unverified_source_authority(tmp_path, kind, fault):
    workflow, transform = _case(kind, tmp_path)
    source = tmp_path / "source"
    workflow.save(source)
    loaded = Workflow.load(source, verify_integrity=fault != "unchecked")
    if fault == "manifest-dropped":
        loaded.manifest = None
    elif fault == "raw-clone":
        loaded = Workflow.model_validate(loaded.model_dump())
    elif fault == "changed-seal":
        loaded.manifest.content_digest = "0" * 64
    original = _files(source)
    with pytest.raises(bundles.BundleIntegrityError):
        transform(loaded)
    assert _files(source) == original


@pytest.mark.parametrize("kind", PIPELINES)
@pytest.mark.parametrize("draft_manifest", (False, True))
def test_genesis_authoring_stays_fresh_and_valid(tmp_path, kind, draft_manifest):
    workflow, transform = _case(kind, tmp_path)
    if draft_manifest:
        workflow.manifest = BundleManifest()
        workflow.manifest.provenance.source_recording_sha256 = "a" * 64
    before = workflow.model_dump()
    candidate = transform(workflow)
    assert (candidate.manifest is not None) == draft_manifest
    if draft_manifest:
        assert not candidate.manifest.content_digest
        assert candidate.manifest.provenance.source_recording_sha256 == "a" * 64
    assert not bundles.has_verified_load_capability(candidate)
    assert workflow.model_dump() == before
    candidate.save(tmp_path / "derived")
    assert Workflow.load(tmp_path / "derived").manifest is not None


@pytest.mark.parametrize(
    "field", ("manifest", "schema_version", "encrypted", "unknown")
)
def test_authoring_update_cannot_replace_authority_fields(field):
    with pytest.raises(bundles.BundleIntegrityError):
        bundles.derive_authored_workflow(Workflow(name="fresh"), update={field: None})


@settings(max_examples=8, deadline=None)
@given(
    records=st.lists(
        st.fixed_dictionaries(
            {"lname": st.text(alphabet="abcdef", min_size=1, max_size=8)}
        ),
        min_size=1,
        max_size=4,
    )
)
def test_a_held_out_certified_bundle_can_be_authored_but_needs_qualification(
    tmp_path_factory, records
):
    # A pre-additive-field certified case, distinct from the current bundle
    # that exposed the failure. Its real certification and template reproduce.
    root = tmp_path_factory.mktemp("certified-loop")
    source = Workflow.load(FIXTURE)
    before = source.model_dump()
    assert source.qualification.last_certification is not None
    assert source.manifest.provenance.governed_authorization_template is not None
    candidate = author_data_driven_loop(source, records)
    assert candidate.qualification.last_certification is None
    assert candidate.manifest.provenance.governed_authorization_template is None
    assert not candidate.manifest.provenance.certified
    destination = root / "derived"
    candidate.save(destination)
    reopened = Workflow.load(destination)
    with pytest.raises(QualificationError):
        build_governed_authorization_template(reopened)
    assert source.model_dump() == before
    # The independent source still passes the original strict load.
    assert (
        Workflow.load(FIXTURE).manifest.content_digest == source.manifest.content_digest
    )


def test_materialized_additive_fields_expire_the_older_contract(tmp_path):
    source = Workflow.load(FIXTURE)
    candidate = bundles.derive_authored_workflow(source, update={})
    assert source.qualification.last_certification is not None
    assert candidate.qualification.last_certification is None
    assert candidate.manifest.provenance.governed_authorization_template is None
    candidate.save(tmp_path / "materialized")
    reopened = Workflow.load(tmp_path / "materialized")
    assert reopened.qualification.last_certification is None
    assert source.qualification.last_certification is not None


def test_unchanged_current_contract_retains_its_certification(tmp_path):
    source_path = (
        Path(__file__).parents[1] / "benchmark/openemr_local/referral_program/program"
    )
    source = Workflow.load(source_path)
    candidate = bundles.derive_authored_workflow(source, update={})
    assert (
        candidate.qualification.last_certification
        == source.qualification.last_certification
    )
    assert candidate.manifest.provenance.governed_authorization_template is not None
    candidate.save(tmp_path / "unchanged")
    reopened = Workflow.load(tmp_path / "unchanged")
    assert (
        reopened.qualification.last_certification
        == source.qualification.last_certification
    )
