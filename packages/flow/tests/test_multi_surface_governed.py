"""Governed, durable, and qualified runs of multi-surface bundles.

docs/MULTI_SURFACE.md, "Governance and verification". A multi-surface run
reaches VERIFIED under exactly the single-surface rules (governed
authorization, durable checkpoints, identity on writes, an effect confirmed by
an independent read of the system of record), plus one rule of its own: every
step result records the surface it ran on, and that record must match the
sealed contract. Screen-only evidence never reaches VERIFIED on any surface.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
)
from openadapt_flow.ir import (
    ActionKind,
    ObservedSurfaceEnvironment,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    StepResult,
    SurfaceSpec,
    Workflow,
)
from openadapt_flow.qualification import (
    EnvironmentBoundary,
    QualificationProject,
    _surface_environment_evidence_refusal,
)
from openadapt_flow.qualification_environment import (
    QualificationEnvironmentObservation,
)
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.durable import CheckpointStore
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_execution_profiles import (
    _gate,
    _key_workflow,
    _ReadyVision,
    _sealed,
    _TieredVerifier,
)
from tests.test_replayer import FakeBackend


class _WindowsFake(FakeBackend):
    surface_kind = "windows"


class _PersistedVerifier(_TieredVerifier):
    verification_tier = VerificationTier.PERSISTED_STATE_REACQUISITION


def _multi_surface_write_workflow() -> Workflow:
    """Primary web surface, then a switch to Windows and a governed write."""

    base = _key_workflow("multi-surface-governed", with_effect=True)
    write = base.steps[0].model_copy(update={"surface": "windows_1"})
    switch = Step(
        id="to_windows",
        intent="switch to surface 'windows_1'",
        action=ActionKind.SWITCH_SURFACE,
        surface="windows_1",
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Desktop Home")
        ],
    )
    return Workflow(
        name=base.name,
        surfaces=[
            SurfaceSpec(id="main", kind="web"),
            SurfaceSpec(id="windows_1", kind="windows"),
        ],
        steps=[switch, write],
    )


def _governed_run(tmp_path, *, verifier=None, admit_with=None):
    workflow, bundle = _sealed(
        tmp_path, _multi_surface_write_workflow(), encrypted=False
    )
    verifier = verifier or _TieredVerifier()
    gate = _gate(
        workflow,
        bundle,
        ExecutionProfile.STANDARD,
        verifier=admit_with or verifier,
        durable=True,
    )
    assert gate.passed, gate.render()
    primary, windows = FakeBackend(), _WindowsFake()
    vision = _ReadyVision()
    vision.text_results["Desktop Home"] = object()
    replayer = Replayer(
        primary,
        vision=vision,
        effect_verifier=verifier,
        governed_authorization=build_runtime_authorization(workflow, gate),
        durable=True,
        require_settled=True,
        surface_backends={"windows_1": windows},
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
    return report, workflow, primary, windows, replayer


def test_governed_durable_multi_surface_run_reaches_verified(tmp_path) -> None:
    report, workflow, primary, windows, replayer = _governed_run(tmp_path)

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value, [
        (r.step_id, r.ok, r.error) for r in report.results
    ]
    assert report.execution_profile == "standard"
    # Each result records the surface it ran on; the write drove only the
    # Windows backend, and the primary is restored after the run.
    assert [(r.step_id, r.surface) for r in report.results] == [
        ("to_windows", "windows_1"),
        ("submit", "windows_1"),
    ]
    assert windows.actions and not primary.actions
    assert replayer.backend is primary
    write = report.results[1]
    assert write.effect_verified is True
    assert write.identity is not None and write.identity.status == "verified"
    # The durable checkpoint of the confirmed write exists.
    store = CheckpointStore(tmp_path / "run")
    assert store.last_checkpoint() is not None
    # report.json carries the per-step surface record.
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert [r.get("surface") for r in saved["results"]] == ["windows_1"] * 2


def test_surface_record_must_match_the_contract(tmp_path) -> None:
    report, workflow, *_ = _governed_run(tmp_path)
    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value

    for tampered_surface in (None, "main"):
        tampered = report.model_copy(deep=True)
        tampered.results[1].surface = tampered_surface
        assert (
            classify_execution_outcome(tampered, workflow, ExecutionProfile.STANDARD)
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_screen_reacquisition_never_verifies_a_multi_surface_run(tmp_path) -> None:
    # Admitted with a system-of-record verifier, but the run's verifier only
    # re-reads the screen: VERIFIED is out of reach, on any surface.
    report, *_ = _governed_run(
        tmp_path, verifier=_PersistedVerifier(), admit_with=_TieredVerifier()
    )
    assert report.execution_outcome != ExecutionOutcome.VERIFIED.value


def test_single_surface_results_carry_no_surface_record(tmp_path) -> None:
    workflow, bundle = _sealed(
        tmp_path, _key_workflow("single", with_effect=True), encrypted=False
    )
    verifier = _TieredVerifier()
    gate = _gate(
        workflow, bundle, ExecutionProfile.STANDARD, verifier=verifier, durable=True
    )
    report = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=verifier,
        governed_authorization=build_runtime_authorization(workflow, gate),
        durable=True,
        require_settled=True,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run")
    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert all(r.surface is None for r in report.results)
    saved = json.loads((tmp_path / "run" / "report.json").read_text())
    assert all("surface" not in r for r in saved["results"])
    assert "observed_surface_environments" not in saved
    # A surface record on a single-surface report is not evidence for it.
    tampered = report.model_copy(deep=True)
    tampered.results[0].surface = "main"
    assert (
        classify_execution_outcome(tampered, workflow, ExecutionProfile.STANDARD)
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


# -- qualification: one environment boundary per surface ---------------------

_DIGEST = "a" * 64


def _boundary(kind: str, identity: str) -> EnvironmentBoundary:
    return EnvironmentBoundary(
        target_kind=kind,
        application=identity,
        application_identity=identity,
        application_version="1.0",
        environment_observer_id="test-observer",
        environment_observer_contract_sha256="b" * 64,
        environment_digest=_DIGEST,
        runtime_version="test",
    )


def _project(**surface_environments) -> QualificationProject:
    return QualificationProject(
        environment=_boundary("web", "https://app.example.test"),
        surface_environments=surface_environments,
    )


def test_single_surface_project_contract_bytes_are_unchanged() -> None:
    project = _project()
    assert "surface_environments" not in project.model_dump(mode="json")
    with_surface = _project(windows_1=_boundary("windows", "Desk.exe"))
    assert with_surface.contract_sha256() != project.contract_sha256()


def _case_replayer(project: QualificationProject) -> tuple[Replayer, Workflow]:
    workflow = _multi_surface_write_workflow().model_copy(
        update={"qualification": project}
    )
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": _WindowsFake()})
    replayer.governed_authorization = SimpleNamespace(qualification_case_id="case-1")
    return replayer, workflow


def test_qualification_case_refuses_an_unqualified_surface() -> None:
    replayer, workflow = _case_replayer(_project())
    refusal = replayer._begin_surfaces(workflow, resume=False)
    assert "no environment boundary for surface 'windows_1'" in (refusal or "")

    replayer, workflow = _case_replayer(
        _project(windows_1=_boundary("rdp", "Desk.exe"))
    )
    refusal = replayer._begin_surfaces(workflow, resume=False)
    assert "declared windows" in (refusal or "")


class _ObservedWindows(_WindowsFake):
    def __init__(self, identity: str) -> None:
        super().__init__()
        self.identity = identity
        self.guard = None

    def set_qualification_input_guard(self, guard) -> None:
        self.guard = guard


class _Observer:
    observer_id = "test-observer"
    contract_sha256 = "b" * 64

    def observe(self, backend, target_kind):
        return QualificationEnvironmentObservation(
            target_kind=target_kind,
            application_identity=backend.identity,
            application_version="1.0",
            session_identity_sha256="c" * 64,
            environment_digest=_DIGEST,
        )


def _switch_into(identity: str):
    project = _project(windows_1=_boundary("windows", "Desk.exe"))
    replayer, workflow = _case_replayer(project)
    windows = _ObservedWindows(identity)
    replayer.surface_backends = {"windows_1": windows}
    replayer.qualification_environment_observer = _Observer()
    assert replayer._begin_surfaces(workflow, resume=False) is None
    replayer.backend = windows
    refusal = replayer._bind_surface_qualification_environment(workflow, "windows_1")
    return refusal, replayer, windows


def test_switch_observes_the_surface_against_its_boundary() -> None:
    refusal, replayer, windows = _switch_into("Desk.exe")
    assert refusal is None
    observed = replayer._surface_environment_observations["windows_1"]
    assert observed.application_sha256 == hashlib.sha256(b"Desk.exe").hexdigest()
    # The input-edge guard now rechecks THIS surface before each input.
    assert windows.guard is not None
    windows.guard()  # unchanged environment: passes

    refusal, _replayer, windows = _switch_into("Other.exe")
    assert "does not match the project" in (refusal or "")
    assert windows.guard is None


def test_case_evidence_needs_a_matching_observation_per_used_surface() -> None:
    project = _project(windows_1=_boundary("windows", "Desk.exe"))
    workflow = _multi_surface_write_workflow()
    refusal, replayer, _ = _switch_into("Desk.exe")
    observed = replayer._surface_environment_observations["windows_1"]
    report = RunReport(
        workflow_name=workflow.name,
        started_at="2026-10-17T00:00:00Z",
        success=True,
        results=[
            StepResult(step_id="to_windows", intent="s", ok=True, surface="windows_1"),
            StepResult(step_id="submit", intent="w", ok=True, surface="windows_1"),
        ],
        observed_surface_environments={"windows_1": observed},
    )
    assert _surface_environment_evidence_refusal(report, workflow, project) is None

    missing = report.model_copy(update={"observed_surface_environments": {}})
    assert _surface_environment_evidence_refusal(missing, workflow, project)

    forged = report.model_copy(deep=True)
    forged.observed_surface_environments["windows_1"] = ObservedSurfaceEnvironment(
        **{
            **observed.model_dump(),
            "application_sha256": hashlib.sha256(b"Other.exe").hexdigest(),
        }
    )
    assert _surface_environment_evidence_refusal(forged, workflow, project)
