"""Durable contracts for the optional canonical runtime overlay event rail."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

openadapt_types = pytest.importorskip("openadapt_types")
if not hasattr(openadapt_types, "ControlOverlayFrameV2"):
    pytest.skip("requires openadapt-types >= 0.5.0", allow_module_level=True)

from openadapt_types import (  # noqa: E402
    ControlOverlayDataClassification,
    ControlOverlayMediaFrameBindingV2,
    ControlOverlayMode,
    ControlOverlayObservationBindingV2,
    ControlOverlayPhase,
)

from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    ParamSpec,
    ProgramGraph,
    Resolution,
    State,
    StateKind,
    Step,
    StructuralHandle,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.control_overlay import (  # noqa: E402
    RuntimeControlOverlayEmitter,
    build_runtime_control_overlay_timeline_v2,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402


def _clock(values: list[int | float]):
    iterator: Iterator[int | float] = iter(values)
    return lambda: next(iterator)


def _emitter(frames, monotonic: list[float]) -> RuntimeControlOverlayEmitter:
    return RuntimeControlOverlayEmitter(
        frames.append,
        mode=ControlOverlayMode.REPLAY,
        unix_ms_clock=_clock([1_000 + i for i in range(len(monotonic))]),
        monotonic_ms_clock=_clock(monotonic),
    )


def test_emitter_uses_canonical_exact_clocks_profile_and_outcome() -> None:
    frames = []
    emitter = _emitter(frames, [50.0, 51.5, 53.0, 55.0])
    emitter.begin(profile="demo")

    emitter.emit_phase("observing", current_step=1, total_steps=2)
    emitter.emit_phase("executing", current_step=1, total_steps=2)
    emitter.emit_phase("verifying", current_step=1, total_steps=2)
    emitter.emit_terminal("COMPLETED_UNVERIFIED")

    assert [frame.event_sequence for frame in frames] == [0, 1, 2, 3]
    assert [frame.observed_at_unix_ms for frame in frames] == [1000, 1001, 1002, 1003]
    assert [frame.observed_at_monotonic_ms for frame in frames] == [
        50.0,
        51.5,
        53.0,
        55.0,
    ]
    assert [frame.phase for frame in frames] == [
        ControlOverlayPhase.OBSERVING,
        ControlOverlayPhase.EXECUTING,
        ControlOverlayPhase.VERIFYING,
        ControlOverlayPhase.COMPLETED_UNVERIFIED,
    ]
    assert all(frame.profile.value == "demo" for frame in frames)
    assert frames[-1].step.current is None
    assert frames[-1].step.total is None
    assert all(not frame.controls.pause for frame in frames)
    assert all(not frame.controls.resume for frame in frames)
    assert all(not frame.controls.stop for frame in frames)


def test_emitter_refuses_generic_success_and_terminal_reuse() -> None:
    frames = []
    emitter = _emitter(frames, [10.0])
    emitter.begin(profile="standard")

    with pytest.raises(ValueError, match="exact execution_outcome"):
        emitter.emit_terminal("SUCCESS")
    emitter.emit_terminal("VERIFIED")
    with pytest.raises(RuntimeError, match="already emitted"):
        emitter.emit_terminal("VERIFIED")


def test_timeline_requires_exact_media_start_and_strict_real_offsets() -> None:
    frames = []
    emitter = _emitter(frames, [100.0, 110.6, 119.2])
    emitter.begin(profile="demo")
    emitter.emit_phase("observing")
    emitter.emit_phase("executing")
    emitter.emit_terminal("HALTED")

    timeline = build_runtime_control_overlay_timeline_v2(
        frames,
        data_classification=ControlOverlayDataClassification.SYNTHETIC,
        evidence_pack_id="fixture-v1",
        media_sha256="a" * 64,
        media_frame_count=3,
        media_frame_indexes=[0, 1, 2],
        duration_ms=20,
        media_started_monotonic_ms=100.0,
    )
    assert [event.at_ms for event in timeline.events] == [0, 11, 19]

    with pytest.raises(ValueError, match="exactly match media start"):
        build_runtime_control_overlay_timeline_v2(
            frames,
            data_classification=ControlOverlayDataClassification.SYNTHETIC,
            evidence_pack_id="fixture-v1",
            media_sha256="a" * 64,
            media_frame_count=3,
            media_frame_indexes=[0, 1, 2],
            duration_ms=20,
            media_started_monotonic_ms=99.0,
        )

    duplicate_frames = []
    duplicate = _emitter(duplicate_frames, [5.0, 5.4])
    duplicate.begin(profile="demo")
    duplicate.emit_phase("observing")
    duplicate.emit_terminal("HALTED")
    with pytest.raises(ValueError, match="strictly increasing"):
        build_runtime_control_overlay_timeline_v2(
            duplicate_frames,
            data_classification=ControlOverlayDataClassification.SYNTHETIC,
            evidence_pack_id="fixture-v1",
            media_sha256="a" * 64,
            media_frame_count=2,
            media_frame_indexes=[0, 1],
            duration_ms=10,
            media_started_monotonic_ms=5.0,
        )


def test_browser_target_is_run_scoped_and_media_rebinds_exact_frame() -> None:
    observation = b"private-browser-frame"
    raw_sha256 = hashlib.sha256(observation).hexdigest()
    frames = []
    emitter = RuntimeControlOverlayEmitter(
        frames.append,
        unix_ms_clock=_clock([1000]),
        monotonic_ms_clock=_clock([50.0]),
        observation_key_factory=lambda: b"k" * 32,
    )
    emitter.begin(profile="standard")
    target = emitter.browser_target_tracking(
        observation_png=observation,
        region_css_px=(100, 80, 200, 40),
        viewport=(1000, 500, 2.0),
        action_kind="click",
    )
    frame = emitter.emit_phase(
        "executing",
        current_step=1,
        total_steps=1,
        target_tracking=target,
    )

    assert isinstance(target.binding, ControlOverlayObservationBindingV2)
    assert target.binding.observation_hmac_sha256 != raw_sha256
    assert frame.tracking_for_observation(
        emitter.observation_hmac_sha256(
            observation,
            event_sequence=frame.event_sequence,
        )
    )
    assert (
        frame.tracking_for_observation(
            emitter.observation_hmac_sha256(observation, event_sequence=99)
        )
        is None
    )
    assert frame.tracking_for_observation("0" * 64) is None
    assert target.rect.model_dump() == {
        "x": 0.1,
        "y": 0.16,
        "width": 0.2,
        "height": 0.08,
    }

    timeline = build_runtime_control_overlay_timeline_v2(
        frames,
        data_classification=ControlOverlayDataClassification.SYNTHETIC,
        evidence_pack_id="fixture-v2",
        media_sha256="b" * 64,
        media_frame_count=10,
        media_frame_indexes=[0],
        duration_ms=1,
        media_started_monotonic_ms=50.0,
    )
    rebound = timeline.events[0].frame.target_tracking
    assert rebound is not None
    assert isinstance(rebound.binding, ControlOverlayMediaFrameBindingV2)
    assert rebound.binding.media_sha256 == "b" * 64
    assert rebound.binding.frame_index == 0
    assert timeline.tracking_for_media_frame(0) == rebound
    assert timeline.tracking_for_media_frame(1) is None


def test_each_begin_rotates_private_observation_binding() -> None:
    keys = iter([b"a" * 32, b"b" * 32])
    emitter = RuntimeControlOverlayEmitter(
        lambda _frame: None,
        observation_key_factory=lambda: next(keys),
    )
    emitter.begin(profile="demo")
    first = emitter.observation_hmac_sha256(
        b"same-private-frame",
        event_sequence=0,
    )
    emitter.begin(profile="demo")
    second = emitter.observation_hmac_sha256(
        b"same-private-frame",
        event_sequence=0,
    )
    assert first != second


class _Backend:
    viewport = (20, 20)

    def screenshot(self) -> bytes:
        return b"presentation-independent-frame"


class _Vision:
    def wait_settled(self, backend, **_kwargs) -> bytes:
        return backend.screenshot()


class _BrowserBackend(_Backend):
    def browser_presentation_viewport(self):
        return (20, 20, 1.0)


def test_replayer_target_geometry_requires_explicit_browser_capability() -> None:
    frame = b"exact-private-browser-frame"
    resolution = Resolution(
        rung="structural",
        point=(10, 10),
        confidence=1.0,
        elapsed_ms=1.0,
        structural_handle=StructuralHandle(
            point=(10, 10),
            region=(5, 6, 8, 4),
        ),
    )
    step = Step(
        id="click",
        intent="click target",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="target.png",
            region=(5, 6, 8, 4),
            click_point=(10, 10),
        ),
    )

    browser_emitter = RuntimeControlOverlayEmitter(lambda _frame: None)
    browser_emitter.begin(profile="demo")
    browser_replayer = Replayer(
        _BrowserBackend(),
        vision=_Vision(),
        control_overlay=browser_emitter,
    )
    browser_target = browser_replayer._control_overlay_browser_target(
        step,
        resolution,
        (0, 0, 1, 1),
        frame,
    )
    assert browser_target is not None
    assert browser_target.rect.model_dump() == {
        "x": 0.25,
        "y": 0.3,
        "width": 0.4,
        "height": 0.2,
    }

    native_emitter = RuntimeControlOverlayEmitter(lambda _frame: None)
    native_emitter.begin(profile="demo")
    native_replayer = Replayer(
        _Backend(),
        vision=_Vision(),
        control_overlay=native_emitter,
    )
    assert (
        native_replayer._control_overlay_browser_target(
            step,
            resolution,
            (5, 6, 8, 4),
            frame,
        )
        is None
    )


def _run(workflow: Workflow, tmp_path, frames, *, sink=None):
    emitter = RuntimeControlOverlayEmitter(
        sink or frames.append,
        unix_ms_clock=_clock(list(range(10_000, 10_050))),
        monotonic_ms_clock=_clock([float(value) for value in range(50, 100)]),
    )
    replayer = Replayer(_Backend(), vision=_Vision(), control_overlay=emitter)
    report = replayer.run(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )
    return report, replayer


def test_replayer_emits_linear_phases_and_exact_persisted_terminal(tmp_path) -> None:
    frames = []
    workflow = Workflow(
        name="linear",
        steps=[
            Step(id="one", intent="wait one", action=ActionKind.WAIT),
            Step(id="two", intent="wait two", action=ActionKind.WAIT),
        ],
    )

    report, replayer = _run(workflow, tmp_path, frames)

    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    assert [frame.phase.value for frame in frames] == [
        "observing",
        "executing",
        "verifying",
        "observing",
        "executing",
        "verifying",
        "completed_unverified",
    ]
    assert [(frame.step.current, frame.step.total) for frame in frames[:-1]] == [
        (1, 2),
        (1, 2),
        (1, 2),
        (2, 2),
        (2, 2),
        (2, 2),
    ]
    assert (tmp_path / "run" / "report.json").is_file()
    assert replayer.control_overlay_error is None


def test_early_refusal_emits_halted_without_invented_action_phase(tmp_path) -> None:
    frames = []
    workflow = Workflow(
        name="missing-param",
        steps=[Step(id="wait", intent="wait", action=ActionKind.WAIT)],
        param_specs={"required": ParamSpec(name="required", example=None)},
    )

    report, _replayer = _run(workflow, tmp_path, frames)

    assert report.execution_outcome == "HALTED"
    assert [frame.phase.value for frame in frames] == ["halted"]


def test_sink_failure_cannot_change_run_outcome(tmp_path) -> None:
    frames = []

    def failing_sink(frame) -> None:
        frames.append(frame)
        raise RuntimeError("viewer disconnected")

    workflow = Workflow(
        name="sink-failure",
        steps=[Step(id="wait", intent="wait", action=ActionKind.WAIT)],
    )
    report, replayer = _run(workflow, tmp_path, frames, sink=failing_sink)

    assert report.success is True
    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    assert replayer.control_overlay_error == "RuntimeError"
    assert [frame.phase.value for frame in frames] == ["observing"]


def test_program_graph_does_not_invent_linear_progress(tmp_path) -> None:
    frames = []
    action = State(
        id="wait-state",
        kind=StateKind.ACTION,
        step=Step(id="wait", intent="wait", action=ActionKind.WAIT),
        transitions=[Transition(target="done")],
    )
    workflow = Workflow(
        name="program",
        program=ProgramGraph(
            entry=action.id,
            states={
                action.id: action,
                "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
            },
        ),
    )

    report, _replayer = _run(workflow, tmp_path, frames)

    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    assert all(frame.step.current is None for frame in frames)
    assert all(frame.step.total is None for frame in frames)
