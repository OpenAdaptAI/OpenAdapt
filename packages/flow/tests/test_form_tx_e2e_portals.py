"""Full-stack end-to-end tests across ``form_tx``, ``CopilotSessionBridge``, and synthetic browser portals."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
)
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    FormHaltKind,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
)

_BENCH_ROOT = str(Path(__file__).resolve().parents[1] / "benchmark")
if _BENCH_ROOT not in sys.path:
    sys.path.insert(0, _BENCH_ROOT)

from form_tx_portals.packs import (  # noqa: E402
    build_underwriting_scenarios,
    build_underwriting_wizard_pack,
)
from form_tx_portals.runner import (  # noqa: E402
    format_markdown_report,
    run_portal_benchmark,
)
from form_tx_portals.servers import (  # noqa: E402
    LegacyHospitalQueuePortal,
    StatefulUnderwritingPortal,
)


def test_full_three_arm_multi_portal_benchmark():
    """End-to-end 3-arm synthetic fixture benchmark across underwriting wizard and second synthetic portal."""
    pytest.importorskip("playwright.sync_api")
    report = run_portal_benchmark(ppr_delay_ms=40)
    by_arm = {s.arm: s for s in report.summaries}

    tx_summary = by_arm["openadapt_form_tx"]
    assert tx_summary.total_cases == 9
    # With the complete read-back, the 7 underwriting cases end
    # readback_incomplete: the estimator inputs are typed in a popup and the
    # portal never shows them, so they cannot be read back. Only the 2
    # hospital-portal jobs verify. This historical benchmark stays invalidated.
    assert tx_summary.verified_accurate_completions == 2
    assert tx_summary.silent_corrupt_commits == 0
    assert tx_summary.receipts_verified == 2
    underwriting_tx = [
        c
        for c in report.cases
        if c.portal == "underwriting_wizard" and c.arm == "openadapt_form_tx"
    ]
    assert {c.outcome for c in underwriting_tx} == {"readback_incomplete"}

    # Repetition alone cannot admit a policy: proposals remain unqualified.
    assert report.learned_candidates_admitted == 0
    uw_06_tx = next(
        c
        for c in report.cases
        if c.case_id == "uw_06_pool_zero_halt_after_learned_rule"
        and c.arm == "openadapt_form_tx"
    )
    assert uw_06_tx.human_decisions == 1
    # The saved record is checked against the server oracle on its own, so a
    # wrong stored answer fails here whatever the run's outcome.
    assert uw_06_tx.saved_record_accurate is True
    assert all(c.saved_record_accurate is True for c in underwriting_tx)

    # Zero-mutation keepalive & request_id idempotency on second synthetic portal
    assert report.hms_keepalive_pings >= 1
    assert report.hms_idempotent_dedupes == 2

    # Ungoverned agent suffers silent corrupt commits (unlisted spouse, prior claims, trampoline=Yes)
    ungov = by_arm["ungoverned_agent"]
    assert ungov.silent_corrupt_commits >= 4

    # Rigid step replay fails on conditional cascades and HMS async combobox/modals
    md = format_markdown_report(report)
    assert "openadapt_form_tx" in md
    assert "2/9" in md


def test_copilot_bridge_roundtrip_and_readback_corruption_refusal(tmp_path):
    """Proves CopilotSessionBridge integration AND SummaryJudge catching server read-back corruption."""
    sync_pw = pytest.importorskip("playwright.sync_api")
    scenario = next(
        s
        for s in build_underwriting_scenarios()
        if s.scenario_id == "uw_03_unlisted_spouse_prefill"
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
            sess = reconciler.start_session(
                surface=surf, pack=pack, dossier=scenario.dossier
            )
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
                            dom_selector=res.halts[0].dom_selector
                            or "#household-table",
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
            # Submitted; the estimator inputs cannot be read back on this
            # portal, so the run is not verified and no receipt is issued.
            assert final_res.outcome == FormTxOutcome.READBACK_INCOMPLETE
            assert final_res.receipt is None
            assert final_res.summary_verdict is not None
            assert final_res.summary_verdict.failed_keys == ()
            # The estimator inputs, and the figure that came only from them.
            assert all(
                key.startswith("estimator.")
                or key == "estimated_replacement_cost_cents"
                for key in final_res.summary_verdict.not_checked_keys
            )
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
            surf3 = PlaywrightFormSurface(
                page3, load_ui_profile("legacy_postback_portal")
            )
            surf3.goto(f"{origin3}/hms/register")
            live_snap = surf3.snapshot()
            induction = induce_form_pack(
                pack_id="induced_hms_v1",
                ui_profile_id="legacy_postback_portal",
                domain=build_patient_registration_domain_schema(),
                form_snapshots=(live_snap,),
            )
            assert "full_name" in induction.matched_keys
            assert "date_of_birth" in induction.matched_keys
            assert "administrative_sex" in induction.matched_keys
            assert "phone_number" in induction.matched_keys
            assert "visit_type" in induction.matched_keys
            assert "clinical_department" in induction.matched_keys
            assert "consulting_provider" in induction.matched_keys
            assert induction.unmapped_portal_required_labels == ()
            ctx3.close()
        finally:
            browser.close()


def test_six_second_server_rerender_preserves_declared_readback() -> None:
    """A delayed synthetic server render exercises the actual browser wait path."""
    sync_pw = pytest.importorskip("playwright.sync_api")
    scenario = build_underwriting_scenarios()[0]
    pack = build_underwriting_wizard_pack()
    with sync_pw.sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chromium", headless=True)
        try:
            context = browser.new_context()
            portal = StatefulUnderwritingPortal(ppr_delay_ms=6000)
            origin = portal.attach_to_playwright_context(context)
            page = context.new_page()
            surface = PlaywrightFormSurface(
                page,
                load_ui_profile("server_ppr_wizard"),
                ignored_mutation_paths=(
                    "/api/applicant/save",
                    "/api/property/ppr",
                    "/api/property/next",
                    "/api/estimator/calculate",
                ),
            )
            surface.goto(f"{origin}/wizard/applicant")
            reconciler = FormReconciler(surface)
            session = reconciler.create_session(pack, scenario.dossier)
            result = reconciler.run_until_halt_or_done(session)
            # The estimator inputs are typed in the popup and the portal does
            # not store or show them, so they cannot be read back. They are
            # not_checked, and the run is submitted but never verified.
            assert result.outcome == FormTxOutcome.READBACK_INCOMPLETE, (
                result.what_happened
            )
            assert result.summary_verdict is not None
            assert result.summary_verdict.passed is False
            assert result.summary_verdict.failed_keys == ()
            # The estimate came only from the popup, whose inputs are not
            # checked, and no displayed result was declared: not checked too.
            assert set(result.summary_verdict.not_checked_keys) == {
                "estimator.finish_grade",
                "estimator.living_area_sqft",
                "estimator.stories",
                "estimated_replacement_cost_cents",
            }
            # Explicit fixture specification: conditional roof/pool follow-ups
            # are absent in this case. Every other intended field and the
            # estimator return must appear in successful saved-state read-back.
            expected_keys = {
                "applicant.first_name",
                "applicant.last_name",
                "applicant.dob",
                "applicant.phone",
                "applicant.marital_status",
                "applicant.prior_claims_count",
                "dwelling.street_address",
                "dwelling.year_built",
                "dwelling.roof_year",
                "dwelling.has_pool",
                "coverage.deductible",
                "coverage.liability_limit",
            }
            checked_keys = {
                verdict.dossier_key
                for verdict in result.summary_verdict.field_verdicts
                if verdict.passed
            }
            assert expected_keys == checked_keys
            assert result.receipt is None
        finally:
            browser.close()
