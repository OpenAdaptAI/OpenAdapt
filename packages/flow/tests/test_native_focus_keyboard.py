"""Observed native focus is required for keyboard anchors and cannot be inferred."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.backends.win_agent import AgentConfig
from openadapt_flow.backends.win_agent.server import (
    AgentRequestError,
    _focused_target,
    _perform_uia_initialized,
)
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.native_focus import anchor_native_keyboard
from openadapt_flow.ir import ActionKind, Step
from openadapt_flow.native_focus import NativeFocusTarget, from_native_candidate
from openadapt_flow.recorder import Recorder
from tests.test_recorder import FakeBackend
from tests.test_win_agent_server import RunningAgent, _fake_uia_module, _FakeControl

LOCATOR = {
    "automation_id": "entry",
    "role": "textbox",
    "name": "Entry",
    "window_name": "Editor",
}
CANDIDATE = {
    **LOCATOR,
    "bounds": [400, 200, 800, 300],
    "point": [600, 250],
    "fingerprint": "a" * 64,
}


def observed():
    target = from_native_candidate(CANDIDATE)
    assert target is not None
    return {"native_focus": target.model_dump(mode="json")}


def png():
    image = Image.new("RGB", (1280, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 200, 800, 300), outline="black")
    draw.text((15, 15), "Synthetic record identity 314159", fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def focused_auto(*, duplicate=False):
    first = _FakeControl(
        "EditControl",
        automation_id="entry",
        name="Entry",
        runtime_id=(1, 2),
        bounds=(400, 200, 800, 300),
    )
    controls = [first]
    if duplicate:
        controls.append(
            _FakeControl(
                "EditControl",
                automation_id="entry",
                name="Entry",
                runtime_id=(1, 3),
                bounds=(400, 320, 800, 420),
            )
        )
    auto = _fake_uia_module(*controls)
    auto.GetFocusedControl = lambda: first
    return auto, first


def test_focused_target_is_unique_visible_and_reobserved():
    auto, first = focused_auto()
    candidate = _focused_target(auto)
    assert candidate is not None and candidate["automation_id"] == "entry"
    assert candidate["point"] == [600, 250]
    assert _perform_uia_initialized(auto, "focused-target", {})["target"] == candidate
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia_initialized(auto, "focused-target", {"command": "anything"})
    assert caught.value.status == 400 and caught.value.code == "invalid_schema"
    assert first.invocations == 0


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "ambiguous",
        "moved",
        "moved_geometry",
        "covered",
        "no_runtime_id",
        "truncated",
    ],
)
def test_focus_observation_declines_missing_ambiguous_moved_or_covered_focus(
    fault, monkeypatch
):
    auto, first = focused_auto(duplicate=fault == "ambiguous")
    if fault == "missing":
        auto.GetFocusedControl = lambda: None
    if fault == "moved":
        other = _FakeControl("EditControl", automation_id="other", runtime_id=(8, 8))
        values = iter([first, other])
        auto.GetFocusedControl = lambda: next(values)
    if fault == "moved_geometry":
        reads = []

        def focused():
            reads.append(True)
            if len(reads) == 2:
                first.BoundingRectangle.left += 1
            return first

        auto.GetFocusedControl = focused
    if fault == "covered":
        auto.ControlFromPoint = lambda *_: _FakeControl(
            "ButtonControl", runtime_id=(8, 8)
        )
    if fault == "no_runtime_id":
        first.runtime_id = ()
    if fault == "truncated":
        monkeypatch.setattr(
            "openadapt_flow.backends.win_agent.server._find_candidates",
            lambda *_: ([], True),
        )
    assert _focused_target(auto) is None
    assert first.invocations == 0


def test_backend_passive_focus_round_trip_contains_no_value():
    calls = []

    def uia(operation, data):
        calls.append((operation, data))
        return {"status": "ok", "target": dict(CANDIDATE)}

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="test"),
        uia_fn=uia,
    )
    try:
        with pytest.warns(UserWarning, match="plaintext"):
            backend = WindowsBackend(agent.url, auth_token="test")
        assert backend.focus_identity() == observed()
    finally:
        agent.close()
    assert calls == [("focused-target", {})]
    assert "value" not in json.dumps(observed())


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_count", True),
        ("candidate_count", 2),
        ("complete", 1),
        ("target_fingerprint", "bad"),
        ("point", [True, 250]),
        ("region", [400, 200, 0, 100]),
    ],
)
def test_recorded_focus_contract_refuses_malformed_data(field, value):
    data = observed()["native_focus"]
    data[field] = value
    with pytest.raises(ValueError):
        NativeFocusTarget.model_validate(data)


@pytest.mark.parametrize("kind", ["type", "key", "hotkey"])
def test_recorder_freezes_input_focus_even_when_after_focus_is_unavailable(
    tmp_path, kind
):
    class Backend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.target = observed()
            self.after = False

        def focus_identity(self):
            return None if self.after else self.target

        def press(self, key):
            self.target["native_focus"]["locator"]["automation_id"] = (
                "changed-after-action"
            )
            self.after = True

        def type_text(self, text):
            self.press("text")

    backend = Backend()
    rec = Recorder(backend, tmp_path / "recording")
    if kind == "type":
        rec.type_text("synthetic")
    elif kind == "hotkey":
        rec.hotkey(["Ctrl"], "Enter")
    else:
        rec.press("Enter")
    recording = rec.finish()
    event = json.loads((recording / "events.jsonl").read_text().splitlines()[0])
    assert event["focus_before"]["native_focus"]["locator"]["automation_id"] == "entry"
    assert "focus_after" not in event


@pytest.mark.parametrize("kind", ["type", "key", "hotkey"])
def test_compile_actual_focus_before_into_an_armed_keyboard_anchor(
    tmp_path, monkeypatch, kind
):
    import openadapt_flow.compiler.compile as compiler

    monkeypatch.setattr(compiler, "ocr", lambda *_args, **_kwargs: [])
    recording = tmp_path / "recording"
    (recording / "frames").mkdir(parents=True)
    event = {"i": 0, "kind": kind, "t": 1, "focus_before": observed()}
    if kind == "type":
        event["text"] = "synthetic"
    else:
        event["key"] = "Enter"
    if kind == "hotkey":
        event["modifiers"] = ["Ctrl"]
    (recording / "events.jsonl").write_text(json.dumps(event) + "\n")
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "native-focus-test",
                "created_at": "2026-11-19T00:00:00Z",
                "viewport": [1280, 800],
                "surface": "windows",
                "params": {},
                "identifier_region": [10, 10, 220, 30],
            }
        )
    )
    for suffix in ["before", "after"]:
        (recording / "frames" / f"0000_{suffix}.png").write_bytes(png())
    bundle = tmp_path / "bundle"
    workflow = compile_recording(recording, bundle, name="native-keyboard")
    step = workflow.steps[0]
    assert step.anchor is not None and step.identity_armed
    assert step.anchor.structural.automation_id == "entry"
    assert step.anchor.click_point == (600, 250)
    assert step.anchor.identifier_region == (10, 10, 220, 30)
    assert step.anchor.identifier_crop
    assert "target_fingerprint" not in step.anchor.structural.model_dump()
    assert "a" * 64 not in step.model_dump_json()


def test_compile_never_infers_native_focus_from_a_previous_click_or_after_observation(
    tmp_path,
):
    step = Step(id="key", action=ActionKind.KEY, key="Enter", intent="press Enter")
    event = {"i": 1, "kind": "key", "focus_after": observed()}
    result = anchor_native_keyboard(
        [(step, png(), png(), event)],
        bundle=tmp_path,
        meta_identifier_region=None,
        reference_date=None,
        cached_lines=lambda *_: [],
    )
    assert result[0][0].anchor is None


def test_keyboard_focus_model_preserves_each_before_target_across_action_sequences(
    tmp_path,
):
    (tmp_path / "bundle" / "templates").mkdir(parents=True)
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=20, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["type", "key", "hotkey"]),
                st.integers(min_value=0, max_value=5),
                st.booleans(),
            ),
            min_size=1,
            max_size=6,
        )
    )
    def sequence(actions):
        pending = []
        expected = []
        for index, (kind, target_id, available) in enumerate(actions):
            focus = observed()
            focus["native_focus"]["locator"]["automation_id"] = f"entry-{target_id}"
            event = {"i": index, "kind": kind, "focus_after": observed()}
            if available:
                event["focus_before"] = focus
            step = Step(
                id=f"step_{index}",
                action=ActionKind.TYPE
                if kind == "type"
                else ActionKind.KEY
                if kind == "key"
                else ActionKind.HOTKEY,
                text="synthetic" if kind == "type" else None,
                key="Enter" if kind != "type" else None,
                modifiers=["Ctrl"] if kind == "hotkey" else [],
                intent="keyboard input",
            )
            pending.append((step, png(), png(), event))
            expected.append(f"entry-{target_id}" if available else None)
        compiled = anchor_native_keyboard(
            pending,
            bundle=tmp_path / "bundle",
            meta_identifier_region=(10, 10, 220, 30),
            reference_date=None,
            cached_lines=lambda *_: [],
        )
        for (step, *_), target in zip(compiled, expected, strict=True):
            assert (
                step.anchor.structural.automation_id if step.anchor else None
            ) == target
            assert bool(step.identity_armed) == (target is not None)

    sequence()


def test_recorder_declines_a_focus_observation_that_cannot_be_frozen(tmp_path):
    class Unreadable:
        def __deepcopy__(self, _memo):
            raise RuntimeError("observation object is no longer readable")

    backend = FakeBackend()
    backend.focus_identity = lambda: {"native_focus": Unreadable()}
    rec = Recorder(backend, tmp_path / "recording")
    rec.press("Enter")
    recording = rec.finish()
    event = json.loads((recording / "events.jsonl").read_text().splitlines()[0])
    assert "focus_before" not in event
    assert backend.calls == [("key", "Enter")]
