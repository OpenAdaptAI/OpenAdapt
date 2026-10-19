"""openadapt_flow.jit: action mapping, the cache key, lifting, and the gates.

No browser and no model here. tests/e2e/test_jit_cua_e2e.py runs the recorded
trajectories against MockMed end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.jit import (
    AdmissionError,
    CaptureComputer,
    ParamError,
    TaskSignature,
    UnsupportedAction,
    WebApp,
    compile_agent,
    translate,
)
from openadapt_flow.jit.adapters import Op
from openadapt_flow.jit.lift import lift_params
from openadapt_flow.jit.recorded import RecordedTrajectoryAgent

FIXTURES = Path(__file__).parent / "fixtures" / "jit"


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def screenshot(self) -> bytes:
        return b"png"

    def __getattr__(self, name: str) -> Any:
        if name in {
            "click",
            "double_click",
            "right_click",
            "drag",
            "type_text",
            "press",
            "hotkey",
            "scroll",
        }:
            return lambda *args: self.calls.append((name, *args))
        raise AttributeError(name)


# -- Anthropic ---------------------------------------------------------------


def test_anthropic_actions_map_to_recorder_steps() -> None:
    a = lambda **kw: translate("anthropic", kw)  # noqa: E731
    assert a(action="left_click", coordinate=[3, 4]) == [Op("click", x=3, y=4)]
    assert a(action="double_click", coordinate=[3, 4]) == [Op("double_click", x=3, y=4)]
    assert a(action="right_click", coordinate=[3, 4]) == [Op("right_click", x=3, y=4)]
    assert a(action="type", text="hi") == [Op("type", text="hi")]
    assert a(action="key", text="Return") == [Op("press", key="Enter")]
    assert a(action="key", text="ctrl+s") == [
        Op("hotkey", key="s", modifiers=("Control",))
    ]
    assert a(action="key", text="ctrl+a BackSpace") == [
        Op("hotkey", key="a", modifiers=("Control",)),
        Op("press", key="Backspace"),
    ]
    assert a(action="left_click_drag", start_coordinate=[1, 2], coordinate=[5, 6]) == [
        Op("drag", x=1, y=2, end_x=5, end_y=6)
    ]
    assert a(
        action="scroll", coordinate=[3, 4], scroll_direction="down", scroll_amount=2
    ) == [Op("scroll", x=3, y=4, dy=240)]
    assert a(action="screenshot") == [Op("observe")]
    assert a(action="mouse_move", coordinate=[1, 1])[0].kind == "observe"


@pytest.mark.parametrize(
    "action",
    [
        {"action": "triple_click", "coordinate": [1, 1]},
        {"action": "middle_click", "coordinate": [1, 1]},
        {"action": "left_mouse_down"},
        {"action": "hold_key", "text": "shift", "duration": 1},
        {"action": "left_click", "coordinate": [1, 1], "text": "shift"},
        {"action": "key", "text": "Hyper_L"},
        {"action": "scroll", "coordinate": [1, 1], "scroll_direction": "sideways"},
        {"action": "teleport"},
    ],
)
def test_anthropic_refuses_what_the_recording_cannot_hold(action: dict) -> None:
    with pytest.raises(UnsupportedAction):
        translate("anthropic", action)


# -- OpenAI ------------------------------------------------------------------


def test_openai_batched_and_single_actions_map() -> None:
    call = {
        "type": "computer_call",
        "actions": [
            {"type": "click", "button": "left", "x": 1, "y": 2},
            {"type": "type", "text": "x"},
            {"type": "keypress", "keys": ["CTRL", "S"]},
            {"type": "keypress", "keys": ["ENTER"]},
            {"type": "click", "button": "right", "x": 1, "y": 2},
            {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
            {"type": "scroll", "x": 3, "y": 4, "scroll_x": 0, "scroll_y": 300},
        ],
    }
    assert [op.kind for op in translate("openai", call)] == [
        "click",
        "type",
        "hotkey",
        "press",
        "right_click",
        "drag",
        "scroll",
    ]
    single = {
        "type": "computer_call",
        "action": {"type": "double_click", "x": 5, "y": 6},
    }
    assert translate("openai", single) == [Op("double_click", x=5, y=6)]


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "button": "wheel", "x": 1, "y": 1},
        {"type": "click", "button": "back", "x": 1, "y": 1},
        {"type": "click", "button": "left", "x": 1, "y": 1, "keys": ["SHIFT"]},
        {
            "type": "drag",
            "path": [{"x": 1, "y": 1}, {"x": 2, "y": 2}, {"x": 3, "y": 3}],
        },
        {"type": "keypress", "keys": ["A", "B"]},
        {"type": "unknown"},
    ],
)
def test_openai_refuses_what_the_recording_cannot_hold(action: dict) -> None:
    with pytest.raises(UnsupportedAction):
        translate("openai", {"actions": [action]})


# -- Playwright MCP ----------------------------------------------------------


def test_playwright_mcp_selector_tools_map() -> None:
    t = lambda name, **args: translate(  # noqa: E731
        "playwright_mcp", {"name": name, "arguments": args}
    )
    assert t("browser_click", target="#save") == [Op("click", target="#save")]
    assert t("browser_click", target="#row", doubleClick=True) == [
        Op("double_click", target="#row")
    ]
    assert [
        op.kind for op in t("browser_type", target="#note", text="hi", submit=True)
    ] == [
        "click",
        "type",
        "press",
    ]
    assert t("browser_press_key", key="Control+a") == [
        Op("hotkey", key="a", modifiers=("Control",))
    ]
    assert t("browser_mouse_click_xy", x=3, y=4) == [Op("click", x=3, y=4)]
    assert t("browser_mouse_wheel", deltaX=0, deltaY=100) == [Op("scroll", dy=100)]
    assert t("browser_snapshot") == [Op("observe")]
    assert t("browser_wait_for", time=1) == [Op("wait", seconds=1.0)]


@pytest.mark.parametrize(
    "call",
    [
        {"name": "browser_click", "arguments": {"target": "e12"}},
        {"name": "browser_click", "arguments": {"ref": "f1e3"}},
        {
            "name": "browser_click",
            "arguments": {"target": "#a", "modifiers": ["Shift"]},
        },
        {
            "name": "browser_select_option",
            "arguments": {"target": "#s", "values": ["x"]},
        },
        {"name": "browser_evaluate", "arguments": {"function": "() => 1"}},
        {"name": "browser_fill_form", "arguments": {"fields": []}},
        {
            "name": "browser_mouse_click_xy",
            "arguments": {"x": 1, "y": 1, "clickCount": 3},
        },
        {"name": "browser_tabs", "arguments": {"action": "new"}},
    ],
)
def test_playwright_mcp_refuses_refs_and_unrecordable_tools(call: dict) -> None:
    with pytest.raises(UnsupportedAction):
        translate("playwright_mcp", call)


# -- the capture computer ----------------------------------------------------


def test_capture_computer_records_through_the_session() -> None:
    session = FakeSession()
    computer = CaptureComputer(session, dialect="anthropic", sleep=lambda s: None)
    computer.execute({"action": "left_click", "coordinate": [10, 20]})
    computer.execute({"action": "type", "text": "hello"})
    obs = computer.execute(
        {"action": "scroll", "coordinate": [10, 20], "scroll_direction": "up"}
    )
    computer.execute({"action": "mouse_move", "coordinate": [1, 1]})
    assert session.calls == [
        ("click", 10, 20),
        ("type_text", "hello"),
        ("scroll", 0, -360),
    ]
    assert obs.png == b"png"
    assert computer.log[-1]["notes"] == ["pointer move not performed"]


def test_scroll_away_from_the_pointer_is_refused_and_nothing_runs() -> None:
    session = FakeSession()
    computer = CaptureComputer(session, dialect="openai", sleep=lambda s: None)
    batch = {
        "actions": [
            {"type": "click", "button": "left", "x": 1, "y": 1},
            {"type": "scroll", "x": 50, "y": 50, "scroll_x": 0, "scroll_y": 100},
        ]
    }
    with pytest.raises(UnsupportedAction, match="pointer"):
        computer.execute(batch)
    assert session.calls == []  # the click in the same call did not run either
    ok = {
        "actions": [
            {"type": "click", "button": "left", "x": 1, "y": 1},
            {"type": "scroll", "x": 1, "y": 1, "scroll_x": 0, "scroll_y": 100},
        ]
    }
    computer.execute(ok)
    assert session.calls == [("click", 1, 1), ("scroll", 0, 100)]


def test_selector_targets_need_a_page() -> None:
    computer = CaptureComputer(FakeSession(), dialect="playwright_mcp")
    with pytest.raises(UnsupportedAction, match="no page"):
        computer.execute({"name": "browser_click", "arguments": {"target": "#a"}})


# -- the cache key -----------------------------------------------------------


def test_signature_key_is_task_id_plus_schema_not_instruction() -> None:
    a = TaskSignature("t.add", "Do {x}", {"x": str})
    assert a.key == TaskSignature("t.add", "Reworded {x}", {"x": "string"}).key
    assert a.key != TaskSignature("t.add", "Do {x}", {"x": int}).key
    assert a.key != TaskSignature("t.add", "Do {x}", {"x": str, "y": str}).key
    assert a.key != TaskSignature("t.other", "Do {x}", {"x": str}).key
    assert a.key.startswith("t.add-")


def test_signature_validates_types_and_names() -> None:
    sig = TaskSignature("t", "", {"n": int, "flag": bool, "s": str})
    assert sig.validate({"n": 3, "flag": True, "s": "a"}) == {
        "n": "3",
        "flag": "true",
        "s": "a",
    }
    with pytest.raises(ParamError):
        sig.validate({"n": True, "flag": True, "s": "a"})  # bool is not an integer
    with pytest.raises(ParamError):
        sig.validate({"n": 3, "flag": True})
    with pytest.raises(ParamError):
        sig.validate({"n": 3, "flag": True, "s": "a", "extra": 1})
    with pytest.raises(ValueError):
        TaskSignature("bad id with spaces", "")


# -- parameter lifting -------------------------------------------------------


def _bundle(tmp_path: Path, texts: list[str]) -> Path:
    steps = [
        Step(id=f"step_{i:03d}", intent=f"type {t!r}", action=ActionKind.TYPE, text=t)
        for i, t in enumerate(texts)
    ]
    bundle = tmp_path / "bundle"
    Workflow(name="lift", steps=steps).save(bundle)
    return bundle


def test_lift_proposes_inputs_typed_whole_in_one_step(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, ["Jane", "Follow up", "x Follow up x", "dup", "dup"])
    report = lift_params(
        bundle, {"name": "Jane", "note": "Follow up", "code": "dup", "missing": "zz"}
    )
    assert [(p.name, p.step_id) for p in report.lifted] == [
        ("name", "step_000"),
        ("note", "step_001"),
    ]
    assert report.unlifted == {
        "code": (
            "typed in 2 steps (step_003, step_004); no mined record check shows "
            "which field kept it"
        ),
        "missing": "not typed in any step",
    }
    proposals = json.loads((bundle / "param_proposals.json").read_text())["proposals"]
    assert {p["name"]: p["source"] for p in proposals} == {
        "name": "task_input",
        "note": "task_input",
    }
    assert all("Jane" not in json.dumps(p) for p in proposals)  # masked only


def test_lift_reports_text_typed_inside_longer_text(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, ["Dear Jane Doe"])
    report = lift_params(bundle, {"name": "Jane"})
    assert report.unlifted == {"name": "typed inside longer text"}
    assert not (bundle / "param_proposals.json").exists()


def _detour_bundle(
    tmp_path: Path, labels: list[str], record: dict[str, str] | None
) -> Path:
    """Two typed steps of one value, then a save whose mined check holds
    ``record`` (OpenAdaptAI/OpenAdapt#1328, synthetic values)."""

    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    steps = [
        Step(
            id=f"step_{i:03d}",
            intent="type",
            action=ActionKind.TYPE,
            text="Female",
            field_label=label,
        )
        for i, label in enumerate(labels)
    ]
    effects = []
    if record is not None:
        effects.append(
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={k: ValueExpr(literal=v) for k, v in record.items()},
            )
        )
    steps.append(
        Step(
            id="step_009",
            intent="save",
            action=ActionKind.KEY,
            key="Enter",
            effects=effects,
        )
    )
    bundle = tmp_path / "bundle"
    Workflow(name="lift", steps=steps).save(bundle)
    return bundle


def test_lift_binds_a_retyped_input_to_the_field_the_record_holds(
    tmp_path: Path,
) -> None:
    bundle = _detour_bundle(
        tmp_path,
        ["form_sex_identified", "form_sex"],
        {"fname": "Testa", "sex": "Female"},
    )
    report = lift_params(bundle, {"sex": "Female"})
    assert report.unlifted == {}
    [lifted] = report.lifted
    assert (lifted.name, lifted.step_id, lifted.also_typed_in) == (
        "sex",
        "step_001",
        ("step_000",),
    )
    [proposal] = json.loads((bundle / "param_proposals.json").read_text())["proposals"]
    assert proposal["step_id"] == "step_001"
    assert proposal["also_typed_in"] == ["step_000"]


@pytest.mark.parametrize(
    ("labels", "record", "why"),
    [
        (
            ["form_sex_identified", "form_sex"],
            None,
            "no mined record check shows which field kept it",
        ),
        (
            ["form_sex_identified", "form_sex"],
            {"fname": "Testa", "sex": "Male"},
            "the saved record holds it in no field",
        ),
        (
            ["Gender", "Sex at birth"],
            {"fname": "Testa", "sex": "Female"},
            "none of their fields is a record field that holds it (sex)",
        ),
        (
            ["form_sex", "sex"],
            {"fname": "Testa", "sex": "Female"},
            "steps step_000, step_001 all write a record field that holds it",
        ),
    ],
)
def test_lift_refuses_a_retyped_input_it_cannot_place(
    tmp_path: Path, labels: list[str], record: dict[str, str] | None, why: str
) -> None:
    bundle = _detour_bundle(tmp_path, labels, record)
    report = lift_params(bundle, {"sex": "Female"})
    assert report.lifted == []
    assert report.unlifted == {"sex": f"typed in 2 steps (step_000, step_001); {why}"}


def test_lift_ignores_a_placeholder_record_check(tmp_path: Path) -> None:
    bundle = _detour_bundle(
        tmp_path, ["form_sex_identified", "form_sex"], {"sex": "Female"}
    )
    workflow = Workflow.load(bundle)
    workflow.steps[-1].effects[0].needs_operator_confirmation = True
    workflow.save(bundle)
    report = lift_params(bundle, {"sex": "Female"})
    assert "no mined record check" in report.unlifted["sex"]


# -- the gates (no browser) --------------------------------------------------


class NeverCalled:
    dialect = "anthropic"

    def run(self, request: Any, computer: Any) -> None:
        raise AssertionError("the agent must not be called")


def _compiled(tmp_path: Path, agent: Any) -> Any:
    return compile_agent(
        agent,
        task=TaskSignature("mockmed.add_note", "Add {note}", {"note": str}),
        app=WebApp(url="http://127.0.0.1:9/"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
    )


def test_a_waiting_candidate_never_calls_the_agent_again(tmp_path: Path) -> None:
    jit = _compiled(tmp_path, NeverCalled())
    jit.program_dir.mkdir(parents=True)
    (jit.program_dir / "candidate.json").write_text(
        json.dumps({"status": "needs_human_admit"})
    )
    result = jit.run(note="x")
    assert result.status == "needs_human_admit"
    assert result.cache == "pending" and result.agent_called is False


def test_admit_needs_a_named_person_and_a_draft(tmp_path: Path) -> None:
    jit = _compiled(tmp_path, NeverCalled())
    with pytest.raises(AdmissionError, match="no draft"):
        jit.admit(principal_id="Dana Reviewer", confirm="ok")
    jit.program_dir.mkdir(parents=True)
    (jit.program_dir / "candidate.json").write_text(
        json.dumps({"status": "needs_human_admit", "recorded_by": "agent:anthropic"})
    )
    with pytest.raises(AdmissionError, match="human principal"):
        jit.admit(principal_id="", confirm="ok")
    with pytest.raises(AdmissionError, match="accepts"):
        jit.admit(principal_id="Dana Reviewer", confirm="maybe")


def test_recorded_agent_refuses_other_inputs() -> None:
    agent = RecordedTrajectoryAgent.from_file(FIXTURES / "anthropic_add_note.json")
    from openadapt_flow.jit import TaskRequest

    computer = CaptureComputer(FakeSession(), dialect="anthropic")
    with pytest.raises(ValueError, match="other inputs"):
        agent.run(TaskRequest("mockmed.add_note", "", {"note": "different"}), computer)


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_fixture_trajectories_are_well_formed(name: str) -> None:
    agent = RecordedTrajectoryAgent.from_file(FIXTURES / f"{name}.json")
    assert agent.actions and agent.task_id == "mockmed.add_note"
