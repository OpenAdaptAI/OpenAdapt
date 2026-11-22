"""A run whose summary shows name and date of birth in one row (headless, synthetic).

The keys are declared only through ``row_readbacks``: each gets its own
verdict from the summary judge, and a portal that stores the wrong date is
refuted on the date alone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Browser, Route, sync_playwright

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    FormTxOutcome,
    InputDossier,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    SummaryRowPartSpec,
    SummaryRowReadbackSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://rows.local"
MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")

FORM = """<!doctype html><html><head><title>Trip</title></head>
<body data-screen-id="trip"><h1>Who is travelling?</h1>
<form onsubmit="return false">
  <label for="n">Full name</label><input id="n" name="n">
  <label for="d">Date of birth</label><input id="d" name="d" placeholder="MM/DD/YYYY">
  <button id="go" type="button">Get my quote</button>
</form>
<script>
document.getElementById('go').onclick = async () => {
  const body = {n: document.getElementById('n').value, d: document.getElementById('d').value};
  await fetch('/submit', {method: 'POST', body: JSON.stringify(body)});
  location.href = '/summary';
};
</script></body></html>"""


def _summary(record: dict[str, Any], wrong_date: bool) -> str:
    dob = "02/01/1990" if wrong_date else record.get("d", "")
    return f"""<!doctype html><html><head><title>Summary</title></head>
<body data-screen-id="summary"><h1>What we stored</h1>
<dl><dt>Travellers</dt><dd>{record.get("n", "")} ({dob})</dd></dl>
</body></html>"""


class _Portal:
    def __init__(self, wrong_date: bool = False) -> None:
        self.wrong_date = wrong_date
        self.submissions: list[dict[str, Any]] = []

    def handle(self, route: Route) -> None:
        req = route.request
        path = req.url[len(ORIGIN) :].split("?")[0]
        if path == "/trip":
            route.fulfill(status=200, content_type="text/html", body=FORM)
        elif path == "/submit" and req.method == "POST":
            self.submissions.append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path == "/summary":
            record = self.submissions[-1] if self.submissions else {}
            route.fulfill(
                status=200,
                content_type="text/html",
                body=_summary(record, self.wrong_date),
            )
        else:
            route.fulfill(status=404, body="not found")


PACK = FormPack(
    pack_id="rows",
    domain_schema="test.trip/v1",
    ui_profile_id="rows-test",
    bindings=(
        FieldBindingSpec(
            binding_id="n",
            dossier_key="traveller_name",
            anchor=FieldAnchorSpec(label_patterns=("^Full name$",)),
        ),
        FieldBindingSpec(
            binding_id="d",
            dossier_key="traveller_dob",
            anchor=FieldAnchorSpec(label_patterns=("^Date of birth$",)),
            codec=MDY,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="trip",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=("^Get my quote$",)),
        ),
        WizardScreenSpec(screen_id="summary"),
    ),
    summary_check=SummaryCheckSpec(
        summary_url_path="/summary",
        row_readbacks=(
            SummaryRowReadbackSpec(
                anchor=FieldAnchorSpec(label_patterns=("^Travellers$",)),
                pattern=r"(?P<n>.+) \((?P<d>[^)]*)\)",
                parts=(
                    SummaryRowPartSpec(group="n", dossier_key="traveller_name"),
                    SummaryRowPartSpec(
                        group="d", dossier_key="traveller_dob", codec=MDY
                    ),
                ),
            ),
        ),
    ),
)


def _dossier() -> InputDossier:
    raw = {"traveller_name": "Ada Byron", "traveller_dob": "1990-01-31"}
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


def _run(browser: Browser, portal: _Portal) -> Any:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        ctx.route(f"{ORIGIN}/**", portal.handle)
        page = ctx.new_page()
        surface = PlaywrightFormSurface(
            page, UIProfileSpec(profile_id="rows-test"), base_origin=ORIGIN
        )
        surface.goto(f"{ORIGIN}/trip")
        reconciler = FormReconciler(surface)
        return reconciler.run_until_halt_or_done(
            reconciler.create_session(PACK, _dossier())
        )
    finally:
        ctx.close()


def _statuses(result: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in result.summary_verdict.field_verdicts:
        out.setdefault(v.dossier_key, set()).add(v.status)
    return out


def test_both_values_in_the_row_pass(chromium_browser: Browser) -> None:
    portal = _Portal()
    result = _run(chromium_browser, portal)
    assert portal.submissions, (result.outcome, result.what_happened, result.halts)
    assert _statuses(result) == {
        "traveller_name": {ReadbackStatus.PASSED},
        "traveller_dob": {ReadbackStatus.PASSED},
    }, result.summary_verdict.field_verdicts
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened


def test_a_stored_wrong_date_refutes_only_the_date(chromium_browser: Browser) -> None:
    portal = _Portal(wrong_date=True)
    result = _run(chromium_browser, portal)
    assert _statuses(result) == {
        "traveller_name": {ReadbackStatus.PASSED},
        "traveller_dob": {ReadbackStatus.FAILED},
    }, result.summary_verdict.field_verdicts
    assert result.outcome == FormTxOutcome.READBACK_REFUTED, result.what_happened
