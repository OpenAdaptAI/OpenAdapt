"""Certification wiring for reusable subroutines (OpenAdapt#1892, slice 2).

``certify_project`` (and so ``flow qualify certify`` and ``flow qualify
--certify``) runs the subroutine gate: a workflow that embeds a subroutine is
certified only when every use is admitted by the supplied library, and
certifying registers the workflow so a new version of that subroutine puts
it back into re-qualification. A workflow that embeds no subroutine is
certified exactly as before.

The real qualification fixture from ``test_qualification_project`` is reused,
so the passing case is a genuine signed campaign. No model calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.ir import ParamSpec, Workflow
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import (
    QualificationRefusalCode,
    certify_project,
    current_certification_matches,
)
from openadapt_flow.subroutines import (
    SUBFLOW_PREFIX,
    SubroutineError,
    SubroutineLibrary,
    certify_subroutine_uses,
    embed,
    load_library,
    save_library,
    uses_subroutines,
)
from openadapt_flow.verification import VerificationTier
from tests.test_qualification_project import (
    _configure,
    _qualification_visual_fixture,
    _record_passing_campaign,
)
from tests.test_qualification_project import (
    _workflow as _qualified_workflow,
)
from tests.test_subroutines import (
    EVIDENCE,
    caller,
    find_record,
    key_step,
    signature,
    subroutine_from_steps,
    type_step,
)


def codes(admission) -> set[str]:
    return {refusal.code for refusal in admission.refusals}


# -- the gate ---------------------------------------------------------------------


def test_workflow_without_subroutines_never_touches_the_library():
    library = SubroutineLibrary()
    before = library.to_json()
    plain = Workflow(name="plain", surface="web")
    assert not uses_subroutines(plain)
    assert certify_subroutine_uses(plain, None).admitted
    assert certify_subroutine_uses(plain, library).admitted
    assert library.to_json() == before


def test_embedded_subroutine_without_a_library_is_refused():
    workflow = caller(find_record())
    admission = certify_subroutine_uses(workflow, None)
    assert not admission.admitted
    assert codes(admission) == {"library_missing"}


def test_certifying_registers_the_workflow_then_admits_it():
    sub = find_record()
    workflow = caller(sub)
    library = SubroutineLibrary()
    library.publish(sub)
    library.qualify(sub.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    assert library.dependents(sub.name) == ()

    admission = certify_subroutine_uses(workflow, library)

    assert admission.admitted, admission.refusals
    assert library.dependents(sub.name) == ("intake",)
    # Run-time admission now finds the registration certification wrote.
    from openadapt_flow.subroutines import admit_workflow

    assert admit_workflow(workflow, library).admitted


def test_certification_keeps_every_other_refusal():
    sub = find_record()
    workflow = caller(sub)
    library = SubroutineLibrary()
    library.publish(sub)  # published but never qualified
    admission = certify_subroutine_uses(workflow, library)
    assert codes(admission) == {"not_qualified"}
    # Registration still happened, so a later version re-qualifies it.
    assert library.dependents(sub.name) == ("intake",)

    unknown = certify_subroutine_uses(caller(find_record()), SubroutineLibrary())
    assert codes(unknown) == {"unknown_subroutine"}


def test_new_version_requalifies_a_certified_workflow():
    v1 = find_record()
    workflow = caller(v1)
    library = SubroutineLibrary()
    library.publish(v1)
    library.qualify(v1.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    assert certify_subroutine_uses(workflow, library, workflow_id="wf-1").admitted

    v2 = find_record(
        steps=[
            key_step("focus", "G"),
            type_step("enter", "query"),
            key_step("go", "Enter"),
        ]
    )
    plan = library.publish(v2)
    library.qualify(v2.sha256, policy_name="standard", evidence_sha256=EVIDENCE)

    assert plan.workflows == ("wf-1",)
    stale = certify_subroutine_uses(workflow, library, workflow_id="wf-1")
    assert "superseded" in codes(stale)
    # Re-embedding the new version and certifying again admits it.
    assert certify_subroutine_uses(caller(v2), library, workflow_id="wf-1").admitted


def test_malformed_subroutine_key_is_refused_not_raised():
    workflow = caller(find_record())
    key = next(iter(workflow.subflows))
    workflow.subflows[f"{SUBFLOW_PREFIX}Bad/not-a-digest"] = workflow.subflows[key]
    library = SubroutineLibrary()
    admission = certify_subroutine_uses(workflow, library)
    assert "malformed_key" in codes(admission)


def test_library_file_round_trip_and_tamper_refusal(tmp_path: Path):
    sub = find_record()
    library = SubroutineLibrary()
    library.publish(sub)
    library.qualify(sub.sha256, policy_name="standard", evidence_sha256=EVIDENCE)
    path = tmp_path / "lib" / "subroutines.json"
    save_library(library, path)
    assert load_library(path).to_json() == library.to_json()
    assert [p.name for p in path.parent.iterdir()] == ["subroutines.json"]

    raw = json.loads(path.read_text())
    digest = next(iter(raw["subroutines"]))
    raw["subroutines"][digest]["signature"]["intent"] = "edited"
    path.write_text(json.dumps(raw))
    with pytest.raises(SubroutineError, match="does not match its digest"):
        load_library(path)
    with pytest.raises(SubroutineError, match="cannot read"):
        load_library(tmp_path / "missing.json")


# -- certify_project on a real signed campaign -------------------------------------


def _note_subroutine():
    return subroutine_from_steps(
        signature(
            name="enter_note",
            intent="type the note into the focused field",
            inputs=(signature().inputs[0].model_copy(update={"name": "note"}),),
            surface_kind="citrix",
        ),
        [key_step("focus", "Tab"), type_step("enter", "note")],
    )


def _certifiable(tmp_path: Path, *, embed_subroutine: bool) -> tuple[Workflow, Path]:
    workflow = _qualified_workflow()
    workflow.surface = "citrix"
    workflow.param_specs = {"note": ParamSpec(name="note", example="example")}
    if embed_subroutine:
        workflow, _ = embed(workflow, _note_subroutine())
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_qualification_visual_fixture()[1])
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    _configure(workflow, tier=VerificationTier.INDEPENDENT_SYSTEM)
    evidence_root = tmp_path / "evidence"
    _record_passing_campaign(workflow, evidence_root)
    return workflow, evidence_root


def _subroutine_refusals(report) -> list:
    return [
        r
        for r in report.refusals
        if r.code is QualificationRefusalCode.SUBROUTINE_NOT_ADMITTED
    ]


def test_certify_project_without_subroutines_is_unchanged(tmp_path: Path):
    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=False)
    policy = load_policy("clinical-write")
    report = certify_project(workflow, policy=policy, evidence_root=evidence_root)
    assert report.passed, report.refusals
    assert not _subroutine_refusals(report)
    assert current_certification_matches(workflow, policy=policy)


def test_certify_project_refuses_subroutines_without_a_library(tmp_path: Path):
    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=True)
    policy = load_policy("clinical-write")
    report = certify_project(workflow, policy=policy, evidence_root=evidence_root)
    assert not report.passed
    refusals = _subroutine_refusals(report)
    assert [r.details["subroutine_code"] for r in refusals] == ["library_missing"]
    # Everything else about the campaign passed; only the gate refused.
    assert refusals == report.refusals
    assert workflow.qualification is not None
    certification = workflow.qualification.last_certification
    assert certification is not None and certification.passed is False
    assert certification.report_sha256 == report.report_sha256()
    assert not current_certification_matches(workflow, policy=policy)


def test_certify_project_refuses_an_unqualified_subroutine(tmp_path: Path):
    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=True)
    library = SubroutineLibrary()
    library.publish(_note_subroutine())
    report = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=evidence_root,
        subroutine_library=library,
    )
    assert not report.passed
    assert [r.details["subroutine_code"] for r in _subroutine_refusals(report)] == [
        "not_qualified"
    ]


def test_certify_project_admits_and_registers_a_qualified_subroutine(tmp_path: Path):
    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=True)
    sub = _note_subroutine()
    library = SubroutineLibrary()
    library.publish(sub)
    library.qualify(sub.sha256, policy_name="clinical-write", evidence_sha256=EVIDENCE)
    policy = load_policy("clinical-write")

    report = certify_project(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
        subroutine_library=library,
        subroutine_workflow_id="qualified-write@1",
    )

    assert report.passed, report.refusals
    assert library.dependents("enter_note") == ("qualified-write@1",)
    assert current_certification_matches(workflow, policy=policy)

    # A new version of the subroutine lists this workflow and refuses it.
    v2 = subroutine_from_steps(
        sub.signature,
        [
            key_step("focus", "Tab"),
            key_step("clear", "Delete"),
            type_step("enter", "note"),
        ],
    )
    plan = library.publish(v2)
    assert plan.workflows == ("qualified-write@1",)
    again = certify_project(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
        subroutine_library=library,
        subroutine_workflow_id="qualified-write@1",
    )
    assert not again.passed
    assert "superseded" in {
        r.details["subroutine_code"] for r in _subroutine_refusals(again)
    }


# -- the `flow qualify certify` command -------------------------------------------


def _cli_bundle(tmp_path: Path) -> tuple[Path, Path]:
    from openadapt_flow.qualification import save_qualified_workflow

    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=True)
    save_qualified_workflow(workflow, tmp_path / "bundle")
    return tmp_path / "bundle", evidence_root


def _certify_cli(bundle: Path, evidence_root: Path, *extra: str) -> int:
    from openadapt_flow.__main__ import main

    return main(
        [
            "qualify",
            "certify",
            str(bundle),
            "--policy",
            "clinical-write",
            "--evidence-root",
            str(evidence_root),
            "--json",
            *extra,
        ]
    )


def test_cli_certify_refuses_an_embedded_subroutine_without_a_library(
    tmp_path: Path, capsys
):
    bundle, evidence_root = _cli_bundle(tmp_path)
    assert _certify_cli(bundle, evidence_root) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is False
    assert {r["code"] for r in out["refusals"]} == {"subroutine_not_admitted"}


def test_cli_certify_admits_and_writes_the_registration_back(tmp_path: Path, capsys):
    bundle, evidence_root = _cli_bundle(tmp_path)
    sub = _note_subroutine()
    library = SubroutineLibrary()
    library.publish(sub)
    library.qualify(sub.sha256, policy_name="clinical-write", evidence_sha256=EVIDENCE)
    path = tmp_path / "subroutines.json"
    save_library(library, path)

    code = _certify_cli(
        bundle,
        evidence_root,
        "--subroutine-library",
        str(path),
        "--subroutine-workflow-id",
        "qualified-write@1",
    )

    assert code == 0, capsys.readouterr()
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert load_library(path).dependents("enter_note") == ("qualified-write@1",)


def test_cli_certify_refuses_a_tampered_library_file(tmp_path: Path):
    bundle, evidence_root = _cli_bundle(tmp_path)
    path = tmp_path / "subroutines.json"
    path.write_text('{"schema": "something-else"}')
    with pytest.raises(SystemExit, match="subroutine library REFUSED"):
        _certify_cli(bundle, evidence_root, "--subroutine-library", str(path))
    assert path.read_text() == '{"schema": "something-else"}'


# -- the `flow qualify --certify` campaign ----------------------------------------


def test_campaign_certify_refuses_before_running_without_a_library(tmp_path: Path):
    from openadapt_flow.qualify import QualifyConfigError, qualify
    from tests.test_qualify_declarative import _config
    from tests.test_qualify_declarative import _workflow as _campaign_workflow

    workflow = _campaign_workflow()
    workflow.surface = "web"
    workflow, _ = embed(workflow, find_record())
    bundle = tmp_path / "bundle"
    workflow.save(bundle)
    out = tmp_path / "out"
    with pytest.raises(QualifyConfigError, match="--subroutine-library"):
        qualify(
            bundle, _config(), out=out, certify="clinical-write", log=lambda _l: None
        )
    assert not out.exists()
