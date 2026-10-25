"""An API step inside a multi-surface program (docs/API_TIER.md, MULTI_SURFACE.md).

The API step is bound to its span's surface the way it is bound to the page
in a single-surface program: every step it replaces runs on one browser
surface, none of them switches, opens or closes a surface, that surface is
the active one, and the request goes to that surface's session on its
origin. Then it runs under the same governed, durable, approval-gated rules,
and the folded steps record the surface they stand for.

The record system is a scripted verifier at INDEPENDENT_SYSTEM; the values
are made up.
"""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow import compatibility as matrix
from openadapt_flow.compiler.api_proposal import admit_api_step
from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    SurfaceObservation,
    SurfaceSpec,
    Workflow,
)
from openadapt_flow.runtime.actuators.web_form import WebFormActuator
from openadapt_flow.runtime.durable import CheckpointStore
from openadapt_flow.runtime.surfaces import SurfaceRouter
from tests.test_api_step_governed import Item, Transport, _workflow
from tests.test_execution_profiles import _KEY, _ReadyVision
from tests.test_replayer import FakeBackend


class _WindowsFake(FakeBackend):
    surface_kind = "windows"


def _switch() -> Step:
    return Step(
        id="to_windows",
        intent="switch to surface 'windows_1'",
        action=ActionKind.SWITCH_SURFACE,
        surface="windows_1",
        expect=[
            Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Desktop Home")
        ],
    )


def _multi_surface_workflow() -> Workflow:
    """Type and save on the browser (one API step), then switch to Windows."""

    single = _workflow(admitted=False)
    wf = Workflow(
        name=single.name,
        surfaces=[
            SurfaceSpec(id="main", kind="web"),
            SurfaceSpec(id="windows_1", kind="windows"),
        ],
        steps=[*single.steps, _switch()],
        params=dict(single.params),
    )
    wf, _ = admit_api_step(
        wf, admitted_by="reviewer", identity={"subject_name": "lname"}
    )
    return wf


class MultiItem(Item):
    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path, workflow=_multi_surface_workflow())
        self.windows = _WindowsFake()

    def run(self, **overrides: Any):
        vision = _ReadyVision()
        vision.text_results["Desktop Home"] = object()
        overrides.setdefault("vision", vision)
        overrides.setdefault("surface_backends", {"windows_1": self.windows})
        return super().run(**overrides)


def test_the_api_step_runs_on_its_surface_in_a_governed_multi_surface_run(tmp_path):
    item = MultiItem(tmp_path)
    report = item.run()

    assert [(r.step_id, r.actuation, r.surface) for r in report.results] == [
        ("type_lname", "api_folded", "main"),
        ("submit", "api", "main"),
        ("to_windows", None, "windows_1"),
    ], [r.api_fallback or r.error for r in report.results]
    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert [path for path, _ in item.transport.posts] == ["/save.php"]
    # The GUI never typed or clicked on the browser surface.
    assert item.backend.actions == []
    # The durable checkpoints carry the surface the folded step stands for.
    checkpoints = CheckpointStore(item.run_dir, key=_KEY).checkpoints()
    assert [(c.actuation, c.surface) for c in checkpoints][:2] == [
        ("api_folded", "main"),
        ("api", "main"),
    ]


def test_a_folded_step_on_the_wrong_surface_is_not_verified(tmp_path):
    item = MultiItem(tmp_path)
    report = item.run()
    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value

    for tampered_surface in (None, "windows_1"):
        tampered = report.model_copy(deep=True)
        tampered.results[0].surface = tampered_surface
        assert (
            classify_execution_outcome(
                tampered, item.workflow, ExecutionProfile.REGULATED
            )
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_the_matrix_says_the_api_step_works_in_a_multi_surface_run():
    combination = next(
        c
        for c in matrix.COMBINATIONS
        if (c.feature, c.profile) == ("api_step", "multi_surface")
    )
    assert combination.status == "works"


# -- when the span can't stand for one surface, the GUI steps run ------------------


def _router_replayer(workflow: Workflow, *, active: str = "main", origin=None):
    from openadapt_flow.runtime.replayer import Replayer

    replayer = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        web_form_actuator=WebFormActuator(Transport()),
    )
    router = SurfaceRouter(
        workflow,
        replayer.backend,
        {"windows_1": _WindowsFake()},
        resume_active=active,
        observations=(
            {"main": SurfaceObservation(surface="main", kind="web", origin=origin)}
            if origin
            else None
        ),
    )
    replayer._surface_router = router
    return replayer


def _span_reason(replayer, workflow: Workflow) -> Any:
    found = replayer._web_form_span_at(workflow, 0, durable=False)
    assert found is not None
    return found[2]


def test_the_span_must_be_the_active_surface():
    wf = _multi_surface_workflow()
    replayer = _router_replayer(wf, active="windows_1")
    assert _span_reason(replayer, wf) == (
        "the API step's surface is not the active one, so the GUI steps run"
    )


def test_the_session_must_be_on_the_surfaces_origin():
    wf = _multi_surface_workflow()
    assert _span_reason(_router_replayer(wf, origin="http://app"), wf) is None
    assert _span_reason(_router_replayer(wf, origin="http://other"), wf) == (
        "the API step's session is on another origin than its surface, so the "
        "GUI steps run"
    )


@pytest.mark.parametrize(
    "change, reason",
    [
        (
            {"opens": ["popup"]},
            "the steps the API step replaces switch, open or close a surface, "
            "so the GUI steps run",
        ),
        (
            {"surface": "windows_1"},
            "the steps the API step replaces run on more than one surface, so "
            "the GUI steps run",
        ),
    ],
)
def test_a_span_that_leaves_its_surface_runs_on_the_gui(change, reason):
    wf = _multi_surface_workflow()
    # Constructed directly: the sealed contract would refuse these programs.
    first = wf.steps[0].model_copy(update=change)
    wf = wf.model_construct(**{**dict(wf), "steps": [first, *wf.steps[1:]]})
    assert _span_reason(_router_replayer(wf), wf) == reason


def test_a_span_on_a_windows_surface_runs_on_the_gui():
    wf = _multi_surface_workflow()
    steps = [s.model_copy(update={"surface": "windows_1"}) for s in wf.steps[:2]]
    wf = wf.model_construct(**{**dict(wf), "steps": [*steps, wf.steps[2]]})
    assert _span_reason(_router_replayer(wf, active="windows_1"), wf) == (
        "the API step's surface is not a browser, so the GUI steps run"
    )
