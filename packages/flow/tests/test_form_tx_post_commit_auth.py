"""Headless Chromium tests: a sign-in wall after the submit click, and
same-named radio groups in two forms.

After the commit control is clicked, the record may or may not exist, so a
session-expired dialog or a sign-in page must end ``delivery_uncertain`` with
no way to resume. Pages are served through ``context.route``; no app names.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.surface import radio_group_key  # noqa: E402
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
    HumanDecisionAction,
    InputDossier,
    ModalRuleSpec,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
)

ORIGIN = "http://postcommit.local"

CONFIRM_THEN_EXPIRE = """<!doctype html><html><body>
<form id="f" onsubmit="return false">
  <label for="nm">Full name</label><input id="nm" name="nm">
  <button id="go" type="button">Submit quote</button>
</form>
<div id="confirm" role="dialog" aria-modal="true" hidden>
  <p>Submit this quote?</p><button id="yes" type="button">Confirm</button>
</div>
<div id="expired" role="dialog" aria-modal="true" hidden>
  <p>Your session has expired. Please sign in again.</p>
</div>
<script>
document.getElementById('go').onclick = () => { document.getElementById('confirm').hidden = false; };
document.getElementById('yes').onclick = async () => {
  document.getElementById('confirm').hidden = true;
  await fetch('/api/submit', {method: 'POST', body: document.getElementById('nm').value});
  document.getElementById('expired').hidden = false;
};
</script></body></html>"""

THEN_LOGIN = """<!doctype html><html><body>
<form id="f" onsubmit="return false">
  <label for="nm">Full name</label><input id="nm" name="nm">
  <button id="go" type="button">Submit quote</button>
</form>
<script>
document.getElementById('go').onclick = async () => {
  await fetch('/api/submit', {method: 'POST', body: document.getElementById('nm').value});
  location.href = '/login';
};
</script></body></html>"""

LOGIN = """<!doctype html><html><body><h1>Agent sign-in</h1>
<label for="u">User</label><input id="u">
<label for="p">Password</label><input id="p" type="password">
</body></html>"""

TWO_FORMS = """<!doctype html><html><body>
<form id="a" onsubmit="return false"><fieldset><legend>First home</legend>
  <label><input type="radio" name="pets" value="yes"> Yes</label>
  <label><input type="radio" name="pets" value="no"> No</label>
</fieldset></form>
<form id="b" onsubmit="return false"><fieldset><legend>Second home</legend>
  <label><input type="radio" name="pets" value="yes" checked> Yes</label>
  <label><input type="radio" name="pets" value="no"> No</label>
</fieldset>
<button id="go" type="button">Submit quote</button></form>
<script>
document.getElementById('go').onclick = async () => {
  await fetch('/api/submit', {method: 'POST', body: 'x'});
  document.body.innerHTML = '<h1>Saved</h1>';
};
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


def _serve(browser: Any, pages: dict[str, str]) -> tuple[Any, Any, list[str]]:
    posts: list[str] = []

    def handle(route: Any) -> None:
        req = route.request
        path = urlsplit(req.url).path
        if path == "/api/submit":
            posts.append(req.post_data or "")
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path in pages:
            route.fulfill(status=200, content_type="text/html", body=pages[path])
        else:
            route.fulfill(status=404, body="")

    ctx = browser.new_context()
    ctx.route(f"{ORIGIN}/**", handle)
    return ctx, ctx.new_page(), posts


def _profile() -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="post-commit-test",
        debounce_grace_ms=40,
        settle_window_ms=80,
        max_settle_ms=5000,
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


def _name_pack(**extra: Any) -> FormPack:
    return FormPack(
        pack_id="post-commit-test",
        domain_schema="test.quote/v1",
        ui_profile_id="post-commit-test",
        bindings=(
            FieldBindingSpec(
                binding_id="nm",
                dossier_key="applicant.name",
                anchor=FieldAnchorSpec(label_patterns=("^Full name$",)),
            ),
        ),
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
        **extra,
    )


def _run(page: Any, url: str, pack: FormPack, dossier: InputDossier) -> Any:
    surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
    surface.goto(url)
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier)
    return reconciler.run_until_halt_or_done(session)


def _assert_uncertain(result: Any) -> None:
    assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN, result.what_happened
    assert result.safe_to_retry is False
    halt = result.halts[0]
    assert halt.kind == FormHaltKind.AUTH_WALL_DETECTED
    assert HumanDecisionAction.FIXED_ON_SCREEN not in halt.allowed_actions
    assert result.receipt is None


def test_expiry_dialog_after_a_confirmed_submit_is_delivery_uncertain(
    browser: Any,
) -> None:
    ctx, page, posts = _serve(browser, {"/q": CONFIRM_THEN_EXPIRE})
    try:
        pack = _name_pack(
            modal_rules=(
                ModalRuleSpec(
                    modal_id="confirm",
                    container_selector="#confirm",
                    confirm_selector="#yes",
                    policy="confirm_if_dossier_flag",
                    dossier_flag_key="flow.confirm",
                ),
            )
        )
        result = _run(
            page,
            f"{ORIGIN}/q",
            pack,
            _dossier(**{"applicant.name": "Marisol Okafor", "flow.confirm": "Yes"}),
        )
    finally:
        ctx.close()
    assert posts == ["Marisol Okafor"]
    _assert_uncertain(result)


def test_sign_in_page_after_submit_without_a_banner_is_delivery_uncertain(
    browser: Any,
) -> None:
    ctx, page, posts = _serve(browser, {"/q": THEN_LOGIN, "/login": LOGIN})
    try:
        result = _run(
            page,
            f"{ORIGIN}/q",
            _name_pack(),
            _dossier(**{"applicant.name": "Ana Ruiz"}),
        )
    finally:
        ctx.close()
    assert posts == ["Ana Ruiz"]
    _assert_uncertain(result)


def test_same_named_groups_in_two_forms_stay_apart_and_a_conflict_stops(
    browser: Any,
) -> None:
    ctx, page, posts = _serve(browser, {"/q": TWO_FORMS})
    try:
        surface = PlaywrightFormSurface(page, _profile(), base_origin=ORIGIN)
        surface.goto(f"{ORIGIN}/q")
        radios = [c for c in surface.snapshot().controls if c.input_type == "radio"]
        assert len({radio_group_key(c) for c in radios}) == 2
        pack = FormPack(
            pack_id="two-forms-test",
            domain_schema="test.quote/v1",
            ui_profile_id="post-commit-test",
            bindings=(
                FieldBindingSpec(
                    binding_id="pets_b",
                    dossier_key="second.pets",
                    anchor=FieldAnchorSpec(
                        name_or_id_patterns=("^pets$",),
                        section_patterns=("^Second home$",),
                    ),
                    control_kind=ControlKind.RADIO_GROUP,
                    codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
                ),
            ),
            screens=(
                WizardScreenSpec(
                    screen_id="form",
                    landmark_selector="#b",
                    is_commit_screen=True,
                    commit_button_anchor=FieldAnchorSpec(
                        label_patterns=("^Submit quote$",)
                    ),
                ),
            ),
        )
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(pack, _dossier(**{"second.pets": "No"}))
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
    assert posts == []
    assert result.halts[0].kind == FormHaltKind.PREFILL_VALUE_CONFLICT
