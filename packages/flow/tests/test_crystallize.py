"""openadapt_flow.crystallize without a browser or a model.

The record check, the commit-label rule, scrubbing that keeps the task's own
values, the Claude loop's accounting and stops, and the emitted MCP server.
tests/e2e/test_crystallize_e2e.py runs the whole pipeline on MockMed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import privacy
from openadapt_flow.crystallize import (
    BudgetExceeded,
    ClaudeAgent,
    GateRefused,
    check_run_record,
)
from openadapt_flow.crystallize.agents import AgentLedger, ModelCall
from openadapt_flow.crystallize.observe import _render, commit_label, scrub_lines
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
            from openadapt_flow.crystallize.harness import StaleRef

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
    from openadapt_flow.crystallize.emit import emit_mcp
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

    def __init__(self, screen: str) -> None:
        self.screen = screen

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if "innerText" in script and arg is None:
            return self.screen
        if "elementFromPoint" in script:
            return {"tag": "button", "type": "submit", "name": "Save", "in_form": False}
        return None


class _Computer:
    dialect = "anthropic"
    pointer = (40, 50)

    def __init__(self, screen: str) -> None:
        self.page = _Page(screen)
        self.executed: list[dict[str, Any]] = []

    def screenshot(self) -> bytes:
        return b"png"

    def execute(self, native: dict[str, Any]) -> Any:
        self.executed.append(native)
        return SimpleNamespace(png=b"png", notes=[])


@pytest.mark.parametrize(
    "native", [{"action": "left_click"}, {"action": "left_click", "coordinate": [1, 2]}]
)
def test_a_save_on_the_wrong_record_is_never_performed(
    native: dict, tmp_path: Path
) -> None:
    from openadapt_flow.crystallize.harness import Harness, SaveGate

    computer = _Computer("Alex Testcase — MRN P2")
    harness = Harness(computer, SaveGate(["MRN P1"]), evidence_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(GateRefused, match="MRN P1"):
        harness.act(native)
    assert computer.executed == []
    assert harness.gate_log[0]["decision"] == "refused_wrong_record"
    assert (tmp_path / "gate-01.png").is_file()


def test_the_hold_decides_after_the_identity_check(tmp_path: Path) -> None:
    from openadapt_flow.crystallize.harness import Harness, SaveGate

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
