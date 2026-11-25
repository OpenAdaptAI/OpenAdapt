"""End-to-end Shadow -> Draft-to-Commit -> Micro-Handoff -> Verified Autopilot test."""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    CommitInterceptor,
    MicroHandoffSession,
    ObligationLedger,
    ReadBackProof,
    generate_ephemeral_ed25519_keypair,
    induce_pack_from_extension_recordings,
    mint_receipt,
    parse_extension_evidence_jsonl,
    qualify_verifier_offline,
    replay_evidence_into_interceptor,
    stage_form,
    verify_receipt,
)

CARRIER_PORTAL_HTML = """<!doctype html>
<html><body>
  <input type="search" role="searchbox" id="quote-search" placeholder="Search existing quotes" />
  <form id="quote-form" onsubmit="event.preventDefault(); submitQuote();">
    <label for="policy_number">Policy Number</label>
    <input id="policy_number" name="policy_number" type="text" />
    <label for="postal_code">Postal Code</label>
    <input id="postal_code" name="postal_code" type="text" />
    <label for="state_code">State</label>
    <input id="state_code" name="state_code" type="text" />
    <label for="roof_material">Roof Material</label>
    <select id="roof_material" name="roof_material">
      <option value="">Select</option>
      <option value="comp_shingle">Composition Shingle</option>
      <option value="wood_shake">Wood Shake</option>
    </select>
    <button type="submit" data-testid="submit-quote-btn">Submit Quote</button>
  </form>
  <div id="confirmation-summary"></div>
  <script>
    function submitQuote() {
      const p = document.getElementById('policy_number').value;
      const z = document.getElementById('postal_code').value;
      const r = document.getElementById('roof_material').value;
      document.getElementById('confirmation-summary').innerText =
        'QUOTE_CONFIRMED REF=QT-8842 POLICY=' + p + ' ZIP=' + z + ' ROOF=' + r;
    }
  </script>
</body></html>"""


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_shadow_to_autopilot_copilot_e2e_on_live_chromium(tmp_path: Path) -> None:
    # Phase 1: Passive Shadow Mode ingests 2 live extension_evidence.jsonl traces
    ev1 = tmp_path / "session1.jsonl"
    ev2 = tmp_path / "session2.jsonl"
    _write_jsonl(
        ev1,
        [
            {"kind": "input", "frame_origin": "https://portal.example", "field": "password", "value": "<secret>"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "quote-search", "role": "searchbox", "value": "old"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "policy_number", "value": "POL-101"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "postal_code", "value": "74103"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "state_code", "value": "OK"},
            {"kind": "network_save", "method": "POST", "url": "https://portal.example/api/quotes", "status": 201},
            {"kind": "readback", "url": "https://portal.example/quotes/QT-101", "text": "QUOTE_CONFIRMED REF=QT-101 POLICY=POL-101 ZIP=74103"},
        ],
    )
    _write_jsonl(
        ev2,
        [
            {"kind": "input", "frame_origin": "https://portal.example", "field": "policy_number", "value": "POL-202"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "postal_code", "value": "73102"},
            {"kind": "input", "frame_origin": "https://portal.example", "field": "state_code", "value": "OK"},
            {"kind": "network_save", "method": "POST", "url": "https://portal.example/api/quotes", "status": 201},
            {"kind": "readback", "url": "https://portal.example/quotes/QT-202", "text": "QUOTE_CONFIRMED REF=QT-202 POLICY=POL-202 ZIP=73102"},
        ],
    )

    s1 = parse_extension_evidence_jsonl(ev1)
    assert s1.secret_fields_skipped == 1
    assert "quote-search" not in s1.submitted_fields
    assert replay_evidence_into_interceptor(s1, CommitInterceptor()) is True

    pack = induce_pack_from_extension_recordings(
        "home-quote-v1",
        [
            (ev1, {"dossier_policy": "POL-101", "dossier_zip": "74103"}, "QT-101"),
            (ev2, {"dossier_policy": "POL-202", "dossier_zip": "73102"}, "QT-202"),
        ],
    )
    assert pack.static_defaults == {"state_code": "OK"}
    assert pack.provenance_map == {"policy_number": "dossier_policy", "postal_code": "dossier_zip"}

    # Offline 4-mutant qualification (zero staging DB)
    def verifier_fn(fields: dict[str, str], text: str) -> bool:
        compact = " ".join(text.split())
        return all(bool(re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact)) for v in fields.values())

    qual = qualify_verifier_offline(
        expected_fields={"policy_number": "POL-101", "postal_code": "74103"},
        pre_write_text="New Home Quote Form",
        post_write_text=s1.readback_text,
        verifier_fn=verifier_fn,
    )
    assert qual.qualified is True

    # Phase 2: Draft-to-Commit + 1-widget Micro-Handoff + Verified Autopilot on live Chromium
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(CARRIER_PORTAL_HTML)

        ledger = ObligationLedger("ep-quote-8842", subject_key="POL-8842")
        ledger.add_obligation("submit_quote", "Submit home quote", target_system="carrier")
        ledger.record_ssa_literal("policy_number", "POL-8842", source_window="dossier", turn=0)
        ledger.record_ssa_literal("postal_code", "74104", source_window="dossier", turn=0)

        interceptor = CommitInterceptor()
        staged = stage_form(
            page,
            {"policy_number": "POL-8842", "postal_code": "74104", **pack.static_defaults},
            ledger=ledger,
            opaque_fields=["policy_number", "postal_code"],
            interceptor=interceptor,
        )
        assert staged.ok is True

        # Micro-Handoff handles an unseen roof_material choice once, then commits and verifies
        mh = MicroHandoffSession(
            pack,
            [{"step_id": "roof_pick", "branch_key": "roof_1998_shake", "resolved": False}],
        )
        st, req = mh.run_until_blocked(lambda step: bool(step.get("resolved")))
        assert st == "AWAITING_MICRO_HANDOFF" and req is not None
        mh.resolve_micro_handoff({"resolved": True, "roof_material": "wood_shake"})
        assert pack.edge_branches["roof_1998_shake"]["roof_material"] == "wood_shake"

        # Apply resolved choice, submit, clear dirty buffer after confirmed read-back
        page.locator("#roof_material").select_option(pack.edge_branches["roof_1998_shake"]["roof_material"])
        page.locator('[data-testid="submit-quote-btn"]').click()
        summary_text = page.locator("#confirmation-summary").inner_text()
        assert verifier_fn({"policy_number": "POL-8842", "postal_code": "74104"}, summary_text)

        interceptor.clear_dirty_fields()
        ledger.mark_committed(
            "submit_quote",
            ReadBackProof(
                channel="fresh_reload",
                url_or_locator="https://portal.example/quotes/QT-8842",
                cited_substrings=["QT-8842", "POL-8842", "74104"],
                external_ref="QT-8842",
            ),
        )
        decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))
        assert decision.outcome == "VERIFIED"

        priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
        receipt = mint_receipt(ledger, "home-quote-v1", priv_pem)
        assert verify_receipt(receipt, pub_pem) is True

        browser.close()
