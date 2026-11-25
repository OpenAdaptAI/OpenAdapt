"""Real headless Chromium Playwright tests for macro staging, wire interception, and stepwise modal healing."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    CommitInterceptor,
    ObligationLedger,
    PlanStep,
    StepwiseHealer,
    observe_schema,
    stage_form,
)

LONG_FORM_HTML = """<!doctype html>
<html><body>
  <nav>
    <a href="#form-section" data-testid="submit-authorizations-link">Submit Authorizations</a>
    <input type="search" role="searchbox" id="nav-search" placeholder="Search records" />
  </nav>
  <div style="height: 2200px;">Spacer forcing form far below fold</div>
  <section id="form-section">
    <form id="auth-form" onsubmit="event.preventDefault(); document.getElementById('status').innerText='SAVED:' + document.getElementById('member_id').value;">
      <label for="member_id">Member ID</label>
      <input id="member_id" name="member_id" type="text" />
      <label for="req_type">Request Type</label>
      <select id="req_type" name="req_type">
        <option value="">Choose</option>
        <option value="outpatient">Outpatient Procedure</option>
      </select>
      <label for="agree_box">Confirm Clinical Necessity</label>
      <input id="agree_box" name="agree_box" type="checkbox" />
      <button type="submit" data-testid="final-save-btn">Save</button>
    </form>
    <div id="status"></div>
  </section>
</body></html>"""

DRIFTED_MODAL_V2_HTML = """<!doctype html>
<html><body>
  <main>
    <button id="open-v2-modal" type="button" onclick="openModal()">Create</button>
    <div id="records-list"></div>
  </main>
  <script>
    function openModal() {
      const dlg = document.createElement('div');
      dlg.className = 'v-dialog--active';
      dlg.setAttribute('role', 'dialog');
      dlg.innerHTML = `
        <label for="v-input-99">Households Filter</label>
        <input id="v-input-99" type="checkbox" />
        <label for="v-input-104">Cookbook Name</label>
        <input id="v-input-104" type="text" placeholder="Name" />
        <label for="v-input-108">Description</label>
        <textarea id="v-input-108" placeholder="Description"></textarea>
        <button id="v-btn-201" type="button" onclick="saveModal()">Save Cookbook</button>
      `;
      document.body.appendChild(dlg);
    }
    function saveModal() {
      const name = document.getElementById('v-input-104').value;
      const desc = document.getElementById('v-input-108').value;
      document.getElementById('records-list').innerText = 'RECORD:' + name + '|' + desc;
      document.querySelector('.v-dialog--active').remove();
    }
  </script>
</body></html>"""


@pytest.mark.parametrize("headless", [True])
def test_stage_form_full_dom_and_wire_interceptor_on_live_chromium(headless: bool) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1024, "height": 720})
        interceptor = CommitInterceptor()
        interceptor.attach_playwright_page(page)
        page.set_content(LONG_FORM_HTML)

        # 1. Clicking nav link containing "submit" in testid must NOT classify as a commit
        interceptor.begin_step()
        page.locator('[data-testid="submit-authorizations-link"]').click()
        c_nav = interceptor.classify_step(element_tag="a", element_role="link")
        assert c_nav.is_commit is False
        assert c_nav.kind == "read_only_nav"

        # 2. Full-DOM observe_schema sees controls 2200px below the fold with 0 scrolls
        controls = observe_schema(page)
        ids = {c.element_id for c in controls}
        assert {"member_id", "req_type", "agree_box"}.issubset(ids)

        # 3. SSA provenance blocks hallucinated Member ID before touching the DOM
        ledger = ObligationLedger("ep-live-1", subject_key="P-1")
        ledger.record_ssa_literal("member_id", "AET123456789", source_window="emr", turn=1)
        blocked = stage_form(
            page,
            {"member_id": "AET999999999", "req_type": "outpatient"},
            ledger=ledger,
            opaque_fields=["member_id"],
            interceptor=interceptor,
        )
        assert blocked.ok is False
        assert len(blocked.provenance_violations) == 1
        assert page.locator("#member_id").input_value() == ""

        # 4. Valid batch stage_form fills all 3 below-the-fold controls in 1 call
        staged = stage_form(
            page,
            {"member_id": "AET123456789", "req_type": "outpatient", "agree_box": "true"},
            ledger=ledger,
            opaque_fields=["member_id"],
            interceptor=interceptor,
        )
        assert staged.ok is True
        assert staged.staged["member_id"] == "AET123456789"
        assert staged.staged["req_type"] == "outpatient"
        assert staged.staged["agree_box"] == "True"
        assert "member_id" in interceptor.dirty_fields
        assert "nav-search" not in interceptor.dirty_fields

        browser.close()


def test_stepwise_healer_regrounds_unopened_v2_modal_live() -> None:
    """Reproduces the Mealie v2.8 shifted modal ID failure and proves stepwise live healing fixes it."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1024, "height": 720})
        page.set_content(DRIFTED_MODAL_V2_HTML)

        # v1 plan has stale selectors that all broke in v2 (and modal inputs do not exist at Step 0)
        stale_v1_plan = [
            PlanStep("s1", "click", "#legacy-create-btn", semantic_label="Create"),
            PlanStep("s2", "fill", "#v-input-12", semantic_label="Cookbook Name", value="Quick Dinners", prefer_modal=True),
            PlanStep("s3", "fill", "#v-input-15", semantic_label="Description", value="30-minute meals", prefer_modal=True),
            PlanStep("s4", "click", "#legacy-submit-btn", semantic_label="Save Cookbook", is_commit=True, prefer_modal=True),
        ]

        healer = StepwiseHealer()
        result = healer.run_and_heal(
            page,
            stale_v1_plan,
            readback_oracle=lambda p: "RECORD:Quick Dinners|30-minute meals" in p.locator("#records-list").inner_text(),
        )
        assert result.verified is True
        assert result.healed_steps == ["s1", "s2", "s3", "s4"]
        assert result.healed_plan[1].selector == "#v-input-104"
        assert result.healed_plan[2].selector == "#v-input-108"

        # Replay the healed v2 plan cleanly with zero further healing
        page.set_content(DRIFTED_MODAL_V2_HTML)
        warm_result = healer.run_and_heal(
            page,
            result.healed_plan,
            readback_oracle=lambda p: "RECORD:Quick Dinners|30-minute meals" in p.locator("#records-list").inner_text(),
        )
        assert warm_result.verified is True
        assert warm_result.healed_steps == []

        browser.close()
