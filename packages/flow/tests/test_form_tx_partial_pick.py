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


def _dossier(**values: Any) -> InputDossier:
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


def _commit_screen(landmark: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id="form",
        landmark_selector=landmark,
        is_commit_screen=True,
        commit_button_anchor=FieldAnchorSpec(label_patterns=("^Submit quote$",)),
    )


def _address_pack() -> FormPack:
    return FormPack(
        pack_id="address-test",
        domain_schema="test.quote/v1",
        ui_profile_id="layout-test",
        bindings=(
            FieldBindingSpec(
                binding_id="addr",
                dossier_key="home.address_full",
                anchor=FieldAnchorSpec(label_patterns=("^Street address$",)),
                control_kind=ControlKind.PORTAL_COMBOBOX,
                codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                typeahead_query_chars=6,
            ),
        ),
        screens=(_commit_screen("#go"),),
    )


def _run_address(browser: Any, inert: bool) -> tuple[Any, list[dict]]:
    body = ADDRESS.replace("<script>", f"<script>const INERT = {str(inert).lower()};")
    ctx, page, submissions = _serve(browser, {"/a": body})
    try:
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/a")
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(
            _address_pack(),
            _dossier(**{"home.address_full": "2210 Juniper Loop Apt 4, Brackenfield, OR"}),
        )
        return reconciler.run_until_halt_or_done(session), submissions
    finally:
        ctx.close()


def test_pick_that_keeps_the_leading_part_of_its_label_is_accepted(
    browser: Any,
) -> None:
    result, submissions = _run_address(browser, inert=False)
    assert not result.halts, [h.message for h in result.halts]
    assert submissions == [{"addr": "2210 Juniper Loop"}]


def test_typed_text_left_in_the_input_is_not_taken_as_a_pick(browser: Any) -> None:
    result, submissions = _run_address(browser, inert=True)
    assert submissions == []
    assert result.halts[0].kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert result.halts[0].field_label == "Street address"