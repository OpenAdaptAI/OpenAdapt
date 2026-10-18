"""openadapt_flow.compile_agent without a browser or a model.

The record check, the commit-label rule, scrubbing that keeps the task's own
values, the Claude loop's accounting and stops, and the emitted MCP server.
tests/e2e/test_compile_agent_e2e.py runs the whole pipeline on MockMed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import privacy
from openadapt_flow.compile_agent import (
    BudgetExceeded,
    ClaudeAgent,
    GateRefused,
    check_run_record,
)
from openadapt_flow.compile_agent.agents import AgentLedger, ModelCall
from openadapt_flow.compile_agent.observe import _render, commit_label, scrub_lines
from openadapt_flow.jit.compile_agent import TaskRequest

BEFORE = [{"id": 1, "patient_id": "p2", "note": "old"}]


def _created(**fields: Any) -> list[dict[str, Any]]:
    return BEFORE + [{"id": 2, **fields}]


def test_record_check_passes_only_for_the_named_record() -> None:
    ok = check_run_record(
        BEFORE,
        _created(patient_id="p1", note="Follow up"),
        expect={"patient_id": "p1"},
        values={"note": "Follow up"},
    )
    assert ok.passed and ok.fields == ["id", "note", "patient_id"]

    wrong = check_run_record(
        BEFORE,
        _created(patient_id="p2", note="Follow up"),
        expect={"patient_id": "p1"},
        values={"note": "Follow up"},
    )
    assert not wrong.passed and "wrong record" in wrong.reasons[0]

    lost = check_run_record(
        BEFORE,
        _created(patient_id="p1", note=""),
        expect={"patient_id": "p1"},
        values={"note": "Follow up"},
    )
    assert lost.reasons == ["input 'note' is not in the new record"]

    nothing = check_run_record(BEFORE, BEFORE, expect={"patient_id": "p1"}, values={})
    assert not nothing.passed and nothing.created == 0

    two = check_run_record(
        [],
        [{"id": 1, "patient_id": "p1"}, {"id": 2, "patient_id": "p1"}],
        expect={"patient_id": "p1"},
        values={},
    )
    assert not two.passed

    edited = check_run_record(
        BEFORE,
        [
            {"id": 1, "patient_id": "p2", "note": "changed"},
            {"id": 2, "patient_id": "p1"},
        ],
        expect={"patient_id": "p1"},
        values={},
    )
    assert edited.reasons == ["1 existing records changed"]

    unread = check_run_record(None, BEFORE, expect={"patient_id": "p1"}, values={})
    assert not unread.passed


@pytest.mark.parametrize(
    ("label", "commits"),
    [
        ("Save Encounter", True),
        ("Submit", True),
        ("Sign note", True),
        ("Approve claim", True),
        ("Sign In", False),
        ("Open", False),
        ("New Encounter", False),
        ("Continue to tasks", False),
        ("Saved searches", True),
    ],
)
def test_commit_labels(label: str, commits: bool) -> None:
    assert commit_label(label) is commits


def test_listing_groups_rows_and_names_refs() -> None:
    raw = {
        "items": [
            {"kind": "heading", "text": "Referral Tasks", "row": -1},
            {"kind": "text", "text": "Jane Sample", "row": 1},
            {"kind": "text", "text": "High", "row": 1},
            {
                "kind": "control",
                "tag": "button",
                "name": "Open",
                "x": 10,
                "y": 20,
                "row": 1,
                "selected": False,
            },
            {
                "kind": "control",
                "tag": "textarea",
                "name": "Note",
                "value": "",
                "x": 5,
                "y": 6,
                "row": -1,
                "focused": True,
            },
        ]
    }
    text, controls = _render(raw)
    assert text.splitlines() == [
        "# Referral Tasks",
        'row: Jane Sample | High | [e1] button "Open"',
        '[e2] textarea "Note" value="" (focused)',
    ]
    assert (controls["e1"].x, controls["e1"].y) == (10, 20)


class _Scrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return re.sub(r"[A-Z][a-z]+ [A-Z][a-z]+", "<PERSON>", text)


def test_scrubbing_keeps_the_values_the_operator_named(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    privacy.set_text_scrubber(_Scrubber())
    try:
        text = "row: Jane Sample | Knee pain\nrow: Alex Testcase | Cardiology"
        assert scrub_lines(text, keep=["Jane Sample"]) == (
            "row: Jane Sample | Knee pain\nrow: <PERSON> | Cardiology"
        )
    finally:
        privacy.reset_scrubbers()


# -- the Claude loop -------------------------------------------------------------


class _Harness:
    """Stands in for the browser harness: a fixed listing and counters."""

    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.clicks: list[str] = []
        self.stats = SimpleNamespace(gate_checks=0, screenshots_requested=0)

    def observe(self) -> Any:
        return SimpleNamespace(text='page: x\n[e1] button "Save"')

    def screenshot(self, why: str = "requested") -> bytes:
        self.stats.screenshots_requested += 1
        return b"\x89PNG fake"

    def click(self, ref: str) -> None:
        if ref == "e9":
            from openadapt_flow.compile_agent.harness import StaleRef

            raise StaleRef("e9 is not in the latest observation")
        if self.refuse:
            raise GateRefused("wrong record")
        self.clicks.append(ref)
        self.stats.gate_checks += 1


class _Client:
    def __init__(self, script: list[tuple[str, dict[str, Any]]], tokens: int = 100):
        self.script = list(script)
        self.tokens = tokens
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw: Any) -> Any:
        self.requests.append(kw)
        name, args = self.script.pop(0)
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": f"t{len(self.requests)}",
                    "name": name,
                    "input": args,
                }
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=self.tokens,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=50,
            ),
        )


REQUEST = TaskRequest(task_id="t", instruction="Save it", inputs={})


def test_claude_loop_counts_tokens_and_the_gate_screenshot() -> None:
    client = _Client(
        [
            ("click", {"ref": "e9"}),
            ("click", {"ref": "e1"}),
            ("finish", {"summary": "done"}),
        ]
    )
    harness = _Harness()
    ledger = ClaudeAgent(client=client).drive(REQUEST, harness)  # type: ignore[arg-type]
    assert ledger.finished and ledger.summary == "done"
    assert ledger.model_calls == 3 and ledger.tool_errors == 1  # the stale ref
    assert harness.clicks == ["e1"]
    # The save's result carried a screenshot: the verification gate.
    messages = client.requests[2]["messages"]  # the loop appends to one list
    third = messages[4]["content"][0]["content"]
    assert [b["type"] for b in third] == ["text", "image"]
    assert ledger.to_dict()["cache_read_input_tokens"] == 150
    assert client.requests[0]["extra_body"]["output_config"] == {"effort": "medium"}
    assert client.requests[0]["tools"][0]["name"] == "observe"


def test_every_turn_mode_uses_the_computer_toolset() -> None:
    agent = ClaudeAgent(
        vision="every_turn", client=_Client([("finish", {"summary": "x"})])
    )
    assert agent.dialect == "anthropic"
    harness = _Harness()
    agent.drive(REQUEST, harness)  # type: ignore[arg-type]
    tools = agent.client.requests[0]["tools"]
    assert tools[0]["type"] == "computer_toolset_20260801" and "name" not in tools[0]


def test_a_refused_save_ends_the_loop() -> None:
    client = _Client([("click", {"ref": "e1"})])
    with pytest.raises(GateRefused):
        ClaudeAgent(client=client).drive(REQUEST, _Harness(refuse=True))  # type: ignore[arg-type]


def test_the_cost_cap_ends_the_loop() -> None:
    client = _Client([("observe", {})] * 5, tokens=400_000)
    with pytest.raises(BudgetExceeded):
        ClaudeAgent(client=client, max_cost_usd=1.0).drive(REQUEST, _Harness())  # type: ignore[arg-type]
    assert len(client.requests) == 1


def test_ledger_prices_opus() -> None:
    ledger = AgentLedger(agent="claude", model="claude-opus-5-5")
    ledger.calls.append(
        ModelCall(1, 1_000_000, 100_000, 0, 1_000_000, 1.0, "tool_use", 0)
    )
    assert ledger.cost_usd() == pytest.approx(4.0 + 2.0 + 0.2)


def test_the_emitted_mcp_server_parses(tmp_path: Path) -> None:
    from openadapt_flow.compile_agent.emit import emit_mcp
    from openadapt_flow.jit.signature import TaskSignature

    fake = SimpleNamespace(
        task=TaskSignature("demo.add-note", 'Add """{note}"""', {"note": str}),
        dir=tmp_path / "task",
    )
    path = emit_mcp(fake, tmp_path / "server.py")  # type: ignore[arg-type]
    tree = ast.parse(path.read_text())
    funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert funcs == ["demo_add_note"]
    assert "note: str" in path.read_text()


# -- the save gate ----------------------------------------------------------------


class _Page:
    url = "http://app.test/#encounter"

    def __init__(self, screen: str, element: Any = "save", focused: Any = None):
        self.screen = screen
        self.element = element
        self.focused = focused

    def _describe(self, name: str, **kw: Any) -> dict[str, Any]:
        return {
            "tag": "button",
            "type": "submit",
            "name": name,
            "role": "",
            "interactive": True,
            "editable": False,
            "context": "",
            "scope": self.screen,
            **kw,
        }

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if "elementFromPoint" in script:
            if self.element is None:
                raise RuntimeError("navigating")
            if self.element == "save":
                return self._describe("Save")
            return self._describe(**self.element)
        if "activeElement" in script:
            return self._describe(**self.focused) if self.focused else None
        return None


class _Computer:
    dialect = "anthropic"
    pointer = (40, 50)

    def __init__(self, screen: str, **page: Any) -> None:
        self.page = _Page(screen, **page)
        self.executed: list[dict[str, Any]] = []

    def screenshot(self) -> bytes:
        return b"png"

    def execute(self, native: dict[str, Any]) -> Any:
        self.executed.append(native)
        return SimpleNamespace(png=b"png", notes=[])


def _refused(computer: _Computer, native: dict[str, Any], tmp_path: Path) -> Any:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate

    harness = Harness(computer, SaveGate(["MRN P1"]), evidence_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(GateRefused, match="MRN P1"):
        harness.act(native)
    assert computer.executed == []
    return harness


@pytest.mark.parametrize(
    "native", [{"action": "left_click"}, {"action": "left_click", "coordinate": [1, 2]}]
)
def test_a_save_on_the_wrong_record_is_never_performed(
    native: dict, tmp_path: Path
) -> None:
    harness = _refused(_Computer("Alex Testcase — MRN P2"), native, tmp_path)
    assert harness.gate_log[0]["decision"] == "refused_wrong_record"
    assert (tmp_path / "gate-01.png").is_file()


def test_the_identity_must_be_whole_words(tmp_path: Path) -> None:
    _refused(
        _Computer("Sam Specimen — MRN P12"),
        {"action": "left_click", "coordinate": [1, 2]},
        tmp_path,
    )


BUTTON = {"name": "Save note", "tag": "div", "role": "button"}
TEXT = {"name": "Note", "tag": "textarea", "editable": True}


@pytest.mark.parametrize(
    ("page", "native"),
    [
        ({"focused": TEXT}, {"action": "type", "text": "done\n"}),
        ({"focused": {"name": "Open"}}, {"action": "key", "text": "space"}),
        ({"focused": TEXT}, {"action": "key", "text": "ctrl+Return"}),
        ({"focused": TEXT}, {"action": "key", "text": "ctrl+s"}),
        ({"focused": TEXT}, {"action": "key", "text": "Tab Return"}),
        ({"focused": TEXT}, {"action": "key", "text": "Return"}),
        ({"element": BUTTON}, {"action": "left_click", "coordinate": [1, 2]}),
        ({"element": None}, {"action": "left_click", "coordinate": [1, 2]}),
        (
            {"element": {"name": "Delete encounter"}},
            {"action": "double_click", "coordinate": [1, 2]},
        ),
    ],
)
def test_every_way_to_commit_is_gated(page: dict, native: dict, tmp_path: Path) -> None:
    _refused(_Computer("Alex Testcase — MRN P2", **page), native, tmp_path)


@pytest.mark.parametrize(
    ("page", "native"),
    [
        ({"element": {"name": "Open"}}, {"action": "left_click", "coordinate": [1, 2]}),
        ({"focused": TEXT}, {"action": "type", "text": "no line break"}),
        ({"focused": TEXT}, {"action": "key", "text": "space"}),
        ({"focused": TEXT}, {"action": "key", "text": "Tab"}),
    ],
)
def test_plain_actions_are_not_gated(page: dict, native: dict) -> None:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate

    computer = _Computer("Alex Testcase — MRN P2", **page)
    harness = Harness(computer, SaveGate(["MRN P1"]))  # type: ignore[arg-type]
    harness.act(native)
    assert len(computer.executed) == 1 and harness.stats.gate_checks == 0


def test_the_hold_decides_after_the_identity_check(tmp_path: Path) -> None:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate

    asked: list[Any] = []
    computer = _Computer("Jane Sample — MRN P1")
    deny = Harness(
        computer,  # type: ignore[arg-type]
        SaveGate(["MRN P1"], hold=lambda req: asked.append(req) or False),
    )
    with pytest.raises(GateRefused, match="hold"):
        deny.act({"action": "left_click", "coordinate": [1, 2]})
    assert computer.executed == [] and asked[0].control == "Save"

    allow = Harness(computer, SaveGate(["MRN P1"], hold=lambda req: True))  # type: ignore[arg-type]
    allow.act({"action": "left_click", "coordinate": [1, 2]})
    assert len(computer.executed) == 1
    assert allow.gate_log[-1]["decision"] == "approved_by_hold"


def test_a_ref_is_stale_when_its_row_changed() -> None:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate, StaleRef
    from openadapt_flow.compile_agent.observe import Control, StructuralObservation

    computer = _Computer(
        "x", element={"name": "Save", "context": "Alex Testcase | Save"}
    )
    harness = Harness(computer, SaveGate(["MRN P1"]))  # type: ignore[arg-type]
    harness._last = StructuralObservation(
        url="",
        title="",
        text="",
        controls={
            "e1": Control("e1", "button", "Save", 1, 2, context="Jane Sample | Save")
        },
    )
    with pytest.raises(StaleRef, match="no longer at its place"):
        harness.click("e1")
    assert computer.executed == []


# -- targets ------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["windows", "macos", "linux", "rdp", "citrix"])
def test_a_non_web_backend_is_refused_before_anything_runs(
    backend: str, tmp_path: Path, capsys
) -> None:
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent import FirstRunCompiler
    from openadapt_flow.compile_agent.cli import cmd_compile_agent
    from openadapt_flow.compile_agent.pipeline import UnsupportedTarget

    args = build_parser().parse_args(
        ["compile-agent", "Save {note}", "--backend", backend, "--url", "http://a"]
    )
    assert cmd_compile_agent(args) == 2
    assert f"the {backend!r} backend" in capsys.readouterr().err
    with pytest.raises(UnsupportedTarget, match="web apps only"):
        FirstRunCompiler(
            "Save {note}",
            params=["note"],
            url="http://a",
            verify=tmp_path / "missing.yaml",  # never read: refused first
            expect={"patient_id": "p1"},
            on_screen=["MRN P1"],
            backend=backend,
        )


def test_a_deployment_naming_another_backend_is_refused(tmp_path: Path, capsys) -> None:
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.cli import cmd_compile_agent

    config = tmp_path / "deployment.yaml"
    config.write_text("backend:\n  kind: citrix\n")
    args = build_parser().parse_args(
        ["compile-agent", "Save {note}", "--url", "http://a", "--config", str(config)]
    )
    assert cmd_compile_agent(args) == 2
    assert "'citrix' backend" in capsys.readouterr().err


def test_a_non_http_url_is_refused(capsys) -> None:
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.cli import cmd_compile_agent

    args = build_parser().parse_args(
        ["compile-agent", "Save {note}", "--url", "rdp://host"]
    )
    assert cmd_compile_agent(args) == 2
    assert "not an http(s) URL" in capsys.readouterr().err


# -- frames and text fields -----------------------------------------------------------


class _FramePage(_Page):
    """A top page whose point and focus land on an iframe."""

    def __init__(self, inner: Any) -> None:
        super().__init__("outer page with no identity")
        self.inner = inner
        self.main_frame = object()
        self.frames = [self.main_frame, inner]

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if "elementFromPoint" in script or "activeElement" in script:
            return self._describe("frame", tag="iframe")
        return None


def test_a_save_inside_an_iframe_is_read_in_the_frame() -> None:
    from openadapt_flow.compile_agent.observe import element_at

    inner = _Page("Alex Testcase — MRN P2")
    backend = SimpleNamespace(
        _frame_point=lambda x, y: SimpleNamespace(
            frame_path=("iframe#chart",), scope=inner, x=x - 10, y=y - 10
        )
    )
    page = _FramePage(inner)
    assert element_at(page, 50, 60, backend=backend)["name"] == "Save"
    # A frame the backend can't map is unreadable, so a click there is gated.
    assert element_at(page, 50, 60) is None


def test_a_click_in_an_iframe_is_gated(tmp_path: Path) -> None:
    computer = _Computer("x")
    inner = _Page("Alex Testcase — MRN P2")
    computer.page = _FramePage(inner)
    computer.session = SimpleNamespace(
        backend=SimpleNamespace(
            _frame_point=lambda x, y: SimpleNamespace(
                frame_path=("iframe",), scope=inner, x=x, y=y
            )
        )
    )
    _refused(computer, {"action": "left_click", "coordinate": [5, 5]}, tmp_path)


def test_focus_inside_an_iframe_is_followed() -> None:
    from openadapt_flow.compile_agent.observe import focused_element

    class _Frame:
        def evaluate(self, script: str, arg: Any = None) -> Any:
            if "hasFocus" in script:
                return True
            return {"tag": "input", "name": "Note", "scope": "Jane Sample — MRN P1"}

    page = _FramePage(_Frame())
    assert focused_element(page)["scope"] == "Jane Sample — MRN P1"


def test_a_click_on_a_text_field_is_not_a_commit() -> None:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate

    computer = _Computer(
        "Alex Testcase — MRN P2",
        element={"name": "Postal code", "tag": "input", "editable": True},
    )
    harness = Harness(computer, SaveGate(["MRN P1"]))  # type: ignore[arg-type]
    harness.act({"action": "left_click", "coordinate": [1, 2]})
    assert len(computer.executed) == 1 and harness.stats.gate_checks == 0
