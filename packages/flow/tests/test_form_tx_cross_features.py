"""Headless Chromium tests that combine the form-layer surface features.

One quote form inside a same-origin iframe, its controls inside an open
shadow root: a text field, a type-ahead that fetches suggestions only after
typed text, and a Yes/No radio group. After submit the frame navigates to a
summary shown as page text in another shadow root. Each feature has its own
tests; these check that they work together. Pages are served through
``context.route``; no sockets, no app names.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.types import (  # noqa: E402
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    InputDossier,
    OutputExtractionSpec,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://quote.local"

SUGGESTIONS = [
    {"value": "slm", "label": "Salem"},
    {"value": "spr1", "label": "Springfield"},
    {"value": "spr2", "label": "Springfield"},
]

SHELL = """<!doctype html><html><body><h1>Agent desk</h1>
<iframe src="/app/quote" style="width:900px;height:700px"></iframe>
</body></html>"""

QUOTE = """<!doctype html><html><body>
<quote-form id="qf"></quote-form>
<script>
class QuoteForm extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `
      <form id="f" onsubmit="return false">
        <label for="first">First name</label><input id="first" name="first">
        <label for="city">City</label>
        <input id="city" name="city" role="combobox" aria-autocomplete="list"
               aria-controls="city-list" autocomplete="off">
        <ul id="city-list" role="listbox" hidden></ul>
        <fieldset><legend>Monitored alarm</legend>
          <label><input type="radio" name="alarm" value="yes"> Yes</label>
          <label><input type="radio" name="alarm" value="no"> No</label>
        </fieldset>
        <button id="submit" type="button">Submit quote</button>
      </form>`;
    const city = root.getElementById('city');
    const list = root.getElementById('city-list');
    let timer = null;
    city.addEventListener('input', () => {
      clearTimeout(timer);
      list.hidden = true;
      const q = city.value;
      if (q.length < 2) return;
      timer = setTimeout(async () => {
        const items = await (await fetch('/api/suggest?q=' + encodeURIComponent(q))).json();
        list.innerHTML = '';
        for (const it of items) {
          const li = document.createElement('li');
          li.setAttribute('role', 'option');
          li.setAttribute('data-option-value', it.value);
          li.id = 'opt-' + it.value;
          li.textContent = it.label;
          li.addEventListener('click', () => { city.value = it.label; list.hidden = true; });
          list.appendChild(li);
        }
        list.hidden = items.length === 0;
      }, 120);
    });
    root.getElementById('submit').addEventListener('click', async () => {
      const alarm = root.querySelector('input[name=alarm]:checked');
      const body = new URLSearchParams({
        first: root.getElementById('first').value,
        city: city.value,
        alarm: alarm ? alarm.value : '',
      });
      await fetch('/api/submit', {method: 'POST', body});
      location.href = '/app/summary';
    });
  }
}
customElements.define('quote-form', QuoteForm);
</script></body></html>"""

SUMMARY = """<!doctype html><html><body><h1>Quote saved</h1>
<quote-summary></quote-summary>
<script>
class QuoteSummary extends HTMLElement {
  async connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    const rec = await (await fetch('/api/last')).json();
    const rows = [['Quote number', 'Q-' + rec.n], ['First name', rec.first],
                  ['City', rec.city], ['Monitored alarm', rec.alarm === 'yes' ? 'Yes' : 'No']];
    root.innerHTML = '<dl id="summary">' + rows.map(
      ([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';
  }
}
customElements.define('quote-summary', QuoteSummary);
</script></body></html>"""


class _Portal:
    def __init__(self, show_city: str | None = None) -> None:
        self.show_city = show_city
        self.submissions: list[dict[str, str]] = []

    def handle(self, route: Any) -> None:
        req = route.request
        parts = urlsplit(req.url)
        path = parts.path
        if path == "/desk":
            route.fulfill(status=200, content_type="text/html", body=SHELL)
        elif path == "/app/quote":
            route.fulfill(status=200, content_type="text/html", body=QUOTE)
        elif path == "/app/summary":
            route.fulfill(status=200, content_type="text/html", body=SUMMARY)
        elif path == "/api/suggest":
            q = parse_qs(parts.query).get("q", [""])[0].lower()
            hits = [s for s in SUGGESTIONS if s["label"].lower().startswith(q)]
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(hits)
            )
        elif path == "/api/submit" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
            self.submissions.append(form)
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path == "/api/last":
            rec = dict(self.submissions[-1]) if self.submissions else {}
            if self.show_city is not None:
                rec["city"] = self.show_city
            rec["n"] = str(1000 + len(self.submissions))
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(rec)
            )
        else:
            route.fulfill(status=404, body="not found")


def _profile() -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="cross-feature-test",
        debounce_grace_ms=40,
        settle_window_ms=80,
        max_settle_ms=8000,
        frame_path_patterns=(r"/app/.*",),
    )


def _pack() -> FormPack:
    first = FieldBindingSpec(
        binding_id="first",
        dossier_key="applicant.first_name",
        anchor=FieldAnchorSpec(label_patterns=("^First name$",)),
    )
    city = FieldBindingSpec(
        binding_id="city",
        dossier_key="property.city",
        anchor=FieldAnchorSpec(label_patterns=("^City$",)),
        control_kind=ControlKind.PORTAL_COMBOBOX,
        codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
    )
    alarm = FieldBindingSpec(
        binding_id="alarm",
        dossier_key="property.monitored_alarm",
        anchor=FieldAnchorSpec(name_or_id_patterns=("^alarm$",)),
        control_kind=ControlKind.RADIO_GROUP,
        codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
    )
    readbacks = tuple(
        b.model_copy(update={"anchor": FieldAnchorSpec(label_patterns=(f"^{label}$",))})
        for b, label in (
            (first, "First name"),
            (city, "City"),
            (alarm, "Monitored alarm"),
        )
    )
    return FormPack(
        pack_id="cross-feature-test",
        domain_schema="test.quote/v1",
        ui_profile_id="cross-feature-test",
        bindings=(first, city, alarm),
        screens=(
            WizardScreenSpec(
                screen_id="quote",
                landmark_selector="#f",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Submit quote$",)
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#summary",
            field_readbacks=readbacks,
            outputs=(
                OutputExtractionSpec(
                    output_key="quote.number",
                    anchor=FieldAnchorSpec(label_patterns=("^Quote number$",)),
                ),
            ),
        ),
    )


def _dossier(city: str) -> InputDossier:
    values = {
        "applicant.first_name": "Marisol",
        "property.city": city,
        "property.monitored_alarm": "Yes",
    }
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


def _run(browser: Any, portal: _Portal, city: str) -> Any:
    context = browser.new_context()
    try:
        context.route(f"{ORIGIN}/**", portal.handle)
        page = context.new_page()
        page.goto(f"{ORIGIN}/desk")
        page.wait_for_load_state()
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        reconciler = FormReconciler(surface, signing_key=Ed25519PrivateKey.generate())
        session = reconciler.create_session(_pack(), _dossier(city))
        return reconciler.run_until_halt_or_done(session)
    finally:
        context.close()


def test_iframe_shadow_form_with_typeahead_and_radios_verifies_from_page_text(
    browser: Any,
) -> None:
    portal = _Portal()
    result = _run(browser, portal, "Salem")
    assert portal.submissions == [{"first": "Marisol", "city": "Salem", "alarm": "yes"}]
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened
    verdicts = {v.dossier_key: v for v in result.summary_verdict.field_verdicts}
    for key in ("applicant.first_name", "property.city", "property.monitored_alarm"):
        assert verdicts[key].status == ReadbackStatus.PASSED, verdicts[key].reason
    assert result.summary_verdict.extracted_outputs["quote.number"] == "Q-1001"
    assert result.receipt is not None


def test_summary_text_that_differs_is_refuted_in_frame_and_shadow_root(
    browser: Any,
) -> None:
    portal = _Portal(show_city="Salem Heights")
    result = _run(browser, portal, "Salem")
    assert len(portal.submissions) == 1
    assert result.outcome == FormTxOutcome.READBACK_REFUTED, result.what_happened
    verdicts = {v.dossier_key: v for v in result.summary_verdict.field_verdicts}
    assert verdicts["property.city"].status == ReadbackStatus.FAILED
    assert result.receipt is None


def test_ambiguous_typeahead_in_frame_and_shadow_root_stops_before_submit(
    browser: Any,
) -> None:
    portal = _Portal()
    result = _run(browser, portal, "Springfield")
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert result.halts[0].field_label == "City"
