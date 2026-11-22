"""The form reconciler running the general GUI mechanisms (``FormPack.gui``).

Real headless Chromium against two small synthetic apps served in-test:

- an EMR-like medication intake: a list of medications filled as a repeating
  group, a "Continue" link the form surface does not list, and a review whose
  attestation box is ticked only on a grant;
- a SaaS setup hub: two sections that open in dialogs, completion read from
  each card, then a "Go to review" link and a submit.

A pack without ``gui`` is untouched; the other form_tx tests cover that.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from openadapt_flow.form_tx.gui_bridge import grants_for
from openadapt_flow.form_tx.gui_pack import (
    GuiAttestationSpec,
    GuiControlRef,
    GuiHubSection,
    GuiHubSpec,
    GuiMechanismsSpec,
    GuiRepeatSlot,
    GuiRepeatSpec,
    GuiStepSpec,
)
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FormPack,
    FormTxOutcome,
    InputDossier,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
)
from openadapt_flow.gui.attestations import statement_digest

CONFIRM = "I confirm this medication list is complete"


class _Site:
    def __init__(
        self,
        *,
        pharmacy: bool = False,
        ignore_saves: bool = False,
        notes: str = "",
        row_note: bool = False,
    ) -> None:
        self.notes = notes
        self.row_note = row_note
        self.state: dict[str, Any] = {"meds": [], "sections": {}}
        self.records: list[dict[str, Any]] = []
        self.pharmacy = pharmacy
        self.ignore_saves = ignore_saves

    def page(self, path: str) -> str | None:
        if path == "/meds":
            extra = (
                f'<label for="nt">Notes</label><input id="nt" value="{self.notes}">'
                if self.notes
                else ""
            )
            page = MEDS_PAGE.replace("<!--NOTES-->", extra)
            return page.replace("ROW_NOTE", "true" if self.row_note else "false")
        if path == "/review":
            rows = "".join(
                f"<div><span>Medication {i + 1}</span><span>{m['drug']} {m['dose']}</span></div>"
                for i, m in enumerate(self.state["meds"])
            )
            pharm = (
                '<label for="ph">Preferred pharmacy</label><select id="ph">'
                '<option value="">Choose one</option><option value="a">Pharmacy A</option></select>'
                if self.pharmacy
                else ""
            )
            return REVIEW_PAGE.replace("<!--ROWS-->", rows).replace("<!--PH-->", pharm)
        if path == "/hub":
            cards = "".join(
                f'<section id="card-{k}"><h2>{t}</h2><p>{"Done" if k in self.state["sections"] else "To do"}</p>'
                f"<button onclick=\"document.getElementById('dlg-{k}').showModal()\">Edit {t.lower()}</button></section>"
                f'<dialog id="dlg-{k}"><h3>{t}</h3><label for="f-{k}">{f}</label><input id="f-{k}">'
                f"<button onclick=\"save('{k}')\">Save section</button></dialog>"
                for k, t, f in (
                    ("profile", "Profile", "Company name"),
                    ("billing", "Billing", "Billing email"),
                )
            )
            return HUB_PAGE.replace("<!--CARDS-->", cards)
        if path == "/hub-review":
            return "<h1>Review</h1><button id=\"go\" onclick=\"fetch('/api/submit',{method:'POST',body:'{}'}).then(()=>location.href='/done')\">Submit setup</button>"
        if path == "/done":
            return "<h1>Done</h1>"
        return None


MEDS_PAGE = """<h1>Medications</h1><!--NOTES--><div id="list"></div>
<button type="button" id="add-med" onclick="add()">Add another medication</button>
<p><a id="next" href="/review" onclick="return keep()">Continue</a></p>
<script>
function add(){const n=document.querySelectorAll('#list fieldset').length+1;
const f=document.createElement('fieldset');
f.innerHTML='<legend>Medication '+n+'</legend><label>Drug <input class="d"></label><label>Dose <input class="s"></label>'
+(ROW_NOTE && n>1 ? '<label>Row note <input value="copied from row 1"></label>' : '');
document.getElementById('list').appendChild(f);}
add();
function keep(){const meds=[...document.querySelectorAll('#list fieldset')].map(f=>({drug:f.querySelector('.d').value,dose:f.querySelector('.s').value}));
const x=new XMLHttpRequest();x.open('POST','/api/save',false);x.send(JSON.stringify({meds}));return true;}
</script>"""

REVIEW_PAGE = (
    """<h1>Check the list</h1><!--ROWS--><!--PH-->
<p><label><input type="checkbox" id="confirm">"""
    + CONFIRM
    + """</label></p>
<button id="sub" onclick="fetch('/api/submit',{method:'POST',body:JSON.stringify({confirm:document.getElementById('confirm').checked})}).then(()=>location.href='/done')">Submit list</button>"""
)

HUB_PAGE = """<h1>Set up your workspace</h1><!--CARDS-->
<p><a href="/hub-review">Go to review</a></p>
<script>function save(k){const v=document.getElementById('f-'+k).value;
fetch('/api/section/'+k,{method:'POST',body:JSON.stringify({v})}).then(()=>location.reload());}</script>"""


def _handler(site: _Site) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            html = site.page(self.path.split("?")[0])
            if html is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send(
                f"<!doctype html><html><head><title>App</title></head><body>{html}</body></html>".encode()
            )

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            if self.path == "/api/save":
                site.state["meds"] = body["meds"]
            elif self.path.startswith("/api/section/") and not site.ignore_saves:
                site.state["sections"][self.path.rsplit("/", 1)[1]] = body["v"]
            elif self.path == "/api/submit":
                site.records.append(
                    {
                        **body,
                        "meds": site.state["meds"],
                        "sections": dict(site.state["sections"]),
                    }
                )
            self._send(b'{"ok": true}', "application/json")

    return Handler


@pytest.fixture()
def browser() -> Iterator[Any]:
    sync_pw = pytest.importorskip("playwright.sync_api")
    with sync_pw.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _serve(site: _Site) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(site))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


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


MEDS_PACK = FormPack(
    pack_id="test.gui.meds",
    domain_schema="test",
    ui_profile_id="test-gui",
    screens=(
        WizardScreenSpec(
            screen_id="review",
            landmark_selector="#confirm",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Submit list$",)),
        ),
        WizardScreenSpec(screen_id="meds", landmark_selector="#add-med"),
    ),
    gui=GuiMechanismsSpec(
        steps=(
            GuiStepSpec(
                screen_id="meds", control=GuiControlRef(role="link", name="Continue")
            ),
        ),
        repeats=(
            GuiRepeatSpec(
                group_id="meds",
                screen_id="meds",
                intake_key="medications",
                add=GuiControlRef(role="button", name="Add another medication"),
                instance_pattern=r"^Medication \d+$",
                slots=(
                    GuiRepeatSlot(role="textbox", name="Drug", item_key="drug"),
                    GuiRepeatSlot(role="textbox", name="Dose", item_key="dose"),
                ),
            ),
        ),
        attestations=(
            GuiAttestationSpec(
                attestation_id="complete",
                screen_id="review",
                control=GuiControlRef(role="checkbox", name=CONFIRM),
                statement_sha256=statement_digest(CONFIRM),
            ),
        ),
        stop_on_blank_choice=True,
    ),
)
PROFILE = UIProfileSpec(profile_id="test-gui", max_settle_ms=8000)
MEDS = [
    {"drug": "Drug A", "dose": "10 mg"},
    {"drug": "Drug B", "dose": "5 mg"},
    {"drug": "Drug C", "dose": "1 mg"},
]


def _run(
    browser: Any,
    site: _Site,
    path: str,
    pack: FormPack,
    dossier: InputDossier,
    *,
    attester: str = "",
    profile: UIProfileSpec = PROFILE,
):
    server, base = _serve(site)
    try:
        page = browser.new_context().new_page()
        page.goto(base + path)
        surface = PlaywrightFormSurface(
            page, profile, base_origin=base, ignored_mutation_paths=("/api/save*",)
        )
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(pack, dossier, run_id="run-gui")
        session.attestation_grants = grants_for(pack, attester)
        return reconciler.run_until_halt_or_done(session), session
    finally:
        server.shutdown()


def test_meds_list_link_step_and_attested_review_submit(browser: Any) -> None:
    site = _Site()
    result, session = _run(
        browser,
        site,
        "/meds",
        MEDS_PACK,
        _dossier(medications=MEDS),
        attester="clinician 7",
    )
    assert site.records == [{"confirm": True, "meds": MEDS, "sections": {}}], (
        result.what_happened
    )
    assert result.outcome is not FormTxOutcome.HALTED_NEEDS_HUMAN
    written = {w.dossier_key for w in session.writes}
    assert {
        f"medications[{i}].{k}" for i in range(3) for k in ("drug", "dose")
    } <= written


def test_without_a_grant_the_run_stops_at_the_attestation_before_submit(
    browser: Any,
) -> None:
    site = _Site()
    result, _ = _run(browser, site, "/meds", MEDS_PACK, _dossier(medications=MEDS))
    assert result.outcome is FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "no one attested" in result.halts[0].message
    assert site.records == []


def test_a_blank_question_no_binding_answers_stops_before_submit(browser: Any) -> None:
    site = _Site(pharmacy=True)
    result, _ = _run(
        browser,
        site,
        "/meds",
        MEDS_PACK,
        _dossier(medications=MEDS),
        attester="clinician 7",
    )
    assert result.outcome is FormTxOutcome.HALTED_NEEDS_HUMAN
    assert result.halts[0].field_label == "Preferred pharmacy"
    assert site.records == []


def test_an_intake_without_the_list_stops_and_adds_nothing(browser: Any) -> None:
    site = _Site()
    result, _ = _run(
        browser, site, "/meds", MEDS_PACK, _dossier(other=1), attester="clinician 7"
    )
    assert "no list 'medications'" in result.halts[0].message
    assert site.records == [] and site.state["meds"] == []


HUB_PACK = FormPack(
    pack_id="test.gui.hub",
    domain_schema="test",
    ui_profile_id="test-gui-hub",
    bindings=(
        FieldBindingSpec(
            binding_id="co",
            dossier_key="company",
            anchor=FieldAnchorSpec(label_patterns=(r"^Company name$",)),
        ),
        FieldBindingSpec(
            binding_id="be",
            dossier_key="billing_email",
            anchor=FieldAnchorSpec(label_patterns=(r"^Billing email$",)),
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="review",
            landmark_selector="#go",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Submit setup$",)),
        ),
        WizardScreenSpec(screen_id="hub", landmark_selector="#card-profile"),
    ),
    gui=GuiMechanismsSpec(
        hubs=(
            GuiHubSpec(
                screen_id="hub",
                sections=tuple(
                    GuiHubSection(
                        section_id=k,
                        opener=GuiControlRef(
                            role="button", name=f"Edit {t.lower()}", containers=(t,)
                        ),
                        dialog_name=t,
                        save=GuiControlRef(role="button", name="Save section"),
                        card=t,
                        done_tokens=("done",),
                        pending_tokens=("to", "do"),
                    )
                    for k, t in (("profile", "Profile"), ("billing", "Billing"))
                ),
                leave=GuiControlRef(
                    role="link", name="Go to review", target="/hub-review"
                ),
            ),
        ),
    ),
)
# The section editors are part of the form, not modals.
HUB_PROFILE = UIProfileSpec(
    profile_id="test-gui-hub",
    max_settle_ms=8000,
    modal_dialog_selectors=("[data-modal-open='true']",),
)


def test_saas_hub_sections_are_saved_and_read_back_before_the_review(
    browser: Any,
) -> None:
    site = _Site()
    result, _ = _run(
        browser,
        site,
        "/hub",
        HUB_PACK,
        _dossier(company="Acme Test", billing_email="ap@example.test"),
        profile=HUB_PROFILE,
    )
    assert site.records == [
        {"meds": [], "sections": {"profile": "Acme Test", "billing": "ap@example.test"}}
    ], result.what_happened


def test_saas_hub_save_the_app_ignores_stops_before_the_review(browser: Any) -> None:
    site = _Site(ignore_saves=True)
    result, _ = _run(
        browser,
        site,
        "/hub",
        HUB_PACK,
        _dossier(company="Acme Test", billing_email="ap@example.test"),
        profile=HUB_PROFILE,
    )
    assert result.outcome is FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "hub section 'profile'" in result.halts[0].message
    assert site.records == []


def test_a_prefilled_field_no_binding_covers_stops_before_the_link_is_taken(
    browser: Any,
) -> None:
    site = _Site(notes="Seen by another clinician")
    result, _ = _run(
        browser,
        site,
        "/meds",
        MEDS_PACK,
        _dossier(medications=MEDS),
        attester="clinician 7",
    )
    assert result.outcome is FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "Notes" in result.halts[0].message
    assert site.state["meds"] == [] and site.records == []


def test_a_prefilled_field_inside_an_added_row_stops_before_the_link(
    browser: Any,
) -> None:
    site = _Site(row_note=True)
    result, _ = _run(
        browser,
        site,
        "/meds",
        MEDS_PACK,
        _dossier(medications=MEDS),
        attester="clinician 7",
    )
    assert result.outcome is FormTxOutcome.HALTED_NEEDS_HUMAN
    assert "Row note" in result.halts[0].message
    assert site.state["meds"] == [] and site.records == []
