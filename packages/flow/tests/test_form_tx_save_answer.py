"""The save answer, the write ledger and the signer, on a real browser page.

* A save answered 2xx is confirmed by its status, even when the page
  navigates away before reading the reply (``await fetch(); location = ...``).
* A save that never gets a status is not confirmed.
* Every control the run actuated must be in the ledger it signs.
* A signer refusal is a stop, never an exception, including after a human
  decision.
"""

from __future__ import annotations

import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, sync_playwright  # noqa: E402

from openadapt_flow.form_tx import reconciler as reconciler_module  # noqa: E402
from openadapt_flow.form_tx.playwright_surface import (  # noqa: E402
    PlaywrightFormSurface,
)
from openadapt_flow.form_tx.reconciler import (  # noqa: E402
    FormReconciler,
    ReconcilerSession,
)
from openadapt_flow.form_tx.types import (  # noqa: E402
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FormPack,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    InputDossier,
    ProvenanceKind,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

_FORM = """<!doctype html><html><body><main id="entry">
<label for="name">Display name</label><input id="name" required>
<button id="save" type="button">Save</button>
</main><script>
document.getElementById('save').addEventListener('click', async () => {
  const body = JSON.stringify({name: document.getElementById('name').value});
  %s
  location.href = '/summary';
});
</script></body></html>"""

# The common page pattern: wait for the status, then leave without reading
# the reply. Leaving cuts the reply off.
_SUBMIT_THEN_LEAVE = "await fetch('/api/submit', {method: 'POST', body});"


class _Portal:
    """A local site whose save reply is still streaming when the page leaves.

    The status and headers go out at once; the rest of the body only after a
    pause, so a page that navigates right after ``await fetch()`` cuts it off.
    """

    def __init__(
        self, *, answer: bool = True, answered_saves: int = 99, script: str = ""
    ) -> None:
        self.answer = answer
        # Saves after this many get no status at all.
        self.answered_saves = answered_saves
        self.posts = 0
        self.script = script or _SUBMIT_THEN_LEAVE
        self.saved: dict[str, str] = {}
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _html(self, body: str) -> None:
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/summary"):
                    self._html(
                        '<main id="summary"><label for="saved-name">Display '
                        'name</label><input id="saved-name" readonly value="'
                        + portal.saved.get("name", "")
                        + '"></main>'
                    )
                else:
                    self._html(_FORM % portal.script)

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n).decode() or "{}"
                portal.posts += 1
                if not portal.answer or portal.posts > portal.answered_saves:
                    # Drop the connection with no status at all.
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                portal.saved = json.loads(data)
                reply = json.dumps({"ok": True, "pad": "x" * 4096}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                try:
                    self.wfile.write(reply[:8])
                    self.wfile.flush()
                    time.sleep(2.0)
                    self.wfile.write(reply[8:])
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _pack() -> FormPack:
    entry = FieldBindingSpec(
        binding_id="name",
        dossier_key="person.name",
        anchor=FieldAnchorSpec(css_selector="#name"),
        required_in_pack=True,
    )
    readback = entry.model_copy(
        update={
            "binding_id": "saved-name",
            "anchor": FieldAnchorSpec(css_selector="#saved-name"),
        }
    )
    return FormPack(
        pack_id="save-answer",
        domain_schema="synthetic-contact",
        ui_profile_id="standard",
        bindings=(entry,),
        screens=(
            WizardScreenSpec(
                screen_id="entry",
                landmark_selector="#entry",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(css_selector="#save"),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_landmark_selector="#summary",
            field_readbacks=(readback,),
        ),
        commit_url_patterns=("/api/submit",),
    )


def _dossier(name: str | None = "Synthetic Name") -> InputDossier:
    facts = (
        {
            "person.name": DossierFact(
                key="person.name",
                value=name,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake",
            )
        }
        if name is not None
        else {}
    )
    return InputDossier(dossier_id="d-1", request_id="r-1", facts=facts)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel="chromium", headless=True)
        except Exception:
            b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _start(
    browser: Browser, portal: _Portal, dossier: InputDossier
) -> tuple[Any, PlaywrightFormSurface, FormReconciler, ReconcilerSession]:
    ctx = browser.new_context()
    page = ctx.new_page()
    surface = PlaywrightFormSurface(
        page, UIProfileSpec(profile_id="standard"), base_origin=portal.origin
    )
    surface.goto(f"{portal.origin}/form")
    reconciler = FormReconciler(surface)
    return ctx, surface, reconciler, reconciler.create_session(_pack(), dossier)


def test_2xx_save_then_leaving_the_page_is_confirmed_by_its_status(
    browser: Browser,
) -> None:
    portal = _Portal()
    ctx, surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert portal.saved == {"name": "Synthetic Name"}
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened
    assert result.receipt is not None
    assert not surface._failed_page_mutation_seqs
    # The page really cut the reply off: this is the path under test.
    assert surface._body_cut_off_mutation_seqs, (
        "the reply was not cut off, so this test does not cover the pattern"
    )


def test_save_that_never_gets_a_status_is_not_confirmed(browser: Browser) -> None:
    portal = _Portal(answer=False)
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN, result.what_happened
    assert result.receipt is None


def test_actuation_missing_from_the_ledger_is_not_signed(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ReconcilerSession.record_write

    def drop_the_name_field(self: ReconcilerSession, **kwargs: Any) -> None:
        # A write path that forgets to note the control it changed.
        if kwargs.get("selector") == "#name":
            return
        original(self, **kwargs)

    monkeypatch.setattr(ReconcilerSession, "record_write", drop_the_name_field)
    portal = _Portal()
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert portal.saved == {"name": "Synthetic Name"}
    assert session._actuations, "the run actuated nothing"
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, result.what_happened
    assert result.receipt is None
    assert "write ledger does not record" in result.what_happened
    assert "#name" in result.what_happened


def test_signer_refusal_after_a_human_decision_is_a_stop(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = _Portal()
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier(None))
    try:
        first = reconciler.run_until_halt_or_done(session)
        assert first.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN, first.what_happened

        def refuse(**_kwargs: Any) -> Any:
            raise ValueError("planted signer refusal")

        monkeypatch.setattr(reconciler_module, "issue_form_tx_receipt", refuse)
        result = reconciler.apply_human_resolution(
            session,
            HumanResolutionInput(
                halt_id=first.halts[0].halt_id,
                action=HumanDecisionAction.PROVIDE_VALUE,
                actor_id="operator-1",
                provided_value="Synthetic Name",
                idempotency_key="decision-1",
            ),
        )
    finally:
        ctx.close()
        portal.close()
    assert portal.saved == {"name": "Synthetic Name"}
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, result.what_happened
    assert result.receipt is None
    assert result.safe_to_retry is False
    assert "planted signer refusal" in result.what_happened


def test_a_control_filled_more_than_once_counts_once_on_the_receipt(
    browser: Browser,
) -> None:
    portal = _Portal()
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier())
    # Earlier fills of the same control (before a sign-in, say) raise the
    # actuation counter but not the number of fields.
    session.fields_filled_auto = 2
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert result.outcome == FormTxOutcome.VERIFIED_DONE, result.what_happened
    assert result.receipt is not None
    assert result.receipt.payload.fields_filled_auto == 1


def test_an_auto_fill_the_ledger_records_under_another_source_is_not_signed(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fill reaches the ledger, so the unrecorded-actuation check passes,
    # but not as an auto-fill. The session's own record of what it auto-filled
    # disagrees with the ledger, and the signer must refuse. A count derived
    # from the ledger itself would agree with the ledger and sign.
    original = ReconcilerSession.record_write

    def relabel_the_fill(self: ReconcilerSession, **kwargs: Any) -> None:
        if kwargs.get("source") == "auto_fill":
            kwargs["source"] = "kept_matching"
        original(self, **kwargs)

    monkeypatch.setattr(ReconcilerSession, "record_write", relabel_the_fill)
    portal = _Portal()
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert portal.saved == {"name": "Synthetic Name"}
    assert session.auto_filled_subject_count == 1
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, result.what_happened
    assert result.receipt is None
    assert "auto-fill count does not match" in result.what_happened


def test_the_session_counts_each_auto_filled_control_once() -> None:
    session = ReconcilerSession(_pack(), _dossier())
    fill = {
        "dossier_key": "name",
        "label": "Name",
        "selector": "#name",
        "in_popup_subroutine": None,
    }
    assert session.note_auto_fill(**fill) is True
    session.record_write(source="auto_fill", **fill)
    # Filled again after a sign-in: still one field.
    assert session.note_auto_fill(**fill) is False
    session.record_write(source="auto_fill", **fill)
    assert session.auto_filled_subject_count == 1
    # An operator's value later replaces the auto-fill on the same control.
    session.record_write(source="operator_value", **fill)
    assert session.auto_filled_subject_count == 0
    assert [w.source for w in session.writes] == ["operator_value"]


def test_a_later_save_with_no_status_vetoes_an_earlier_2xx(browser: Browser) -> None:
    # The first save is answered 2xx and read; the second never gets a
    # status. Nothing confirms that the record ended up saved.
    portal = _Portal(
        answered_saves=1,
        script=(
            "await (await fetch('/api/submit', {method: 'POST', body})).text();\n"
            "  try { await fetch('/api/submit', {method: 'POST', body}); }"
            " catch (e) {}"
        ),
    )
    ctx, surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert portal.posts == 2
    assert surface._failed_page_mutation_seqs
    assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN, result.what_happened
    assert result.receipt is None


def test_a_write_noted_for_another_popup_does_not_cover_an_actuation(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ReconcilerSession.record_write

    def note_it_elsewhere(self: ReconcilerSession, **kwargs: Any) -> None:
        # The ledger names the same selector, but in a popup the run never
        # opened: it is another control.
        if kwargs.get("selector") == "#name":
            kwargs["in_popup_subroutine"] = "elsewhere"
        original(self, **kwargs)

    monkeypatch.setattr(ReconcilerSession, "record_write", note_it_elsewhere)
    portal = _Portal()
    ctx, _surface, reconciler, session = _start(browser, portal, _dossier())
    try:
        result = reconciler.run_until_halt_or_done(session)
    finally:
        ctx.close()
        portal.close()
    assert result.receipt is None
    assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, result.what_happened
    assert "write ledger does not record" in result.what_happened
    assert "main|" in result.what_happened
