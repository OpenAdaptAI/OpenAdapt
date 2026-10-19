"""Code as action end to end, in a real browser, with no model calls.

* MockMed through the whole compile-agent pipeline with a scripted code
  agent: run 1 passes its record check, every element the agent acted on is
  on the compiled program's steps with the locator the recorder stored, the
  proposed checks carry what the agent read, a person confirms, a key admits,
  and run 2 replays with no model calls and is read back VERIFIED.
* The wrong patient is refused at the save; nothing is written or compiled.
* A form inside an iframe: the save gate reads the Save button inside the
  frame (it used to see only the frame), a click on a field whose label holds
  "post" is not a save, and the code tool fills and saves through the frame.
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.compile_agent import (
    CodeActionTool,
    FirstRunCompiler,
    GateRefused,
    ScriptedCodeAgent,
)
from openadapt_flow.compile_agent.harness import Harness, SaveGate
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.tutorial import _http_json, _records

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
NEW_NOTE = "Recheck knee in ten days"

SCRIPTS = [
    "tree max=20",
    'click role=button name=Open near="{patient}"\ntext grep=MRN',
    'click name="New Encounter"\nclick name=Triage\nfill name=Note value="{note}"\n'
    'click name="Save Encounter"',
    "text grep={note}",
]


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _compiler(tmp_path: Path, base: str, task_id: str) -> FirstRunCompiler:
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )
    return FirstRunCompiler(
        PROMPT,
        params=["note"],
        url=f"{base}/?fault=ok&idempotency=demo#tasks",
        verify=verify,
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "MRN P1"],
        task_id=task_id,
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
        reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
        on_warning=lambda text: None,
    )


def _scripts(patient: str) -> list[str]:
    return [s.replace("{patient}", patient) for s in SCRIPTS]


def _admitter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    pem = tmp_path / "keyfiles" / "admitter.pem"
    authority.register_principal(
        tmp_path / "keys",
        role="admitter_key",
        principal_id="ci:code-action",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(pem), tmp_path / "keys"
    )


def test_code_agent_run_compiles_into_a_program_that_replays(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler = _compiler(tmp_path, mockmed, "mockmed.code_action")
    agent = ScriptedCodeAgent(_scripts("Jane Sample"))
    result = compiler.record(agent, {"note": NOTE})
    assert result.status == "draft", result.detail

    trace = result.run1["code_action"]
    assert trace["surface"] == "dom" and trace["screenshots"] == 0
    assert trace["actions"] == trace["by_structure"] == 5
    assert trace["with_recorded_locator"] == trace["locator_unique"] == 5
    assert trace["commits"] == 1
    # The identity was read before the save, and the note after it.
    assert trace["identity_read_before_commit"] == [
        "identity:Jane Sample",
        "identity:MRN P1",
    ]
    assert trace["program"]["passed"] and trace["program"]["in_program"] == 5
    steps = json.loads(
        (Path(result.run1["attempt"]) / "code_action_trace.json").read_text()
    )["steps"]
    assert [s["action"] for s in steps] == ["click", "click", "click", "fill", "click"]
    assert steps[0]["element"]["row"].startswith("Jane Sample")
    assert steps[0]["recorded"]["selector"] == "#open-p1"
    assert all(s.get("matches_precheck") for s in steps if s.get("recorded"))

    proposed = compiler.proposed()
    assert proposed["agent_read"]["program"]["passed"]
    compiler.confirm_checks(principal_id="dana.reviewer", drop_fields=["key", "source"])
    admitted = compiler.admit(
        admitter=_admitter(tmp_path, monkeypatch),
        canary_params=[{"note": "Canary test note"}],
    )
    assert admitted.admitted, admitted.detail
    replay = compiler.run(note=NEW_NOTE)
    assert replay.verified and replay.model_calls == 0
    assert replay.receipt_outcome == "VERIFIED"
    rows = _records(mockmed)
    assert [(r["patient_id"], r["note"]) for r in rows] == [("p1", NEW_NOTE)]


def test_code_agent_on_the_wrong_patient_is_refused_at_the_save(
    mockmed: str, tmp_path: Path
) -> None:
    compiler = _compiler(tmp_path, mockmed, "mockmed.code_action_wrong")
    result = compiler.record(
        ScriptedCodeAgent(_scripts("Alex Testcase")), {"note": NOTE}
    )
    assert result.status == "refused_wrong_record", result.detail
    assert "MRN P1" in (result.detail or "")
    assert _records(mockmed) == []
    assert not (compiler.dir / "program").exists()


# -- a form in an iframe ------------------------------------------------------------

OUTER = """<!doctype html><html><head><title>Clinic</title></head><body
style="margin:0"><h1>Clinic</h1><iframe name="form" src="form.html"
style="width:900px;height:500px;border:0"></iframe></body></html>"""

FORM = """<!doctype html><html><head><title>Edit address</title></head><body>
<form id="f" onsubmit="event.preventDefault();
 document.getElementById('done').textContent='Saved ' +
 document.getElementById('zip').value;">
<p>{who}</p>
<label for="zip">Postal code</label> <input id="zip" name="zip">
<button id="save" type="submit">Save address</button>
</form><p id="done"></p></body></html>"""


@pytest.fixture
def frame_site(tmp_path: Path) -> Iterator[Any]:
    site = tmp_path / "site"
    site.mkdir()

    def write(who: str) -> None:
        (site / "index.html").write_text(OUTER)
        (site / "form.html").write_text(FORM.replace("{who}", who))

    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(site), **kw)

        def log_message(self, *a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html", write
    server.shutdown()


def _frame_harness(url: str, tmp_path: Path, on_screen: list[str], dialect: str) -> Any:
    from openadapt_flow.authoring import AuthoringSession
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.jit.adapters import CaptureComputer

    backend, close = PlaywrightBackend.launch(url, headless=True)
    backend.page.frame(name="form").wait_for_selector("#save")
    session = AuthoringSession(
        backend, tmp_path / "rec", backend_kind="web", app_url=url
    )
    computer = CaptureComputer(session, dialect=dialect, page=backend.page)
    session.start_record()
    harness = Harness(computer, SaveGate(on_screen), evidence_dir=tmp_path / "gates")
    return harness, backend, close


def test_the_gate_sees_a_save_inside_an_iframe(frame_site: Any, tmp_path: Path) -> None:
    url, write = frame_site
    write("Alex Testcase — MRN P2")
    harness, backend, close = _frame_harness(url, tmp_path, ["MRN P1"], "anthropic")
    try:
        frame = backend.page.frame(name="form")
        # Playwright boxes are in the page's coordinates, frames included.
        inner = frame.locator("#save").bounding_box()
        x = int(inner["x"] + inner["width"] / 2)
        y = int(inner["y"] + inner["height"] / 2)
        zip_box = frame.locator("#zip").bounding_box()
        zx = int(zip_box["x"] + zip_box["width"] / 2)
        zy = int(zip_box["y"] + zip_box["height"] / 2)
        # A click on the Postal code field is not a save.
        harness.act({"action": "left_click", "coordinate": [zx, zy]})
        assert harness.stats.gate_checks == 0
        # Enter in the frame's form is gated, and read inside the frame.
        with pytest.raises(GateRefused, match="MRN P1"):
            harness.act({"action": "key", "text": "Return"})
        assert "Postal code" in harness.gate_log[-1]["control"]
        with pytest.raises(GateRefused, match="MRN P1"):
            harness.act({"action": "left_click", "coordinate": [x, y]})
        assert frame.locator("#done").inner_text() == ""
    finally:
        close()


def test_only_a_drop_down_that_submits_is_gated(tmp_path: Path) -> None:
    from openadapt_flow.compile_agent.observe import focused_element

    page_html = (
        '<form><select id=a onchange="this.form.submit()"><option>x</option>'
        '</select><select id=b onchange="capitalize(this)"><option>y</option>'
        "</select></form>"
    )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(page_html)
        page.focus("#a")
        assert focused_element(page)["submits"] is True
        page.focus("#b")
        assert focused_element(page)["submits"] is False
        browser.close()


def test_the_code_tool_fills_and_saves_inside_an_iframe(
    frame_site: Any, tmp_path: Path
) -> None:
    url, write = frame_site
    write("Jane Sample — MRN P1")
    harness, backend, close = _frame_harness(url, tmp_path, ["MRN P1"], "generic")
    try:
        tool = CodeActionTool(harness, workdir=tmp_path / "work")
        listing = tool.run("tree").text
        assert "# frame 1: Edit address" in listing
        assert 'textbox "Postal code"' in listing
        out = tool.run(
            'fill name="Postal code" value="02139"\nclick name="Save address"\n'
            "text grep=Saved"
        )
        assert "(save gate passed)" in out.text, out.text
        assert backend.page.frame(name="form").locator("#done").inner_text() == (
            "Saved 02139"
        )
        steps = tool.trace.steps
        assert [s["action"] for s in steps] == ["fill", "click"]
        assert steps[1]["recorded"]["frame_path"]
        assert harness.gate_log[0]["decision"] == "passed_no_hold"
    finally:
        close()
