"""Headless Chromium tests: table-cell labels, "Choose" placeholders, a
type-ahead that keeps only part of its pick, and a child-window popup opened
from a form inside a routed frame. Pages are served through
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
    PopupSubroutineSpec,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://layout.local"

TABLE = """<!doctype html><html><body><form onsubmit="return false"><table>
<tr><td>Driver name</td><td><input name="dn"></td></tr>
<tr><td>Vehicle use</td><td><select name="vu"><option value="">Choose</option>
<option value="c">Commute</option><option value="p">Pleasure</option></select></td></tr>
<tr><td>Two</td><td><input name="a"><input name="b"></td></tr>
</table></form></body></html>"""


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


def _serve(browser: Any, pages: dict[str, str]) -> tuple[Any, Any, list[dict]]:
    submissions: list[dict] = []

    def handle(route: Any) -> None:
        req = route.request
        path = urlsplit(req.url).path
        if path == "/api/submit":
            submissions.append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path in pages:
            route.fulfill(status=200, content_type="text/html", body=pages[path])
        else:
            route.fulfill(status=404, body="")

    ctx = browser.new_context()
    ctx.route(f"{ORIGIN}/**", handle)
    return ctx, ctx.new_page(), submissions


def _profile(**kw: Any) -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="layout-test",
        debounce_grace_ms=40,
        settle_window_ms=80,
        max_settle_ms=5000,
        **kw,
    )


def test_label_in_the_cell_before_and_choose_placeholder(browser: Any) -> None:
    ctx, page, _ = _serve(browser, {"/t": TABLE})
    try:
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/t")
        by_name = {c.name_attr: c for c in surface.snapshot().controls}
        assert by_name["dn"].label_text == "Driver name"
        assert by_name["vu"].label_text == "Vehicle use"
        assert by_name["vu"].current_value == ""
        # Two controls share a cell: neither takes the neighbouring text.
        assert by_name["a"].label_text == "" and by_name["b"].label_text == ""
    finally:
        ctx.close()