"""The surface harness and the driven agent, on a fake native window.

Windows, macOS and Linux apps give the harness an accessibility tree (UI
Automation, Accessibility, AT-SPI) but no way to read which form or row a
control is in. These tests check, with no app and no browser, that every
action that may commit a write waits for a person, that nothing is performed
when it is refused, and that the teach server's configuration keeps an agent
from admitting without a signed policy.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_flow.compile_agent.driven import DrivenAgent, DrivenRun
from openadapt_flow.compile_agent.harness import (
    GateRefused,
    GateRequest,
    SaveGate,
    StaleRef,
    SurfaceHarness,
)
from openadapt_flow.jit.adapters import CaptureComputer


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


class FakeSession:
    """An authoring session over a native window with three controls."""

    screen_only = False

    def __init__(self) -> None:
        self.nodes = [
            {
                "node_id": "n_00000001",
                "role": "text_input",
                "name": "Note",
                "c": (100, 40),
            },
            {"node_id": "n_00000002", "role": "button", "name": "Save", "c": (100, 80)},
            {
                "node_id": "n_00000003",
                "role": "button",
                "name": "Open",
                "c": (100, 120),
            },
            {"node_id": "n_00000004", "role": "button", "name": "Yes", "c": (100, 160)},
        ]
        self.performed: list[tuple[Any, ...]] = []

    def observe(self) -> dict[str, Any]:
        return {
            "backend": "windows",
            "tree": [{k: v for k, v in n.items() if k != "c"} for n in self.nodes],
        }

    def node_center(self, node_id: str) -> Optional[tuple[int, int]]:
        return next((n["c"] for n in self.nodes if n["node_id"] == node_id), None)

    def click(self, x: int, y: int) -> None:
        self.performed.append(("click", x, y))

    def double_click(self, x: int, y: int) -> None:
        self.performed.append(("double_click", x, y))

    def right_click(self, x: int, y: int) -> None:
        self.performed.append(("right_click", x, y))

    def type_text(self, text: str) -> None:
        self.performed.append(("type", text))

    def press(self, key: str) -> None:
        self.performed.append(("press", key))

    def hotkey(self, modifiers: list[str], key: str) -> None:
        self.performed.append(("hotkey", tuple(modifiers), key))

    def screenshot(self) -> bytes:
        return _png()


def _harness(hold: Any = None) -> tuple[SurfaceHarness, FakeSession]:
    session = FakeSession()
    computer = CaptureComputer(session, dialect="generic", sleep=lambda s: None)
    harness = SurfaceHarness(computer, SaveGate(["Jane Sample", "MRN P1"], hold))
    harness.observe()
    return harness, session


def test_the_observation_lists_controls_by_ref_without_values() -> None:
    harness, _ = _harness()
    obs = harness.observe()
    assert obs.text.splitlines() == [
        '[n_00000001] text_input "Note"',
        '[n_00000002] button "Save"',
        '[n_00000003] button "Open"',
        '[n_00000004] button "Yes"',
    ]
    assert obs.find("save").ref == "n_00000002"
    assert not obs.screen_only


def test_a_save_without_a_person_is_refused_and_not_performed() -> None:
    harness, session = _harness()
    with pytest.raises(GateRefused, match="a person must approve it"):
        harness.click("n_00000002")
    assert session.performed == []
    assert harness.gate_log[-1]["decision"] == "refused_no_hold"
    assert harness.refusal is not None


def test_a_person_approves_or_refuses_each_save() -> None:
    asked: list[GateRequest] = []
    harness, session = _harness(hold=lambda req: asked.append(req) or True)
    harness.click("n_00000002")
    assert session.performed == [("click", 100, 80)]
    assert asked[0].control == "Save" and asked[0].identity == ("Jane Sample", "MRN P1")
    assert harness.gate_log[-1]["decision"] == "approved_by_hold"

    harness, session = _harness(hold=lambda req: False)
    with pytest.raises(GateRefused, match="did not approve"):
        harness.click("n_00000002")
    assert session.performed == []


NOTE, SAVE, OPEN, YES = "n_00000001", "n_00000002", "n_00000003", "n_00000004"


@pytest.mark.parametrize(
    ("setup", "last", "gated"),
    [
        ([], ("click", OPEN), None),
        ([], ("click", YES), "Yes"),
        ([], ("click", SAVE), "Save"),
        ([("click", NOTE)], ("type", "a note"), None),
        ([], ("type_into", "a note"), None),
        ([("click", NOTE)], ("press", "Space"), None),
        (
            [("click", NOTE), ("press", "Tab")],
            ("press", "Space"),
            "Space on the focused control",
        ),
        ([("click", OPEN)], ("type", "y"), "typing outside a text field"),
        ([], ("press", "y"), "the key 'y' outside a text field"),
        ([("click", NOTE)], ("type", "a note\n"), "a typed line break"),
        ([("click", NOTE)], ("press", "Enter"), "Enter on the focused control"),
        ([], ("hotkey", "s"), "the shortcut Control+s"),
        # Save's own center, but by x,y: nothing names the control.
        ([], ("at", (100, 80)), "a click at a point, not on a named control"),
    ],
)
def test_what_is_gated(setup: list, last: tuple, gated: Optional[str]) -> None:
    harness, session = _harness()
    for kind, arg in setup:
        if kind == "click":
            harness.click(arg)
        else:
            harness.press(arg)
    kind, arg = last
    ref = None
    if kind == "click":
        native, ref = harness.native_for({"ref": arg})
    elif kind == "type":
        native = {"kind": "type", "text": arg}
    elif kind == "type_into":
        native, ref = {"kind": "type", "text": arg}, NOTE
    elif kind == "press":
        native = {"kind": "press", "key": arg}
    elif kind == "hotkey":
        native = {"kind": "hotkey", "key": arg, "modifiers": ["ctrl"]}
    else:
        native = {"kind": "click", "x": arg[0], "y": arg[1]}
    assert harness.commit_target(native, ref) == gated
    before = list(session.performed)
    if gated is None:
        harness.act(native, ref)
        assert len(session.performed) == len(before) + 1
    else:
        with pytest.raises(GateRefused):
            harness.act(native, ref)
        assert session.performed == before


def test_an_approval_covers_one_action_on_one_run_as_the_app_was() -> None:
    harness, session = _harness(hold=None)
    agent = DrivenAgent()
    harness.gate.hold = agent.hold
    agent.harness = harness
    agent.request = SimpleNamespace(inputs={})

    def call(kind: str, payload: dict) -> dict:
        from concurrent.futures import Future

        from openadapt_flow.compile_agent.driven import _Command

        return agent._handle(_Command(kind, payload, Future()))

    save = {"action": "click", "ref": SAVE}
    preview = call("preview", save)
    assert preview["commit"] == "Save" and preview["action"] == "click at x=100, y=80"
    # Another action happens while the person reads the form: the approval
    # no longer covers what is on screen.
    call("act", {"action": "click", "ref": OPEN})
    with pytest.raises(ValueError, match="changed after the person approved"):
        call("act", {**save, "_held_by_person": preview["binding"]})
    assert ("click", 100, 80) not in session.performed
    # Another run never matches.
    other = DrivenAgent()
    assert (
        other.binding({"kind": "click", "x": 100, "y": 80}, SAVE)
        != call("preview", save)["binding"]
    )
    # The fresh approval goes through, once.
    fresh = call("preview", save)
    call("act", {**save, "_held_by_person": fresh["binding"]})
    assert session.performed[-1] == ("click", 100, 80)
    assert harness.gate_log[-1]["decision"] == "approved_by_hold"


def test_type_into_a_control_that_is_not_a_field_is_refused() -> None:
    harness, session = _harness()
    agent = DrivenAgent()
    agent.harness, agent.request = harness, SimpleNamespace(inputs={})
    from concurrent.futures import Future

    from openadapt_flow.compile_agent.driven import _Command

    with pytest.raises(ValueError, match="not a text field"):
        agent._handle(
            _Command("preview", {"action": "type", "text": "x", "ref": SAVE}, Future())
        )
    assert session.performed == []


def test_a_control_that_changed_is_a_stale_ref() -> None:
    harness, session = _harness()
    session.nodes[1]["name"] = "Delete"
    with pytest.raises(StaleRef):
        harness.click("n_00000002")
    assert session.performed == []


def test_the_driven_agent_approves_only_the_action_a_person_approved_once() -> None:
    agent = DrivenAgent()
    agent._approved = "Save"
    assert not agent.hold(GateRequest("Delete", (), "", None))
    assert not agent.hold(GateRequest("Save", (), "", None))  # used up
    agent._approved = "Save"
    assert agent.hold(GateRequest("Save", (), "", None))


def test_a_person_approves_every_save_off_the_web() -> None:
    for kind, expected in (("web", False), ("macos", True), ("rdp", True)):
        run = DrivenRun(SimpleNamespace(backend_kind=kind), {})
        assert run.person_approves_saves is expected
    assert DrivenRun(SimpleNamespace(backend_kind="web"), {}, hold=True).hold


def _verify(tmp_path: Path) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(
        "verifier:\n  kind: rest\n  base_url: http://127.0.0.1:9\n"
        "  records_path: /api/db\n  records_key: records\n"
    )
    return path


def test_only_an_agent_that_drives_surfaces_records_one(tmp_path: Path) -> None:
    from openadapt_flow.compile_agent import FirstRunCompiler, ScriptedAgent
    from openadapt_flow.compile_agent.pipeline import UnsupportedTarget

    compiler = FirstRunCompiler(
        "Save this note: {note}",
        params=["note"],
        surface={"kind": "macos", "macos_app": "TextEdit"},
        verify=_verify(tmp_path),
        expect={"patient_id": "p1"},
        on_screen=["Jane Sample"],
        cache_dir=tmp_path / "cache",
    )
    assert compiler.backend_kind == "macos" and compiler.url is None
    with pytest.raises(UnsupportedTarget, match="surface harness"):
        compiler.record(ScriptedAgent(steps=[]), {"note": "x"})
    # The spec round-trips with its surface, for runs after a restart.
    compiler.save_spec()
    again = FirstRunCompiler.load(compiler.dir)
    assert again.backend_kind == "macos"
    assert again.surface.macos_app == "TextEdit"


def test_the_teach_server_admits_only_as_an_agent_under_a_policy(
    tmp_path: Path,
) -> None:
    from openadapt_flow.agent.teach import TeachConfig

    common = {"verify": _verify(tmp_path), "cache_dir": tmp_path / "teach"}
    with pytest.raises(ValueError, match="needs the app"):
        TeachConfig(**common)
    with pytest.raises(ValueError, match="needs the app"):
        TeachConfig(**common, url="http://x", surface={"kind": "macos"})
    key = SimpleNamespace(kind="admitter_key", policy=None, principal_id="ci:x")
    with pytest.raises(ValueError, match="registered agent under a signed"):
        TeachConfig(**common, url="http://x", admitter=key)
    no_policy = SimpleNamespace(kind="agent", policy=None, principal_id="agent:x")
    with pytest.raises(ValueError, match="registered agent under a signed"):
        TeachConfig(**common, url="http://x", admitter=no_policy)
