"""Second held-out run rework: re-check after a sign-in, unbound controls, named stops.

Real headless Chromium against small pages served in-test by the standard
library. Each page is plain HTML and JavaScript; ``POST /api/<name>`` stores
the JSON body, so the stored record is what the layer's read-back is judged
against.

- A sign-in in the middle that resets earlier answers: the layer walks back
  through the screens and re-fills them before submit, or stops when the pack
  gives no way back.
- Controls no binding covers: free text holding a value stops, other values
  keep the run from ``verified_done``, pack-declared ones are named on the
  receipt, and an intake key that matches an empty unbound field stops.
- Stops name the field: a "Required" marker next to it, an error summary, an
  alert inside its box, and a radio group's own name. A pack-declared blank
  default is written over.
- A "Select" button chosen by its row's cell, a date in three boxes read back
  as one value, a quote number in a sentence, and an output shown as
  "Label: value".
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    InputDossier,
    OutputExtractionSpec,
    PrefillPolicy,
    ProvenanceKind,
    ReadbackStatus,
    SplitDateReadbackSpec,
    SummaryCheckSpec,
    UIProfileSpec,
    UnboundControlSpec,
    WizardScreenSpec,
)

_PROFILE = UIProfileSpec(profile_id="test-rework-gaps", max_settle_ms=8000)
_MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")


class _Site:
    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.records: list[dict[str, Any]] = []


def _handler(site: _Site) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            html = site.pages.get(path)
            if html is None:
                self._send(b"not found", "text/plain", 404)
                return
            doc = (
                "<!doctype html><html><head><title>Quote</title></head><body>"
                + html
                + "</body></html>"
            )
            self._send(doc.encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            if self.path.startswith("/api/submit"):
                site.records.append(body)
            self._send(b'{"ok": true}', "application/json")

    return Handler


@pytest.fixture()
def site() -> Iterator[tuple[_Site, str]]:
    s = _Site()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield s, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture()
def browser() -> Iterator[Any]:
    sync_pw = pytest.importorskip("playwright.sync_api")
    with sync_pw.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _dossier(**facts: Any) -> InputDossier:
    return InputDossier(
        dossier_id="d1",
        request_id="r1",
        facts={
            k: DossierFact(
                key=k,
                value=v,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake",
            )
            for k, v in facts.items()
        },
    )


def _start(browser: Any, base: str, path: str, pack: FormPack, dossier: InputDossier):
    page = browser.new_context().new_page()
    page.goto(base + path)
    surface = PlaywrightFormSurface(
        page, _PROFILE, base_origin=base, ignored_mutation_paths=("/api/save*",)
    )
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier, run_id="run-rework")
    return page, surface, reconciler, session


def _resolve(reconciler: Any, session: Any, halt: Any, action: Any, value: Any = None):
    return reconciler.apply_human_resolution(
        session,
        HumanResolutionInput(
            halt_id=halt.halt_id,
            action=action,
            actor_id="operator-1",
            provided_value=value,
            idempotency_key=f"{halt.halt_id}:{action.value}",
        ),
    )


def _bind(key: str, label: str, kind: ControlKind = ControlKind.TEXT, **kw: Any):
    return FieldBindingSpec(
        binding_id=key,
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=kw.pop("section", ())
        ),
        control_kind=kind,
        **kw,
    )


def _shown(key: str, label: str, **kw: Any):
    return FieldBindingSpec(
        binding_id=f"rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=("^Quote issued$",)
        ),
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. A sign-in in the middle resets earlier answers.

_TABBED = """
<nav><button type="button" role="tab" id="tab-app">Applicant</button>
<button type="button" role="tab" id="tab-cov">Coverage</button>
<button type="button" role="tab" id="tab-rev">Review</button></nav>
<main id="panel"></main>
<dialog id="relogin"><p>Your session timed out.</p>
<label for="pw">Password</label><input id="pw" type="password">
<button type="button" id="rl-go">Sign in</button></dialog>
<script>
const D = {first_name: '', deductible: '500'};
let expired = false;
const P = () => document.getElementById('panel');
function save(next) {
  for (const el of P().querySelectorAll('input,select')) D[el.name] = el.value;
  fetch('/api/save', {method: 'POST', body: JSON.stringify(D)}).then(() => show(next));
}
function show(t) {
  if (t === 'app') {
    P().innerHTML = '<h2>Applicant</h2><label for="fn">First name</label>' +
      `<input id="fn" name="first_name" value="${D.first_name}">` +
      '<button type="button" id="next">Save and continue</button>';
    document.getElementById('next').onclick = () => save('cov');
  } else if (t === 'cov') {
    let o = '';
    for (const v of ['500', '1000', '2500']) o += `<option value="${v}"${D.deductible === v ? ' selected' : ''}>$${v}</option>`;
    P().innerHTML = '<h2>Coverage</h2><label for="ded">Deductible</label>' +
      `<select id="ded" name="deductible">${o}</select>` +
      '<button type="button" id="next">Save and continue</button>';
    document.getElementById('next').onclick = () => save('rev');
  } else {
    if (!expired) {
      expired = true;
      D.first_name = ''; D.deductible = '1000';
      document.getElementById('relogin').showModal();
      return;
    }
    P().innerHTML = '<h2>Review</h2><table id="sum"><tr><th>First name</th><td>' + D.first_name +
      '</td></tr><tr><th>Deductible</th><td>$' + D.deductible + '</td></tr></table>' +
      '<button type="button" id="submit">Request quote</button>';
    document.getElementById('submit').onclick = () =>
      fetch('/api/submit', {method: 'POST', body: JSON.stringify(D)}).then(() => {
        P().innerHTML = '<section><h2>Quote issued</h2><dl><dt>First name</dt><dd>' + D.first_name +
          '</dd><dt>Deductible</dt><dd>$' + D.deductible + '</dd></dl></section>';
      });
  }
}
document.getElementById('tab-app').onclick = () => show('app');
document.getElementById('tab-cov').onclick = () => show('cov');
document.getElementById('tab-rev').onclick = () => show('rev');
document.getElementById('rl-go').onclick = () => { document.getElementById('relogin').close(); show('rev'); };
show('app');
</script>
"""


def _tabbed_pack(*, revisit: bool, first_revisit: bool = True) -> FormPack:
    def screen(sid: str, landmark: str, tab: str) -> WizardScreenSpec:
        on = revisit and (first_revisit or sid != "app")
        return WizardScreenSpec(
            screen_id=sid,
            landmark_selector=landmark,
            next_button_anchor=FieldAnchorSpec(label_patterns=("^Save and continue$",)),
            revisit_selector=tab if on else None,
        )

    return FormPack(
        pack_id="test-tabbed",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(
            _bind("applicant.first_name", "First name"),
            _bind(
                "coverage.deductible",
                "Deductible",
                ControlKind.SELECT,
                codec=_MONEY,
                portal_default_values=("500",),
            ),
        ),
        screens=(
            screen("app", "#fn", "#tab-app"),
            screen("cov", "#ded", "#tab-cov"),
            WizardScreenSpec(
                screen_id="rev",
                landmark_selector="#submit",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Request quote$",)
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            field_readbacks=(
                _shown("applicant.first_name", "First name"),
                _shown("coverage.deductible", "Deductible", codec=_MONEY),
            )
        ),
    )


def _sign_in_again(page: Any) -> None:
    page.fill("#pw", "synthetic-test-pass")
    page.click("#rl-go")


def test_sign_in_mid_quote_rewalks_and_refills_before_submit(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _TABBED
    page, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _tabbed_pack(revisit=True),
        _dossier(**{"applicant.first_name": "Avery", "coverage.deductible": "2500"}),
    )
    first = reconciler.run_until_halt_or_done(session)
    assert first.outcome == FormTxOutcome.AUTH_REQUIRED, first.what_happened
    assert s.records == []
    _sign_in_again(page)
    done = _resolve(
        reconciler, session, first.halts[0], HumanDecisionAction.FIXED_ON_SCREEN
    )
    assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
    assert len(s.records) == 1
    assert s.records[0]["first_name"] == "Avery"
    assert s.records[0]["deductible"] == "2500"


@pytest.mark.parametrize("later_only", [False, True])
def test_sign_in_mid_quote_without_a_way_back_stops_naming_fields(
    site: tuple[_Site, str], browser: Any, later_only: bool
) -> None:
    # later_only: only a screen after the first can be revisited, so a walk
    # from there would skip the first screen's fields; it must stop instead.
    s, base = site
    s.pages["/"] = _TABBED
    page, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _tabbed_pack(revisit=later_only, first_revisit=False),
        _dossier(**{"applicant.first_name": "Avery", "coverage.deductible": "2500"}),
    )
    first = reconciler.run_until_halt_or_done(session)
    _sign_in_again(page)
    stop = _resolve(
        reconciler, session, first.halts[0], HumanDecisionAction.FIXED_ON_SCREEN
    )
    assert stop.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, stop.what_happened
    (halt,) = stop.halts
    assert halt.kind == FormHaltKind.FIELDS_NOT_RECHECKED
    assert set(halt.portal_value) == {"First name", "Deductible"}
    assert s.records == []


# ---------------------------------------------------------------------------
# 2. Controls no binding covers.

_UNBOUND = """
<main id="form"><h2>About you</h2>
<div class="row"><label for="fn">First name</label><input id="fn" name="first_name"></div>
<div class="row"><label for="rm">Roommate</label><input id="rm" name="roommate" value="Jasper Vellacott"></div>
<div class="row"><label for="pp">Paperless documents</label>
<select id="pp" name="paperless"><option>Yes</option><option>No</option></select></div>
<div class="row"><label for="cn">Client number</label><input id="cn" name="client_no"></div>
<button type="button" id="submit">Get quote</button></main>
<script>
document.getElementById('submit').onclick = () => {
  const rec = {};
  for (const el of document.querySelectorAll('input,select')) rec[el.name] = el.value;
  fetch('/api/submit', {method: 'POST', body: JSON.stringify(rec)}).then(() => {
    document.getElementById('form').outerHTML = '<section id="done"><h2>Quote issued</h2><dl><dt>First name</dt><dd>' +
      rec.first_name + '</dd></dl></section>';
  });
};
</script>
"""


def _unbound_pack(*, declare_paperless: bool, declare_client: bool = True) -> FormPack:
    unbound = [
        UnboundControlSpec(anchor=FieldAnchorSpec(label_patterns=("^Roommate$",)))
    ]
    if declare_paperless:
        unbound.append(
            UnboundControlSpec(
                anchor=FieldAnchorSpec(label_patterns=("^Paperless documents$",)),
                blank_values=("Yes",),
            )
        )
    if declare_client:
        unbound.append(
            UnboundControlSpec(
                anchor=FieldAnchorSpec(label_patterns=("^Client number$",))
            )
        )
    return FormPack(
        pack_id="test-unbound",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(_bind("applicant.first_name", "First name"),),
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(label_patterns=("^Get quote$",)),
            ),
        ),
        summary_check=SummaryCheckSpec(
            field_readbacks=(_shown("applicant.first_name", "First name"),)
        ),
        unbound_controls=tuple(unbound),
    )


def test_unbound_free_text_with_a_value_stops_and_can_be_cleared(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _UNBOUND
    pack = _unbound_pack(declare_paperless=True).model_copy(
        update={
            "unbound_controls": _unbound_pack(declare_paperless=True).unbound_controls[
                1:
            ]
        }
    )
    _, _, reconciler, session = _start(
        browser, base, "/", pack, _dossier(**{"applicant.first_name": "Avery"})
    )
    first = reconciler.run_until_halt_or_done(session)
    assert first.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, first.what_happened
    (halt,) = first.halts
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.field_label == "Roommate"
    assert halt.portal_value == "Jasper Vellacott"
    assert s.records == []
    done = _resolve(reconciler, session, halt, HumanDecisionAction.PROVIDE_VALUE, "")
    assert s.records and s.records[0]["roommate"] == ""
    # The cleared field has no read-back, so the run is not verified.
    assert done.outcome == FormTxOutcome.READBACK_INCOMPLETE, done.what_happened
    assert done.receipt is None


def test_unbound_choice_value_keeps_the_run_from_verified(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _UNBOUND.replace(' value="Jasper Vellacott"', "")
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _unbound_pack(declare_paperless=False),
        _dossier(**{"applicant.first_name": "Avery"}),
    )
    done = reconciler.run_until_halt_or_done(session)
    assert len(s.records) == 1
    assert done.outcome == FormTxOutcome.READBACK_INCOMPLETE, done.what_happened
    assert "unbound:Paperless documents" in done.summary_verdict.not_checked_keys


def test_declared_unbound_control_at_its_blank_value_is_named_on_the_receipt(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _UNBOUND.replace(' value="Jasper Vellacott"', "")
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _unbound_pack(declare_paperless=True),
        _dossier(**{"applicant.first_name": "Avery"}),
    )
    done = reconciler.run_until_halt_or_done(session)
    assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
    payload = done.receipt.payload
    assert payload.covered_keys == ("applicant.first_name",)
    assert {"Paperless documents", "Roommate", "Client number"} <= set(
        payload.not_covered
    )


@pytest.mark.parametrize("readonly", [False, True])
def test_intake_key_matching_an_empty_unbound_field_stops(
    site: tuple[_Site, str], browser: Any, readonly: bool
) -> None:
    s, base = site
    html = _UNBOUND.replace(' value="Jasper Vellacott"', "")
    if readonly:
        # Set only by a client lookup the pack does not run.
        html = html.replace('name="client_no">', 'name="client_no" readonly>')
    s.pages["/"] = html
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _unbound_pack(declare_paperless=True, declare_client=False),
        _dossier(
            **{"applicant.first_name": "Avery", "history.client_number": "FC-C-20418"}
        ),
    )
    first = reconciler.run_until_halt_or_done(session)
    assert first.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, first.what_happened
    (halt,) = first.halts
    assert halt.kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
    assert halt.field_label == "Client number"
    assert halt.dossier_key == "history.client_number"
    assert s.records == []


# ---------------------------------------------------------------------------
# 3. Stops name the field.

_STEP = """
<main id="step"><h2>Details</h2>
<div class="row"><label for="units">Number of units</label><input id="units" name="units"></div>
<div class="row" id="occ-row"><label for="occ">Unit occupancy</label>
<select id="occ" name="occ"><option value="">Select...</option><option>Owner</option><option>Tenant</option></select></div>
<div class="row"><label for="yv">Has any driver under 25 had a moving violation?</label>
<select id="yv" name="yv"><option value="">Select...</option><option>No</option><option>Yes</option></select></div>
<div id="esum"></div>
<button type="button" id="next">Next</button></main>
<script>
const MODE = '__MODE__';
document.getElementById('next').onclick = () => {
  if (MODE === 'marker') {
    document.getElementById('occ-row').insertAdjacentHTML('beforeend', '<span class="msg">Required</span>');
  } else if (MODE === 'summary') {
    document.getElementById('esum').innerHTML = '<div role="alert"><h2>There is a problem</h2>' +
      '<a href="#yv">Enter or choose: Has any driver under 25 had a moving violation?</a></div>';
  } else {
    document.getElementById('occ-row').insertAdjacentHTML('beforeend', '<div class="err" role="alert">Please complete this.</div>');
  }
};
</script>
"""


def _step_pack() -> FormPack:
    return FormPack(
        pack_id="test-step",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(_bind("property.units", "Number of units"),),
        screens=(
            WizardScreenSpec(
                screen_id="step",
                landmark_selector="#step",
                next_button_anchor=FieldAnchorSpec(label_patterns=("^Next$",)),
            ),
        ),
        unbound_controls=(
            UnboundControlSpec(
                anchor=FieldAnchorSpec(label_patterns=("^Has any driver",))
            ),
            UnboundControlSpec(
                anchor=FieldAnchorSpec(label_patterns=("^Unit occupancy$",))
            ),
        ),
    )


@pytest.mark.parametrize(
    ("mode", "label"),
    [
        ("marker", "Unit occupancy"),
        ("summary", "Has any driver under 25 had a moving violation?"),
        ("alert", "Unit occupancy"),
    ],
)
def test_validation_stop_names_the_field(
    site: tuple[_Site, str], browser: Any, mode: str, label: str
) -> None:
    s, base = site
    s.pages["/"] = _STEP.replace("__MODE__", mode)
    _, _, reconciler, session = _start(
        browser, base, "/", _step_pack(), _dossier(**{"property.units": "2"})
    )
    stop = reconciler.run_until_halt_or_done(session)
    assert stop.outcome == FormTxOutcome.VALIDATION_BLOCKED, stop.what_happened
    (halt,) = stop.halts
    assert halt.kind == FormHaltKind.PORTAL_VALIDATION_BLOCKED
    assert halt.field_label == label
    assert halt.dom_selector


_RADIOS = """
<main id="form"><fieldset><legend>Umbrella limit</legend>
<label><input type="radio" name="lim" value="1000000" checked> $1,000,000</label>
<label><input type="radio" name="lim" value="2000000"> $2,000,000</label></fieldset>
<button type="button" id="submit">Get quote</button></main>
"""


def _radio_pack(*, defaults: tuple[str, ...]) -> FormPack:
    return FormPack(
        pack_id="test-radios",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(
            FieldBindingSpec(
                binding_id="lim",
                dossier_key="coverage.umbrella_limit",
                anchor=FieldAnchorSpec(label_patterns=("^Umbrella limit$",)),
                control_kind=ControlKind.RADIO_GROUP,
                codec=_MONEY,
                prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
                portal_default_values=defaults,
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#form",
                next_button_anchor=FieldAnchorSpec(css_selector="#nowhere"),
            ),
        ),
    )


def test_radio_conflict_names_the_group(site: tuple[_Site, str], browser: Any) -> None:
    s, base = site
    s.pages["/"] = _RADIOS
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _radio_pack(defaults=()),
        _dossier(**{"coverage.umbrella_limit": "2000000"}),
    )
    stop = reconciler.run_until_halt_or_done(session)
    (halt,) = stop.halts
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.field_label == "Umbrella limit"


def test_declared_blank_default_is_written_over(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _RADIOS
    page, _, reconciler, session = _start(
        browser,
        base,
        "/",
        _radio_pack(defaults=("1000000",)),
        _dossier(**{"coverage.umbrella_limit": "2000000"}),
    )
    stop = reconciler.run_until_halt_or_done(session)
    # No prefill stop: the default is replaced (the run then stops at the
    # missing Next button, which this page does not have).
    assert all(h.kind != FormHaltKind.PREFILL_VALUE_CONFLICT for h in stop.halts)
    assert page.locator("input[value='2000000']").is_checked()


# ---------------------------------------------------------------------------
# 4. Row choice, split date, sentence output, "Label: value" output.

_ROWS = """
<main id="lookup"><h3>Vehicle Lookup</h3><table id="results">
<tr><th>2021 Okel</th><td>Kite</td><td>4D Hatchback</td><td><input type="button" value="Select"></td></tr>
<tr><th>2021 Okel</th><td>Kite Wagon</td><td>4D Wagon</td><td><input type="button" value="Select"></td></tr>
</table>
<output id="calc-total">Estimated value: $18,500</output></main>
"""


def test_select_button_chosen_by_its_row_and_label_value_output(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _ROWS
    _, surface, _, _ = _start(
        browser,
        base,
        "/",
        _radio_pack(defaults=()),
        _dossier(),
    )
    snap = surface.snapshot()
    anchor = FieldAnchorSpec(
        label_patterns=("^Select$",), row_cell_dossier_keys=("vehicle.model",)
    )
    with pytest.raises(AmbiguousAnchorError):
        match_anchor_in_snapshot(anchor, snap)
    pick = match_anchor_in_snapshot(anchor, snap, facts={"vehicle.model": "Kite Wagon"})
    assert pick is not None and "Kite Wagon" in pick.row_cells
    other = match_anchor_in_snapshot(anchor, snap, facts={"vehicle.model": "Kite"})
    assert other is not None and other.dom_selector != pick.dom_selector
    with pytest.raises(AmbiguousAnchorError):
        match_anchor_in_snapshot(anchor, snap, facts={"vehicle.model": "Harrier"})
    out = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=("^Estimated value$",)),
        snap,
        include_readonly=True,
    )
    assert out is not None and out.current_value == "$18,500"


_DATE_FORM = """
<main id="form"><fieldset><legend>Date of birth</legend>
<label for="d">Day</label><input id="d" name="dob_day">
<label for="m">Month</label><input id="m" name="dob_month">
<label for="y">Year</label><input id="y" name="dob_year"></fieldset>
<button type="button" id="submit">Apply</button></main>
<script>
document.getElementById('submit').onclick = () => {
  const r = {};
  for (const el of document.querySelectorAll('input')) r[el.name] = el.value;
  fetch('/api/submit', {method: 'POST', body: JSON.stringify(r)}).then(() => {
    const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    document.getElementById('form').outerHTML = '<section id="done"><h2>Quote issued</h2>' +
      '<p>Your quote number is VW-' + '12345. Keep it for your records.</p>' +
      '<dl><dt>Date of birth</dt><dd>' + r.dob_day + ' ' + months[parseInt(r.dob_month, 10) - 1] + ' ' + r.dob_year + '</dd></dl></section>';
  });
};
</script>
"""


@pytest.mark.parametrize(("year", "verified"), [("1988", True), ("1989", False)])
def test_split_date_and_sentence_quote_number_read_back(
    site: tuple[_Site, str], browser: Any, year: str, verified: bool
) -> None:
    s, base = site
    s.pages["/"] = _DATE_FORM
    keys = ("applicant.dob_day", "applicant.dob_month", "applicant.dob_year")
    pack = FormPack(
        pack_id="test-date",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=tuple(
            FieldBindingSpec(
                binding_id=k,
                dossier_key=k,
                anchor=FieldAnchorSpec(name_or_id_patterns=(rf"^{n}(?:\s|$)",)),
            )
            for k, n in zip(keys, ("dob_day", "dob_month", "dob_year"))
        ),
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(label_patterns=("^Apply$",)),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#done",
            split_date_readbacks=(
                SplitDateReadbackSpec(
                    anchor=FieldAnchorSpec(label_patterns=("^Date of birth$",)),
                    day_key=keys[0],
                    month_key=keys[1],
                    year_key=keys[2],
                ),
            ),
            outputs=(
                OutputExtractionSpec(
                    output_key="quote_number",
                    anchor=FieldAnchorSpec(label_patterns=("^Quote number$",)),
                    value_pattern=r"quote number is\s+([A-Z]{2}-\d+)",
                ),
            ),
        ),
    )
    page, _, reconciler, session = _start(
        browser, base, "/", pack, _dossier(**dict(zip(keys, ("4", "3", "1988"))))
    )
    if not verified:
        # The portal stores a different year than was typed.
        page.evaluate(
            "y => { const b = document.getElementById('submit'); const f = b.onclick;"
            " b.onclick = () => { document.getElementById('y').value = y; f(); }; }",
            year,
        )
    done = reconciler.run_until_halt_or_done(session)
    statuses = {v.dossier_key: v.status for v in done.summary_verdict.field_verdicts}
    if verified:
        assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
        assert done.outputs["quote_number"] == "VW-12345"
        assert all(statuses[k] == ReadbackStatus.PASSED for k in keys)
    else:
        assert done.outcome == FormTxOutcome.READBACK_REFUTED, done.what_happened
        assert all(statuses[k] == ReadbackStatus.FAILED for k in keys)


# ---------------------------------------------------------------------------
# 5. Intake values no binding enters; radio groups named by their row; a
# form that asks one question at a time in the same control.


@pytest.mark.parametrize("ignored", [False, True])
def test_intake_value_no_binding_enters_keeps_the_run_from_verified(
    site: tuple[_Site, str], browser: Any, ignored: bool
) -> None:
    s, base = site
    s.pages["/"] = _UNBOUND.replace(' value="Jasper Vellacott"', "")
    pack = _unbound_pack(declare_paperless=True)
    if ignored:
        pack = pack.model_copy(update={"ignored_intake_keys": ("drivers.additional",)})
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        pack,
        _dossier(**{"applicant.first_name": "Avery", "drivers.additional": "Pavel"}),
    )
    done = reconciler.run_until_halt_or_done(session)
    assert len(s.records) == 1
    # An intake value the portal never asks for is named on the receipt as
    # not covered; it blocks verified_done only when the pack requires it.
    assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
    assert "intake:drivers.additional" in done.receipt.payload.not_covered
    assert "drivers.additional" not in done.receipt.payload.covered_keys


def test_required_intake_value_no_binding_enters_blocks_verified(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _UNBOUND.replace(' value="Jasper Vellacott"', "")
    pack = _unbound_pack(declare_paperless=True).model_copy(
        update={"required_intake_keys": ("drivers.additional",)}
    )
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        pack,
        _dossier(**{"applicant.first_name": "Avery", "drivers.additional": "Pavel"}),
    )
    done = reconciler.run_until_halt_or_done(session)
    assert done.outcome == FormTxOutcome.READBACK_INCOMPLETE, done.what_happened
    assert "intake:drivers.additional" in done.summary_verdict.not_checked_keys


_ROW_RADIO = """
<main id="step"><table>
<tr><th>Completed a driver training course?:</th><td>
<label><input type="radio" name="tc" value="Y"> Yes</label>
<label><input type="radio" name="tc" value="N"> No</label><span id="m"></span></td></tr>
</table><button type="button" id="next">Next</button></main>
<script>
document.getElementById('next').onclick = () => { document.getElementById('m').textContent = 'Required'; };
</script>
"""


def test_radio_group_without_legend_is_named_by_its_row(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _ROW_RADIO
    pack = _step_pack().model_copy(update={"unbound_controls": ()})
    _, _, reconciler, session = _start(browser, base, "/", pack, _dossier())
    stop = reconciler.run_until_halt_or_done(session)
    (halt,) = stop.halts
    assert halt.field_label == "Completed a driver training course?"


_ONE_AT_A_TIME = """
<main id="ask"><label for="answer" id="q">First name</label><input id="answer" name="answer">
<button type="button" id="next">Continue</button></main>
<script>
const Q = ['First name', 'Last name'];
const A = {};
let i = 0;
document.getElementById('next').onclick = () => {
  A[Q[i]] = document.getElementById('answer').value;
  i += 1;
  if (i < Q.length) {
    document.getElementById('q').textContent = Q[i];
    document.getElementById('answer').value = '';
    return;
  }
  fetch('/api/submit', {method: 'POST', body: JSON.stringify(A)}).then(() => {
    document.getElementById('ask').outerHTML = '<section id="done"><h2>Quote issued</h2><dl>' +
      '<dt>First name</dt><dd>' + A['First name'] + '</dd><dt>Last name</dt><dd>' + A['Last name'] + '</dd></dl></section>';
  });
};
</script>
"""


def test_one_question_at_a_time_in_the_same_control_advances(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _ONE_AT_A_TIME
    pack = FormPack(
        pack_id="test-ask",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(
            _bind("applicant.first_name", "First name"),
            _bind("applicant.last_name", "Last name"),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="ask",
                landmark_selector="#ask",
                next_button_anchor=FieldAnchorSpec(label_patterns=("^Continue$",)),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#done",
            field_readbacks=(
                _shown("applicant.first_name", "First name"),
                _shown("applicant.last_name", "Last name"),
            ),
        ),
        max_commit_mutations=1,
    )
    _, _, reconciler, session = _start(
        browser,
        base,
        "/",
        pack,
        _dossier(**{"applicant.first_name": "Avery", "applicant.last_name": "Quist"}),
    )
    done = reconciler.run_until_halt_or_done(session)
    assert s.records == [{"First name": "Avery", "Last name": "Quist"}]
    assert done.outcome != FormTxOutcome.VALIDATION_BLOCKED, done.what_happened


# ---------------------------------------------------------------------------
# 6. A bound field shown with no intake value (cv-s04 class).

_SHOWN_BLANK = """
<main id="form"><h2>Household</h2>
<div class="row"><label for="fn">First name</label><input id="fn" name="first_name"></div>
<div class="row"><label for="br">Breed of each dog</label><input id="br" name="breeds"></div>
<button type="button" id="submit">Get quote</button></main>
<script>
document.getElementById('submit').onclick = () => {
  const rec = {};
  for (const el of document.querySelectorAll('input')) rec[el.name] = el.value;
  fetch('/api/submit', {method: 'POST', body: JSON.stringify(rec)}).then(() => {
    document.getElementById('form').outerHTML = '<section id="done"><h2>Quote issued</h2><dl><dt>First name</dt><dd>' +
      rec.first_name + '</dd></dl></section>';
  });
};
</script>
"""


@pytest.mark.parametrize("optional", [False, True])
def test_bound_field_shown_with_no_intake_value(
    site: tuple[_Site, str], browser: Any, optional: bool
) -> None:
    s, base = site
    s.pages["/"] = _SHOWN_BLANK
    pack = FormPack(
        pack_id="test-shown-blank",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(
            _bind("applicant.first_name", "First name"),
            _bind("household.dog_breeds", "Breed of each dog", optional=optional),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#form",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(label_patterns=("^Get quote$",)),
            ),
        ),
        summary_check=SummaryCheckSpec(
            field_readbacks=(_shown("applicant.first_name", "First name"),)
        ),
    )
    _, _, reconciler, session = _start(
        browser, base, "/", pack, _dossier(**{"applicant.first_name": "Avery"})
    )
    done = reconciler.run_until_halt_or_done(session)
    if not optional:
        assert s.records == []
        (halt,) = done.halts
        assert halt.kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
        assert halt.field_label == "Breed of each dog"
    else:
        assert len(s.records) == 1
        assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
        payload = done.receipt.payload
        assert "household.dog_breeds" not in payload.covered_keys
        assert "optional-blank:household.dog_breeds" in payload.not_covered


# ---------------------------------------------------------------------------
# 7. The review page shows a value the portal changed: stop before submit.

_REVIEW = """
<main id="panel"></main>
<script>
const D = {hp: ''};
const P = () => document.getElementById('panel');
function step1() {
  P().innerHTML = '<section id="s1"><label for="hp">Total horsepower</label><input id="hp" name="hp">' +
    '<button type="button" id="next">Continue</button></section>';
  document.getElementById('next').onclick = () => {
    D.hp = String(2 * parseInt(document.getElementById('hp').value, 10));
    review();
  };
}
function review() {
  P().innerHTML = '<section id="rev"><h2>Review</h2><dl><dt>Total horsepower</dt><dd>' + D.hp + '</dd></dl>' +
    '<button type="button" id="submit">Get my quote</button></section>';
  document.getElementById('submit').onclick = () =>
    fetch('/api/submit', {method: 'POST', body: JSON.stringify(D)});
}
step1();
</script>
"""


def test_review_page_value_that_differs_stops_before_submit(
    site: tuple[_Site, str], browser: Any
) -> None:
    s, base = site
    s.pages["/"] = _REVIEW
    pack = FormPack(
        pack_id="test-review",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=(
            _bind(
                "boat.hp",
                "Total horsepower",
                codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="s1",
                landmark_selector="#s1",
                next_button_anchor=FieldAnchorSpec(label_patterns=("^Continue$",)),
            ),
            WizardScreenSpec(
                screen_id="rev",
                landmark_selector="#rev",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Get my quote$",)
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            review_readbacks=(
                FieldBindingSpec(
                    binding_id="rv_hp",
                    dossier_key="boat.hp",
                    anchor=FieldAnchorSpec(
                        label_patterns=("^Total horsepower$",),
                        section_patterns=("^Review$",),
                    ),
                    codec=FieldCodecSpec(kind=FormCodecKind.INTEGER),
                ),
            )
        ),
    )
    _, _, reconciler, session = _start(
        browser, base, "/", pack, _dossier(**{"boat.hp": "400"})
    )
    stop = reconciler.run_until_halt_or_done(session)
    assert s.records == []
    (halt,) = stop.halts
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.field_label == "Total horsepower"
    assert halt.portal_value == "800"
