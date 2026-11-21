"""Read-back of quote summaries that show stored values as page text.

Headless Chromium against pages served through ``context.route`` (no sockets).
A small form is filled and submitted; the response is a read-only summary
laid out as a definition list, a two-column table, a header-row table,
"Label: value" lines, or a definition list inside an open shadow root. The
server can show a different value, an empty value, or a label twice.
Zero application or vendor names.
"""

from __future__ import annotations

import html
from typing import Any, Callable
from urllib.parse import parse_qs

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.surface import (  # noqa: E402
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
    match_text_pair_in_snapshot,
)
from openadapt_flow.form_tx.types import (  # noqa: E402
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    InputDossier,
    ObservedTextPair,
    OutputExtractionSpec,
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://portal.local"

FORM_HTML = """<!doctype html><html><head><title>Quote</title></head><body>
<h1>New quote</h1>
<p>Roof material: see the inspection report</p>
<form id="f" method="post" action="/submit">
  <label for="fn">First name</label><input id="fn" name="fn">
  <label for="rm">Roof material</label>
  <select id="rm" name="rm">
    <option value="">-- Select --</option>
    <option value="metal">Metal</option>
    <option value="slate">Slate</option>
  </select>
  <button type="submit" id="go">Submit quote</button>
</form></body></html>"""

ROOF_LABELS = {"metal": "Metal", "slate": "Slate", "": ""}


def _rows(stored: dict[str, str], mutation: str) -> list[tuple[str, str]]:
    roof = ROOF_LABELS.get(stored.get("rm", ""), "")
    if mutation == "differs":
        roof = "Slate" if roof != "Slate" else "Metal"
    if mutation == "empty":
        roof = ""
    rows = [
        ("First name", stored.get("fn", "")),
        ("Roof material", roof),
        ("Quote number", "Q-1001"),
    ]
    if mutation == "duplicate":
        rows.append(("First name", stored.get("fn", "")))
    return rows


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _dl(rows: list[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>" for k, v in rows)
    return f"<dl id='summary'>{items}</dl>"


def _table_rows(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f"<table id='summary'><caption>Your quote</caption>{body}</table>"


def _table_columns(rows: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{_esc(k)}</th>" for k, _ in rows)
    cells = "".join(f"<td>{_esc(v)}</td>" for _, v in rows)
    return f"<table id='summary'><tr>{head}</tr><tr>{cells}</tr></table>"


def _inline(rows: list[tuple[str, str]]) -> str:
    out = []
    for i, (k, v) in enumerate(rows):
        if i % 3 == 0:
            out.append(f"<p>{_esc(k)}: {_esc(v)}</p>")
        elif i % 3 == 1:
            out.append(f"<p><strong>{_esc(k)}:</strong> {_esc(v)}</p>")
        else:
            out.append(f"<div><span>{_esc(k)}:</span><span>{_esc(v)}</span></div>")
    return f"<div id='summary'>{''.join(out)}</div>"


def _shadow(rows: list[tuple[str, str]]) -> str:
    inner = _dl(rows).replace("id='summary'", "")
    return (
        "<div id='summary' style='min-height:10px'></div><script>"
        "const root = document.getElementById('summary').attachShadow({mode:'open'});"
        f"root.innerHTML = {inner!r};"
        "</script>"
    )


LAYOUTS: dict[str, Callable[[list[tuple[str, str]]], str]] = {
    "dl": _dl,
    "table_row": _table_rows,
    "table_columns": _table_columns,
    "inline": _inline,
    "shadow": _shadow,
}


def _summary_page(layout: str, stored: dict[str, str], mutation: str) -> str:
    body = LAYOUTS[layout](_rows(stored, mutation))
    return (
        "<!doctype html><html><head><title>Quote saved</title></head><body>"
        f"<h1>Quote saved</h1>{body}</body></html>"
    )


class _Portal:
    def __init__(self, layout: str, mutation: str) -> None:
        self.layout = layout
        self.mutation = mutation
        self.submissions: list[dict[str, str]] = []

    def handle(self, route: Any) -> None:
        req = route.request
        path = req.url[len(ORIGIN) :].split("?")[0]
        if path == "/submit" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.post_data or "").items() if v}
            self.submissions.append(form)
            page = _summary_page(self.layout, form, self.mutation)
            route.fulfill(status=200, content_type="text/html", body=page)
            return
        route.fulfill(status=200, content_type="text/html", body=FORM_HTML)


def _profile() -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="text-readback-test",
        debounce_grace_ms=40,
        settle_window_ms=60,
        max_settle_ms=8000,
    )


def _binding(key: str, label: str, kind: ControlKind) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=key,
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
    )


def _pack() -> FormPack:
    bindings = (
        _binding("applicant.first_name", "First name", ControlKind.TEXT),
        _binding("property.roof_material", "Roof material", ControlKind.SELECT),
    )
    return FormPack(
        pack_id="text-readback-test",
        domain_schema="test.quote/v1",
        ui_profile_id="text-readback-test",
        bindings=bindings,
        screens=(
            WizardScreenSpec(
                screen_id="form",
                landmark_selector="#f",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=("^Submit quote$",)
                ),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#summary",
            field_readbacks=bindings,
            outputs=(
                OutputExtractionSpec(
                    output_key="quote.number",
                    anchor=FieldAnchorSpec(label_patterns=("^Quote number$",)),
                ),
            ),
        ),
    )


def _dossier() -> InputDossier:
    values = {"applicant.first_name": "Marisol", "property.roof_material": "Metal"}
    return InputDossier(
        dossier_id="d-1",
        request_id="r-1",
        facts={
            k: DossierFact(
                key=k,
                value=v,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake",
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


def _run(browser: Any, layout: str, mutation: str) -> tuple[Any, _Portal]:
    portal = _Portal(layout, mutation)
    context = browser.new_context()
    try:
        context.route(f"{ORIGIN}/**", portal.handle)
        page = context.new_page()
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/form")
        reconciler = FormReconciler(surface, signing_key=Ed25519PrivateKey.generate())
        session = reconciler.create_session(_pack(), _dossier())
        result = reconciler.run_until_halt_or_done(session)
    finally:
        context.close()
    return result, portal


def _verdicts(result: Any) -> dict[str, Any]:
    assert result.summary_verdict is not None, result.what_happened
    return {v.dossier_key: v for v in result.summary_verdict.field_verdicts}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_text_summary_that_matches_is_verified_with_a_receipt(
    browser: Any, layout: str
) -> None:
    result, portal = _run(browser, layout, "none")
    assert portal.submissions == [{"fn": "Marisol", "rm": "metal"}]
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened
    verdicts = _verdicts(result)
    for key in ("applicant.first_name", "property.roof_material"):
        assert verdicts[key].status == ReadbackStatus.PASSED
        assert "page text" in verdicts[key].reason
    assert result.receipt is not None
    assert result.outputs["quote.number"] == "Q-1001"


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_text_summary_showing_a_different_value_is_refuted(
    browser: Any, layout: str
) -> None:
    result, _ = _run(browser, layout, "differs")
    assert result.outcome == FormTxOutcome.READBACK_REFUTED, result.what_happened
    verdicts = _verdicts(result)
    assert verdicts["property.roof_material"].status == ReadbackStatus.FAILED
    assert verdicts["applicant.first_name"].status == ReadbackStatus.PASSED
    assert result.receipt is None


@pytest.mark.parametrize("layout", ["dl", "table_row", "inline"])
def test_text_summary_showing_an_empty_value_fails(browser: Any, layout: str) -> None:
    result, _ = _run(browser, layout, "empty")
    assert result.outcome == FormTxOutcome.READBACK_REFUTED, result.what_happened
    verdict = _verdicts(result)["property.roof_material"]
    assert verdict.status == ReadbackStatus.FAILED
    assert "empty" in verdict.reason
    assert result.receipt is None


@pytest.mark.parametrize(
    "layout", ["dl", "table_row", "table_columns", "inline", "shadow"]
)
def test_label_shown_twice_is_not_checked(browser: Any, layout: str) -> None:
    result, _ = _run(browser, layout, "duplicate")
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, result.what_happened
    verdicts = _verdicts(result)
    assert verdicts["applicant.first_name"].status == ReadbackStatus.NOT_CHECKED
    assert verdicts["property.roof_material"].status == ReadbackStatus.PASSED
    assert result.receipt is None


HIDDEN_DD = """<!doctype html><html><body><h1>Quote saved</h1><dl id="summary">
<dt>Middle name</dt><dd style="display:none"></dd>
<dt>Last name</dt><dd>Smith</dd>
<dt style="display:none">Suffix</dt><dd>Jr</dd>
<dt>City</dt><dd>Saltmarsh</dd>
</dl></body></html>"""


def test_hidden_dd_never_lets_a_later_value_join_earlier_labels(
    browser: Any,
) -> None:
    context = browser.new_context()
    try:
        context.route(
            f"{ORIGIN}/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=HIDDEN_DD
            ),
        )
        page = context.new_page()
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/summary")
        pairs = {p.label: p.value for p in surface.snapshot().text_pairs}
    finally:
        context.close()
    # The hidden dd is an empty value, not "Smith"; the hidden dt's value is
    # dropped rather than joined onto "Last name".
    assert pairs.get("Middle name", "") == ""
    assert pairs["Last name"] == "Smith"
    assert "Suffix" not in pairs
    assert pairs["City"] == "Saltmarsh"
    snap_pairs = [p for p in pairs.values() if "Jr" in p]
    assert not snap_pairs


def test_page_text_is_never_a_fill_target(browser: Any) -> None:
    context = browser.new_context()
    try:
        context.route(f"{ORIGIN}/**", _Portal("dl", "none").handle)
        page = context.new_page()
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/form")
        snap = surface.snapshot()
    finally:
        context.close()
    # The hint line is read as page text but never becomes a control.
    assert any(p.label == "Roof material" for p in snap.text_pairs)
    assert all(c.tag in ("input", "select", "button") for c in snap.controls)
    anchor = FieldAnchorSpec(label_patterns=("^Roof material$",))
    ctrl = match_anchor_in_snapshot(anchor, snap)
    assert ctrl is not None and ctrl.tag == "select"


def test_text_pair_matcher_rules() -> None:
    snap = FormSnapshot(
        url_origin_and_path=f"{ORIGIN}/summary",
        text_pairs=(
            ObservedTextPair(label="Roof year", value="2016", section_text="Home"),
            ObservedTextPair(label="Roof year", value="2016", section_text="Prior"),
            ObservedTextPair(label="City", value="Saltmarsh"),
        ),
    )
    # Name or selector anchors never match page text.
    assert (
        match_text_pair_in_snapshot(
            FieldAnchorSpec(name_or_id_patterns=("city",)), snap
        )
        is None
    )
    city = match_text_pair_in_snapshot(
        FieldAnchorSpec(label_patterns=("^City$",)), snap
    )
    assert city is not None and city.value == "Saltmarsh"
    # A label shown twice is ambiguous even when the values agree.
    with pytest.raises(AmbiguousAnchorError):
        match_text_pair_in_snapshot(
            FieldAnchorSpec(label_patterns=("^Roof year$",)), snap
        )
    # A section narrows it to one.
    home = match_text_pair_in_snapshot(
        FieldAnchorSpec(label_patterns=("^Roof year$",), section_patterns=("^Home$",)),
        snap,
    )
    assert home is not None and home.section_text == "Home"
