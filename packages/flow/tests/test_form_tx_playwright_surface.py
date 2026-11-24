"""Headless Chromium integration tests for ``PlaywrightFormSurface`` and UI profiles.

Uses CDP network interception (``context.route("http://portal.local/**", ...)``)
exclusively so zero loopback TCP sockets are opened. Zero application or vendor names.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Route, sync_playwright

from openadapt_flow.form_tx.playwright_surface import (
    KeepaliveMutationError,
    PlaywrightFormSurface,
    PopupContractError,
    SurfaceSettleTimeoutError,
)
from openadapt_flow.form_tx.profiles import (
    _parse_minimal_yaml_subset,
    list_builtin_ui_profiles,
    load_ui_profile,
)
from openadapt_flow.form_tx.surface import (
    FormSurfaceBackend,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    EntityTableSpec,
    FieldAnchorSpec,
    ModalRuleSpec,
    ObservedOption,
    UIProfileSpec,
    WizardScreenSpec,
)


@pytest.fixture(scope="module")
def chromium_browser() -> Any:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chromium", headless=True)
        except Exception:
            browser = pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_context(chromium_browser: Browser) -> Any:
    ctx = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        yield ctx
    finally:
        ctx.close()


def test_builtin_ui_profiles_load_and_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtins = list_builtin_ui_profiles()
    assert "server_ppr_wizard" in builtins
    assert "legacy_postback_portal" in builtins

    ppr = load_ui_profile("server_ppr_wizard")
    assert isinstance(ppr, UIProfileSpec)
    assert ppr.profile_id == "server_ppr_wizard"
    assert ppr.authenticated_landmark_selectors == (
        '[data-authenticated-shell="true"]',
    )
    assert ppr.debounce_grace_ms == 120
    assert ppr.settle_window_ms == 120
    assert ppr.max_settle_ms == 15000
    assert ppr.keepalive_path is None
    assert '[data-busy="true"]' in ppr.busy_selectors

    legacy = load_ui_profile("legacy_postback_portal")
    assert isinstance(legacy, UIProfileSpec)
    assert legacy.profile_id == "legacy_postback_portal"
    assert legacy.keepalive_path == "/api/keepalive"
    assert legacy.authenticated_landmark_selectors == (
        '[data-authenticated-shell="true"]',
        '[data-hms-ready="true"]',
    )

    # Custom JSON profile path loading
    custom_json = tmp_path / "custom_spa.json"
    custom_json.write_text(
        json.dumps(
            {
                "profile_id": "custom_spa",
                "debounce_grace_ms": 50,
                "settle_window_ms": 80,
                "max_settle_ms": 4000,
            }
        ),
        encoding="utf-8",
    )
    loaded_custom = load_ui_profile(custom_json)
    assert loaded_custom.profile_id == "custom_spa"
    assert loaded_custom.settle_window_ms == 80

    # Minimal YAML fallback parser when PyYAML is absent
    builtin_yaml_path = (
        Path(__file__).resolve().parent.parent
        / "openadapt_flow"
        / "form_tx"
        / "profiles"
        / "server_ppr_wizard.yaml"
    )
    fallback_parsed = _parse_minimal_yaml_subset(
        builtin_yaml_path.read_text(encoding="utf-8")
    )
    assert UIProfileSpec.model_validate(fallback_parsed) == ppr

    monkeypatch.setitem(sys.modules, "yaml", None)
    assert load_ui_profile("legacy_postback_portal") == legacy

    with pytest.raises(FileNotFoundError):
        load_ui_profile("nonexistent_profile_xyz")


def test_delayed_async_ppr_reload_reminted_ids_and_conditional_field(
    browser_context: BrowserContext,
) -> None:
    ppr_calls: list[dict[str, Any]] = []

    wizard_html = """<!doctype html>
<html>
<head><title>Intake Wizard - Step 1</title></head>
<body data-authenticated-shell="true" data-screen-id="step_coverage">
  <div id="busy-mask" data-busy="false" style="display:none;width:120px;height:30px;">
    Refreshing form...
  </div>
  <form id="wizard-form">
    <fieldset>
      <legend>Property Details</legend>
      <div class="form-row" id="occupancy-container">
        <label for="occupancy_101">Occupancy Type *</label>
        <select id="occupancy_101" name="occupancy_type" required>
          <option value="">-- Select --</option>
          <option value="primary">Primary Residence</option>
          <option value="rental">Tenant Occupied</option>
        </select>
      </div>
      <div class="form-row" id="postal-container">
        <label for="postal_101">Postal Code *</label>
        <input id="postal_101" type="text" value="" />
      </div>
      <div id="conditional-region"></div>
    </fieldset>
  </form>
  <script>
    let renderGen = 101;
    const busyMask = document.getElementById('busy-mask');

    async function triggerPpr(triggerField, newValue) {
      busyMask.style.display = 'block';
      busyMask.setAttribute('data-busy', 'true');

      const resp = await fetch('http://portal.local/api/ppr-refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field: triggerField, value: newValue, gen: renderGen }),
      });
      const payload = await resp.json();

      // Additional client-side delayed DOM swap so total PPR cycle exceeds 300ms
      await new Promise((r) => setTimeout(r, 160));
      renderGen = payload.next_gen;

      const currentZipEl = document.querySelector('#postal-container input');
      const preservedZip = currentZipEl ? currentZipEl.value : '';

      document.getElementById('postal-container').innerHTML = `
        <label for="postal_${renderGen}">Postal Code *</label>
        <input id="postal_${renderGen}" type="text" value="${preservedZip}" />
      `;
      bindPostalListener();

      if (payload.show_lease_term) {
        document.getElementById('conditional-region').innerHTML = `
          <div class="form-row">
            <label for="lease_months_${renderGen}">Minimum Lease Months *</label>
            <input id="lease_months_${renderGen}" name="lease_months" type="text" value="" />
          </div>
        `;
      }
      if (payload.coastal_surcharge_zone) {
        document.getElementById('conditional-region').insertAdjacentHTML('beforeend', `
          <div class="form-row">
            <label for="wind_deductible_${renderGen}">Wind Deductible Tier *</label>
            <input id="wind_deductible_${renderGen}" type="text" value="5%" readonly />
          </div>
        `);
      }

      busyMask.setAttribute('data-busy', 'false');
      busyMask.style.display = 'none';
    }

    function bindPostalListener() {
      const inp = document.querySelector('#postal-container input');
      inp.addEventListener('change', (ev) => {
        setTimeout(() => triggerPpr('postal_code', ev.target.value), 40);
      });
    }

    document.getElementById('occupancy_101').addEventListener('change', (ev) => {
      triggerPpr('occupancy_type', ev.target.value);
    });
    bindPostalListener();
  </script>
</body>
</html>"""

    gen_counter = {"val": 101}

    def handle_route(route: Route) -> None:
        url = route.request.url
        if url.startswith("http://portal.local/wizard"):
            route.fulfill(status=200, content_type="text/html", body=wizard_html)
            return
        if url == "http://portal.local/api/ppr-refresh":
            time.sleep(0.16)
            body = json.loads(route.request.post_data or "{}")
            ppr_calls.append(body)
            gen_counter["val"] += 1
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "next_gen": gen_counter["val"],
                        "show_lease_term": body.get("value") == "rental",
                        "coastal_surcharge_zone": str(body.get("value", "")).startswith(
                            "33"
                        ),
                    }
                ),
            )
            return
        route.fulfill(status=404, content_type="text/plain", body="not found")

    browser_context.route("http://portal.local/**", handle_route)
    page = browser_context.new_page()

    profile = load_ui_profile("server_ppr_wizard")
    surface = PlaywrightFormSurface(
        page,
        profile,
        base_origin="http://portal.local",
        ignored_mutation_paths=("/api/ppr-refresh",),
    )
    assert isinstance(surface, FormSurfaceBackend)

    surface.goto("/wizard/step1?sensitive_token=abc999#frag")
    snap1 = surface.snapshot()
    assert snap1.url_origin_and_path == "http://portal.local/wizard/step1"
    assert snap1.authenticated_landmark_present is True
    assert snap1.auth_wall_present is False
    assert snap1.screen_id == "step_coverage"

    occ_anchor = FieldAnchorSpec(label_patterns=(r"^Occupancy Type",))
    zip_anchor = FieldAnchorSpec(label_patterns=(r"^Postal Code",))
    lease_anchor = FieldAnchorSpec(label_patterns=(r"^Minimum Lease Months",))

    occ_ctrl = match_anchor_in_snapshot(occ_anchor, snap1)
    zip_ctrl_before = match_anchor_in_snapshot(zip_anchor, snap1)
    assert occ_ctrl is not None
    assert zip_ctrl_before is not None
    assert occ_ctrl.current_value == ""
    assert occ_ctrl.section_text == "Property Details"
    assert occ_ctrl.required is True
    assert zip_ctrl_before.id_attr == "postal_101"
    assert match_anchor_in_snapshot(lease_anchor, snap1) is None

    # Actuating occupancy triggers >300ms PPR reload + busy mask + ID re-mint + conditional field
    t0 = time.monotonic()
    surface.select_native_option(occ_ctrl.dom_selector, "rental")
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    assert elapsed_ms >= 300.0

    # Non-commit PPR endpoint was configured in ignored_mutation_paths:
    assert surface.mutation_count == 0
    assert surface.total_mutation_count == 1

    snap2 = surface.snapshot()
    zip_ctrl_after = match_anchor_in_snapshot(zip_anchor, snap2)
    lease_ctrl = match_anchor_in_snapshot(lease_anchor, snap2)
    assert zip_ctrl_after is not None
    assert zip_ctrl_after.id_attr == "postal_102"
    # Structural selector remains stable across re-minted dynamic IDs when no static ID exists
    assert zip_ctrl_after.dom_selector == zip_ctrl_before.dom_selector
    assert lease_ctrl is not None
    assert lease_ctrl.required is True

    # Fill postal code -> debounced 40ms + >300ms PPR reload materializes readonly output
    surface.fill_text(zip_ctrl_after.dom_selector, "33139")
    snap3 = surface.snapshot()
    zip_ctrl_final = match_anchor_in_snapshot(zip_anchor, snap3)
    assert zip_ctrl_final is not None
    assert zip_ctrl_final.id_attr == "postal_103"
    assert zip_ctrl_final.current_value == "33139"

    wind_anchor = FieldAnchorSpec(label_patterns=(r"^Wind Deductible Tier",))
    assert match_anchor_in_snapshot(wind_anchor, snap3, include_readonly=False) is None
    wind_ctrl = match_anchor_in_snapshot(wind_anchor, snap3, include_readonly=True)
    assert wind_ctrl is not None
    assert wind_ctrl.readonly is True
    assert wind_ctrl.current_value == "5%"


def test_body_portaled_async_combobox_options_and_selection(
    browser_context: BrowserContext,
) -> None:
    combo_html = """<!doctype html>
<html>
<head><title>Portal Combobox Surface</title></head>
<body data-authenticated-shell="true">
  <section role="region" aria-label="Routing Preferences">
    <div class="field">
      <label id="dept-lbl">Receiving Department *</label>
      <button
        id="dept-combobox"
        type="button"
        role="combobox"
        aria-labelledby="dept-lbl"
        aria-expanded="false"
        aria-required="true"
        data-selected-value=""
      >
        Select...
      </button>
    </div>
  </section>
  <script>
    const trigger = document.getElementById('dept-combobox');
    trigger.addEventListener('click', async () => {
      if (trigger.getAttribute('aria-expanded') === 'true') return;
      const resp = await fetch('http://portal.local/api/departments');
      const items = await resp.json();
      const ul = document.createElement('ul');
      ul.id = 'detached-dept-listbox';
      ul.setAttribute('role', 'listbox');
      ul.style.position = 'fixed';
      ul.style.top = '100px';
      ul.style.left = '40px';
      ul.style.width = '240px';
      for (const item of items) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.setAttribute('data-option-value', item.code);
        li.style.padding = '6px';
        li.textContent = item.label;
        li.addEventListener('click', () => {
          trigger.setAttribute('data-selected-value', item.code);
          trigger.textContent = item.label;
          trigger.setAttribute('aria-expanded', 'false');
          ul.remove();
        });
        ul.appendChild(li);
      }
      document.body.appendChild(ul);
      trigger.setAttribute('aria-controls', ul.id);
      trigger.setAttribute('aria-expanded', 'true');
    });
  </script>
</body>
</html>"""

    def handle_route(route: Route) -> None:
        if route.request.url == "http://portal.local/combo":
            route.fulfill(status=200, content_type="text/html", body=combo_html)
        elif route.request.url == "http://portal.local/api/departments":
            time.sleep(0.08)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    [
                        {"code": "CARD", "label": "Cardiology"},
                        {"code": "NEUR", "label": "Neurology"},
                        {"code": "ORTH", "label": "Orthopedics"},
                    ]
                ),
            )
        else:
            route.fulfill(status=404, body="not found")

    browser_context.route("http://portal.local/**", handle_route)
    page = browser_context.new_page()
    surface = PlaywrightFormSurface(page, load_ui_profile("server_ppr_wizard"))
    surface.goto("http://portal.local/combo")

    snap_before = surface.snapshot()
    combo_ctrl = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"Receiving Department",), role="combobox"),
        snap_before,
    )
    assert combo_ctrl is not None
    assert combo_ctrl.current_value == ""
    assert combo_ctrl.section_text == "Routing Preferences"
    assert combo_ctrl.required is True

    options = surface.open_portal_combobox_options(combo_ctrl.dom_selector)
    assert options == (
        ObservedOption(value="CARD", label="Cardiology"),
        ObservedOption(value="NEUR", label="Neurology"),
        ObservedOption(value="ORTH", label="Orthopedics"),
    )

    surface.choose_portal_combobox_option(
        combo_ctrl.dom_selector, ObservedOption(value="NEUR", label="Neurology")
    )
    snap_after = surface.snapshot()
    updated_ctrl = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"Receiving Department",), role="combobox"),
        snap_after,
    )
    assert updated_ctrl is not None
    assert updated_ctrl.current_value == "NEUR"


def test_child_popup_window_fill_postmessage_close_and_contract_guard(
    browser_context: BrowserContext,
) -> None:
    parent_html = """<!doctype html>
<html>
<head><title>Main Application Worksheet</title></head>
<body data-authenticated-shell="true">
  <h2>Coverage Estimation</h2>
  <div class="field">
    <label for="estimated-amount">Estimated Replacement Amount</label>
    <input id="estimated-amount" name="estimated_amount" type="text" value="" readonly />
  </div>
  <button id="open-estimator" type="button">Launch Estimator</button>
  <button id="open-wrong-popup" type="button">Launch Unrelated Popup</button>
  <script>
    window.addEventListener('message', (event) => {
      if (event.data && event.data.kind === 'ESTIMATE_COMMITTED') {
        setTimeout(() => {
          document.getElementById('estimated-amount').value = event.data.formatted;
        }, 50);
      }
    });
    document.getElementById('open-estimator').addEventListener('click', () => {
      window.open('http://portal.local/popup/estimator?session=xyz', 'estimatorWin', 'width=600,height=500');
    });
    document.getElementById('open-wrong-popup').addEventListener('click', () => {
      window.open('http://portal.local/popup/help', 'helpWin', 'width=500,height=400');
    });
  </script>
</body>
</html>"""

    popup_html = """<!doctype html>
<html>
<head><title>Replacement Cost Estimator</title></head>
<body>
  <h1>Estimator Tool</h1>
  <label for="sqft-input">Living Area SqFt *</label>
  <input id="sqft-input" name="sqft" type="text" value="" />
  <label for="rate-input">Cost Per SqFt *</label>
  <input id="rate-input" name="rate" type="text" value="250" />
  <button id="apply-estimate" type="button">Apply &amp; Return</button>
  <script>
    document.getElementById('apply-estimate').addEventListener('click', () => {
      const sqft = Number(document.getElementById('sqft-input').value || 0);
      const rate = Number(document.getElementById('rate-input').value || 0);
      const total = sqft * rate;
      window.opener.postMessage(
        { kind: 'ESTIMATE_COMMITTED', formatted: '$' + total.toLocaleString('en-US') + '.00' },
        '*'
      );
      window.close();
    });
  </script>
</body>
</html>"""

    help_html = """<!doctype html>
<html><head><title>General Help</title></head><body><p>Help page</p></body></html>"""

    def handle_route(route: Route) -> None:
        url = route.request.url
        if url.startswith("http://portal.local/worksheet"):
            route.fulfill(status=200, content_type="text/html", body=parent_html)
        elif url.startswith("http://portal.local/popup/estimator"):
            route.fulfill(status=200, content_type="text/html", body=popup_html)
        elif url.startswith("http://portal.local/popup/help"):
            route.fulfill(status=200, content_type="text/html", body=help_html)
        else:
            route.fulfill(status=404, body="not found")

    browser_context.route("http://portal.local/**", handle_route)
    page = browser_context.new_page()
    surface = PlaywrightFormSurface(page, load_ui_profile("server_ppr_wizard"))
    surface.goto("http://portal.local/worksheet")

    # 1. Fail-closed PopupContractError on mismatched URL/title, closing child popup
    with pytest.raises(PopupContractError):
        with surface.open_popup_surface(
            "#open-wrong-popup",
            url_pattern=r"/popup/estimator$",
            title_pattern=r"Replacement Cost Estimator",
        ):
            pytest.fail("Mismatched popup must not yield child surface")
    assert len(browser_context.pages) == 1

    # 2. Valid popup subroutine: fill, postMessage back to parent, window.close(), parent read-back
    with surface.open_popup_surface(
        "#open-estimator",
        url_pattern=r"/popup/estimator$",
        title_pattern=r"Estimator",
    ) as popup_surface:
        assert isinstance(popup_surface, FormSurfaceBackend)
        pop_snap = popup_surface.snapshot()
        assert pop_snap.url_origin_and_path == "http://portal.local/popup/estimator"
        sqft_ctrl = match_anchor_in_snapshot(
            FieldAnchorSpec(label_patterns=(r"Living Area SqFt",)), pop_snap
        )
        apply_btn = match_anchor_in_snapshot(
            FieldAnchorSpec(label_patterns=(r"Apply & Return",)), pop_snap
        )
        assert sqft_ctrl is not None
        assert apply_btn is not None
        popup_surface.fill_text(sqft_ctrl.dom_selector, "1940")
        popup_surface.click(apply_btn.dom_selector)

    parent_snap = surface.snapshot()
    amount_ctrl = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"Estimated Replacement Amount",)),
        parent_snap,
        include_readonly=True,
    )
    assert amount_ctrl is not None
    assert amount_ctrl.current_value == "$485,000.00"
    assert len(browser_context.pages) == 1


def test_secret_redaction_and_snapshot_entities_modals_errors(
    browser_context: BrowserContext,
) -> None:
    security_html = """<!doctype html>
<html>
<head><title>Verification &amp; Household Roster</title></head>
<body data-hms-ready="true">
  <div role="alert" class="error-banner">Please review inline errors before continuing.</div>
  <form>
    <label for="user-email">Operator Email</label>
    <input id="user-email" name="operator_email" type="email" value="ops@example.org" />

    <label for="acct-pwd">Session Password</label>
    <input id="acct-pwd" name="account_password" type="password" value="SuperSecret!99" />

    <label for="mfa-otp">Verification Code</label>
    <input id="mfa-otp" name="mfa_input" type="text" autocomplete="one-time-code" value="849201" />

    <label for="api-sec">Signing Token</label>
    <input id="api-sec" name="clientSecret" type="text" value="tok_live_hidden_value" />

    <div class="form-group">
      <label for="dob-field">Birth Date *</label>
      <input id="dob-field" name="birth_date" type="text" value="99/99/9999" aria-invalid="true" />
      <span class="field-error">Invalid calendar date</span>
    </div>

    <label><input id="agree-terms" name="agree_terms" type="checkbox" /> Accept Terms</label>
  </form>

  <table id="roster-table">
    <tbody>
      <tr class="member-row">
        <td class="col-name">Jordan Rivera</td>
        <td class="col-role">Primary</td>
        <td><button type="button" class="dismiss-row" disabled>Remove</button></td>
      </tr>
      <tr class="member-row">
        <td class="col-name">Taylor Rivera</td>
        <td class="col-role">Dependent</td>
        <td><button type="button" class="dismiss-row">Remove</button></td>
      </tr>
    </tbody>
  </table>

  <!-- Closed native dialog must NOT appear in active_modals -->
  <dialog id="closed-dialog">
    <p>Inactive hidden dialog</p>
    <button type="button" id="closed-confirm">OK</button>
  </dialog>

  <!-- Open native dialog MUST appear in active_modals -->
  <dialog id="dup-warning-dialog" open style="display:block;width:300px;height:120px;">
    <p class="modal-msg">Potential duplicate record detected.</p>
    <button type="button" id="confirm-dup">Proceed Anyway</button>
  </dialog>
</body>
</html>"""

    browser_context.route(
        "http://portal.local/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=security_html
        ),
    )
    page = browser_context.new_page()
    surface = PlaywrightFormSurface(page, load_ui_profile("legacy_postback_portal"))
    surface.goto("http://portal.local/roster?ssn=000-00-0000")

    snap = surface.snapshot(
        entity_tables=(
            EntityTableSpec(
                table_id="household_roster",
                entity_kind="member",
                row_selector="#roster-table tr.member-row",
                column_selectors={"full_name": ".col-name", "role": ".col-role"},
                key_fields=("full_name",),
                dismiss_button_selector="button.dismiss-row",
            ),
        ),
        modal_rules=(
            ModalRuleSpec(
                modal_id="duplicate_warning",
                container_selector="#dup-warning-dialog",
                message_selector=".modal-msg",
                confirm_selector="#confirm-dup",
            ),
            ModalRuleSpec(
                modal_id="inactive_dialog",
                container_selector="#closed-dialog",
                confirm_selector="#closed-confirm",
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="roster_step",
                title_pattern=r"Household Roster",
            ),
        ),
    )

    assert snap.url_origin_and_path == "http://portal.local/roster"
    assert snap.authenticated_landmark_present is True
    assert snap.auth_wall_present is True
    assert snap.screen_id == "roster_step"
    assert snap.banner_errors == ("Please review inline errors before continuing.",)

    by_id = {c.id_attr: c for c in snap.controls if c.id_attr}
    assert by_id["user-email"].current_value == "ops@example.org"
    assert by_id["user-email"].is_secret is False

    # Secret inputs are scrubbed to <secret> and never leak raw credentials/OTPs
    for secret_id in ("acct-pwd", "mfa-otp", "api-sec"):
        ctrl = by_id[secret_id]
        assert ctrl.is_secret is True
        assert ctrl.current_value == "<secret>"

    serialized = snap.model_dump_json()
    assert "SuperSecret!99" not in serialized
    assert "849201" not in serialized
    assert "tok_live_hidden_value" not in serialized
    assert "000-00-0000" not in serialized

    assert by_id["dob-field"].error_text == "Invalid calendar date"

    assert len(snap.entity_rows) == 2
    assert snap.entity_rows[0].values == {
        "full_name": "Jordan Rivera",
        "role": "Primary",
    }
    assert snap.entity_rows[0].has_dismiss_action is False
    assert snap.entity_rows[1].values == {
        "full_name": "Taylor Rivera",
        "role": "Dependent",
    }
    assert snap.entity_rows[1].has_dismiss_action is True

    assert len(snap.active_modals) == 1
    assert snap.active_modals[0].modal_id == "duplicate_warning"
    assert snap.active_modals[0].message_text == "Potential duplicate record detected."

    # Verify checkbox actuation
    assert by_id["agree-terms"].checked is False
    surface.set_checkbox_or_radio(by_id["agree-terms"].dom_selector, checked=True)
    snap2 = surface.snapshot()
    by_id_2 = {c.id_attr: c for c in snap2.controls if c.id_attr}
    assert by_id_2["agree-terms"].checked is True
    assert by_id_2["agree-terms"].current_value == "true"


def test_ping_keepalive_get_success_and_fail_closed_mutation_guard(
    browser_context: BrowserContext,
) -> None:
    shell_html = """<!doctype html>
<html>
<head><title>Authenticated Portal Shell</title></head>
<body data-authenticated-shell="true">
  <h1>Session Active</h1>
  <script>
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function(input, init) {
      const resp = await nativeFetch(input, init);
      const urlStr = typeof input === 'string' ? input : input.url;
      if (urlStr.includes('/api/keepalive-mutating')) {
        // Simulate misconfigured portal middleware issuing a POST subrequest
        await nativeFetch('http://portal.local/api/session/touch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ event: 'keepalive_side_effect' }),
        });
      }
      return resp;
    };
  </script>
</body>
</html>"""

    def handle_route(route: Route) -> None:
        url = route.request.url
        if url == "http://portal.local/shell":
            route.fulfill(status=200, content_type="text/html", body=shell_html)
        elif url == "http://portal.local/api/keepalive":
            assert route.request.method == "GET"
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"alive": true}',
            )
        elif url == "http://portal.local/api/keepalive-mutating":
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"alive": true, "touch": true}',
            )
        elif url == "http://portal.local/api/session/touch":
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"touched": true}',
            )
        else:
            route.fulfill(status=404, body="not found")

    browser_context.route("http://portal.local/**", handle_route)
    page = browser_context.new_page()
    profile = load_ui_profile("legacy_postback_portal")
    # Even if /api/session/touch is listed in ignored_mutation_paths, ping_keepalive
    # enforces strict zero-mutation!
    surface = PlaywrightFormSurface(
        page,
        profile,
        ignored_mutation_paths=("/api/session/touch",),
    )
    surface.goto("http://portal.local/shell")

    assert profile.keepalive_path == "/api/keepalive"
    status = surface.ping_keepalive(profile.keepalive_path)
    assert status == 200
    assert surface.mutation_count == 0
    assert surface.total_mutation_count == 0

    expired_status = surface.ping_keepalive("/api/unauthenticated-ping")
    assert expired_status == 404
    assert surface.mutation_count == 0

    with pytest.raises(KeepaliveMutationError, match="violated zero-mutation contract"):
        surface.ping_keepalive("/api/keepalive-mutating")
    assert surface.total_mutation_count == 1


def test_mutation_counter_reset_radio_textarea_and_canvas_quiescence_fallback(
    browser_context: BrowserContext,
) -> None:
    canvas_form_html = """<!doctype html>
<html>
<head><title>Signature &amp; Billing Step</title></head>
<body data-authenticated-shell="true">
  <div id="step-landmark" data-step="billing">Billing Step</div>
  <!-- Visible canvas forces PageQuiescence.probe() to return None, exercising DOM-stability fallback -->
  <canvas id="sig-pad" width="160" height="60" style="border:1px solid #333;"></canvas>
  <fieldset>
    <legend>Billing Cadence</legend>
    <label><input type="radio" name="cadence" value="monthly" checked /> Monthly</label>
    <label><input type="radio" name="cadence" value="annual" /> Annual</label>
  </fieldset>
  <label for="notes-box">Underwriting Notes</label>
  <textarea id="notes-box" name="notes"></textarea>
  <button id="save-draft" type="button">Save Draft</button>
  <script>
    document.getElementById('save-draft').addEventListener('click', async () => {
      await fetch('http://portal.local/api/drafts', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notes: document.getElementById('notes-box').value }),
      });
    });
  </script>
</body>
</html>"""

    def handle_route(route: Route) -> None:
        if route.request.url == "http://portal.local/billing":
            route.fulfill(status=200, content_type="text/html", body=canvas_form_html)
        elif route.request.url == "http://portal.local/api/drafts":
            route.fulfill(
                status=200, content_type="application/json", body='{"saved": true}'
            )
        else:
            route.fulfill(status=404, body="not found")

    browser_context.route("http://portal.local/**", handle_route)
    page = browser_context.new_page()
    surface = PlaywrightFormSurface(page, load_ui_profile("server_ppr_wizard"))
    surface.goto("http://portal.local/billing")

    snap = surface.snapshot(
        screens=(
            WizardScreenSpec(
                screen_id="billing_screen",
                landmark_selector='#step-landmark[data-step="billing"]',
            ),
        )
    )
    assert snap.screen_id == "billing_screen"

    annual_radio = match_anchor_in_snapshot(
        FieldAnchorSpec(
            section_patterns=(r"Billing Cadence",), label_patterns=(r"^Annual$",)
        ),
        snap,
    )
    notes_ctrl = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"Underwriting Notes",)), snap
    )
    save_btn = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"^Save Draft$",)), snap
    )
    assert annual_radio is not None
    assert notes_ctrl is not None
    assert save_btn is not None
    assert annual_radio.checked is False

    surface.set_checkbox_or_radio(annual_radio.dom_selector, checked=True)
    surface.fill_text(notes_ctrl.dom_selector, "Verified inspection report on file.")
    surface.click(save_btn.dom_selector)

    assert surface.mutation_count == 1
    surface.reset_mutation_counter()
    assert surface.mutation_count == 0
    assert surface.total_mutation_count == 0

    snap_after = surface.snapshot()
    annual_after = match_anchor_in_snapshot(
        FieldAnchorSpec(
            section_patterns=(r"Billing Cadence",), label_patterns=(r"^Annual$",)
        ),
        snap_after,
    )
    notes_after = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(r"Underwriting Notes",)), snap_after
    )
    assert annual_after is not None and annual_after.checked is True
    assert (
        notes_after is not None
        and notes_after.current_value == "Verified inspection report on file."
    )


def test_quiescence_timeout_fails_closed_when_busy_never_clears(
    browser_context: BrowserContext,
) -> None:
    stuck_html = """<!doctype html>
<html>
<head><title>Stuck Busy Surface</title></head>
<body>
  <div data-busy="true" style="width:100px;height:40px;">Still processing...</div>
</body>
</html>"""

    browser_context.route(
        "http://portal.local/stuck",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=stuck_html
        ),
    )
    page = browser_context.new_page()
    fast_timeout_profile = UIProfileSpec(
        profile_id="fast_timeout_test",
        debounce_grace_ms=20,
        settle_window_ms=50,
        max_settle_ms=250,
    )
    surface = PlaywrightFormSurface(page, fast_timeout_profile)
    with pytest.raises(SurfaceSettleTimeoutError):
        surface.goto("http://portal.local/stuck")
