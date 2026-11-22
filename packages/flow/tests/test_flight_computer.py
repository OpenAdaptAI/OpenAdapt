"""Gated drop-in executors: a commit-class action is never performed unasked."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_flow.flight.computer import (
    GatedComputer,
    GatedComputerCall,
    HeldResult,
    RemoteGate,
    classify,
    label_is_commit,
)
from openadapt_flow.flight.gate import FileGate, GateAction, RefuseGate, TerminalGate
from openadapt_flow.flight.timeline import Timeline, validate_flight


def _png(color: tuple[int, int, int] = (250, 250, 250)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (200, 100), color).save(out, format="PNG")
    return out.getvalue()


class FakeTool:
    """An Anthropic-style executor: ``tool(action=..., **input)``."""

    def __init__(self, labels: Optional[dict[tuple[int, int], str]] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.labels = labels
        self.name = "computer"

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("action") != "screenshot":
            self.calls.append(kwargs)
        return {"base64_image": base64.b64encode(_png()).decode()}

    def to_params(self) -> dict[str, str]:
        return {"name": "computer", "type": "computer_20250124"}


class LabelledTool(FakeTool):
    def label_at(self, x: int, y: int) -> Optional[str]:
        return (self.labels or {}).get((x, y))


class AllowGate:
    def __init__(self) -> None:
        self.asked: list[GateAction] = []

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> str:
        self.asked.append(action)
        return "allow"


class BrokenGate:
    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> str:
        raise RuntimeError("gate broke")


def _click(x: int = 10, y: int = 20) -> dict[str, Any]:
    return {"action": "left_click", "coordinate": [x, y]}


def test_a_save_click_is_held_and_the_executor_is_never_called():
    tool = LabelledTool({(10, 20): "button Save Encounter"})
    computer = GatedComputer(tool, RefuseGate())
    result = computer(**_click())
    assert isinstance(result, HeldResult)
    assert "held for a person: not performed" in result.error
    assert tool.calls == []
    assert computer.gate_events == [
        {"decision": "hold", "label": "button Save Encounter"}
    ]


def test_an_unknown_label_is_held():
    tool = FakeTool()
    computer = GatedComputer(tool, RefuseGate(), ocr=lambda png, x, y: None)
    result = computer(**_click())
    assert isinstance(result, HeldResult)
    assert tool.calls == []


def test_a_non_commit_label_is_performed_without_asking():
    gate = AllowGate()
    tool = LabelledTool({(10, 20): "textbox Note"})
    computer = GatedComputer(tool, gate)
    computer(**_click())
    assert tool.calls == [_click()]
    assert gate.asked == []


@pytest.mark.parametrize(
    "label,held",
    [
        ("Submit", True),
        ("Send now", True),
        ("Cancel", False),
        # Fail closed: a label that is not on the safe list is held.
        ("Saved filters", True),
    ],
)
def test_ocr_reads_the_label_when_there_is_no_accessibility_hook(label, held):
    tool = FakeTool()
    seen: list[tuple[int, int]] = []

    def ocr(png: bytes, x: int, y: int) -> str:
        seen.append((x, y))
        return label

    computer = GatedComputer(tool, RefuseGate(), ocr=ocr)
    computer(**_click(5, 6))
    assert seen == [(5, 6)]
    assert (tool.calls == []) is held


def test_an_allowed_commit_is_performed_once():
    gate = AllowGate()
    tool = LabelledTool({(10, 20): "Approve"})
    computer = GatedComputer(tool, gate)
    computer(**_click())
    assert tool.calls == [_click()]
    assert len(gate.asked) == 1 and gate.asked[0].label == "Approve"


@pytest.mark.parametrize(
    "action",
    [
        {"action": "triple_click", "coordinate": [10, 20]},
        {"action": "left_mouse_down"},
        {"action": "key", "text": "Return"},
        {"action": "key", "text": "ctrl+s"},
        {"action": "key", "text": "space"},
        {"action": "type", "text": "done\n"},
        {"action": "left_click"},  # at a pointer that was never placed
        {"action": "something_new"},
    ],
)
def test_actions_that_can_commit_or_cant_be_classified_are_held(action):
    tool = LabelledTool({})
    computer = GatedComputer(tool, RefuseGate(), ocr=lambda png, x, y: "Name")
    computer(**action)
    assert tool.calls == []


@pytest.mark.parametrize(
    "action",
    [
        {"action": "type", "text": "follow up"},
        {"action": "key", "text": "Tab"},
        {"action": "scroll", "coordinate": [1, 1], "scroll_direction": "down"},
        {"action": "mouse_move", "coordinate": [3, 4]},
        {"action": "right_click", "coordinate": [3, 4]},
    ],
)
def test_actions_that_commit_nothing_pass_through(action):
    tool = FakeTool()
    computer = GatedComputer(tool, RefuseGate(), ocr=lambda png, x, y: None)
    computer(**action)
    assert tool.calls == [action]


def test_enter_in_a_text_area_passes_but_enter_in_a_single_line_field_is_held():
    class Focused(FakeTool):
        focus = "textarea Note"

        def focused_label(self) -> str:
            return self.focus

    tool = Focused()
    computer = GatedComputer(tool, RefuseGate())
    computer(action="key", text="Return")
    assert len(tool.calls) == 1
    tool.focus = "textbox Name"
    computer(action="key", text="Return")
    assert len(tool.calls) == 1


def test_a_failing_gate_refuses():
    tool = LabelledTool({(10, 20): "Pay"})
    computer = GatedComputer(tool, BrokenGate())
    assert isinstance(computer(**_click()), HeldResult)
    assert tool.calls == []


def test_attributes_pass_through_and_raise_mode_raises():
    from openadapt_flow.flight.computer import ActionHeld

    tool = LabelledTool({(10, 20): "Confirm"})
    computer = GatedComputer(tool, RefuseGate(), on_held="raise")
    assert computer.to_params()["name"] == "computer"
    with pytest.raises(ActionHeld):
        computer(**_click())


def test_an_async_executor_stays_async_and_is_gated():
    class AsyncTool:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("action") != "screenshot":
                self.calls.append(kwargs)
            return {"base64_image": base64.b64encode(_png()).decode()}

        def label_at(self, x: int, y: int) -> str:
            return "Submit" if (x, y) == (10, 20) else "textbox Name"

    tool = AsyncTool()
    computer = GatedComputer(tool, RefuseGate())

    async def run() -> tuple[Any, Any]:
        held = await computer(**_click())
        done = await computer(**_click(1, 1))
        return held, done

    held, done = asyncio.run(run())
    assert isinstance(held, HeldResult)
    assert tool.calls == [_click(1, 1)]


class FakeCuaComputer:
    """An OpenAI CUA sample-style computer object."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def screenshot(self) -> str:
        return base64.b64encode(_png()).decode()

    def click(self, x: int, y: int, button: str = "left") -> None:
        self.calls.append(("click", (x, y, button)))

    def double_click(self, x: int, y: int) -> None:
        self.calls.append(("double_click", (x, y)))

    def keypress(self, keys: list[str]) -> None:
        self.calls.append(("keypress", tuple(keys)))

    def type(self, text: str) -> None:
        self.calls.append(("type", (text,)))

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        self.calls.append(("scroll", (x, y, scroll_x, scroll_y)))

    def get_current_url(self) -> str:
        return "http://127.0.0.1/app"

    def label_at(self, x: int, y: int) -> str:
        return {(5, 5): "button Save", (9, 9): "link Home"}.get((x, y), "")


def test_a_cua_save_click_is_held_inner_click_never_called_and_the_next_frame_says_so():
    inner = FakeCuaComputer()
    computer = GatedComputerCall(inner, RefuseGate())
    computer.click(5, 5, "left")
    assert inner.calls == []
    assert "held for a person" in (computer.last_held or "")
    marked = Image.open(io.BytesIO(base64.b64decode(computer.screenshot())))
    assert marked.getpixel((1, 1)) != (250, 250, 250)  # the red banner
    plain = Image.open(io.BytesIO(base64.b64decode(computer.screenshot())))
    assert plain.getpixel((1, 1)) == (250, 250, 250)
    computer.click(9, 9)
    computer.click(5, 5, "right")
    computer.scroll(1, 1, 0, 100)
    computer.keypress(["ENTER"])
    computer.type("abc")
    assert inner.calls == [
        ("click", (9, 9, "left")),
        ("click", (5, 5, "right")),
        ("scroll", (1, 1, 0, 100)),
        ("type", ("abc",)),
    ]
    assert computer.get_current_url() == "http://127.0.0.1/app"


def test_label_matching_is_whole_words_from_the_closed_list():
    assert label_is_commit("Save Encounter") == "save"
    assert label_is_commit("FINALIZE") == "finalize"
    assert label_is_commit("Saved") is None
    assert label_is_commit("Profile") is None
    assert label_is_commit(None) is None


def test_classify_reads_accessibility_before_ocr():
    called: list[str] = []
    result = classify(
        "anthropic",
        _click(),
        screenshot=lambda: called.append("shot") or _png(),
        label_at=lambda x, y: "button Post",
        ocr=lambda png, x, y: called.append("ocr") or "Cancel",
    )
    assert result.commit and result.label_source == "accessibility"
    assert called == []


def test_the_recorder_gets_actions_gate_events_and_scrubbed_frames(tmp_path):
    timeline = Timeline(
        tmp_path / "run",
        task_id="t",
        executor_label="cua",
        scrub_image=lambda png: _png((0, 0, 0)),
        scrub_text=lambda text: text.replace("Zyxwv", "<PERSON>"),
    )
    tool = LabelledTool({(10, 20): "Save", (1, 1): "textbox Zyxwv"})
    computer = GatedComputer(tool, RefuseGate(), timeline)
    computer(**_click(1, 1))
    computer(**_click())
    computer(action="type", text="Zyxwv Qtest")
    summary = timeline.finish()
    assert validate_flight(summary) == []
    assert [e["kind"] for e in summary["gate_events"]] == ["held"]
    assert summary["actions_count"] == 3
    raw = (tmp_path / "run/timeline.jsonl").read_text()
    assert "Zyxwv" not in raw
    frames = list((tmp_path / "run/frames").glob("*.png"))
    assert frames
    for frame in frames:
        assert Image.open(frame).convert("RGB").getpixel((5, 5)) == (0, 0, 0)


def test_an_unreachable_remote_gate_refuses():
    gate = RemoteGate("http://127.0.0.1:9/token/flight", timeout_s=1)
    action = GateAction("anthropic", {}, "left_click", 1, 1, "Save", "ocr", "test")
    assert gate.decide(action, _png()) == "refuse"
    with pytest.raises(ValueError):
        RemoteGate("http://example.test/flight")


def test_the_terminal_gate_refuses_without_a_terminal(tmp_path):
    gate = TerminalGate(tty_path=str(tmp_path / "no-tty"))
    action = GateAction("anthropic", {}, "left_click", 1, 1, "Save", "ocr", "test")
    assert gate.decide(action, _png()) == "refuse"


def _action(index: int = 1) -> GateAction:
    return GateAction(
        "anthropic",
        _click(),
        "left_click",
        10,
        20,
        "Save",
        "accessibility",
        "the target reads 'Save' (save)",
        index,
    )


def test_the_file_gate_writes_an_item_approval_request_and_holds_without_an_answer(
    tmp_path,
):
    from openadapt_flow.approval_mode.item_approval import ItemApprovalRequest

    gate = FileGate(tmp_path / "run", run_id="task-7", timeout_s=0.3, poll_s=0.05)
    assert gate.decide(_action(), _png()) == "hold"
    requests = list((tmp_path / "run/item-approval").glob("request-*.json"))
    assert len(requests) == 1
    request = ItemApprovalRequest.model_validate_json(requests[0].read_bytes())
    assert request.run_id == "task-7" and request.step_id == "action-1"
    assert request.provenance is not None and request.provenance.actor == "agent:flight"
    assert gate.decide(_action(2), None) == "refuse"  # no frame to show a person


def _answer_when_asked(run_dir: Path, decision: str) -> threading.Thread:
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
    from tests._approver_keys import answer_as_person

    def answer() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = list((run_dir / "item-approval").glob("request-*.json"))
            if found:
                store = ItemApprovalStore(run_dir)
                request = store.read_request(found[0].stem.split("-", 1)[1])
                fields: dict[str, Any] = dict(
                    decision=decision,
                    decided_by="human",
                    route="desktop_run_it",
                    approver_id=None,
                    auth_method="none",
                )
                if decision == "decline":
                    fields["decline_reason"] = "wrong_record"
                store.write_answer(answer_as_person(store, request, **fields))
                return
            time.sleep(0.05)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    "decision,expected", [("approve", "allow"), ("decline", "refuse")]
)
def test_the_file_gate_follows_a_persons_signed_answer(
    tmp_path, approver, decision, expected
):
    run_dir = tmp_path / "run"
    thread = _answer_when_asked(run_dir, decision)
    gate = FileGate(run_dir, run_id="task-8", timeout_s=10, poll_s=0.05)
    assert gate.decide(_action(), _png()) == expected
    thread.join(timeout=5)
    if expected == "allow":
        consumed = list((run_dir / "item-approval").glob("consumed-*"))
        assert len(consumed) == 1


# -- fail-closed allowlist (not a blocklist of verbs) -----------------------


@pytest.mark.parametrize(
    "label",
    [
        "OK",
        "Done",
        "Apply",
        "Update",
        "Create",
        "Add",
        "Delete",
        "Remove",
        "Next",
        "Finish",
        "Book",
        "Place order",
        "Accept",
        "Publish",
        "Discharge",
        "Cancel order",
        "Close account",
        "button Delete",
        "menuitem Delete",
        "link Remove",
        "Speichern",
        "Enregistrer",
        "Guardar",
        "保存",
        "Cancelar",
        "?",
    ],
)
def test_a_click_on_a_label_not_known_to_be_safe_is_held(label):
    tool = LabelledTool({(10, 20): label})
    computer = GatedComputer(tool, RefuseGate())
    assert isinstance(computer(**_click()), HeldResult)
    assert tool.calls == []


@pytest.mark.parametrize(
    "label",
    [
        "Cancel",
        "Close",
        "‹ Back",
        "Back »",
        "Search",
        "View details",
        "link Back",
        "button Open",
        "tab Demographics",
        "textbox Note",
        "combobox Reason",
    ],
)
def test_a_click_on_a_known_safe_control_is_performed(label):
    tool = LabelledTool({(10, 20): label})
    gate = AllowGate()
    computer = GatedComputer(tool, gate)
    computer(**_click())
    assert tool.calls == [_click()]
    assert gate.asked == []


@pytest.mark.parametrize(
    "focused,key,held",
    [
        ("button Next", "space", True),
        ("button Delete", "Return", True),
        ("textbox Name", "space", False),
        ("textbox Name", "Return", True),
        ("searchbox Search", "Return", False),
        ("button Cancel", "Return", False),
        ("textarea Note", "Return", False),
    ],
)
def test_keys_on_the_focused_control_use_the_same_list(focused, key, held):
    class Focused(FakeTool):
        def focused_label(self) -> str:
            return focused

    tool = Focused()
    computer = GatedComputer(tool, RefuseGate())
    computer(action="key", text=key)
    assert (tool.calls == []) is held


def _fake_ocr(monkeypatch, lines):
    import importlib

    ocr_mod = importlib.import_module("openadapt_flow.vision.ocr")

    class Line:
        def __init__(self, text, region):
            self.text, self.region = text, region

    monkeypatch.setattr(
        ocr_mod, "ocr", lambda png: [Line(t, r) for t, r in lines], raising=True
    )


def test_an_icon_only_save_next_to_a_field_label_is_held(monkeypatch):
    # "Notes" is 20 px left of the click: the old nearest-line rule named the
    # icon "Notes" and let the click through.
    _fake_ocr(monkeypatch, [("Notes", (40, 14, 40, 12))])
    tool = FakeTool()
    computer = GatedComputer(tool, RefuseGate())
    assert isinstance(computer(**_click(100, 20)), HeldResult)
    assert tool.calls == []
    assert computer.gate_events == [{"decision": "hold", "label": None}]


def test_ocr_names_the_target_only_with_a_line_that_holds_the_click(monkeypatch):
    _fake_ocr(monkeypatch, [("Cancel", (5, 14, 40, 12)), ("Save", (60, 14, 30, 12))])
    tool = FakeTool()
    computer = GatedComputer(tool, RefuseGate())
    computer(**_click(20, 20))  # inside "Cancel"
    assert tool.calls == [_click(20, 20)]
    assert isinstance(computer(**_click(70, 20)), HeldResult)  # inside "Save"
    assert tool.calls == [_click(20, 20)]


def test_target_bounds_join_every_line_inside_the_target(monkeypatch):
    _fake_ocr(
        monkeypatch,
        [
            ("Cancel", (12, 12, 40, 10)),
            ("order", (12, 24, 30, 10)),
            ("x", (200, 0, 5, 5)),
        ],
    )

    class Bounded(FakeTool):
        def target_bounds(self, x: int, y: int) -> tuple[int, int, int, int]:
            return (10, 10, 60, 30)

    tool = Bounded()
    computer = GatedComputer(tool, RefuseGate())
    assert isinstance(computer(**_click(30, 18)), HeldResult)
    assert computer.gate_events[0]["label"] == "Cancel order"


def test_a_deployment_can_add_safe_labels_but_not_a_commit(monkeypatch):
    from openadapt_flow.flight.computer import SAFE_LABELS_ENV, parse_safe_labels

    tool = LabelledTool({(10, 20): "button New Encounter"})
    computer = GatedComputer(tool, RefuseGate(), safe_labels=["New encounter"])
    computer(**_click())
    assert tool.calls == [_click()]
    with pytest.raises(ValueError):
        parse_safe_labels(["Save draft"])
    monkeypatch.setenv(SAFE_LABELS_ENV, '["Triage"]')
    tool = LabelledTool({(10, 20): "button Triage"})
    GatedComputer(tool, RefuseGate())(**_click())
    assert tool.calls == [_click()]
    monkeypatch.setenv(SAFE_LABELS_ENV, '["Triage", "Submit"]')  # refused whole
    tool = LabelledTool({(10, 20): "button Triage"})
    assert isinstance(GatedComputer(tool, RefuseGate())(**_click()), HeldResult)
