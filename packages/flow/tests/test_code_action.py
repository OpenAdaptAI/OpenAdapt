"""Code as action without a browser or a model.

The sandboxed shell, the script language against a fake desktop surface (the
harness has no page, so the save gate works from the tool's hints), the
structured trace and its check against a compiled program, and the Claude
loop's ``code`` mode with a fake client. tests/e2e/test_code_action_e2e.py
runs the tool on MockMed and on a form inside an iframe in a real browser.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow.compile_agent import (
    ClaudeAgent,
    CodeActionTool,
    GateRefused,
    ScriptedCodeAgent,
    check_trace_against_ir,
)
from openadapt_flow.compile_agent.code_action import Element, LeftTheApp, PixelSurface
from openadapt_flow.compile_agent.harness import CommitHint, Harness, SaveGate
from openadapt_flow.compile_agent.sandbox import Sandbox, ShellRefused
from openadapt_flow.ir import StructuralLocator
from openadapt_flow.jit.compile_agent import TaskRequest

# -- the shell ----------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path / "work")
    box.write("page.txt", "row: Jane Sample | MRN P1\nrow: Alex Testcase | MRN P2")
    return box


def test_the_shell_reads_files_in_its_directory(sandbox: Sandbox) -> None:
    assert sandbox.run("grep -n P1 page.txt").output == "1:row: Jane Sample | MRN P1\n"
    assert sandbox.run("grep MRN page.txt | wc -l").output.strip() == "2"
    wrote = sandbox.run("grep P2 page.txt > p2.txt")
    assert wrote.wrote == "p2.txt"
    assert (sandbox.workdir / "p2.txt").read_text().startswith("row: Alex")
    assert sandbox.run("cat p2.txt").exit_code == 0


@pytest.mark.parametrize(
    "command",
    [
        "curl http://example.invalid",
        "python -c 1",
        "sh -c ls",
        "cat /etc/passwd",
        "cat ~/x",
        "cat ../page.txt",
        "cat sub/../../x",
        "ls; cat page.txt",
        "ls && ls",
        "ls & ls",
        "cat page.txt $(ls)",
        "cat `ls`",
        "cat < page.txt",
        "ls > /tmp/x",
        "ls > ../x",
        "ls > a > b",
        "sort -o out.txt page.txt",
        "sort -uo/tmp/x page.txt",
        "sort --compress=sh -S 64K -T . page.txt",
        "grep -f/etc/hosts page.txt",
        "grep --file=/etc/hosts page.txt",
        "diff --from-file=/etc/hosts page.txt",
        "head -5 page.txt",
        "tail -f page.txt",
        "ls |",
    ],
)
def test_the_shell_refuses_anything_else(sandbox: Sandbox, command: str) -> None:
    with pytest.raises(ShellRefused):
        sandbox.run(command)


def test_a_quoted_operator_is_a_word(sandbox: Sandbox) -> None:
    assert sandbox.run("cut -d '|' -f 2 page.txt").output == " MRN P1\n MRN P2\n"
    assert sandbox.run("echo 'a;b'").output == "a;b\n"


def test_the_shell_has_no_environment(sandbox: Sandbox) -> None:
    # printenv is not allowed; echo of a variable is not expanded (no shell).
    assert sandbox.run("echo $HOME").output.strip() == "$HOME"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS")
def test_on_macos_the_profile_denies_reads_and_programs(sandbox: Sandbox) -> None:
    import subprocess

    assert sandbox.isolation == "sandbox-exec"
    # Even a command the parser let through can't read outside the directory
    # or start another program.
    outside = subprocess.run(
        sandbox._wrap(["/bin/cat", "/etc/hosts"]),
        capture_output=True,
        cwd=sandbox.workdir,
    )
    assert outside.returncode != 0
    inside = subprocess.run(
        sandbox._wrap(["/bin/cat", "page.txt"]),
        capture_output=True,
        cwd=sandbox.workdir,
    )
    assert inside.returncode == 0 and b"MRN P1" in inside.stdout
    profile = sandbox._wrap(["/bin/cat"])[2]
    exec_other = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", "true"],
        capture_output=True,
    )
    assert exec_other.returncode != 0


# -- a fake desktop surface ------------------------------------------------------------


class FakeSurface:
    """A desktop window with a structured layer: named controls and text."""

    kind = "tree"

    def __init__(self, screens: dict[str, list[dict[str, Any]]], start: str) -> None:
        self.screens = screens
        self.screen = start
        self.unique = True

    def _elements(self) -> list[Element]:
        out = []
        for i, item in enumerate(self.screens[self.screen], 1):
            out.append(
                Element(
                    ref=str(i),
                    role=item["role"],
                    name=item["name"],
                    x=10 * i,
                    y=20 * i,
                    value=item.get("value"),
                    context=item.get("row", ""),
                    scope=item.get("scope", ""),
                    tag=item.get("tag", ""),
                    options=tuple(item.get("options", ())),
                    source="tree",
                    submits=bool(item.get("submits")),
                )
            )
        return out

    def read(self) -> tuple[str, list[dict[str, Any]], list[Element]]:
        elements = self._elements()
        items = [{"kind": "control", "ref": e.ref, "row": -1} for e in elements]
        return f"window: {self.screen}", items, elements

    def locate(self, role: str, name: str) -> Optional[Element]:
        return None

    def origin(self) -> Optional[str]:
        return None

    def at(self, element: Element) -> Optional[dict[str, Any]]:
        item = next(
            (i for i in self.screens[self.screen] if i["name"] == element.name), {}
        )
        return {
            "name": element.name,
            "context": element.context,
            "value": item.get("value"),
        }

    def focused(self) -> Optional[dict[str, Any]]:
        return {"name": "Note", "scope": "Jane Sample MRN P1"}

    def screen_text(self) -> str:
        return "\n".join(f"{i['role']} {i['name']}" for i in self.screens[self.screen])

    def locator_unique(self, locator: Any) -> Optional[bool]:
        return self.unique


class FakeBackend:
    def __init__(self, surface: FakeSurface) -> None:
        self.surface = surface

    def structural_locator_at(self, x: int, y: int) -> StructuralLocator:
        element = next(e for e in self.surface._elements() if (e.x, e.y) == (x, y))
        return StructuralLocator(role=element.role, name=element.name)


class FakeComputer:
    """A recording computer with no page (a desktop surface)."""

    dialect = "generic"
    page = None

    def __init__(self, surface: FakeSurface, flow: dict[str, str]) -> None:
        self.surface = surface
        self.flow = flow
        self.executed: list[dict[str, Any]] = []
        self.pointer: Optional[tuple[int, int]] = None
        self.focus: Optional[str] = None
        self.session = SimpleNamespace(backend=FakeBackend(surface), _recorder=None)

    def screenshot(self) -> bytes:
        return b"png"

    def execute(self, native: dict[str, Any]) -> Any:
        self.executed.append(dict(native))
        if native["kind"] == "click":
            self.pointer = (native["x"], native["y"])
            clicked = next(
                e for e in self.surface._elements() if (e.x, e.y) == self.pointer
            )
            self.focus = clicked.name
            nxt = self.flow.get(clicked.name)
            if nxt:
                self.surface.screen = nxt
        if native["kind"] == "type":
            for item in self.surface.screens[self.surface.screen]:
                if item["name"] == self.focus:
                    item["value"] = native["text"]
        return SimpleNamespace(png=b"png", notes=[])


SCREENS = {
    "list": [
        {"role": "button", "name": "Open", "row": "Jane Sample | MRN P1"},
        {"role": "button", "name": "Open", "row": "Alex Testcase | MRN P2"},
    ],
    "form": [
        {"role": "textbox", "name": "Note", "value": ""},
        {"role": "textbox", "name": "Postal code", "value": ""},
        {
            "role": "combobox",
            "name": "Type",
            "tag": "select",
            "value": "Consult",
            "options": ["Consult", "Triage"],
        },
        {"role": "button", "name": "Save note", "scope": "Jane Sample — MRN P1"},
        {"role": "button", "name": "Cancel"},
    ],
}


def _tool(
    tmp_path: Path, on_screen: tuple[str, ...] = ("MRN P1",), **gate: Any
) -> tuple[CodeActionTool, FakeComputer, Harness]:
    screens = {k: [dict(i) for i in v] for k, v in SCREENS.items()}
    surface = FakeSurface(screens, "list")
    computer = FakeComputer(surface, {"Open": "form", "Save note": "list"})
    harness = Harness(
        computer,  # type: ignore[arg-type]
        SaveGate(on_screen, **gate),
        evidence_dir=tmp_path / "gates",
    )
    tool = CodeActionTool(
        harness,
        surface=surface,
        watch={"identity:MRN P1": "MRN P1", "input:note": "Done"},
    )
    return tool, computer, harness


def test_a_listing_names_controls_with_refs(tmp_path: Path) -> None:
    tool, _, _ = _tool(tmp_path)
    out = tool.run("tree").text
    assert '@1 button "Open"' in out and '@2 button "Open"' in out


def test_an_ambiguous_target_is_refused_with_the_candidates(tmp_path: Path) -> None:
    tool, computer, _ = _tool(tmp_path)
    out = tool.run("click role=button name=Open").text
    assert "2 elements match" in out and "Alex Testcase" in out
    assert computer.executed == []


def test_one_script_reads_acts_and_saves_and_each_action_is_a_step(
    tmp_path: Path,
) -> None:
    tool, computer, harness = _tool(tmp_path)
    out = tool.run(
        'click role=button name=Open near="Jane Sample"\n'
        'fill name=Note value="Done"\n'
        'click name="Postal code"\n'
        "select name=Type option=Triage\n"
        'click name="Save note"\n'
        "tree"
    )
    assert 'ok: click button "Save note" (save gate passed)' in out.text
    # The write stopped the script: the trailing tree did not run.
    assert out.committed and "1 lines not run" in out.text
    steps = tool.trace.steps
    assert [s["action"] for s in steps] == ["click", "fill", "click", "select", "click"]
    assert steps[0]["element"] == {
        "role": "button",
        "name": "Open",
        "row": "Jane Sample | MRN P1",
        "source": "tree",
    }
    assert steps[1]["value"] == "Done" and steps[1]["value_after"] == "Done"
    assert steps[3]["value"] == "Triage"
    assert all(s["recorded"]["role"] for s in steps)
    assert [s["gated"] for s in steps] == [False, False, False, False, True]
    # Postal code holds "post" but a click on a text field commits nothing.
    assert len(harness.gate_log) == 1
    assert harness.gate_log[0]["decision"] == "passed_no_hold"
    # Each action became generic clicks and keys at element centers.
    kinds = [n["kind"] for n in computer.executed]
    assert kinds == [
        "click",
        "click",
        "type",
        "click",
        "click",
        "type",
        "press",
        "click",
    ]
    summary = tool.trace.summary()
    assert summary["by_structure"] == 5 and summary["commits"] == 1
    assert summary["locator_agrees"] == 5


def test_a_save_on_the_wrong_record_is_refused_before_it_is_performed(
    tmp_path: Path,
) -> None:
    tool, computer, harness = _tool(tmp_path, on_screen=("MRN P2",))
    tool.run('click role=button name=Open near="Jane Sample"')
    with pytest.raises(GateRefused, match="MRN P2"):
        tool.run('click name="Save note"')
    assert computer.executed[-1]["kind"] == "click"
    assert len(computer.executed) == 1
    assert harness.gate_log[-1]["decision"] == "refused_wrong_record"


def test_the_hold_is_asked_before_the_save(tmp_path: Path) -> None:
    asked: list[Any] = []
    tool, computer, _ = _tool(tmp_path, hold=lambda req: asked.append(req) or False)
    tool.run('click role=button name=Open near="Jane Sample"')
    with pytest.raises(GateRefused, match="hold"):
        tool.run('click name="Save note"')
    assert asked and asked[0].control == "Save note"


def test_without_a_page_an_unhinted_click_is_gated(tmp_path: Path) -> None:
    _tool_, computer, harness = _tool(tmp_path, on_screen=("MRN P9",))
    with pytest.raises(GateRefused):
        harness.act({"kind": "click", "x": 1, "y": 2})
    assert computer.executed == []
    # A hint can add a gate, and Enter on a hinted control is gated.
    with pytest.raises(GateRefused):
        harness.act(
            {"kind": "press", "key": "Enter"},
            hint=CommitHint(name="Note", scope="", key="Enter"),
        )


def test_enter_on_a_native_drop_down_is_not_a_save(tmp_path: Path) -> None:
    from openadapt_flow.compile_agent import observe

    class Page:
        url = "http://app.test/"

        def evaluate(self, script: str, arg: Any = None) -> Any:
            return {"tag": "select", "name": "State", "scope": "", "editable": False}

    computer = SimpleNamespace(
        dialect="generic",
        page=Page(),
        pointer=None,
        executed=[],
        screenshot=lambda: b"png",
    )
    computer.execute = lambda native: computer.executed.append(native)
    harness = Harness(computer, SaveGate(["MRN P1"]))  # type: ignore[arg-type]
    harness.act({"kind": "press", "key": "Enter"})
    assert harness.stats.gate_checks == 0 and computer.executed
    assert observe.focused_element(Page())["tag"] == "select"


def test_without_a_page_enter_and_space_are_always_gated(tmp_path: Path) -> None:
    tool, computer, harness = _tool(tmp_path)
    tool.run('click role=button name=Open near="Jane Sample"')
    # Space on a control the tool can't name as a save: still gated, and with
    # no reading of what is around it, refused.
    with pytest.raises(GateRefused):
        tool.run("press Space")
    assert [n["kind"] for n in computer.executed] == ["click"]
    # Enter is gated; here the focused field's scope shows the record.
    gates = harness.stats.gate_checks
    assert "(save gate passed)" in tool.run("press Enter").text
    assert harness.stats.gate_checks == gates + 1


def test_select_on_a_button_is_refused_before_any_click(tmp_path: Path) -> None:
    tool, computer, _ = _tool(tmp_path)
    tool.run('click role=button name=Open near="Jane Sample"')
    out = tool.run('select name="Save note" option=x').text
    assert "not a list or drop-down" in out
    assert len(computer.executed) == 1


def test_a_drop_down_that_submits_is_gated(tmp_path: Path) -> None:
    tool, computer, harness = _tool(tmp_path, on_screen=("MRN P9",))
    tool.run('click role=button name=Open near="Jane Sample"')
    tool.surface.screens["form"][2]["submits"] = True
    with pytest.raises(GateRefused):
        tool.run("select name=Type option=Triage")
    assert len(computer.executed) == 1


def test_a_bad_number_is_a_line_error_and_earlier_lines_are_kept(
    tmp_path: Path,
) -> None:
    tool, computer, _ = _tool(tmp_path)
    out = tool.run('click role=button name=Open near="Jane Sample"\nwait ms=soon')
    assert 'ok: click button "Open"' in out.text
    assert "ms= needs a whole number" in out.text
    assert tool.trace.errors == 1 and len(tool.trace.log) == 1
    assert "max= needs" in tool.run("tree max=lots").text


def test_error_text_is_scrubbed(tmp_path: Path) -> None:
    tool, _, _ = _tool(tmp_path)
    tool._out = lambda text: "SCRUBBED"  # type: ignore[method-assign]
    assert "error: SCRUBBED" in tool.run("click role=button name=Open").text


def test_fill_refuses_a_field_that_holds_text(tmp_path: Path) -> None:
    tool, computer, _ = _tool(tmp_path)
    tool.run('click role=button name=Open near="Jane Sample"')
    tool.surface.screens["form"][0]["value"] = "old"
    out = tool.run('fill name=Note value="new"').text
    assert "already holds text" in out
    assert len(computer.executed) == 1


def test_select_refuses_an_option_the_list_does_not_have(tmp_path: Path) -> None:
    tool, computer, _ = _tool(tmp_path)
    tool.run('click role=button name=Open near="Jane Sample"')
    out = tool.run("select name=Type option=Surgery").text
    assert "not an option" in out and '"Triage"' in out
    assert len(computer.executed) == 1


def test_a_stale_ref_and_unknown_commands_are_errors_not_actions(
    tmp_path: Path,
) -> None:
    tool, computer, _ = _tool(tmp_path)
    assert "not in the latest listing" in tool.run("click @9").text
    assert "unknown command" in tool.run("delete everything").text
    assert "find needs" in tool.run("find").text
    assert computer.executed == []
    assert tool.trace.errors == 3


def test_reads_are_evidence_and_shell_output_is_kept_on_disk(tmp_path: Path) -> None:
    tool, _, _ = _tool(tmp_path)
    out = tool.run("tree\nsave list.txt\nsh grep -c Open list.txt").text
    assert out.rstrip().endswith("2")
    assert tool.trace.shell_commands == 1
    assert tool.trace.reads[0]["shows"] == []
    tool.run('click role=button name=Open near="Jane Sample"\nread name="Save note"')
    assert "identity:MRN P1" in tool.trace.reads[-1]["shows"]


def test_page_urls_drop_their_query_string() -> None:
    from openadapt_flow.compile_agent.code_action import _shown_url

    assert _shown_url("http://h/main.php?token_main=abc#tab") == (
        "http://h/main.php?...#tab"
    )
    assert _shown_url("http://h/?fault=ok#patient/p1") == "http://h/?...#patient/p1"


def test_screenshots_only_when_asked(tmp_path: Path) -> None:
    tool, _, harness = _tool(tmp_path)
    assert tool.run("tree").images == []
    out = tool.run("screenshot")
    assert len(out.images) == 1 and harness.stats.screenshots_requested == 1


def test_scroll_needs_a_pointer(tmp_path: Path) -> None:
    tool, computer, _ = _tool(tmp_path)
    assert "click inside the area" in tool.run("scroll down").text
    tool.run('click role=button name=Open near="Jane Sample"')
    assert "ok: scroll down 500px" in tool.run("scroll down").text
    assert computer.executed[-1] == {"kind": "scroll", "dx": 0, "dy": 500}


def test_leaving_the_app_ends_the_run(tmp_path: Path) -> None:
    tool, _, _ = _tool(tmp_path)
    tool.app_origin = "http://app.test"
    tool.surface.origin = lambda: "https://elsewhere.example"  # type: ignore[method-assign]
    with pytest.raises(LeftTheApp):
        tool.run('click role=button name=Open near="Jane Sample"')


def test_pixel_surface_targets_ocr_text(tmp_path: Path) -> None:
    lines = [
        SimpleNamespace(text="Jane Sample MRN P1", region=(0, 0, 100, 20)),
        SimpleNamespace(text="Save", region=(200, 300, 40, 20)),
    ]
    session = SimpleNamespace(screenshot=lambda: b"png")
    surface = PixelSurface(session, ocr=lambda png: lines)
    _header, _items, elements = surface.read()
    assert [(e.name, e.x, e.y, e.source) for e in elements] == [
        ("Jane Sample MRN P1", 50, 10, "ocr"),
        ("Save", 220, 310, "ocr"),
    ]
    assert "MRN P1" in elements[1].scope


# -- the trace against the compiled program -----------------------------------------


def _workflow(*locators: Optional[dict[str, Any]]) -> Any:
    steps = []
    for loc in locators:
        structural = StructuralLocator(**loc) if loc else None
        steps.append(SimpleNamespace(anchor=SimpleNamespace(structural=structural)))
    return SimpleNamespace(steps=steps)


def test_the_program_must_carry_every_locator_in_order() -> None:
    trace = {
        "steps": [
            {
                "i": 0,
                "recorded": {"selector": "#open-p1", "role": "button", "name": "Open"},
            },
            {
                "i": 1,
                "recorded": {"selector": "#note", "role": "textbox", "name": "Note"},
            },
            {"i": 2},
        ]
    }
    ok = check_trace_against_ir(
        trace,
        _workflow(
            {"selector": "#open-p1", "role": "button", "name": "Open"},
            None,
            {"selector": "#note", "role": "textbox", "name": "Note"},
        ),
    )
    assert ok["passed"] and ok["in_program"] == 2
    swapped = check_trace_against_ir(
        trace,
        _workflow(
            {"selector": "#note", "role": "textbox", "name": "Note"},
            {"selector": "#open-p1", "role": "button", "name": "Open"},
        ),
    )
    assert not swapped["passed"] and swapped["missing_steps"] == [1]


def test_a_detour_distill_dropped_is_not_missing(tmp_path: Path) -> None:
    import json

    from openadapt_flow.compile_agent.code_action import distilled_events

    trace = {
        "steps": [
            {"i": 0, "events": [0], "recorded": {"role": "button", "name": "Contact"}},
            {"i": 1, "events": [1], "recorded": {"role": "button", "name": "Contact"}},
            {"i": 2, "events": [2], "recorded": {"selector": "#save"}},
        ]
    }
    (tmp_path / "distill.json").write_text(
        json.dumps(
            {
                "dropped": [
                    {"index": 0, "kind": "click", "rule": "dead_end"},
                    {"index": 1, "kind": "click", "rule": "dead_end"},
                ]
            }
        )
    )
    dropped = distilled_events(tmp_path)
    assert dropped == {0: "dead_end", 1: "dead_end"}
    program = _workflow({"selector": "#save"})
    assert not check_trace_against_ir(trace, program)["passed"]
    ok = check_trace_against_ir(trace, program, distilled=dropped)
    assert ok["passed"] and ok["in_program"] == 1
    assert ok["distilled_steps"] == [
        {"step": 0, "rule": "dead_end"},
        {"step": 1, "rule": "dead_end"},
    ]
    assert distilled_events(tmp_path / "none") == {}


# -- the scripted agent and the Claude loop -----------------------------------------


def _request() -> TaskRequest:
    return TaskRequest(
        task_id="t", instruction="Save the note", inputs={"note": "Done"}
    )


def test_the_scripted_agent_fills_inputs_and_fails_on_an_error(tmp_path: Path) -> None:
    tool, _, harness = _tool(tmp_path)
    agent = ScriptedCodeAgent(
        [
            'click role=button name=Open near="Jane Sample"\nfill name=Note value="{note}"'
        ]
    )
    import openadapt_flow.compile_agent.code_action as ca

    original = ca.surface_for
    ca.surface_for = lambda h: tool.surface  # type: ignore[assignment]
    try:
        ledger = agent.drive(_request(), harness)
        assert ledger.finished and ledger.model_calls == 0
        assert agent.last_trace.steps[1]["value"] == "Done"
        with pytest.raises(ca.ScriptError):
            ScriptedCodeAgent(["click @7"]).drive(_request(), harness)
    finally:
        ca.surface_for = original


class FakeClaude:
    """Answers with scripts, like a model using the run tool would."""

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = list(scripts)
        self.seen: list[Any] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw: Any) -> Any:
        self.seen.append(kw)
        if self.scripts:
            block = {
                "type": "tool_use",
                "id": f"t{len(self.seen)}",
                "name": "run",
                "input": {"script": self.scripts.pop(0)},
            }
        else:
            block = {
                "type": "tool_use",
                "id": "done",
                "name": "finish",
                "input": {"summary": "saved"},
            }
        return SimpleNamespace(
            content=[block],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def test_the_claude_loop_in_code_mode(tmp_path: Path, monkeypatch: Any) -> None:
    tool, computer, harness = _tool(tmp_path)
    import openadapt_flow.compile_agent.code_action as ca

    monkeypatch.setattr(ca, "surface_for", lambda h: tool.surface)
    client = FakeClaude(
        [
            'click role=button name=Open near="Jane Sample"\n'
            'fill name=Note value="Done"\nclick name="Save note"',
            "text",
        ]
    )
    agent = ClaudeAgent(vision="code", model="claude-sonnet-5-5", client=client)
    assert agent.dialect == "generic"
    ledger = agent.drive(_request(), harness)
    assert ledger.finished and ledger.summary == "saved"
    assert ledger.model_calls == 3
    first = client.seen[0]
    assert [t["name"] for t in first["tools"]] == ["run", "finish"]
    assert "tree" in first["messages"][0]["content"][1]["text"]
    assert ledger.to_dict()["images_in_requests"] == 0
    assert agent.last_trace.summary()["commits"] == 1
    assert harness.stats.gate_checks == 1


def test_code_as_action_is_the_default_first_run_agent() -> None:
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.agents import CODE_DEFAULT_MODEL, DEFAULT_MODEL
    from openadapt_flow.compile_agent.cli import _agent

    agent = ClaudeAgent()
    assert agent.vision == "code" and agent.model == CODE_DEFAULT_MODEL
    assert agent.dialect == "generic"
    assert ClaudeAgent(vision="selective").model == DEFAULT_MODEL
    args = build_parser().parse_args(
        ["compile-agent", "Save the note {note}", "--url", "http://app.test/"]
    )
    cli_agent = _agent(args)
    assert cli_agent.vision == "code" and cli_agent.model == CODE_DEFAULT_MODEL


def test_record_readback_names_the_record_and_flags_any_other() -> None:
    from openadapt_flow.compile_agent.harness import record_readback

    before = [{"id": 1, "patient_id": "p2", "note": "old"}]
    state = {"now": list(before)}
    check = record_readback(lambda: state["now"], {"patient_id": "p1"}, lambda: before)
    assert check().outcome == "no_change"
    state["now"] = before + [{"id": 2, "patient_id": "p1", "note": "x"}]
    landed = check()
    assert landed.outcome == "landed" and landed.fields == ("id", "note", "patient_id")
    state["now"] = [{"id": 1, "patient_id": "p2", "note": "edited"}]
    assert check().outcome == "wrong_record"
    state["now"] = []
    assert check().outcome == "wrong_record"
    unreadable = record_readback(lambda: None, {"patient_id": "p1"}, lambda: before)
    assert unreadable().outcome == "unreadable"
