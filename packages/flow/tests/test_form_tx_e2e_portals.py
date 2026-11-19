"""Full-stack end-to-end tests across ``form_tx``, ``CopilotSessionBridge``, and real portals."""

from __future__ import annotations

import pytest

from benchmark.form_tx_portals.packs import (
    build_underwriting_scenarios,
    build_underwriting_wizard_pack,
)
from benchmark.form_tx_portals.runner import (
    format_markdown_report,
    run_portal_benchmark,
)
from benchmark.form_tx_portals.servers import (
    LegacyHospitalQueuePortal,
    StatefulUnderwritingPortal,
)
from openadapt_flow.copilot_bridge import (
    CopilotFieldHighlight,
    CopilotSessionBridge,
    CopilotViewState,
)
from openadapt_flow.form_tx.pack_compiler import (
    build_patient_registration_domain_schema,
    induce_form_pack,
)
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.profiles import load_ui_profile
from openadapt_flow.form_tx.receipt import (
    generate_ed25519_keypair,
    verify_form_tx_receipt,
)
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    FormHaltKind,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
)


def test_full_three_arm_multi_portal_benchmark():
    """End-to-end 3-arm realism benchmark across underwriting wizard and held-out HMS."""
    pytest.importorskip("playwright.sync_api")
    report = run_portal_benchmark(ppr_delay_ms=40)
    by_arm = {s.arm: s for s in report.summaries}

    tx_summary = by_arm["openadapt_form_tx"]
    assert tx_summary.total_cases == 9
    assert tx_summary.verified_accurate_completions == 9
    assert tx_summary.silent_corrupt_commits == 0
    assert tx_summary.failed_or_aborted == 0
    assert tx_summary.receipts_verified == 9

    # RuleAccumulator promoted trampoline binding after uw_04 & uw_05 so uw_06 had 0 halts!
    assert report.learned_candidates_admitted >= 1
    uw_06_tx = next(
        c
        for c in report.cases
        if c.case_id == "uw_06_pool_zero_halt_after_learned_rule"
        and c.arm == "openadapt_form_tx"
    )
    assert uw_06_tx.completed is True
    assert uw_06_tx.ground_truth_accurate is True
    assert uw_06_tx.human_decisions == 0

    # Zero-mutation keepalive & request_id idempotency on held-out HMS
    assert report.hms_keepalive_pings >= 1
    assert report.hms_idempotent_dedupes == 2

    # Ungoverned agent suffers silent corrupt commits (unlisted spouse, prior claims, trampoline=Yes)
    ungov = by_arm["ungoverned_agent"]
    assert ungov.silent_corrupt_commits >= 4

    # Rigid step replay fails on conditional cascades and HMS async combobox/modals
    rigid = by_arm["rigid_step_replay"]
    assert rigid.verified_accurate_completions < tx_summary.verified_accurate_completions

    md = format_markdown_report(report)
    assert "openadapt_form_tx" in md
    assert "9/9" in md


def test_copilot_bridge_roundtrip_and_readback_corruption_refusal(tmp_path):
    """Proves CopilotSessionBridge integration AND SummaryJudge catching server read-back corruption."""
    sync_pw = pytest.importorskip("playwright.sync_api")
    scenario = next(
        s for s in build_underwriting_scenarios() if s.scenario_id == "uw_03_unlisted_spouse_prefill"
    )
    pack = build_underwriting_wizard_pack()
    profile = load_ui_profile("server_ppr_wizard")
    signing_key, _ = generate_ed25519_keypair()
    bridge = CopilotSessionBridge(tmp_path / "copilot_session")

    with sync_pw.sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chromium", headless=True)
        try:
            # 1. Halt -> publish CopilotViewState -> drain VA decision -> VERIFIED_DONE
            portal = StatefulUnderwritingPortal(
                ppr_delay_ms=30,
                inject_unlisted_spouse=True,
            )
            ctx = browser.new_context()
            origin = portal.attach_to_playwright_context(ctx)
            page = ctx.new_page()
            surf = PlaywrightFormSurface(
                page,
                profile,
                ignored_mutation_paths=(
                    "/api/applicant/save",
                    "/api/property/ppr",
                    "/api/property/next",
                    "/api/household/dismiss",
                    "/api/estimator/calculate",
                ),
            )
            surf.goto(f"{origin}/wizard/applicant")
            reconciler = FormReconciler(signing_key=signing_key)
            sess = reconciler.start_session(surface=surf, pack=pack, dossier=scenario.dossier)
            res = reconciler.run_until_halt_or_done(sess)
            assert res.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
            assert res.halts[0].kind == FormHaltKind.PREFILL_ENTITY_CONFLICT

            bridge.publish_state(
                CopilotViewState(
                    run_id=res.run_id,
                    outcome=res.outcome,
                    status_line="Portal prefilled unlisted household spouse",
                    fields_filled_auto=res.fields_filled_auto,
                    total_bound_fields=len(pack.bindings),
                    halts=res.halts,
                    field_highlights=(
                        CopilotFieldHighlight(
                            dom_selector=res.halts[0].dom_selector or "#household-table",
                            badge_state="conflict",
                            caption="Unlisted Spouse",
                        ),
                    ),
                )
            )
            loaded = bridge.read_state()
            assert loaded is not None
            assert loaded.halts[0].kind == FormHaltKind.PREFILL_ENTITY_CONFLICT

            accepted, count = bridge.append_decision(
                HumanResolutionInput(
                    halt_id=res.halts[0].halt_id,
                    action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
                    actor_id="va_07",
                    idempotency_key="va_dec_001",
                )
            )
            assert accepted is True and count == 1
            seen: set[str] = set()
            decisions = bridge.drain_new_decisions(seen)
            assert len(decisions) == 1

            final_res = reconciler.apply_human_resolution(sess, decisions[0])
            assert final_res.outcome == FormTxOutcome.VERIFIED_DONE
            assert final_res.receipt is not None
            assert verify_form_tx_receipt(final_res.receipt) is True
            ctx.close()

            # 2. Planted server read-back corruption -> SummaryJudge MUST refuse (READBACK_REFUTED)!
            clean_scenario = build_underwriting_scenarios()[0]

            class CorruptingSummaryPortal(StatefulUnderwritingPortal):
                def _render_quote_summary(self, saved):
                    corrupted = dict(saved)
                    corrupted["street_address"] = "999 WRONG CORRUPTED AVE"
                    return super()._render_quote_summary(corrupted)

            bad_portal = CorruptingSummaryPortal(ppr_delay_ms=20)
            ctx2 = browser.new_context()
            origin2 = bad_portal.attach_to_playwright_context(ctx2)
            page2 = ctx2.new_page()
            surf2 = PlaywrightFormSurface(
                page2,
                profile,
                ignored_mutation_paths=(
                    "/api/applicant/save",
                    "/api/property/ppr",
                    "/api/property/next",
                    "/api/estimator/calculate",
                ),
            )
            surf2.goto(f"{origin2}/wizard/applicant")
            sess2 = reconciler.start_session(
                surface=surf2, pack=pack, dossier=clean_scenario.dossier
            )
            bad_res = reconciler.run_until_halt_or_done(sess2)
            assert bad_res.outcome == FormTxOutcome.READBACK_REFUTED
            assert bad_res.safe_to_retry is False
            assert bad_res.receipt is None
            assert bad_res.summary_verdict is not None
            assert "dwelling.street_address" in bad_res.summary_verdict.failed_keys
            ctx2.close()

            # 3. Zero-model Pack Induction on live HMS HTML snapshot!
            hms = LegacyHospitalQueuePortal(require_auth=False)
            ctx3 = browser.new_context()
            origin3 = hms.attach_to_playwright_context(ctx3)
            page3 = ctx3.new_page()
            surf3 = PlaywrightFormSurface(page3, load_ui_profile("legacy_postback_portal"))
            surf3.goto(f"{origin3}/hms/register")
            live_snap = surf3.snapshot()
            induction = induce_form_pack(
                pack_id="induced_hms_v1",
                ui_profile_id="legacy_postback_portal",
                domain=build_patient_registration_domain_schema(),
                form_snapshots=(live_snap,),
            )
            assert "patient.full_name" in induction.matched_keys
            assert "patient.mobile" in induction.matched_keys
            assert "visit.department" in induction.matched_keys
            assert "visit.consulting_doctor" in induction.matched_keys
            ctx3.close()
        finally:
            browser.close()
