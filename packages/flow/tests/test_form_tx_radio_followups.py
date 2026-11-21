"""Headless Chromium tests: checkboxes and radios on pages that re-render.

A form that rebuilds every control with new ids on each change; native
radios labelled only by the text after them; an ARIA radiogroup whose radios
are named by their own text; a select whose placeholder is "Select one"; and
a checkbox that refuses to change. Pages are served through
``context.route``; no sockets, no app names.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.surface import match_anchor_in_snapshot  # noqa: E402
from openadapt_flow.form_tx.types import (  # noqa: E402
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    InputDossier,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://rerender.local"

PAGE = """<!doctype html><html><body><h1>Quote</h1><div id="app"></div>
<script>
const state = {alarm: '', pool: false, garage: '', roof: ''};
const rid = () => 'x' + Math.random().toString(36).slice(2, 9).replace(/[0-9]/g, 'q');
function render() {
  const p = rid(), r = rid(), g = rid();
  document.getElementById('app').innerHTML = `
    <form onsubmit="return false">
      <fieldset><legend>Monitored alarm</legend>
        <input type="radio" name="alarm" value="yes" ${state.alarm === 'yes' ? 'checked' : ''}> Yes
        <input type="radio" name="alarm" value="no" ${state.alarm === 'no' ? 'checked' : ''}> No
      </fieldset>
      <label for="${p}">Has pool</label>
      <input type="checkbox" id="${p}" name="pool" ${state.pool ? 'checked' : ''} ${LOCKED ? 'data-locked="1"' : ''}>
      <span id="${g}">Garage</span>
      <div role="radiogroup" aria-labelledby="${g}">
        <div role="radio" tabindex="0" aria-checked="${state.garage === 'Attached'}">Attached</div>
        <div role="radio" tabindex="0" aria-checked="${state.garage === 'Detached'}">Detached</div>
      </div>
      <label for="${r}">Roof</label>
      <select id="${r}" name="roof">
        <option value="">Select one</option>
        <option value="metal" ${state.roof === 'metal' ? 'selected' : ''}>Metal</option>
        <option value="tile" ${state.roof === 'tile' ? 'selected' : ''}>Tile</option>
      </select>
      <button type="button" id="go">Submit quote</button>
    </form>`;
  const app = document.getElementById('app');
  app.querySelectorAll('input[name=alarm]').forEach((el) => el.addEventListener('change',
    () => { state.alarm = el.value; render(); }));
  const pool = app.querySelector('input[name=pool]');
  pool.addEventListener('click', (ev) => {
    if (pool.dataset.locked) { ev.preventDefault(); return; }
    state.pool = pool.checked; setTimeout(render, 30);
  });
  app.querySelectorAll('[role=radio]').forEach((el) => el.addEventListener('click',
    () => { state.garage = el.textContent.trim(); render(); }));
  app.querySelector('select').addEventListener('change',
    (ev) => { state.roof = ev.target.value; render(); });
  app.querySelector('#go').addEventListener('click', async () => {
    await fetch('/api/submit', {method: 'POST', body: JSON.stringify(state)});
    document.body.innerHTML = '<h1>Saved</h1>';
  });
}
render();
</script></body></html>"""


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel="chromium", headless=True)
        except Exception:
            b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _open(browser: Any, locked: bool = False) -> tuple[Any, Any, list[dict[str, Any]]]:
    submissions: list[dict[str, Any]] = []
    body = PAGE.replace("<script>", f"<script>const LOCKED = {str(locked).lower()};", 1)

    def handle(route: Any) -> None:
        path = urlsplit(route.request.url).path
        if path == "/api/submit":
            submissions.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    ctx = browser.new_context()
    ctx.route(f"{ORIGIN}/**", handle)
    page = ctx.new_page()
    profile = UIProfileSpec(
        profile_id="rerender-test", debounce_grace_ms=40, settle_window_ms=80,
        max_settle_ms=4000,
    )
    surface = PlaywrightFormSurface(page, profile, base_origin=ORIGIN)
    surface.goto(f"{ORIGIN}/quote")
    return ctx, surface, submissions


def _binding(key: str, label: str, kind: ControlKind) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=key,
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
    )


def _pack() -> FormPack:
    return FormPack(
        pack_id="rerender-test",
        domain_schema="test.quote/v1",
        ui_profile_id="rerender-test",
        bindings=(
            _binding("alarm", "Monitored alarm", ControlKind.RADIO_GROUP),
            _binding("pool", "Has pool", ControlKind.CHECKBOX).model_copy(
                update={"codec": FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)}
            ),
            _binding("garage", "Garage", ControlKind.RADIO_GROUP),
            _binding("roof", "Roof", ControlKind.SELECT),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="quote",
                landmark_selector="form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Submit quote$",)
                ),
            ),
        ),
    )


def _dossier() -> InputDossier:
    values = {"alarm": "No", "pool": "Yes", "garage": "Detached", "roof": "Metal"}
    return InputDossier(
        dossier_id="d-1",
        request_id="r-1",
        facts={
            k: DossierFact(
                key=k, value=v, provenance=ProvenanceKind.INTAKE_DOCUMENT, source_id="t"
            )
            for k, v in values.items()
        },
    )


def test_labels_groups_and_placeholders_read_as_a_person_sees_them(
    browser: Any,
) -> None:
    ctx, surface, _ = _open(browser)
    try:
        snap = surface.snapshot()
        native = [c for c in snap.controls if c.input_type == "radio"]
        assert [c.label_text for c in native] == ["Yes", "No"]
        assert all(c.group_label == "Monitored alarm" for c in native)
        assert all(c.current_value == "" for c in native)
        aria = [c for c in snap.controls if c.role == "radio"]
        assert [c.label_text for c in aria] == ["Attached", "Detached"]
        assert all(c.group_label == "Garage" and c.current_value == "" for c in aria)
        assert [o.value for o in aria[0].options] == ["Attached", "Detached"]
        roof = next(c for c in snap.controls if c.tag == "select")
        assert roof.current_value == ""
        garage = match_anchor_in_snapshot(
            FieldAnchorSpec(label_patterns=("^Garage$",)), snap
        )
        assert garage is not None and garage.role == "radio"
    finally:
        ctx.close()


def test_controls_rebuilt_on_every_change_are_set_and_submitted(
    browser: Any,
) -> None:
    ctx, surface, submissions = _open(browser)
    try:
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(_pack(), _dossier())
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
    assert not result.halts, [h.message for h in result.halts]
    assert submissions == [
        {"alarm": "no", "pool": True, "garage": "Detached", "roof": "metal"}
    ]


def test_checkbox_that_does_not_change_stops_before_submit(browser: Any) -> None:
    ctx, surface, submissions = _open(browser, locked=True)
    try:
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(_pack(), _dossier())
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
    assert submissions == []
    assert result.halts
    assert result.halts[0].kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert "did not become checked" in result.halts[0].message
