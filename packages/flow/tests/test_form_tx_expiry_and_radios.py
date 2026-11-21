"""Session expiry is a sign-in wall, and an unchecked radio group is empty.

Real headless Chromium against a small stdlib HTTP portal served in-test: a
two-step wizard (applicant step with a Yes/No radio group, then a review step
with Submit) and a confirmation page that shows the stored record.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.surface import match_anchor_in_snapshot
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
    ProvenanceKind,
    ReadbackStatus,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)


class _Portal:
    def __init__(self) -> None:
        self.authed = False
        self.precheck_pool: Optional[str] = None
        self.expire_on_step = False  # redirect to sign-in with a banner
        self.soft_expire_on_step = False  # banner only, no password field
        self.draft: dict[str, str] = {}
        self.records: list[dict[str, str]] = []


def _page(body: str) -> bytes:
    return (
        "<!doctype html><html><head><title>Quote</title></head><body>"
        + body
        + "</body></html>"
    ).encode()


def _radios(name: str, checked: Optional[str], *, disabled: bool = False) -> str:
    out = []
    for value, label in (("yes", "Yes"), ("no", "No")):
        attrs = " checked" if checked == value else ""
        attrs += " disabled" if disabled else " required"
        out.append(
            f'<label><input type="radio" name="{name}" value="{value}"{attrs}> {label}</label>'
        )
    return "".join(out)


def _make_handler(portal: _Portal) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where: str) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode()
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def _login_page(self, expired: bool) -> None:
            banner = (
                '<div role="alert">Your session has expired. Please sign in again.</div>'
                if expired
                else ""
            )
            self._send(
                _page(
                    banner + '<form method="post" action="/login">'
                    '<label for="user">User</label><input id="user" name="user">'
                    '<label for="password">Password</label>'
                    '<input id="password" name="password" type="password">'
                    '<button id="signin" type="submit">Sign in</button></form>'
                )
            )

        def _step1(self, banner: str = "") -> None:
            pool = portal.draft.get("pool") or portal.precheck_pool
            self._send(
                _page(
                    banner + '<main id="step1"><h1>Applicant</h1>'
                    '<form method="post" action="/step1">'
                    '<label for="first_name">First name</label>'
                    f'<input id="first_name" name="first_name" required value="{portal.draft.get("first_name", "")}">'
                    "<fieldset><legend>Does the home have a pool?</legend>"
                    + _radios("pool", pool)
                    + '</fieldset><button id="next" type="submit">Next</button>'
                    "</form></main>"
                )
            )

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if url.path == "/login":
                self._login_page("expired" in parse_qs(url.query))
                return
            if not portal.authed:
                self._redirect("/login?expired=1")
                return
            if url.path == "/step1":
                self._step1()
            elif url.path == "/review":
                self._send(
                    _page(
                        '<main id="review"><h1>Review</h1>'
                        '<form method="post" action="/submit">'
                        '<button id="submit" type="submit">Submit quote</button>'
                        "</form></main>"
                    )
                )
            elif url.path == "/done":
                rec = portal.records[-1] if portal.records else {}
                self._send(
                    _page(
                        '<main id="done"><h1>Quote saved</h1>'
                        '<label for="first_name_ro">First name</label>'
                        f'<input id="first_name_ro" readonly value="{rec.get("first_name", "")}">'
                        "<fieldset><legend>Pool</legend>"
                        + _radios("pool_ro", rec.get("pool"), disabled=True)
                        + "</fieldset></main>"
                    )
                )
            else:
                self._send(_page("not found"), 404)

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            form = self._form()
            if url.path == "/login":
                if form.get("user") and form.get("password"):
                    portal.authed = True
                    self._redirect("/step1")
                else:
                    self._login_page(True)
                return
            if url.path == "/step1":
                if portal.expire_on_step:
                    portal.expire_on_step = False
                    portal.authed = False
                    self._redirect("/login?expired=1")
                    return
                if portal.soft_expire_on_step:
                    self._step1(
                        '<div role="alert">Your session timed out. '
                        "Log in again to continue.</div>"
                    )
                    return
                portal.draft.update(form)
                self._redirect("/review")
                return
            if url.path == "/submit" and portal.authed:
                portal.records.append(dict(portal.draft))
                self._redirect("/done")
                return
            self._send(_page("bad request"), 400)

    return Handler


@pytest.fixture()
def portal_env() -> Iterator[tuple[_Portal, str]]:
    portal = _Portal()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(portal))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield portal, f"http://127.0.0.1:{server.server_address[1]}"
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


_PROFILE = UIProfileSpec(profile_id="test-expiry-radios", max_settle_ms=8000)
_YES_NO = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)


def _pack(*, bind_pool: bool = True) -> FormPack:
    bindings = [
        FieldBindingSpec(
            binding_id="first_name",
            dossier_key="applicant.first_name",
            anchor=FieldAnchorSpec(name_or_id_patterns=(r"^first_name(?:\s|$)",)),
        )
    ]
    if bind_pool:
        bindings.append(
            FieldBindingSpec(
                binding_id="pool",
                dossier_key="property.has_pool",
                anchor=FieldAnchorSpec(name_or_id_patterns=(r"^pool$",)),
                control_kind=ControlKind.RADIO_GROUP,
                codec=_YES_NO,
            )
        )
    readbacks = [
        FieldBindingSpec(
            binding_id="rb_first_name",
            dossier_key="applicant.first_name",
            anchor=FieldAnchorSpec(name_or_id_patterns=(r"^first_name_ro$",)),
        ),
        FieldBindingSpec(
            binding_id="rb_pool",
            dossier_key="property.has_pool",
            anchor=FieldAnchorSpec(name_or_id_patterns=(r"^pool_ro$",)),
            control_kind=ControlKind.RADIO_GROUP,
            codec=_YES_NO,
        ),
    ]
    return FormPack(
        pack_id="test-expiry-radios",
        domain_schema="test.quote/v1",
        ui_profile_id=_PROFILE.profile_id,
        bindings=tuple(bindings),
        screens=(
            WizardScreenSpec(
                screen_id="step1",
                landmark_selector="#step1",
                next_button_anchor=FieldAnchorSpec(css_selector="#next"),
            ),
            WizardScreenSpec(
                screen_id="review",
                landmark_selector="#review",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(css_selector="#submit"),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#done",
            field_readbacks=tuple(readbacks),
        ),
    )


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


def _start(browser: Any, base: str, pack: FormPack, dossier: InputDossier):
    page = browser.new_context().new_page()
    surface = PlaywrightFormSurface(
        page,
        _PROFILE,
        base_origin=base,
        ignored_mutation_paths=("/step1", "/login"),
    )
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier, run_id="run-expiry-radios")
    return page, surface, reconciler, session


def _sign_in(page: Any) -> None:
    page.fill("#user", "operator")
    page.fill("#password", "synthetic-test-pass")
    page.click("#signin")
    page.wait_for_load_state()


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


def test_expiry_redirect_is_a_sign_in_wall_and_sign_in_resumes(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    portal.expire_on_step = True
    page, _, reconciler, session = _start(
        browser,
        base,
        _pack(),
        _dossier(**{"applicant.first_name": "Avery", "property.has_pool": "No"}),
    )
    page.goto(base + "/step1")
    first = reconciler.run_until_halt_or_done(session)
    assert first.outcome == FormTxOutcome.AUTH_REQUIRED
    assert first.safe_to_retry is True
    (halt,) = first.halts
    assert halt.kind == FormHaltKind.AUTH_WALL_DETECTED
    assert HumanDecisionAction.FIXED_ON_SCREEN in halt.allowed_actions
    assert portal.records == []

    _sign_in(page)
    done = _resolve(reconciler, session, halt, HumanDecisionAction.FIXED_ON_SCREEN)
    assert done.outcome == FormTxOutcome.VERIFIED_DONE, done.what_happened
    assert portal.records == [{"first_name": "Avery", "pool": "no"}]
    statuses = {v.dossier_key: v.status for v in done.summary_verdict.field_verdicts}
    assert statuses["property.has_pool"] == ReadbackStatus.PASSED


def test_expiry_banner_without_password_field_is_a_sign_in_wall(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    portal.soft_expire_on_step = True
    page, _, reconciler, session = _start(
        browser,
        base,
        _pack(),
        _dossier(**{"applicant.first_name": "Avery", "property.has_pool": "Yes"}),
    )
    page.goto(base + "/step1")
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.AUTH_REQUIRED, result.what_happened
    assert result.halts[0].kind == FormHaltKind.AUTH_WALL_DETECTED
    assert "timed out" in result.halts[0].message
    # The banner is still there, so "fixed on screen" is refused.
    still = _resolve(
        reconciler, session, result.halts[0], HumanDecisionAction.FIXED_ON_SCREEN
    )
    assert still.outcome == FormTxOutcome.AUTH_REQUIRED
    assert portal.records == []


def test_unchecked_radios_read_empty_and_collapse_to_one_field(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    page, surface, _, _ = _start(browser, base, _pack(), _dossier())
    page.goto(base + "/step1")
    snap = surface.snapshot()
    radios = [c for c in snap.controls if c.name_attr == "pool"]
    assert len(radios) == 2
    assert all(c.current_value == "" and c.checked is False for c in radios)
    assert {c.radio_value for c in radios} == {"yes", "no"}
    assert {o.label for o in radios[0].options} == {"Yes", "No"}
    group = match_anchor_in_snapshot(
        FieldAnchorSpec(name_or_id_patterns=(r"^pool$",)), snap
    )
    assert group is not None and group.current_value == ""


def test_unchecked_required_group_with_no_fact_is_one_halt(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    page, _, reconciler, session = _start(
        browser,
        base,
        _pack(bind_pool=False),
        _dossier(**{"applicant.first_name": "Avery"}),
    )
    page.goto(base + "/step1")
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, result.what_happened
    pool_halts = [
        h
        for h in result.halts
        if h.kind == FormHaltKind.UNGROUNDED_REQUIRED_FIELD
        and h.dom_selector
        and "pool" in h.dom_selector
    ]
    assert len(pool_halts) == 1
    assert {o.value for o in pool_halts[0].available_options} == {"yes", "no"}
    assert portal.records == []


def test_prechecked_radio_that_contradicts_intake_stops(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    portal.precheck_pool = "yes"
    page, _, reconciler, session = _start(
        browser,
        base,
        _pack(),
        _dossier(**{"applicant.first_name": "Avery", "property.has_pool": "No"}),
    )
    page.goto(base + "/step1")
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, result.what_happened
    (halt,) = result.halts
    assert halt.kind == FormHaltKind.PREFILL_VALUE_CONFLICT
    assert halt.portal_value == "yes"
    assert portal.records == []


def test_group_set_from_intake_reads_back_passed(
    portal_env: tuple[_Portal, str], browser: Any
) -> None:
    portal, base = portal_env
    portal.authed = True
    page, _, reconciler, session = _start(
        browser,
        base,
        _pack(),
        _dossier(**{"applicant.first_name": "Avery", "property.has_pool": "Yes"}),
    )
    page.goto(base + "/step1")
    result = reconciler.run_until_halt_or_done(session)
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened
    assert result.receipt is not None
    assert portal.records == [{"first_name": "Avery", "pool": "yes"}]
