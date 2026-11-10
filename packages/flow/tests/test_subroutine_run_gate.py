"""The run gate re-admits embedded subroutines (OpenAdapt#1892, runtime gate).

A certificate records what was admitted when it was made. Publishing a newer
version of a subroutine afterwards does not change that certificate, so
before this gate a workflow certified against the old version kept running
the old version until someone certified it again. Every run now re-admits
each embedded subroutine against the CURRENT library, with the same refusal
codes certification uses, and refuses fail-closed without a library.

No model calls; the certified case reuses the real signed campaign fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.ir import ParamSpec, Workflow
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import (
    certify_project,
    current_certification_matches,
    save_qualified_workflow,
)
from openadapt_flow.run_gate import GATE_SUBROUTINES
from openadapt_flow.subroutines import (
    SUBFLOW_PREFIX,
    SubroutineLibrary,
    admit_for_run,
    certify_subroutine_uses,
    embed,
    save_library,
)
from tests.test_run_gate import _KEY, _good_workflow, _no_execute, _run, _seal
from tests.test_subroutine_certification import _certifiable, _note_subroutine
from tests.test_subroutines import (
    EVIDENCE,
    find_record,
    key_step,
    subroutine_from_steps,
    type_step,
)

_SENTINEL = "Sentinel-Value-4711"


def codes(admission) -> set[str]:
    return {refusal.code for refusal in admission.refusals}


def _library(*subs) -> SubroutineLibrary:
    library = SubroutineLibrary()
    for sub in subs:
        library.publish(sub)
        library.qualify(
            sub.sha256, policy_name="clinical-write", evidence_sha256=EVIDENCE
        )
    return library


def _v2():
    return find_record(
        steps=[
            key_step("focus", "G"),
            type_step("enter", "query"),
            key_step("go", "Enter"),
        ]
    )


def _admissible(tmp_path: Path, sub=None, *, name: str = "intake"):
    """A bundle that passes every other gate and embeds ``sub``.

    Certification registers it in the returned library, as ``flow qualify
    certify`` does.
    """

    sub = sub or find_record()
    workflow = _good_workflow(name)
    workflow.surface = "web"
    workflow.param_specs = {"query": ParamSpec(name="query", example=_SENTINEL)}
    workflow, _ = embed(workflow, sub)
    library = _library(sub)
    assert certify_subroutine_uses(workflow, library).admitted
    loaded, bundle = _seal(workflow, tmp_path)
    return loaded, bundle, library


# -- admit_for_run ----------------------------------------------------------------


def test_run_admission_ignores_workflows_without_subroutines():
    plain = Workflow(name="plain", surface="web")
    assert admit_for_run(plain, None).admitted


def test_run_admission_refuses_without_a_library():
    workflow, _ = embed(
        Workflow(
            name="intake",
            surface="web",
            param_specs={"query": ParamSpec(name="query", example="x")},
        ),
        find_record(),
    )
    assert codes(admit_for_run(workflow, None)) == {"library_missing"}


def test_run_admission_never_changes_the_library(tmp_path: Path):
    workflow, _bundle, library = _admissible(tmp_path)
    before = library.to_json()
    library.publish(_v2())
    after_publish = library.to_json()
    assert codes(admit_for_run(workflow, library)) == {"superseded"}
    assert library.to_json() == after_publish != before


def test_run_admission_requires_the_certified_registration(tmp_path: Path):
    sub = find_record()
    workflow, _ = embed(
        Workflow(
            name="intake",
            surface="web",
            param_specs={"query": ParamSpec(name="query", example="x")},
        ),
        sub,
    )
    library = _library(sub)  # qualified, but this workflow was never certified
    assert codes(admit_for_run(workflow, library)) == {"unregistered_use"}
    # Qualification case runs happen before certification registers the
    # workflow; only that one requirement is relaxed for them.
    assert admit_for_run(workflow, library, qualification_evidence=True).admitted
    library.publish(_v2())
    assert codes(admit_for_run(workflow, library, qualification_evidence=True)) == {
        "superseded"
    }


# -- evaluate_run_gate --------------------------------------------------------------


def test_bundle_without_subroutines_passes_the_gate_untouched(tmp_path: Path):
    wf, bundle = _seal(_good_workflow(), tmp_path)
    report = _run(wf, bundle)
    assert report.passed, report.render()
    gate = report.gate(GATE_SUBROUTINES)
    assert gate is not None and gate.passed
    assert gate.detail == "bundle embeds no subroutine"


def test_gate_admits_a_current_registered_subroutine(tmp_path: Path):
    wf, bundle, library = _admissible(tmp_path)
    report = _run(wf, bundle, subroutine_library=library)
    assert report.passed, report.render()
    assert "find_record" in report.gate(GATE_SUBROUTINES).detail


def test_gate_refuses_an_embedded_subroutine_without_a_library(tmp_path: Path):
    wf, bundle, _library_unused = _admissible(tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    gate = report.gate(GATE_SUBROUTINES)
    assert not gate.passed
    assert "library_missing" in gate.detail
    assert [g.gate for g in report.refusals] == [GATE_SUBROUTINES]


def test_superseded_after_publish_refuses_the_run(tmp_path: Path):
    wf, bundle, library = _admissible(tmp_path)
    assert _run(wf, bundle, subroutine_library=library).passed

    library.publish(_v2())

    report = _run(wf, bundle, subroutine_library=library)
    assert not report.passed
    assert [g.gate for g in report.refusals] == [GATE_SUBROUTINES]
    assert "superseded" in report.gate(GATE_SUBROUTINES).detail
    assert "Nothing was executed" in report.render()


def test_gate_refuses_a_subroutine_the_library_does_not_hold(tmp_path: Path):
    wf, bundle, _library_unused = _admissible(tmp_path)
    report = _run(wf, bundle, subroutine_library=SubroutineLibrary())
    assert not report.passed
    assert "unknown_subroutine" in report.gate(GATE_SUBROUTINES).detail


def test_gate_refuses_an_unqualified_version(tmp_path: Path):
    wf, bundle, _library_unused = _admissible(tmp_path)
    unqualified = SubroutineLibrary()
    unqualified.publish(find_record())
    unqualified.register(wf.name, wf)
    report = _run(wf, bundle, subroutine_library=unqualified)
    assert not report.passed
    assert "not_qualified" in report.gate(GATE_SUBROUTINES).detail


def test_gate_refuses_an_edited_embedded_body(tmp_path: Path):
    wf, bundle, library = _admissible(tmp_path)
    key = next(k for k in wf.subflows if k.startswith(SUBFLOW_PREFIX))
    edited = wf.subflows[key].model_copy(deep=True)
    edited.states.pop(next(s for s in edited.states if s != edited.entry))
    wf.subflows[key] = edited
    report = _run(wf, bundle, subroutine_library=library)
    assert not report.passed
    assert "embedded_drift" in report.gate(GATE_SUBROUTINES).detail


def test_gate_messages_carry_no_parameter_values(tmp_path: Path):
    wf, bundle, library = _admissible(tmp_path)
    library.publish(_v2())
    rendered = _run(wf, bundle, subroutine_library=library).render()
    assert "superseded" in rendered
    assert _SENTINEL not in rendered


# -- the failure mode this gate closes: certified before publish, still runs -------


def test_certified_before_publish_no_longer_runs(tmp_path: Path):
    workflow, evidence_root = _certifiable(tmp_path, embed_subroutine=True)
    sub = _note_subroutine()
    library = _library(sub)
    policy = load_policy("clinical-write")
    report = certify_project(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
        subroutine_library=library,
    )
    assert report.passed, report.refusals
    bundle = tmp_path / "bundle"
    save_qualified_workflow(workflow, bundle)
    certified = Workflow.load(bundle)

    gate_before = _run(certified, bundle, subroutine_library=library).gate(
        GATE_SUBROUTINES
    )
    assert gate_before.passed, gate_before.detail

    v2 = subroutine_from_steps(
        sub.signature,
        [
            key_step("focus", "Tab"),
            key_step("clear", "Delete"),
            type_step("enter", "note"),
        ],
    )
    plan = library.publish(v2)
    assert plan.workflows == (certified.name,)

    # The certificate is a snapshot: it still independently reproduces...
    assert current_certification_matches(certified, policy=policy)
    # ...but the run gate consults the current library and refuses.
    gate_after = _run(certified, bundle, subroutine_library=library).gate(
        GATE_SUBROUTINES
    )
    assert not gate_after.passed
    assert "superseded" in gate_after.detail


# -- `flow run` -----------------------------------------------------------------------


def _cli_run(bundle: Path, *extra: str) -> int:
    import openadapt_flow.__main__ as main

    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--policy",
            "clinical-write",
            "--effects-kind",
            "rest",
            "--effects-records-path",
            "/api/db",
            "--effects-base-url",
            "http://sor.local",
            "--dry-run",
            *extra,
        ]
    )
    return args.func(args)


def test_cli_run_refuses_a_subroutine_bundle_without_a_library(
    tmp_path: Path, monkeypatch, capsys
):
    _no_execute(monkeypatch)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    _wf, bundle, _lib = _admissible(tmp_path)
    assert _cli_run(bundle) == 2
    out = capsys.readouterr().out
    assert "[REFUSE] Subroutine admission" in out
    assert "library_missing" in out


def test_cli_run_admits_then_refuses_after_a_new_version(
    tmp_path: Path, monkeypatch, capsys
):
    _no_execute(monkeypatch)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    _wf, bundle, library = _admissible(tmp_path)
    path = tmp_path / "subroutines.json"
    save_library(library, path)

    assert _cli_run(bundle, "--subroutine-library", str(path)) == 0
    assert "[PASS] Subroutine admission" in capsys.readouterr().out

    library.publish(_v2())
    save_library(library, path)
    assert _cli_run(bundle, "--subroutine-library", str(path)) == 2
    out = capsys.readouterr().out
    assert "[REFUSE] Subroutine admission" in out
    assert "superseded" in out


def test_cli_run_uses_the_registered_workflow_id(tmp_path: Path, monkeypatch, capsys):
    _no_execute(monkeypatch)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    wf, bundle, library = _admissible(tmp_path)
    library.register("intake@deployment-1", wf)
    library._uses = {u for u in library.uses() if u.workflow_id != wf.name}
    path = tmp_path / "subroutines.json"
    save_library(library, path)

    assert _cli_run(bundle, "--subroutine-library", str(path)) == 2
    assert "unregistered_use" in capsys.readouterr().out
    assert (
        _cli_run(
            bundle,
            "--subroutine-library",
            str(path),
            "--subroutine-workflow-id",
            "intake@deployment-1",
        )
        == 0
    )


def test_cli_run_refuses_a_tampered_library_file(tmp_path: Path, monkeypatch, capsys):
    _no_execute(monkeypatch)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    _wf, bundle, _lib = _admissible(tmp_path)
    path = tmp_path / "subroutines.json"
    path.write_text('{"schema": "something-else"}')
    assert _cli_run(bundle, "--subroutine-library", str(path)) == 2
    out = capsys.readouterr().out
    assert "run REFUSED" in out and "Nothing was executed" in out


# -- the qualification campaign -------------------------------------------------------


def test_campaign_without_certify_still_needs_the_library(tmp_path: Path):
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
        qualify(bundle, _config(), out=out, log=lambda _l: None)
    assert not out.exists()
