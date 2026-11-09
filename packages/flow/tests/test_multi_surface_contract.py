"""Unit tests for the multi-surface IR contract and the replayer's refusals."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    SurfaceSpec,
    Workflow,
    surface_contract_errors,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend


def _wait(step_id: str, **kw) -> Step:
    return Step(id=step_id, intent="wait", action=ActionKind.WAIT, **kw)


def _switch(step_id: str, surface: str, **kw) -> Step:
    kw.setdefault(
        "expect",
        [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Desktop Home")],
    )
    return Step(
        id=step_id,
        intent=f"switch to surface '{surface}'",
        action=ActionKind.SWITCH_SURFACE,
        surface=surface,
        **{"risk": "reversible", **kw},
    )


_MAIN = SurfaceSpec(id="main", kind="web")
_WIN = SurfaceSpec(id="windows_1", kind="windows")
_POPUP = SurfaceSpec(
    id="popup_1",
    kind="web",
    opened_by_step="step_000",
    opener="main",
    open_order=0,
    opener_link="window_opener",
    origin="http://127.0.0.1:8000",
    url_pattern=r"http://127\.0\.0\.1:8000/popup\.html(?:[?#].*)?",
)


def _multi() -> Workflow:
    return Workflow(
        name="wf",
        surfaces=[_MAIN, _WIN],
        steps=[
            _wait("step_000"),
            _switch("step_001", "windows_1"),
            _wait("step_002", surface="windows_1"),
        ],
    )


def test_single_surface_bundle_serializes_without_surface_fields() -> None:
    workflow = Workflow(name="wf", steps=[_wait("step_000")])
    dumped = json.loads(workflow.model_dump_json())
    assert "surfaces" not in dumped
    assert surface_contract_errors(workflow) == []
    assert workflow.is_multi_surface is False


def test_multi_surface_round_trips() -> None:
    workflow = _multi()
    again = Workflow.model_validate_json(workflow.model_dump_json())
    assert [s.id for s in again.surfaces] == ["main", "windows_1"]
    assert again.steps[1].action is ActionKind.SWITCH_SURFACE
    assert again.is_multi_surface


def test_surface_fields_without_declared_surfaces_are_refused() -> None:
    with pytest.raises(ValidationError, match="declares no surfaces"):
        Workflow(name="wf", steps=[_wait("step_000", surface="windows_1")])


def test_step_on_a_surface_never_switched_to_is_refused() -> None:
    with pytest.raises(ValidationError):
        Workflow(
            name="wf",
            surfaces=[_MAIN, _WIN],
            steps=[_wait("step_000", surface="windows_1")],
        )


def test_switch_to_a_page_before_it_opens_is_refused() -> None:
    with pytest.raises(ValidationError):
        Workflow(
            name="wf",
            surfaces=[_MAIN, _POPUP],
            steps=[_switch("step_000", "popup_1"), _wait("step_001")],
        )


def test_declared_page_must_match_the_step_opens_list() -> None:
    with pytest.raises(ValidationError, match="opens"):
        Workflow(
            name="wf",
            surfaces=[_MAIN, _POPUP],
            steps=[_wait("step_000"), _switch("step_001", "popup_1")],
        )
    ok = Workflow(
        name="wf",
        surfaces=[_MAIN, _POPUP],
        steps=[
            _wait("step_000", opens=["popup_1"]),
            _switch("step_001", "popup_1"),
        ],
    )
    assert ok.steps[0].opens == ["popup_1"]


def test_primary_surface_cannot_be_a_page() -> None:
    with pytest.raises(ValidationError, match="primary"):
        Workflow(name="wf", surfaces=[_POPUP], steps=[_wait("step_000")])


@pytest.mark.parametrize(
    "extra",
    [{"text": "x"}, {"param": "p"}, {"risk": "irreversible"}, {"opens": ["a"]}],
)
def test_switch_step_carries_no_input(extra) -> None:
    with pytest.raises(ValidationError):
        _switch("step_001", "windows_1", **extra)


def test_switch_step_requires_a_surface() -> None:
    with pytest.raises(ValidationError):
        Step(id="s", intent="switch", action=ActionKind.SWITCH_SURFACE)


def test_surface_origin_only_on_switch() -> None:
    with pytest.raises(ValidationError):
        _wait("s", surface_origin="http://127.0.0.1:8000")


def test_page_surface_requires_its_identity_fields() -> None:
    with pytest.raises(ValidationError):
        SurfaceSpec(id="popup_1", kind="web", opened_by_step="step_000", opener="main")
    with pytest.raises(ValidationError):
        SurfaceSpec(id="Bad-Id", kind="web")


class _WindowsFake(FakeBackend):
    surface_kind = "windows"


def _run(replayer: Replayer, workflow: Workflow, tmp_path, **kw):
    bundle_dir = tmp_path / "bundle"
    workflow.save(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    return replayer.run(workflow, bundle_dir=bundle_dir, run_dir=tmp_path / "run", **kw)


def test_resume_without_a_surface_observation_is_refused(tmp_path) -> None:
    # A resume re-binds surfaces by identity from the last checkpoint. A
    # checkpoint that carries no surface observation (written by an older
    # runtime) cannot be re-bound, so the resume is refused before any input.
    windows = _WindowsFake()
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": windows})
    refusal = replayer._begin_surfaces(_multi(), resume=True, resume_from=1)
    assert "cannot be re-bound" in (refusal or "")
    assert windows.actions == []


def test_resume_of_a_multi_surface_program_is_refused() -> None:
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": _WindowsFake()})
    refusal = replayer._begin_surfaces(_multi(), resume=True, resume_program=True)
    assert "program" in (refusal or "")


def test_resume_from_the_start_binds_the_primary() -> None:
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": _WindowsFake()})
    assert replayer._begin_surfaces(_multi(), resume=True, resume_from=0) is None
    assert replayer._surface_router is not None
    assert replayer._surface_router.active_id == "main"


def test_managed_dispatch_needs_every_backend_surface_bound() -> None:
    # Cloud dispatch needs only the deployment profile's surfaces: map. The
    # one refusal kept guards something real: an unattended run must not
    # start when a surface it will switch to has no target.
    unbound = Replayer(FakeBackend())
    unbound._managed_dispatch_snapshot = ("run", "sha", "{}")
    refusal = unbound._begin_surfaces(_multi(), resume=False)
    assert "surfaces: map" in (refusal or "") and "windows_1" in (refusal or "")
    bound = Replayer(FakeBackend(), surface_backends={"windows_1": _WindowsFake()})
    bound._managed_dispatch_snapshot = ("run", "sha", "{}")
    assert bound._begin_surfaces(_multi(), resume=False) is None


def test_surface_backends_for_a_single_surface_bundle_is_refused(tmp_path) -> None:
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": _WindowsFake()})
    report = _run(replayer, Workflow(name="wf", steps=[_wait("step_000")]), tmp_path)
    assert not report.success
    assert report.results[-1].step_id == "<surfaces>"


def test_switch_without_supplied_backend_halts_before_later_steps(tmp_path) -> None:
    primary = FakeBackend()
    replayer = Replayer(primary)
    report = _run(replayer, _multi(), tmp_path)
    assert not report.success
    assert report.results[-1].step_id == "step_001"
    assert report.results[-1].safety_halt
    assert "no backend was supplied" in (report.results[-1].error or "")
    assert replayer.backend is primary


def test_switch_to_wrong_kind_backend_halts(tmp_path) -> None:
    replayer = Replayer(FakeBackend(), surface_backends={"windows_1": FakeBackend()})
    report = _run(replayer, _multi(), tmp_path)
    assert not report.success
    assert report.results[-1].step_id == "step_001"
