"""A lookup table's chosen row as an effect location (headless Chromium, synthetic).

A class finder shows one row per result, each with its own Select button.
With ``result_row_pattern`` the chosen row's code cell is read before Select,
and the value put back in the parent must be that code. Without it, the code
stays ``not_checked`` because it came from a popup whose search term is not
checked (the old behaviour, still the default).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
from playwright.sync_api import Browser, Route, sync_playwright

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    InputDossier,
    PopupSubroutineSpec,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://lookup.local"
CLASSES = [("5461", "Bakery"), ("5812", "Restaurant"), ("7230", "Beauty salon")]


def _form(lie: Optional[str]) -> str:
    rows = json.dumps(CLASSES)
    put_back = json.dumps(lie) if lie is not None else "code"
    return f"""<!doctype html><html><head><title>Application</title></head>
<body data-screen-id="app">
<h1>Application</h1>
<form onsubmit="return false">
  <label for="biz">Business name</label>
  <input id="biz" name="biz">
  <label for="code">Class code</label>
  <input id="code" name="class_code" readonly value="">
  <button id="find" type="button">Find class...</button>
  <button id="submit" type="button">Submit application</button>
</form>
<dialog id="finder">
  <h2>Class finder</h2>
  <label for="q">Search classes</label>
  <input id="q" name="q">
  <button id="go" type="button">Search</button>
  <table><thead><tr><th>Code</th><th>Description</th><th></th></tr></thead>
  <tbody id="rows"></tbody></table>
</dialog>
<script>
const all = {rows};
document.getElementById('find').onclick = () => document.getElementById('finder').showModal();
document.getElementById('go').onclick = () => {{
  const q = document.getElementById('q').value.toLowerCase();
  const tb = document.getElementById('rows'); tb.innerHTML = '';
  for (const [code, label] of all) {{
    if (!label.toLowerCase().includes(q)) continue;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + code + '</td><td>' + label +
      '</td><td><button type="button">Select</button></td>';
    tr.querySelector('button').onclick = () => {{
      document.getElementById('code').value = {put_back};
      document.getElementById('finder').close();
    }};
    tb.appendChild(tr);
  }}
}};
document.getElementById('submit').onclick = async () => {{
  const body = {{biz: document.getElementById('biz').value,
                 class_code: document.getElementById('code').value}};
  await fetch('/submit', {{method: 'POST', body: JSON.stringify(body)}});
  location.href = '/summary';
}};
</script></body></html>"""


def _summary(record: dict[str, Any]) -> str:
    return f"""<!doctype html><html><head><title>Summary</title></head>
<body data-screen-id="summary"><h1>Summary</h1>
<label for="s-biz">Business name</label>
<input id="s-biz" readonly value="{record.get("biz", "")}">
<label for="s-code">Class code</label>
<input id="s-code" readonly value="{record.get("class_code", "")}">
</body></html>"""


class _Portal:
    def __init__(self, lie: Optional[str] = None) -> None:
        self.lie = lie
        self.submissions: list[dict[str, Any]] = []

    def handle(self, route: Route) -> None:
        req = route.request
        path = req.url[len(ORIGIN) :].split("?")[0]
        if path == "/app":
            route.fulfill(status=200, content_type="text/html", body=_form(self.lie))
        elif path == "/submit" and req.method == "POST":
            self.submissions.append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path == "/summary":
            record = self.submissions[-1] if self.submissions else {}
            route.fulfill(status=200, content_type="text/html", body=_summary(record))
        else:
            route.fulfill(status=404, body="not found")


def _pack(row_pattern: Optional[str]) -> FormPack:
    return FormPack(
        pack_id="lookup-row",
        domain_schema="test.application/v1",
        ui_profile_id="lookup-row-test",
        bindings=(
            FieldBindingSpec(
                binding_id="biz",
                dossier_key="business_name",
                anchor=FieldAnchorSpec(label_patterns=("^Business name$",)),
            ),
        ),
        popup_subroutines=(
            PopupSubroutineSpec(
                subroutine_id="class_finder",
                popup_mode="in_page",
                trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Find class\.\.\.$",)),
                bindings=(
                    FieldBindingSpec(
                        binding_id="kw",
                        dossier_key="class_keyword",
                        anchor=FieldAnchorSpec(label_patterns=("^Search classes$",)),
                    ),
                ),
                compute_anchors=(FieldAnchorSpec(label_patterns=("^Search$",)),),
                result_row_pattern=row_pattern,
                commit_anchor=FieldAnchorSpec(label_patterns=("^Select$",)),
                parent_return_anchor=FieldAnchorSpec(label_patterns=("^Class code$",)),
                return_output_key="class_code",
                return_codec=FieldCodecSpec(),
            ),
        ),
        screens=(
            WizardScreenSpec(
                screen_id="app",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Submit application$",)
                ),
            ),
            WizardScreenSpec(screen_id="summary"),
        ),
        summary_check=SummaryCheckSpec(
            summary_url_path="/summary",
            field_readbacks=(
                FieldBindingSpec(
                    binding_id="rb_biz",
                    dossier_key="business_name",
                    anchor=FieldAnchorSpec(label_patterns=("^Business name$",)),
                ),
                FieldBindingSpec(
                    binding_id="rb_code",
                    dossier_key="class_code",
                    anchor=FieldAnchorSpec(label_patterns=("^Class code$",)),
                ),
            ),
        ),
    )


def _dossier() -> InputDossier:
    raw = {"business_name": "Crumb & Co", "class_keyword": "bakery"}
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


def _run(browser: Browser, portal: _Portal, row_pattern: Optional[str]) -> Any:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        ctx.route(f"{ORIGIN}/**", portal.handle)
        page = ctx.new_page()
        surface = PlaywrightFormSurface(
            page, UIProfileSpec(profile_id="lookup-row-test"), base_origin=ORIGIN
        )
        surface.goto(f"{ORIGIN}/app")
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(_pack(row_pattern), _dossier())
        return reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()


def _status(result: Any, key: str) -> Any:
    by_key = {v.dossier_key: v for v in result.summary_verdict.field_verdicts}
    return by_key[key]


def test_without_a_row_pattern_the_code_stays_not_checked(
    chromium_browser: Browser,
) -> None:
    portal = _Portal()
    result = _run(chromium_browser, portal, None)
    assert portal.submissions == [{"biz": "Crumb & Co", "class_code": "5461"}], (
        result.outcome,
        result.what_happened,
    )
    assert _status(result, "class_code").status == ReadbackStatus.NOT_CHECKED


def test_the_chosen_row_lets_the_code_be_checked(chromium_browser: Browser) -> None:
    portal = _Portal()
    result = _run(chromium_browser, portal, r"\d{4}")
    assert portal.submissions == [{"biz": "Crumb & Co", "class_code": "5461"}], (
        result.outcome,
        result.what_happened,
        result.halts,
    )
    assert _status(result, "class_code").status == ReadbackStatus.PASSED
    # The search term itself is still only re-read in the popup.
    assert _status(result, "class_keyword").status == ReadbackStatus.NOT_CHECKED
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE


def test_a_popup_that_puts_back_another_code_stops_before_submit(
    chromium_browser: Browser,
) -> None:
    portal = _Portal(lie="9999")
    result = _run(chromium_browser, portal, r"\d{4}")
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED
    assert "chosen row" in result.halts[0].message


def test_a_row_with_no_matching_cell_stops_before_submit(
    chromium_browser: Browser,
) -> None:
    portal = _Portal()
    result = _run(chromium_browser, portal, r"[A-Z]{3}-\d+")
    assert portal.submissions == []
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].kind == FormHaltKind.POPUP_CONTRACT_FAILED


def test_an_unset_row_pattern_is_not_dumped() -> None:
    spec = PopupSubroutineSpec(
        subroutine_id="s",
        trigger_anchor=FieldAnchorSpec(label_patterns=("x",)),
        commit_anchor=FieldAnchorSpec(label_patterns=("y",)),
        parent_return_anchor=FieldAnchorSpec(label_patterns=("z",)),
        return_output_key="k",
        return_codec=FieldCodecSpec(),
    )
    assert "result_row_pattern" not in spec.model_dump(mode="json")
    with pytest.raises(ValueError):
        PopupSubroutineSpec.model_validate(
            {**spec.model_dump(mode="json"), "result_row_pattern": "("}
        )
