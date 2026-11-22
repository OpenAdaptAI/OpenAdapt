"""Two-step and in-page popup estimators, driven in headless Chromium.

A small quote page sends the operator to an estimator (a child window or a
dialog in the same page) that needs Calculate before Use This Value. The
layer must run both steps, stop when a step is missing or the estimator
disagrees with the intake, never raise out of the run, and keep the
estimator's own input ``not_checked`` because the stored quote does not show
it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
from playwright.sync_api import Browser, Route, sync_playwright

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
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
    InputDossier,
    OutputExtractionSpec,
    PopupSubroutineSpec,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://quote.local"
RATE = 150


def _quote_page(mode: str) -> str:
    if mode == "window":
        opener = "window.open('/estimator', 'est', 'width=500,height=400');"
        dialog = ""
    else:
        opener = "document.getElementById('est').showModal();"
        dialog = _estimator_body(in_page=True, calculate=True, factor=RATE)
    return f"""<!doctype html><html><head><title>Quote</title></head>
<body data-screen-id="quote">
<h1>New quote</h1>
<form id="q" onsubmit="return false">
  <label for="first">First name</label>
  <input id="first" name="first" required>
  <label for="dwelling">Dwelling coverage</label>
  <input id="dwelling" name="dwelling" readonly value="">
  <button id="open-est" type="button">Estimate Dwelling</button>
  <button id="submit" type="button">Submit Quote</button>
</form>
{dialog}
<script>
document.getElementById('open-est').addEventListener('click', () => {{ {opener} }});
document.getElementById('submit').addEventListener('click', async () => {{
  const body = {{first: document.getElementById('first').value,
                 dwelling: document.getElementById('dwelling').value}};
  // Read the reply before leaving the page, so the request is not cut off.
  await (await fetch('/submit', {{method: 'POST', body: JSON.stringify(body)}})).text();
  location.href = '/summary';
}});
</script>
</body></html>"""


def _estimator_body(
    *,
    in_page: bool,
    calculate: bool,
    factor: int,
    offset: int = 0,
    round_back: bool = False,
) -> str:
    calc_btn = '<button id="calc" type="button">Calculate</button>' if calculate else ""
    if in_page:
        apply_js = (
            "document.getElementById('dwelling').value = r;"
            "document.getElementById('est').close();"
        )
        open_tag, close_tag = '<dialog id="est">', "</dialog>"
    else:
        apply_js = "window.opener.document.getElementById('dwelling').value = r; window.close();"
        open_tag, close_tag = "<div>", "</div>"
    return f"""{open_tag}
  <h2>Estimator</h2>
  <label for="sqft">Square feet</label>
  <input id="sqft" name="sqft">
  {calc_btn}
  <label for="result">Estimated value</label>
  <output id="result" data-readonly-output></output>
  <button id="use" type="button">Use This Value</button>
  <script>
  (() => {{
    let r = '';
    const calc = document.getElementById('calc');
    if (calc) calc.addEventListener('click', () => {{
      r = String(Number(document.getElementById('sqft').value) * {factor} + {offset});
      document.getElementById('result').textContent = Number(r).toLocaleString('en-US');
      if ({"true" if round_back else "false"}) r = String(Math.floor(Number(r) / 1000) * 1000);
    }});
    document.getElementById('use').addEventListener('click', () => {{
      if (!r) return;
      {apply_js}
    }});
  }})();
  </script>
{close_tag}"""


def _estimator_page(
    *, calculate: bool, factor: int, offset: int, round_back: bool
) -> str:
    body = _estimator_body(
        in_page=False,
        calculate=calculate,
        factor=factor,
        offset=offset,
        round_back=round_back,
    )
    return f"""<!doctype html><html><head><title>Estimator</title></head>
<body>{body}</body></html>"""


def _summary_page(record: dict[str, Any]) -> str:
    return f"""<!doctype html><html><head><title>Summary</title></head>
<body data-screen-id="summary">
<h1>Quote summary</h1>
<label for="s-first">First name</label>
<input id="s-first" readonly value="{record.get("first", "")}">
<label for="s-dwelling">Dwelling coverage</label>
<input id="s-dwelling" readonly value="{record.get("dwelling", "")}">
<label for="s-num">Quote number</label>
<output id="s-num">Q-1001</output>
</body></html>"""


class _Portal:
    def __init__(
        self,
        *,
        mode: str,
        calculate: bool = True,
        factor: int = RATE,
        offset: int = 0,
        round_back: bool = False,
    ):
        self.offset = offset
        self.round_back = round_back
        self.mode = mode
        self.calculate = calculate
        self.factor = factor
        self.submissions: list[dict[str, Any]] = []
        self.open_window = True
        # Window that closes itself when Calculate is pressed.
        self.close_on_calculate = False
        # Parent form re-rendered with new ids when the in-page popup opens.
        self.rerender_parent = False
        # In-page popup that is a plain <div>, not a dialog.
        self.plain_div = False

    def handle(self, route: Route) -> None:
        req = route.request
        path = req.url[len(ORIGIN) :].split("?")[0]
        if path == "/quote":
            html = _quote_page(self.mode)
            if self.mode == "in_page":
                html = html.replace(
                    _estimator_body(in_page=True, calculate=True, factor=RATE),
                    _estimator_body(
                        in_page=True,
                        calculate=self.calculate,
                        factor=self.factor,
                        offset=self.offset,
                        round_back=self.round_back,
                    ),
                )
            if self.mode == "in_page" and self.rerender_parent:
                for name in ("first", "dwelling"):
                    html = html.replace(
                        f"document.getElementById('{name}')",
                        f"document.querySelector('#q [name={name}]')",
                    )
                html = html.replace(
                    "document.getElementById('est').showModal();",
                    "for (const el of document.querySelectorAll('#q input')) {"
                    " const fresh = el.cloneNode(); fresh.value = el.value;"
                    " fresh.id = el.name + '-' + Math.random().toString(36).slice(2, 8);"
                    " const lab = document.querySelector('label[for=' + el.id + ']');"
                    " if (lab) lab.htmlFor = fresh.id; el.replaceWith(fresh); }"
                    " document.getElementById('est').showModal();",
                )
            if self.mode == "in_page" and self.plain_div:
                html = (
                    html.replace('<dialog id="est">', '<div id="est" hidden>')
                    .replace("</dialog>", "</div>")
                    .replace(
                        "document.getElementById('est').showModal();",
                        "document.getElementById('est').hidden = false;",
                    )
                    .replace(
                        "document.getElementById('est').close();",
                        "document.getElementById('est').hidden = true;",
                    )
                )
            if not self.open_window:
                html = html.replace(
                    "window.open('/estimator', 'est', 'width=500,height=400');", ""
                )
            route.fulfill(status=200, content_type="text/html", body=html)
        elif path == "/estimator":
            body = _estimator_page(
                calculate=self.calculate,
                factor=self.factor,
                offset=self.offset,
                round_back=self.round_back,
            )
            if self.close_on_calculate:
                body = body.replace(
                    "calc.addEventListener('click', () => {",
                    "calc.addEventListener('click', () => { window.close();",
                )
            route.fulfill(status=200, content_type="text/html", body=body)
        elif path == "/submit" and req.method == "POST":
            self.submissions.append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path == "/summary":
            record = self.submissions[-1] if self.submissions else {}
            route.fulfill(
                status=200, content_type="text/html", body=_summary_page(record)
            )
        else:
            route.fulfill(status=404, body="not found")


def _pack(mode: str, result_label: Optional[str] = None) -> FormPack:
    int_codec = FieldCodecSpec(kind=FormCodecKind.INTEGER)
    return FormPack(
        commit_url_patterns=("/submit",),
        pack_id=f"popup-steps-{mode}",
        domain_schema="test.quote/v1",
        ui_profile_id="popup-steps-test",
        bindings=(
            FieldBindingSpec(
                binding_id="first",
                dossier_key="applicant.first_name",
                anchor=FieldAnchorSpec(label_patterns=("^First name$",)),
            ),
        ),
        popup_subroutines=(
            PopupSubroutineSpec(
                subroutine_id="estimator",
                popup_mode=mode,  # type: ignore[arg-type]
                trigger_anchor=FieldAnchorSpec(label_patterns=("^Estimate Dwelling$",)),
                popup_url_pattern=r"/estimator$" if mode == "window" else None,
                bindings=(
                    FieldBindingSpec(
                        binding_id="sqft",
                        dossier_key="property.square_feet",
                        anchor=FieldAnchorSpec(label_patterns=("^Square feet$",)),
                        control_kind=ControlKind.TEXT,
                        codec=int_codec,
                    ),
                ),
                compute_anchors=(FieldAnchorSpec(label_patterns=("^Calculate$",)),),
                result_anchor=(
                    FieldAnchorSpec(label_patterns=(f"^{result_label}$",))
                    if result_label
                    else None
                ),
                commit_anchor=FieldAnchorSpec(label_patterns=("^Use This Value$",)),
                parent_return_anchor=FieldAnchorSpec(
                    label_patterns=("^Dwelling coverage$",)
                ),
                return_output_key="coverage.dwelling",
                return_codec=int_codec,
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="quote",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Submit Quote$",)
                ),
            ),
            WizardScreenSpec(screen_id="summary"),
        ),
        summary_check=SummaryCheckSpec(
            summary_url_path="/summary",
            field_readbacks=(
                FieldBindingSpec(
                    binding_id="rb_first",
                    dossier_key="applicant.first_name",
                    anchor=FieldAnchorSpec(label_patterns=("^First name$",)),
                ),
                FieldBindingSpec(
                    binding_id="rb_dwelling",
                    dossier_key="coverage.dwelling",
                    anchor=FieldAnchorSpec(label_patterns=("^Dwelling coverage$",)),
                    codec=int_codec,
                ),
            ),
            outputs=(
                OutputExtractionSpec(
                    output_key="quote_number",
                    anchor=FieldAnchorSpec(label_patterns=("^Quote number$",)),
                ),
            ),
        ),
    )


def _dossier(dwelling: Optional[int] = None, sqft: int = 2000) -> InputDossier:
    raw: dict[str, Any] = {
        "applicant.first_name": "Mirela",
        "property.square_feet": sqft,
    }
    if dwelling is not None:
        raw["coverage.dwelling"] = dwelling
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
            for k, v in raw.items()
        },
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


def _run(
    browser: Browser,
    portal: _Portal,
    dossier: InputDossier,
    result_label: Optional[str] = None,
) -> tuple[Any, _Portal]:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        ctx.route(f"{ORIGIN}/**", portal.handle)
        page = ctx.new_page()
        surface = PlaywrightFormSurface(
            page, UIProfileSpec(profile_id="popup-steps-test"), base_origin=ORIGIN
        )
        surface.goto(f"{ORIGIN}/quote")
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(_pack(portal.mode, result_label), dossier)
        result = reconciler.run_until_halt_or_done(session)
        return result, portal
    finally:
        ctx.close()


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_two_step_estimator_runs_calculate_then_use_and_keeps_its_input_unchecked(
    chromium_browser: Browser, mode: str
) -> None:
    result, portal = _run(chromium_browser, _Portal(mode=mode), _dossier())
    assert portal.submissions == [{"first": "Mirela", "dwelling": str(2000 * RATE)}], (
        result.outcome,
        result.what_happened,
        result.halts,
    )
    # The stored quote does not show the square feet typed into the estimator,
    # so that field stays not_checked and the run cannot end verified.
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert result.receipt is None
    verdict = result.summary_verdict
    assert verdict is not None
    assert verdict.failed_keys == ()
    assert set(verdict.not_checked_keys) == {
        "property.square_feet",
        "coverage.dwelling",
    }
    by_key = {v.dossier_key: v for v in verdict.field_verdicts}
    # The dwelling figure came only from the popup, whose input is not checked,
    # and no displayed result was declared to compare it with.
    assert by_key["coverage.dwelling"].status == ReadbackStatus.NOT_CHECKED
    assert "displayed result" in by_key["coverage.dwelling"].reason
    assert by_key["applicant.first_name"].status == ReadbackStatus.PASSED
    assert "re-read before apply" in by_key["property.square_feet"].reason


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_estimator_that_disagrees_with_the_intake_stops_before_submit(
    chromium_browser: Browser, mode: str
) -> None:
    result, portal = _run(
        chromium_browser, _Portal(mode=mode), _dossier(dwelling=2000 * RATE + 1000)
    )
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.dossier_key == "coverage.dwelling"
    assert halt.portal_value == str(2000 * RATE)


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_missing_compute_button_is_a_stop_not_a_crash(
    chromium_browser: Browser, mode: str
) -> None:
    result, portal = _run(
        chromium_browser, _Portal(mode=mode, calculate=False), _dossier()
    )
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "step 1" in result.halts[0].message


def test_popup_window_that_never_opens_is_a_stop_not_a_crash(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(mode="window")
    portal.open_window = False
    result, _ = _run(chromium_browser, portal, _dossier())
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "could not be run" in result.halts[0].message


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_popup_that_shows_one_figure_and_returns_another_stops_before_submit(
    chromium_browser: Browser, mode: str
) -> None:
    # 3414 * 150 + 240 = 512,340 shown; 512,000 handed back.
    result, portal = _run(
        chromium_browser,
        _Portal(mode=mode, offset=240, round_back=True),
        _dossier(sqft=3414),
        result_label="Estimated value",
    )
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.portal_value == "512000"
    assert halt.dossier_value == "512,340"
    assert "512,340" in halt.message and "512000" in halt.message


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_returned_value_that_matches_the_displayed_result_proceeds(
    chromium_browser: Browser, mode: str
) -> None:
    result, portal = _run(
        chromium_browser,
        _Portal(mode=mode, offset=240),
        _dossier(sqft=3414),
        result_label="Estimated value",
    )
    assert portal.submissions == [{"first": "Mirela", "dwelling": "512340"}], (
        result.outcome,
        result.halts,
    )
    verdict = result.summary_verdict
    assert verdict is not None
    by_key = {v.dossier_key: v for v in verdict.field_verdicts}
    assert by_key["coverage.dwelling"].status == ReadbackStatus.PASSED
    # The founder rule stands: the estimator input is still not checked.
    assert verdict.not_checked_keys == ("property.square_feet",)
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE
    assert result.receipt is None


@pytest.mark.parametrize("mode", ["window", "in_page"])
def test_declared_result_that_cannot_be_read_stops_before_submit(
    chromium_browser: Browser, mode: str
) -> None:
    result, portal = _run(
        chromium_browser,
        _Portal(mode=mode),
        _dossier(),
        result_label="Replacement cost",
    )
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "shows no result" in result.halts[0].message


def test_window_that_closes_during_a_step_is_a_stop_not_a_crash(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(mode="window")
    portal.close_on_calculate = True
    result, _ = _run(chromium_browser, portal, _dossier())
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert result.safe_to_retry is True  # nothing was sent


def test_parent_rerendered_when_a_dialog_opens_is_left_alone(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(mode="in_page")
    portal.rerender_parent = True
    result, _ = _run(chromium_browser, portal, _dossier())
    assert portal.submissions == [{"first": "Mirela", "dwelling": str(2000 * RATE)}], (
        result.outcome,
        result.halts,
    )


def test_plain_div_popup_with_a_rerendered_parent_stops(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(mode="in_page")
    portal.rerender_parent = True
    portal.plain_div = True
    result, _ = _run(chromium_browser, portal, _dossier())
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "told apart" in result.halts[0].message


def test_plain_div_popup_without_a_rerender_still_runs(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(mode="in_page")
    portal.plain_div = True
    result, _ = _run(chromium_browser, portal, _dossier())
    assert portal.submissions == [{"first": "Mirela", "dwelling": str(2000 * RATE)}], (
        result.outcome,
        result.halts,
    )
