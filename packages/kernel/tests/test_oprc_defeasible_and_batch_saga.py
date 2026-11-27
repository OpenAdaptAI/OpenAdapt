"""Tests for BijectiveSymbolCodec $var isomorphism, twin-before-detokenize, OPRC, defeasible guard, and BatchSaga."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    BatchSagaCheckpoint,
    BijectiveSymbolCodec,
    EgressPolicy,
    LocalEgressGuard,
    OrderPreservingRankCodec,
    RawDemoTrace,
    SOPBranchSpec,
    capture_zero_phi_digital_twin,
    evaluate_defeasible_branch,
    induce_cartridge_from_raw_demos,
    inspect_outbound_payload,
    qualify_app_cartridge_offline,
    reconcile_batch_after_reauth,
    replay_distilled_zero_phi_repair,
    self_heal_in_digital_twin,
    verify_per_artifact_note_citations,
)
from openadapt_kernel.egress_guard import _HTML_ARIA_STRUCTURAL_VOCAB


def test_bijective_var_isomorphism_and_german_japanese_twin_heal_before_detokenize(
    tmp_path: Path,
) -> None:
    """Test 1: $Prüfgebühr maps to $SYM_* matching <label>SYM_*</label> 1-to-1; twin executes SYM_* select & detokenizes for live replay."""
    codec = BijectiveSymbolCodec()
    sym_label = codec.symbolize_token("Prüfgebühr")
    sym_var = codec.symbolize_token("$Prüfgebühr")
    assert sym_var == f"${sym_label}"
    assert codec.detokenize_text(sym_var) == "$Prüfgebühr"
    assert codec.symbolize_token(sym_var) == sym_var  # idempotent

    # Strict zero-English-domain policy allows $SYM_* and SYM_*
    strict_policy = EgressPolicy(
        allowed_tokens=_HTML_ARIA_STRUCTURAL_VOCAB,
        include_html_aria_vocab=True,
        include_clinical_vocab=False,
    )
    assert inspect_outbound_payload({"var": sym_var, "label": sym_label}, strict_policy).allowed is True

    german_jp_html = """<!doctype html><html><body>
      <div role="dialog" style="display:block;width:520px;height:360px;padding:12px;">
        <h2>Antragsprüfung</h2>
        <label for="fee">Prüfgebühr</label>
        <input id="fee" type="text" required />
        <label for="status_sel">承認状態</label>
        <select id="status_sel" required>
          <option value="">---</option>
          <option value="Genehmigt">Genehmigt</option>
          <option value="Abgelehnt">Abgelehnt</option>
        </select>
        <button id="submit_btn" type="submit" disabled>Absenden</button>
      </div>
      <div id="live_out"></div>
      <script>
        const fee = document.getElementById('fee');
        const sel = document.getElementById('status_sel');
        const btn = document.getElementById('submit_btn');
        const sync = () => { btn.disabled = !(fee.value.trim().length > 0 && sel.value.length > 0); };
        fee.addEventListener('input', sync);
        sel.addEventListener('change', sync);
        btn.addEventListener('click', () => {
          document.getElementById('live_out').innerText = fee.value + '|' + sel.value;
        });
      </script>
    </body></html>"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            live_page = browser.new_page()
            live_page.set_content(german_jp_html, wait_until="domcontentloaded")
            twin = capture_zero_phi_digital_twin(live_page, symbol_codec=codec)
        finally:
            browser.close()

    # Verify idempotence: re-symbolizing twin HTML does not double-symbolize SYM_*
    assert codec.symbolize_html(twin.sanitized_html) == twin.sanitized_html
    assert "Prüfgebühr" not in twin.sanitized_html
    assert "Genehmigt" not in twin.sanitized_html
    assert "承認状態" not in twin.sanitized_html

    sym_genehmigt = codec.symbolize_token("Genehmigt")
    assert sym_genehmigt in twin.sanitized_html

    seen_var_keys: List[str] = []

    def _mock_cloud_agent(twin_html: str, var_keys: List[str], _err: str, _fb: Dict[str, Any]) -> str:
        seen_var_keys.extend(var_keys)
        # Cloud agent ONLY sees $SYM_* matching <label>SYM_*</label> and SYM_* <option>
        assert "Prüfgebühr" not in twin_html
        assert "Genehmigt" not in twin_html
        assert sym_var in var_keys
        assert sym_label in twin_html
        # Find indices from data-oa-idx on the twin HTML
        idx_fee = int(re.search(r'<input[^>]*data-oa-idx="(\d+)"', twin_html).group(1))  # type: ignore[union-attr]
        idx_sel = int(re.search(r'<select[^>]*data-oa-idx="(\d+)"', twin_html).group(1))  # type: ignore[union-attr]
        idx_btn = int(re.search(r'<button[^>]*data-oa-idx="(\d+)"', twin_html).group(1))  # type: ignore[union-attr]
        return (
            "def repair(ui, vars):\n"
            f"    ui.fill_var({idx_fee}, {sym_var!r})\n"
            f"    ui.select({idx_sel}, {sym_genehmigt!r})\n"
            f"    ui.click({idx_btn})\n"
        )

    guard = LocalEgressGuard(tmp_path / "twin_egress.jsonl")
    res = self_heal_in_digital_twin(
        twin,
        {"$Prüfgebühr": "145.00"},
        _mock_cloud_agent,
        egress_guard=guard,
        symbol_codec=codec,
    )
    assert res.verified is True, f"Twin heal failed: {res.failure_reason} {res.turn_history}"
    assert res.live_network_requests_attempted == 0
    assert "$Prüfgebühr" in res.synthesized_code
    assert "Genehmigt" in res.synthesized_code
    assert "SYM_" not in res.synthesized_code

    # Verify DistilledRepairStep & ControlSignature detokenized cleanly
    fill_step = next(s for s in res.distilled_steps if s.method == "fill_var")
    sel_step = next(s for s in res.distilled_steps if s.method == "select")
    assert fill_step.var_key == "$Prüfgebühr"
    assert fill_step.control_signature is not None
    assert "Prüfgebühr" in fill_step.control_signature.label_tokens
    assert sel_step.arg == "Genehmigt"
    assert sel_step.control_signature is not None
    assert "承認状態" in sel_step.control_signature.label_tokens

    # Replay detokenized steps directly on the live German/Japanese page
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            live_page = browser.new_page()
            live_page.set_content(german_jp_html, wait_until="domcontentloaded")
            replayed = replay_distilled_zero_phi_repair(
                live_page,
                res.distilled_steps,
                private_vars={"$Prüfgebühr": "145.00"},
                readback_oracle=lambda p: p.locator("#live_out").inner_text() == "145.00|Genehmigt",
            )
            assert replayed is True
        finally:
            browser.close()


def test_order_preserving_rank_codec_and_eu_currency_induction(tmp_path: Path) -> None:
    """Test 2: OPRC preserves numeric/EU/Swiss currency ordering under strict zero-clinical EgressPolicy."""
    strict_policy = EgressPolicy(
        allowed_tokens=_HTML_ARIA_STRUCTURAL_VOCAB,
        include_html_aria_vocab=True,
        include_clinical_vocab=False,
        extra_allowed_tokens=frozenset(),
    )
    oprc = OrderPreservingRankCodec().fit(["1.234,56 €", "CHF 1'234.50", "$25,000", "38.500,00 €"])
    t_chf = oprc.encode_value("CHF 1'234.50")
    t_eu_lo = oprc.encode_value("1.234,56 €")
    t_us_mid = oprc.encode_value("$25,000")
    t_eu_hi = oprc.encode_value("38.500,00 €")
    assert (t_chf, t_eu_lo, t_us_mid, t_eu_hi) == (
        "ORD_NUM_0001",
        "ORD_NUM_0002",
        "ORD_NUM_0003",
        "ORD_NUM_0004",
    )
    for tok in (t_chf, t_eu_lo, t_us_mid, t_eu_hi):
        assert inspect_outbound_payload({"value": tok}, strict_policy).allowed is True
    assert oprc.detokenize_text(">= ORD_NUM_0003") == ">= 25000"
    assert oprc.detokenize_text(">= ORD_NUM_0004") == ">= 38500"

    # End-to-end induction via mock cloud coding_agent_fn that ONLY sees SYM_* + ORD_NUM_*
    demos = [
        RawDemoTrace(
            demo_id="eu_low",
            goal_text="Standard invoice",
            visited_views=("summary",),
            dom_observations={"betrag": "1.234,56 €"},
            committed_fields={"disposition": "AUTO_APPROVE"},
            post_commit_url="/inv/1",
        ),
        RawDemoTrace(
            demo_id="us_thresh",
            goal_text="Threshold invoice",
            visited_views=("summary", "audit"),
            dom_observations={"betrag": "$25,000"},
            committed_fields={"disposition": "ESCALATE_VP"},
            post_commit_url="/inv/2",
        ),
        RawDemoTrace(
            demo_id="eu_high",
            goal_text="High EU invoice",
            visited_views=("summary", "audit"),
            dom_observations={"betrag": "38.500,00 €"},
            committed_fields={"disposition": "ESCALATE_VP"},
            post_commit_url="/inv/3",
        ),
    ]

    def _mock_oprc_agent(prompt: str) -> str:
        for raw_secret in ("1.234,56", "25,000", "25000", "38.500", "38500", "betrag"):
            assert raw_secret not in prompt
        # Parse the outbound JSON from the prompt
        payload = json.loads(prompt.split("from symbolized demos:\n", 1)[1].strip())
        traces = payload["demo_traces"]
        amt_sym = next(iter(traces[0]["observations"].keys()))
        disp_sym = next(iter(traces[0]["fields"].keys()))
        # Group by disposition symbol and find monotone ORD_NUM_* threshold
        by_d: Dict[str, List[str]] = {}
        for t in traces:
            by_d.setdefault(t["fields"][disp_sym], []).append(t["observations"][amt_sym])
        low_d = min(by_d, key=lambda d: max(by_d[d]))
        high_d = max(by_d, key=lambda d: min(by_d[d]))
        return json.dumps(
            {
                "branches": [
                    {"branch_id": "b_high", "when": {amt_sym: f">= {min(by_d[high_d])}"}, "disposition": high_d, "priority": 2},
                    {"branch_id": "b_low", "when": {amt_sym: f"<= {max(by_d[low_d])}"}, "disposition": low_d, "priority": 1},
                ]
            }
        )

    cart = induce_cartridge_from_raw_demos(
        demos,
        cartridge_id="oprc_eu_cart",
        portal_origin="https://erp.example.de",
        coding_agent_fn=_mock_oprc_agent,
        egress_policy=strict_policy,
        ledger_path=tmp_path / "oprc_egress.jsonl",
    )
    by_disp = {b.disposition: b for b in cart.sop_spec.branches}
    assert by_disp["ESCALATE_VP"].when == {"betrag": ">= 25000"}
    assert by_disp["AUTO_APPROVE"].when == {"betrag": "<= 1234.56"}
    qual = qualify_app_cartridge_offline(cart)
    assert qual.qualified is True, f"OPRC cartridge qualification failed: {qual}"


def test_defeasible_branch_guard_and_per_artifact_span_anchor_citations() -> None:
    """Test 3: Defeasible guard blocks CO-29 Write Off on timely-filing receipt; per-artifact citation check catches uncited tab."""
    branch = SOPBranchSpec(
        branch_id="co29_writeoff",
        when={"denial_code": "CO-29"},
        disposition="WRITE_OFF",
        required_read_artifacts=("eob_tab", "clearinghouse_tab"),
        unless_defeaters=("clearinghouse_timely_acceptance_receipt",),
    )

    # Undercutting defeater present in case evidence -> blocks commit, requires PREPARE
    defeated = evaluate_defeasible_branch(
        branch,
        {
            "denial_code": "CO-29",
            "clearinghouse_tab": "Clearinghouse timely acceptance receipt #CH-88412 dated 2026-01-14",
        },
    )
    assert defeated.allowed is False
    assert defeated.defeated is True
    assert defeated.requires_prepare is True
    assert "clearinghouse_timely_acceptance_receipt" in defeated.defeater_reason

    # No defeater -> branch allowed
    clean = evaluate_defeasible_branch(
        branch,
        {"denial_code": "CO-29", "clearinghouse_tab": "No prior submission on file"},
    )
    assert clean.allowed is True
    assert clean.defeated is False
    assert clean.requires_prepare is False

    # Pluggable defeater_detector_fn also blocks
    custom = evaluate_defeasible_branch(
        branch,
        {"denial_code": "CO-29", "receipt_days_before_limit": 12},
        defeater_detector_fn=lambda _b, ev: "timely_submission_proven"
        if isinstance(ev, dict) and int(ev.get("receipt_days_before_limit", 0)) > 0
        else None,
    )
    assert custom.allowed is False
    assert custom.defeated is True
    assert custom.defeater_reason == "timely_submission_proven"

    # Per-artifact SpanAnchor citation coverage catches uncited visited tab
    visited = {
        "eob_tab": "Denial CO-29 for $420.00 on 2026-02-01.",
        "clearinghouse_tab": "Accepted receipt CH-88412 confirmed on 2026-01-14.",
        "empty_scratch_tab": "   ",
    }
    incomplete_note = "Reviewed denial CO-29 ($420.00) and recommending appeal."
    rep_missing = verify_per_artifact_note_citations(incomplete_note, visited)
    assert rep_missing.covered is False
    assert rep_missing.missing_facts == ("clearinghouse_tab",)
    assert any(m.startswith("eob_tab:") for m in rep_missing.matched_facts)

    amended_note = "Reviewed denial CO-29 ($420.00) and clearinghouse receipt CH-88412; appealing."
    rep_ok = verify_per_artifact_note_citations(amended_note, visited)
    assert rep_ok.covered is True
    assert rep_ok.missing_facts == ()
    assert len(rep_ok.matched_facts) == 2


def test_batch_saga_checkpoint_reconcile_after_mid_batch_reauth() -> None:
    """Test 4: Exact-once recovery across 3-item batch when session drops mid-Item-2 after Item 1 + 2 persisted."""
    ckpt = BatchSagaCheckpoint(
        batch_id="batch-2026-04",
        items=("CLM-001", "CLM-002", "CLM-003"),
        committed_item_ids=("CLM-001",),
        current_item_idx=1,
    )
    probed: List[str] = []
    persisted_in_portal = {"CLM-001", "CLM-002"}

    def _ui_readback_present(item_id: str) -> bool:
        probed.append(item_id)
        return item_id in persisted_in_portal

    reconciled = reconcile_batch_after_reauth(ckpt, _ui_readback_present)
    # CLM-001 was already committed so it was not re-probed; CLM-002 was persisted so it is marked committed
    assert probed == ["CLM-002", "CLM-003"]
    assert reconciled.committed_item_ids == ("CLM-001", "CLM-002")
    assert reconciled.current_item_idx == 2
    assert reconciled.remaining_items == ("CLM-003",)
    assert reconciled.items[reconciled.current_item_idx :] == ("CLM-003",)
    assert reconciled.is_complete is False

    # After committing CLM-003, reconciling again marks batch complete with 0 remaining items
    persisted_in_portal.add("CLM-003")
    done_ckpt = reconcile_batch_after_reauth(reconciled, _ui_readback_present)
    assert done_ckpt.committed_item_ids == ("CLM-001", "CLM-002", "CLM-003")
    assert done_ckpt.current_item_idx == 3
    assert done_ckpt.remaining_items == ()
    assert done_ckpt.is_complete is True
