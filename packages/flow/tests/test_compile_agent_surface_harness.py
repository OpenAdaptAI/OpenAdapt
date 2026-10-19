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


@pytest.mark.parametrize(
    ("steps", "gated"),
    [
        ([("click", "n_00000003")], None),
        ([("click", "n_00000001"), ("type", "a note")], None),
        ([("click", "n_00000001"), ("press", "Space")], None),
        ([("click", "n_00000001"), ("type", "a note\n")], "a typed line break"),
        ([("press", "Enter")], "Enter on the focused control"),
        ([("hotkey", "s")], "the shortcut Control+s"),
        ([("at", (5, 5))], "a click on a point the harness can't name"),
    ],
)
def test_what_is_gated(steps: list[tuple[str, Any]], gated: Optional[str]) -> None:
    harness, session = _harness()
    last = steps[-1]
    for kind, arg in steps[:-1]:
        getattr(harness, {"click": "click", "type": "type_text"}[kind])(arg)
    native = {
        "click": lambda a: harness.native_for({"ref": a})[0],
        "type": lambda a: {"kind": "type", "text": a},
        "press": lambda a: {"kind": "press", "key": a},
        "hotkey": lambda a: {"kind": "hotkey", "key": a, "modifiers": ["ctrl"]},
        "at": lambda a: {"kind": "click", "x": a[0], "y": a[1]},
    }[last[0]](last[1])
    assert harness.commit_target(native) == gated
    if gated is None:
        harness.act(native)
    else:
        before = list(session.performed)
        with pytest.raises(GateRefused):
            harness.act(native)
        assert session.performed == before


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
