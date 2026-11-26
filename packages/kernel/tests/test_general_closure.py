"""Multi-domain unit tests for general-purpose closure primitives.

Covers PolicyDecisionTable routing, non-waivable step-limit default-deny,
required dossier key coverage, triage evidence citation, ControlSignature,
ephemeral selector detection, and cold-trace-to-pack lifting across
ERP, insurance underwriting, clinical triage, and SaaS UI frameworks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from openadapt_kernel import (
    ControlSchema,
    ControlSignature,
    DistilledRepairStep,
    EgressPolicy,
    ExtractedFact,
    FieldType,
    ObligationLedger,
    PolicyDecisionTable,
    PolicyRule,
    PrepareCard,
    PrepareResumeResponse,
    ReadBackProof,
    RubberStampViolationError,
    UnresolvedSlot,
    bind_derived_calculation,
    calibrate_critic_decision,
    compile_freetext_template,
    compile_policy_rules_to_table,
    evaluate_derived_calculation,
    evaluate_policy_decision,
    evaluate_pre_step_policy,
    evaluate_reactive_policy_on_dossier_update,
    extract_dossier_facts,
    inspect_outbound_payload,
    is_ephemeral_selector,
    lift_cold_trace_to_pack,
    lift_distilled_repair_to_pack,
    parameterize_value_against_dossier,
    resume_from_prepare_card,
    verify_completion_closure,
    verify_freetext_fact_coverage,
    verify_query_view_preconditions,
    verify_readonly_answer_provenance,
)


def test_erp_invoice_policy_routing_and_prepare_fallback() -> None:
    table = PolicyDecisionTable(
        table_id="erp_invoice_approval_v1",
        rules=(
            PolicyRule(
                rule_id="sanctioned_vendor_block",
                conditions={"vendor_status": "sanctioned", "variance_tier": "*"},
                required_disposition="block_payment",
            ),
            PolicyRule(
                rule_id="auto_approve_matched",
                conditions={"po_matched": "yes", "variance_tier": "under_threshold"},
                required_disposition="auto_approve",
            ),
        ),
        require_human_if_unmatched=True,
    )

    ok = evaluate_policy_decision(
        table, {"po_matched": "YES", "variance_tier": "under_threshold"}, "auto_approve"
    )
    assert ok.allowed is True
    assert ok.matched_rule_id == "auto_approve_matched"
    assert ok.requires_prepare is False

    mismatch = evaluate_policy_decision(
        table, {"po_matched": "yes", "variance_tier": "under_threshold"}, "reject_invoice"
    )
    assert mismatch.allowed is False
    assert mismatch.reason == "policy_disposition_mismatch"
    assert mismatch.expected_disposition == "auto_approve"
    assert mismatch.requires_prepare is False

    wildcard = evaluate_policy_decision(
        table, {"vendor_status": "sanctioned", "variance_tier": "over_threshold"}, "auto_approve"
    )
    assert wildcard.allowed is False
    assert wildcard.matched_rule_id == "sanctioned_vendor_block"
    assert wildcard.expected_disposition == "block_payment"

    uncodified = evaluate_policy_decision(
        table, {"po_matched": "partial", "variance_tier": "manual_review"}, "auto_approve"
    )
    assert uncodified.allowed is False
    assert uncodified.requires_prepare is True
    assert uncodified.reason == "uncodified_judgment_requires_prepare"

    permissive_table = PolicyDecisionTable(
        table_id="erp_advisory", rules=table.rules, require_human_if_unmatched=False
    )
    allowed_unmatched = evaluate_policy_decision(
        permissive_table, {"po_matched": "partial"}, "manual_review"
    )
    assert allowed_unmatched.allowed is True
    assert allowed_unmatched.reason == "no_matching_policy_rule_allowed"


def test_insurance_underwriting_policy_and_dossier_closure() -> None:
    uw_table = PolicyDecisionTable(
        table_id="commercial_uw_routing",
        rules=(
            PolicyRule(
                rule_id="high_risk_refer",
                conditions={"risk_tier": "high", "loss_history": "prior_claim"},
                required_disposition="refer_senior_underwriter",
            ),
        ),
    )
    blocked = evaluate_policy_decision(
        uw_table, {"risk_tier": "high", "loss_history": "prior_claim"}, "auto_bind"
    )
    assert blocked.allowed is False
    assert blocked.reason == "policy_disposition_mismatch"
    assert blocked.expected_disposition == "refer_senior_underwriter"

    # Step-limit default-deny when an obligation is still pending
    ledger_pending = ObligationLedger("uw-ep-1", subject_key="POL-8812")
    ledger_pending.add_obligation("submit_quote", "Bind commercial policy quote")
    denied_limit = verify_completion_closure(ledger_pending, step_idx=20, max_steps=20)
    assert denied_limit.allowed is False
    assert denied_limit.outcome == "ESCALATED"
    assert denied_limit.reason == "default_deny_step_limit"
    assert denied_limit.pending_obligations == ["submit_quote"]
    assert ledger_pending.status == "ESCALATED"

    # Committed obligation with canonical dossier key verification
    ledger = ObligationLedger("uw-ep-2", subject_key="POL-8812")
    ledger.add_obligation("submit_quote", "Bind commercial policy quote")
    ledger.mark_committed(
        "submit_quote",
        ReadBackProof(
            channel="fresh_reload",
            url_or_locator="https://portal.example/policies/POL-8812",
            cited_substrings=["POL-8812", "UW-402"],
            external_ref="POL-8812",
        ),
    )
    dossier = {
        "effective_date": "2026-04-01",
        "premium_amount": "$1,450.00",
        "policy_code": "UW-402",
    }
    ftypes: Dict[str, FieldType] = {
        "effective_date": "date",
        "premium_amount": "currency",
        "policy_code": "code_prefix",
    }

    missing_code = verify_completion_closure(
        ledger,
        step_idx=8,
        max_steps=20,
        required_dossier_keys=["$effective_date", "$premium_amount", "$policy_code"],
        readback_values={"effective_date": "04/01/2026", "premium_amount": "1450.00"},
        dossier_values=dossier,
        field_types=ftypes,
    )
    assert missing_code.allowed is False
    assert missing_code.reason == "missing_required_dossier_keys"
    assert missing_code.missing_dossier_keys == ["$policy_code"]

    verified = verify_completion_closure(
        ledger,
        step_idx=9,
        max_steps=20,
        required_dossier_keys=["$effective_date", "$premium_amount", "$policy_code"],
        readback_values={
            "effective_date": "Apr 1, 2026",
            "premium_amount": "1450.00",
            "policy_code": "UW-402 - Commercial Property",
        },
        dossier_values=dossier,
        field_types=ftypes,
    )
    assert verified.allowed is True
    assert verified.outcome == "VERIFIED"
    assert ledger.status == "VERIFIED"


def test_clinical_triage_readonly_citation_and_missing_primary_code() -> None:
    triage_ledger = ObligationLedger("triage-ep-1", subject_key="CASE-309")
    evidence = {
        "triage_date": "2026-04-12",
        "primary_code": "J45.909",
        "callback_phone": "4155550192",
        "member_id": "998124821",
    }
    ftypes: Dict[str, FieldType] = {
        "triage_date": "date",
        "primary_code": "code_prefix",
        "callback_phone": "phone",
        "member_id": "masked_id",
    }

    # Missing primary_code in triage summary blocks VERIFIED on read-only task
    uncited = verify_completion_closure(
        triage_ledger,
        step_idx=5,
        max_steps=12,
        field_types=ftypes,
        triage_summary_text="Reviewed chart on Apr 12, 2026 for member XXX-XX-4821; callback (415) 555-0192.",
        required_triage_evidence=evidence,
    )
    assert uncited.allowed is False
    assert uncited.outcome == "ESCALATED"
    assert uncited.reason == "uncited_triage_evidence"
    assert uncited.uncited_triage_keys == ["primary_code"]

    # Step-limit exhaustion with uncited evidence enforces default-deny step limit
    limit_denied = verify_completion_closure(
        triage_ledger,
        step_idx=12,
        max_steps=12,
        field_types=ftypes,
        triage_summary_text="Reviewed chart without required codes.",
        required_triage_evidence=evidence,
    )
    assert limit_denied.allowed is False
    assert limit_denied.reason == "default_deny_step_limit"

    # Canonical citation of all four evidence values passes on read-only task
    cited_ok = verify_completion_closure(
        triage_ledger,
        step_idx=6,
        max_steps=12,
        field_types=ftypes,
        triage_summary_text=(
            "Triage summary (Apr 12, 2026): member ***4821 presents with J45.909 "
            "and confirmed callback +1 (415) 555-0192."
        ),
        required_triage_evidence=evidence,
    )
    assert cited_ok.allowed is True
    assert cited_ok.outcome == "VERIFIED"
    assert triage_ledger.status == "VERIFIED"


def test_ephemeral_selector_detection_and_cold_trace_lifting() -> None:
    # Ephemeral selectors across React, Vue, Angular, CSS-in-JS, and UUIDs
    for bad_sel in (
        "#input-145",
        "#v-input-32",
        "#mat-input-0",
        ".css-1a2b3c4",
        ".sc-bdVaJa",
        "button.Action_primary__9xK2p",
        "#550e8400-e29b-41d4-a716-446655440000",
        r"#\:r1\:",
        "#a1b2c3d4e5f6",
    ):
        assert is_ephemeral_selector(bad_sel) is True, f"Expected ephemeral: {bad_sel}"

    # Durable selectors remain untouched
    for good_sel in (
        '[data-testid="submit-invoice"]',
        'input[name="service_date"]',
        'button[aria-label="Approve"]',
        "#invoice-form",
        'form input[type="email"]',
    ):
        assert is_ephemeral_selector(good_sel) is False, f"Expected durable: {good_sel}"

    dossier = {
        "service_date": "2026-04-01",
        "invoice_total": "$1,250.00",
        "contact_phone": "415-555-0199",
        "vendor_name": "Acme Logistics Corp",
    }
    assert parameterize_value_against_dossier("04/01/2026", dossier) == "$service_date"
    assert parameterize_value_against_dossier("1250.00", dossier) == "$invoice_total"
    assert parameterize_value_against_dossier("(415) 555-0199", dossier) == "$contact_phone"
    assert parameterize_value_against_dossier("  acme  logistics corp ", dossier) == "$vendor_name"
    assert parameterize_value_against_dossier("Unmatched Literal", dossier) == "Unmatched Literal"
    assert parameterize_value_against_dossier("$already_param", dossier) == "$already_param"

    vue_ctrl = ControlSchema(
        tag="input",
        input_type="text",
        role="textbox",
        label="Service Date",
        placeholder="MM/DD/YYYY",
        name="svc_date",
        element_id="input-145",
        testid="",
        in_modal=True,
        selector="#input-145",
    )
    raw_steps: List[Dict[str, Any]] = [
        {
            "step_id": "s1",
            "action": "fill",
            "selector": "#input-145",
            "value": "04/01/2026",
            "control": vue_ctrl,
            "ordinal": 1,
        },
        {
            "step_id": "s2",
            "action": "fill",
            "selector": ".css-1a2b3c4 input",
            "value": "1250.00",
            "tag": "input",
            "input_type": "text",
            "role": "textbox",
            "label": "Invoice Total",
            "in_modal": True,
            "ordinal": 2,
        },
        {
            "step_id": "s3",
            "action": "fill",
            "selector": "#mat-input-7",
            "value": "(415) 555-0199",
            "tag": "input",
            "input_type": "tel",
            "label": "Contact Phone",
            "ordinal": 3,
        },
        {
            "step_id": "s4",
            "action": "click",
            "selector": '[data-testid="save-record"]',
            "tag": "button",
            "role": "button",
            "text": "Save Record",
        },
    ]

    lifted = lift_cold_trace_to_pack(raw_steps, dossier)
    assert lifted.stripped_selector_count == 3
    assert lifted.parameterized_vars == ["$service_date", "$invoice_total", "$contact_phone"]

    s1, s2, s3, s4 = lifted.steps
    assert s1.selector is None
    assert s1.stripped_ephemeral_selector == "#input-145"
    assert s1.value == "$service_date"
    assert s1.control_signature == ControlSignature(
        in_modal=True,
        tag="input",
        input_type="text",
        role="textbox",
        name="svc_date",
        placeholder="MM/DD/YYYY",
        label_tokens=("date", "service"),
        text_tokens=(),
        ordinal=1,
    )
    assert s2.selector is None
    assert s2.value == "$invoice_total"
    assert s2.control_signature is not None and s2.control_signature.in_modal is True
    assert s3.selector is None
    assert s3.value == "$contact_phone"
    assert s4.selector == '[data-testid="save-record"]'
    assert s4.stripped_ephemeral_selector is None
    assert s4.control_signature is not None and s4.control_signature.text_tokens == ("record", "save")


def test_pre_step_policy_gate_ecommerce_and_crm() -> None:
    # Step 0: missing required params -> PREPARE_ASK before opening any page
    missing_res = evaluate_pre_step_policy(
        stage="step_0",
        required_params=("order_id", "$refund_reason"),
        dossier={"order_id": "ORD-7741"},
    )
    assert missing_res.allowed is False
    assert missing_res.action == "PREPARE_ASK"
    assert missing_res.missing_params == ("$refund_reason",)

    # Step 0: policy contradiction -> REFUSE before step 1
    refund_policy = PolicyDecisionTable(
        table_id="ecommerce_refund_rules",
        rules=(
            PolicyRule(
                rule_id="expired_window_deny",
                conditions={"return_window": "expired"},
                required_disposition="deny_refund",
            ),
        ),
    )
    contradiction = evaluate_pre_step_policy(
        stage="step_0",
        required_params=("order_id",),
        dossier={"order_id": "ORD-7741"},
        policy_table=refund_policy,
        observed_facts={"return_window": "expired"},
        task_disposition="issue_full_refund",
    )
    assert contradiction.allowed is False
    assert contradiction.action == "REFUSE"
    assert contradiction.reason == "policy_contradiction"

    # Pre-navigation: access management allowlist / denylist
    denied_admin = evaluate_pre_step_policy(
        stage="pre_navigation",
        target_url="https://crm.example/admin/delete-account",
        allow_patterns=("https://crm.example/*",),
        deny_patterns=("*/admin/*", "*/billing/delete*"),
    )
    assert denied_admin.allowed is False
    assert denied_admin.action == "REFUSE"
    assert denied_admin.reason == "access_denied_by_denylist"

    outside_allow = evaluate_pre_step_policy(
        stage="pre_navigation",
        target_url="https://external.example/export",
        allow_patterns=("https://crm.example/accounts/*",),
    )
    assert outside_allow.allowed is False
    assert outside_allow.action == "REFUSE"
    assert outside_allow.reason == "access_denied_outside_allowlist"

    allowed_nav = evaluate_pre_step_policy(
        stage="pre_navigation",
        target_url="https://crm.example/accounts/ACC-901",
        allow_patterns=("https://crm.example/accounts/*",),
        deny_patterns=("*/admin/*",),
    )
    assert allowed_nav.allowed is True
    assert allowed_nav.action == "ALLOW"

    # Pre-fill: grounded value verification via canonical_equal and substring grounding
    dossier = {
        "order_date": "2026-04-15",
        "order_total": "$249.50",
        "account_name": "Northwind Supplies LLC",
    }
    assert evaluate_pre_step_policy(
        stage="pre_fill", field_name="order_date", proposed_value="04/15/2026", field_type="date", dossier=dossier
    ).action == "ALLOW"
    assert evaluate_pre_step_policy(
        stage="pre_fill", field_name="order_total", proposed_value="249.50", field_type="currency", dossier=dossier
    ).action == "ALLOW"
    assert evaluate_pre_step_policy(
        stage="pre_fill", field_name="account_name", proposed_value="Northwind Supplies", dossier=dossier
    ).action == "ALLOW"

    vetoed = evaluate_pre_step_policy(
        stage="pre_fill", field_name="order_total", proposed_value="999.99", field_type="currency", dossier=dossier
    )
    assert vetoed.allowed is False
    assert vetoed.action == "VETO"
    assert vetoed.reason == "ungrounded_fill_value"


def test_interactive_prepare_card_resume_loop() -> None:
    card = PrepareCard(
        card_id="prep-crm-42",
        portal_origin="https://crm.example",
        staged_bindings={0: "$account_id"},
        unresolved_slots=[
            UnresolvedSlot(control_idx=1, control_label="Priority", reason="enum_required", allowed_enum_options=("High", "Normal"))
        ],
    )
    base_dossier = {"account_id": "ACC-901"}

    # Anti-rubber-stamping rejects approve_all=True and blank responses
    with pytest.raises(RubberStampViolationError):
        resume_from_prepare_card(
            card, PrepareResumeResponse(card_id="prep-crm-42", operator_id="op1", approve_all=True), base_dossier
        )
    with pytest.raises(RubberStampViolationError):
        resume_from_prepare_card(
            card, PrepareResumeResponse(card_id="prep-crm-42", operator_id="op1"), base_dossier
        )

    # Rejection and timeout return HALT without mutating base_dossier
    halted_rej = resume_from_prepare_card(
        card,
        PrepareResumeResponse(card_id="prep-crm-42", operator_id="op1", rejected=True, rejection_reason="customer_cancelled"),
        base_dossier,
    )
    assert halted_rej.status == "HALT"
    assert halted_rej.reason == "customer_cancelled"
    assert base_dossier == {"account_id": "ACC-901"}

    halted_to = resume_from_prepare_card(
        card, PrepareResumeResponse(card_id="prep-crm-42", operator_id="op1", timed_out=True), base_dossier
    )
    assert halted_to.status == "HALT"
    assert halted_to.reason == "prepare_card_timed_out"

    # Valid response binds user parameter + confirmation with provenance and unblocks Step 0
    resumed = resume_from_prepare_card(
        card,
        PrepareResumeResponse(
            card_id="prep-crm-42",
            operator_id="op1",
            parameter_values={"refund_reason": "Damaged in transit"},
            confirmed_fields={"priority": "High"},
            slot_bindings={1: "High"},
        ),
        base_dossier,
        required_params=("account_id", "refund_reason"),
    )
    assert resumed.status == "RESUME"
    assert resumed.updated_dossier["refund_reason"] == "Damaged in transit"
    assert resumed.updated_dossier["priority"] == "High"
    assert resumed.provenance == {"refund_reason": "user_prepare_card", "priority": "user_prepare_card"}
    assert "refund_reason" not in base_dossier

    step0_after = evaluate_pre_step_policy(
        stage="step_0", required_params=("account_id", "refund_reason"), dossier=resumed.updated_dossier
    )
    assert step0_after.allowed is True
    assert step0_after.action == "ALLOW"


def test_post_commit_obligation_chains_and_freetext_fact_coverage(tmp_path: Path) -> None:
    ledger = ObligationLedger("issue-chain-1", subject_key="INC-501")
    ledger.add_obligation("submit_ticket_fix", "Deploy patch ticket", target_system="issue_tracker")
    ledger.add_post_commit_obligation(
        "record_confirmation_in_crm",
        "Log ticket confirmation ID in customer CRM",
        dependent_on="submit_ticket_fix",
        target_system="crm",
    )

    proof_primary = ReadBackProof(
        channel="api_read",
        url_or_locator="https://tracker.example/issues/INC-501",
        cited_substrings=["INC-501", "CONF-8821"],
        external_ref="CONF-8821",
    )
    proof_followup = ReadBackProof(
        channel="fresh_reload",
        url_or_locator="https://crm.example/cases/501",
        cited_substrings=["CONF-8821", "Closed"],
        external_ref="CRM-501",
    )

    # Ordering enforced: dependent post-commit obligation cannot commit before parent
    with pytest.raises(ValueError, match="before dependency"):
        ledger.mark_committed("record_confirmation_in_crm", proof_followup)

    ledger.mark_committed("submit_ticket_fix", proof_primary)

    # Completion refused until post-commit follow-up obligation is verified
    blocked_chain = verify_completion_closure(
        ledger,
        step_idx=4,
        max_steps=15,
        post_commit_obligations=("record_confirmation_in_crm",),
    )
    assert blocked_chain.allowed is False
    assert blocked_chain.outcome == "ESCALATED"
    assert blocked_chain.reason == "unverified_post_commit_obligations"
    assert blocked_chain.pending_obligations == ["record_confirmation_in_crm"]

    ledger.mark_committed("record_confirmation_in_crm", proof_followup)

    # Saga persistence preserves dependency chain metadata
    saga_path = tmp_path / "chain_saga.json"
    ledger.save_saga(saga_path)
    reloaded = ObligationLedger.load_saga(saga_path)
    assert reloaded.obligations["record_confirmation_in_crm"].dependent_on == "submit_ticket_fix"
    assert reloaded.obligations["record_confirmation_in_crm"].is_post_commit is True

    # Free-text fact coverage blocks notes that omit retrieved dossier facts
    required_facts = {
        "confirmation_id": "CONF-8821",
        "service_date": "2026-04-15",
        "denial_reason": "prior authorization expired",
    }
    ftypes: Dict[str, FieldType] = {"confirmation_id": "code_prefix", "service_date": "date"}

    cov_missing = verify_freetext_fact_coverage(
        "Logged confirmation CONF-8821 on Apr 15, 2026.", required_facts, field_types=ftypes
    )
    assert cov_missing.covered is False
    assert cov_missing.missing_facts == ("denial_reason",)

    blocked_note = verify_completion_closure(
        reloaded,
        step_idx=6,
        max_steps=15,
        post_commit_obligations=("record_confirmation_in_crm",),
        field_types=ftypes,
        freetext_notes="Logged confirmation CONF-8821 on Apr 15, 2026.",
        required_freetext_facts=required_facts,
    )
    assert blocked_note.allowed is False
    assert blocked_note.reason == "missing_freetext_facts"
    assert blocked_note.missing_freetext_facts == ["denial_reason"]

    verified_all = verify_completion_closure(
        reloaded,
        step_idx=7,
        max_steps=15,
        post_commit_obligations=("record_confirmation_in_crm",),
        field_types=ftypes,
        freetext_notes="Closed CONF-8821 on Apr 15, 2026: authorization expired prior to service.",
        required_freetext_facts=required_facts,
    )
    assert verified_all.allowed is True
    assert verified_all.outcome == "VERIFIED"
    assert reloaded.status == "VERIFIED"


def test_prune_exploratory_prefix_before_verified_distilled_repair() -> None:
    exploratory_prefix: List[Dict[str, Any]] = [
        {"step_id": f"dead_end_{i}", "action": "click", "selector": f'[data-testid="tab-{i}"]'}
        for i in range(7)
    ]
    verified_repairs = [
        DistilledRepairStep(
            method="fill_var",
            selector="#input-99",
            modal_ordinal=0,
            var_key="collection_title",
            ctrl_sig=("input", "text", "textbox", "title"),
        ),
        DistilledRepairStep(
            method="click",
            selector='[data-testid="create-submit"]',
            modal_ordinal=0,
            ctrl_sig=("button", "submit", "button", "create"),
        ),
    ]
    dossier = {"collection_title": "Q4 Archive"}

    lifted = lift_distilled_repair_to_pack(
        verified_repairs, dossier, exploratory_prefix=exploratory_prefix
    )
    assert lifted.pruned_exploratory_count == 7
    assert len(lifted.steps) == 2
    assert lifted.stripped_selector_count == 1
    assert lifted.steps[0].selector is None
    assert lifted.steps[0].stripped_ephemeral_selector == "#input-99"
    assert lifted.steps[0].value == "$collection_title"
    assert lifted.steps[0].control_signature is not None
    assert lifted.steps[0].control_signature.in_modal is True
    assert lifted.steps[0].control_signature.label_tokens == ("title",)
    assert lifted.steps[1].selector == '[data-testid="create-submit"]'

    # Inline navigation reset marker also prunes unverified exploratory prefix
    mixed_steps: List[Dict[str, Any]] = [
        {"step_id": "explore_1", "action": "click", "selector": 'button[name="wrong"]'},
        {"step_id": "explore_2", "action": "click", "selector": 'button[name="retry"]'},
        {"step_id": "nav_reset", "action": "click", "selector": '[data-testid="new-item"]', "is_navigation_reset": True},
        {"step_id": "fill_1", "action": "fill", "selector": 'input[name="title"]', "value": "Q4 Archive"},
    ]
    pruned_nav = lift_cold_trace_to_pack(mixed_steps, dossier)
    assert pruned_nav.pruned_exploratory_count == 2
    assert [s.step_id for s in pruned_nav.steps] == ["nav_reset", "fill_1"]
    assert pruned_nav.steps[1].value == "$collection_title"

    # Opt-out preserves exploratory prefix when requested
    unpruned = lift_cold_trace_to_pack(
        exploratory_prefix[:2], dossier, verified_repair_steps=verified_repairs, prune_exploratory_prefix_before_repair=False
    )
    assert unpruned.pruned_exploratory_count == 0
    assert len(unpruned.steps) == 4


def test_egress_guard_allows_standard_w3c_css_keywords() -> None:
    policy = EgressPolicy()
    css_twin_payload = {
        "sanitized_html": (
            '<div style="display: flow-root; margin: 0; padding: 0;">'
            '<span style="display: contents; color: unset; font: inherit; border: initial;"></span>'
            '<div style="display: table-cell; -webkit-box-sizing: border-box; all: revert;"></div>'
            "</div>"
        )
    }
    verdict = inspect_outbound_payload(css_twin_payload, policy)
    assert verdict.allowed is True, f"Unexpectedly blocked W3C CSS tokens: {verdict.rejected_tokens}"

    # Verify non-CSS opaque/PHI identifiers are still blocked by default policy
    phi_payload = {"sanitized_html": '<div style="display: flow-root;">JonathanDoe1984</div>'}
    blocked = inspect_outbound_payload(phi_payload, policy)
    assert blocked.allowed is False
    assert "JonathanDoe1984" in blocked.rejected_tokens


def test_extract_dossier_facts_and_closure_autowiring() -> None:
    sources = {
        "policy_doc": (
            "Order SKU-4821 billed $1,250.00 on 2026-05-01; appeals due within 30 days. "
            "Do not issue partial credit without manager signoff."
        ),
        "case_record": "Primary Code: J45.909\nStatus: Denied\nReason: Prior authorization expired",
    }
    facts = extract_dossier_facts(
        sources, extra_patterns={"batch_tag": r"\b(BATCH-\d{3})\b"}
    )
    cats = {f.category for f in facts}
    vals = {f.value for f in facts}
    assert {"code", "date", "currency", "deadline", "policy_clause", "kv_finding"}.issubset(cats)
    assert "SKU-4821" in vals
    assert "J45.909" in vals
    assert "$1,250.00" in vals
    assert "2026-05-01" in vals
    assert any("within 30 days" in v.lower() for v in vals)
    assert any("do not issue partial credit" in v.lower() for v in vals)
    assert "Prior authorization expired" in vals

    # Auto-wiring into verify_completion_closure catches omitted required facts
    ledger = ObligationLedger("extract-ep-1", subject_key="CASE-77")
    ledger.add_obligation("log_note", "Record case determination")
    ledger.mark_committed(
        "log_note",
        ReadBackProof("api_read", "https://portal.example/cases/77", ["CASE-77", "Saved"]),
    )
    targeted_facts = (
        ExtractedFact("code", "J45.909", "case_record", required_in_freetext=True),
        ExtractedFact("date", "2026-05-01", "policy_doc", required_in_freetext=True),
        ExtractedFact("currency", "$1,250.00", "policy_doc", required_in_freetext=True),
        ExtractedFact("kv_finding", "optional background memo", "case_record", required_in_freetext=False),
    )

    blocked = verify_completion_closure(
        ledger,
        step_idx=3,
        max_steps=10,
        freetext_notes="Reviewed case on May 1, 2026 for $1,250.00.",
        extracted_facts=targeted_facts,
    )
    assert blocked.passed is False
    assert blocked.reason == "missing_freetext_facts"
    assert any("J45.909" in k or k.startswith("code:") for k in blocked.missing_freetext_facts)

    passed = verify_completion_closure(
        ledger,
        step_idx=4,
        max_steps=10,
        freetext_notes="Reviewed J45.909 on May 1, 2026; billed amount 1250.00 verified.",
        extracted_facts=targeted_facts,
    )
    assert passed.passed is True
    assert passed.outcome == "VERIFIED"


def test_closure_anchored_critic_calibration() -> None:
    ledger = ObligationLedger("calib-ep-1", subject_key="ORD-99")
    ledger.add_obligation("commit_order", "Confirm order update")
    failed_closure = verify_completion_closure(ledger, step_idx=2, max_steps=10)
    assert failed_closure.passed is False

    # Failed closure ALWAYS escalates even if critic did not veto
    cal_fail = calibrate_critic_decision(failed_closure, critic_vetoed=False)
    assert cal_fail.action == "ESCALATE"
    assert cal_fail.overrode_false_escalation is False

    ledger.mark_committed(
        "commit_order",
        ReadBackProof("fresh_reload", "https://shop.example/orders/99", ["ORD-99", "Confirmed"]),
    )
    verified_closure = verify_completion_closure(ledger, step_idx=3, max_steps=10)
    assert verified_closure.passed is True

    # Verified closure + conservative LLM critic veto -> override false escalation
    cal_override = calibrate_critic_decision(
        verified_closure, critic_vetoed=True, critic_reason="unsure about sidebar count"
    )
    assert cal_override.action == "ALLOW_COMPLETE"
    assert cal_override.overrode_false_escalation is True

    # Policy human gate is NEVER weakened even when closure passed
    cal_human = calibrate_critic_decision(
        verified_closure, critic_vetoed=False, policy_requires_human=True
    )
    assert cal_human.action == "PREPARE"
    assert cal_human.overrode_false_escalation is False


def test_readonly_answer_provenance_grounding() -> None:
    observed_spans = [
        "Ticket INC-501 status: Pending Approval. Total billed: $249.50 on 2026-04-15 (14 items).",
    ]
    observed_urls = ["https://tracker.example/issues/INC-501"]

    grounded = verify_readonly_answer_provenance(
        'Ticket INC-501 at https://tracker.example/issues/INC-501 is "Pending Approval" '
        "for $249.50 on Apr 15, 2026 with 14 items.",
        observed_spans,
        observed_urls,
    )
    assert grounded.covered is True
    assert not grounded.missing_facts

    hallucinated = verify_readonly_answer_provenance(
        "Ticket INC-999 at https://fake.example/admin was billed $999.00.",
        observed_spans,
        observed_urls,
    )
    assert hallucinated.covered is False
    assert "code:INC-999" in hallucinated.missing_facts
    assert "url:https://fake.example/admin" in hallucinated.missing_facts
    assert "currency:$999.00" in hallucinated.missing_facts

    empty_res = verify_readonly_answer_provenance("", observed_spans)
    assert empty_res.covered is False
    assert empty_res.missing_facts == ("empty_answer",)


def test_compile_policy_rules_to_table_and_step0_wiring() -> None:
    specs: List[Dict[str, Any]] = [
        {"kind": "missing_params", "params": ["order_id", "$refund_reason"]},
        {
            "kind": "access_management",
            "allow_patterns": ["https://portal.example/orders/*"],
            "deny_patterns": ["*/admin/*", "*/delete*"],
        },
        {
            "kind": "policy_contradiction",
            "rule_id": "expired_return",
            "when": {"return_window": "expired"},
            "then": "deny_refund",
        },
        {"kind": "ask_the_user", "triggers": ["discount_override"]},
        {
            "kind": "irreversible_actions",
            "actions": ["wire_transfer", "account_purge"],
            "when": {"action_kind": "wire_transfer"},
        },
        {
            "rule_id": "nl_high_risk",
            "rule_text": "When risk_tier=high and loss_history=prior_claim then refer_senior",
        },
    ]

    table, gate_cfg = compile_policy_rules_to_table(specs, table_id="enterprise_step0_v1")
    assert table.table_id == "enterprise_step0_v1"
    assert table.require_human_if_unmatched is True
    assert gate_cfg["required_params"] == ["order_id", "$refund_reason"]
    assert "*/admin/*" in gate_cfg["deny_patterns"]
    assert "wire_transfer" in gate_cfg["irreversible_actions"]
    assert "discount_override" in gate_cfg["ask_user_triggers"]

    # Compiled NL rule works directly in evaluate_policy_decision
    nl_res = evaluate_policy_decision(
        table, {"risk_tier": "high", "loss_history": "prior_claim"}, "auto_bind"
    )
    assert nl_res.allowed is False
    assert nl_res.expected_disposition == "refer_senior"

    # Compiled gate_cfg + table plug directly into evaluate_pre_step_policy
    missing = evaluate_pre_step_policy(
        stage="step_0", required_params=gate_cfg["required_params"], dossier={"order_id": "ORD-1"}
    )
    assert missing.action == "PREPARE_ASK"
    assert missing.missing_params == ("$refund_reason",)

    contradiction = evaluate_pre_step_policy(
        stage="step_0",
        required_params=gate_cfg["required_params"],
        dossier={"order_id": "ORD-1", "refund_reason": "late"},
        policy_table=table,
        observed_facts={"return_window": "expired"},
        task_disposition="issue_refund",
    )
    assert contradiction.action == "REFUSE"
    assert contradiction.reason == "policy_contradiction"

    blocked_nav = evaluate_pre_step_policy(
        stage="pre_navigation",
        target_url="https://portal.example/admin/users",
        allow_patterns=gate_cfg["allow_patterns"],
        deny_patterns=gate_cfg["deny_patterns"],
    )
    assert blocked_nav.action == "REFUSE"


def test_hybrid_extract_dossier_facts_real_world_and_llm_merge() -> None:
    real_sources = {
        "clinical_review": (
            "Diagnosis H35.32 evaluated on 2025-02-01. Slit-lamp exam shows 3+ nuclear sclerosis opacity. "
            "Appeal window is 60 calendar days from notice date. "
            "This is a documentation gap rather than a clinical dispute. "
            "The delegation/capitation arrangement requires rerouting to the delegated group."
        )
    }
    facts = extract_dossier_facts(real_sources)
    vals_lower = [f.value.lower() for f in facts]
    assert any("h35.32" in v for v in vals_lower)
    assert any("2025-02-01" in v for v in vals_lower)
    assert any("60 calendar days" in v for v in vals_lower)
    assert any("3+ nuclear sclerosis opacity" in v for v in vals_lower)
    assert any("documentation gap rather than a clinical dispute" in v for v in vals_lower)
    assert any("delegation/capitation arrangement requires rerouting" in v for v in vals_lower)

    # Pluggable local/LLM extractor merges and deduplicates cleanly
    def mock_llm_extractor(_src: Dict[str, str] | Any) -> List[Dict[str, Any]]:
        return [
            {"category": "code", "value": "H35.32", "source_key": "llm", "required_in_freetext": True},
            {
                "category": "policy_clause",
                "value": "attach operative note prior to resubmission",
                "source_key": "llm_summary",
                "required_in_freetext": True,
            },
        ]

    merged = extract_dossier_facts(real_sources, llm_extractor_fn=mock_llm_extractor)
    assert sum(1 for f in merged if f.category == "code" and f.value == "H35.32") == 1
    assert any(f.value == "attach operative note prior to resubmission" for f in merged)


def test_real_schema_and_hybrid_policy_compiler_and_critic_guard() -> None:
    nl_policies: List[Dict[str, Any]] = [
        {
            "policy_template_id": "missing_params",
            "description": "Ask the user for the customer email before creating a lead.",
        },
        {
            "policy_template_id": "access_management",
            "description": "Do not access /admin or admin settings pages.",
        },
        {
            "policy_template_id": "irreversible_actions",
            "description": "Never delete production records without explicit approval.",
        },
    ]

    table, gate_cfg = compile_policy_rules_to_table(nl_policies, table_id="real_schema_v1")
    assert table.rules, "Expected compiled PolicyRule entries from prohibited verbs"
    assert "customer_email" in gate_cfg["required_params"]
    assert "customer_email" in gate_cfg["ask_user_triggers"]
    assert any("/admin" in p or "admin" in p for p in gate_cfg["deny_patterns"])

    # Verify compiled config fires accurately in evaluate_pre_step_policy
    missing_res = evaluate_pre_step_policy(
        stage="step_0", required_params=gate_cfg["required_params"], dossier={"lead_name": "Ada"}
    )
    assert missing_res.action == "PREPARE_ASK"
    assert "customer_email" in missing_res.missing_params

    nav_res = evaluate_pre_step_policy(
        stage="pre_navigation",
        target_url="https://crm.example/admin/settings",
        deny_patterns=gate_cfg["deny_patterns"],
    )
    assert nav_res.action == "REFUSE"

    prohibit_res = evaluate_pre_step_policy(
        stage="step_0",
        policy_table=table,
        observed_facts={"action": "delete", "target": "production_records"},
        task_disposition="proceed_delete",
    )
    assert prohibit_res.action == "REFUSE"
    assert prohibit_res.reason == "policy_contradiction"

    # Pluggable LLM policy compiler merges structured rules cleanly
    def mock_llm_compiler(_specs: Any) -> Dict[str, Any]:
        return {
            "required_params": ["billing_zip"],
            "deny_patterns": ["*/billing/purge*"],
            "rules": [
                {
                    "rule_id": "llm_export_guard",
                    "conditions": {"export_scope": "bulk"},
                    "required_disposition": "require_manager_approval",
                }
            ],
        }

    hybrid_table, hybrid_cfg = compile_policy_rules_to_table(
        nl_policies, llm_compiler_fn=mock_llm_compiler
    )
    assert "billing_zip" in hybrid_cfg["required_params"]
    assert "*/billing/purge*" in hybrid_cfg["deny_patterns"]
    assert any(r.rule_id == "llm_export_guard" for r in hybrid_table.rules)

    # Safe calibrate_critic_decision guard: un-extracted facts preserve conservative critic veto
    ledger = ObligationLedger("guard-ep-1", subject_key="CASE-9")
    ledger.add_obligation("submit_note", "Save free-text note")
    ledger.mark_committed(
        "submit_note",
        ReadBackProof("api_read", "https://portal.example/cases/9", ["CASE-9", "Saved"]),
    )
    verified = verify_completion_closure(ledger, step_idx=2, max_steps=10)
    assert verified.passed is True

    preserved = calibrate_critic_decision(
        verified,
        critic_vetoed=True,
        critic_reason="possible missing clinical nuance",
        require_extracted_facts=True,
        extracted_facts_count=0,
    )
    assert preserved.action == "ESCALATE"
    assert preserved.overrode_false_escalation is False
    assert preserved.reason == "unextracted_facts_preserve_critic_veto"

    overridden = calibrate_critic_decision(
        verified,
        critic_vetoed=True,
        critic_reason="vague hesitation",
        require_extracted_facts=True,
        extracted_facts_count=4,
    )
    assert overridden.action == "ALLOW_COMPLETE"
    assert overridden.overrode_false_escalation is True


def test_split_brain_prose_overlap_entailment_and_template_compilation() -> None:
    facts = {
        "code:dx:0": "H35.32",
        "currency:amt:1": "$1,250.00",
        "policy_clause:note:2": "documentation gap rather than a clinical dispute",
        "routing_clause:route:3": "delegation arrangement requires rerouting to specialty group",
    }
    ftypes: Dict[str, FieldType] = {"code:dx:0": "code_prefix", "currency:amt:1": "currency"}

    # Paraphrased prose passes via content-word overlap (>= 0.65), while atomic code/currency stay strict
    paraphrased_ok = verify_freetext_fact_coverage(
        "Case H35.32 billed 1250.00 reflects a documentation gap instead of clinical dispute; "
        "delegation arrangement requires rerouting to the specialty team.",
        facts,
        field_types=ftypes,
    )
    assert paraphrased_ok.covered is True

    # Missing atomic code MUST still fail even if prose overlaps 100%
    missing_atomic = verify_freetext_fact_coverage(
        "Case billed $1,250.00 is a documentation gap rather than a clinical dispute; "
        "delegation arrangement requires rerouting to specialty group.",
        facts,
        field_types=ftypes,
    )
    assert missing_atomic.covered is False
    assert missing_atomic.missing_facts == ("code:dx:0",)

    # Pluggable entailment_fn handles true synonyms on prose while NOT bypassing atomic code checks
    synonym_cov = verify_freetext_fact_coverage(
        "Case H35.32 for $1,250.00 needs paperwork fixes only and transfer to outside unit.",
        facts,
        field_types=ftypes,
        entailment_fn=lambda draft, claim: "paperwork" in draft or "transfer" in draft,
    )
    assert synonym_cov.covered is True

    atomic_not_bypassed = verify_freetext_fact_coverage(
        "Case for $1,250.00 needs paperwork fixes only.",
        {"code:dx:0": "H35.32"},
        field_types={"code:dx:0": "code_prefix"},
        entailment_fn=lambda _draft, _claim: True,
    )
    assert atomic_not_bypassed.covered is False

    # Deterministic prose template compilation converts repeatable notes into PutGet
    rendered, missing = compile_freetext_template(
        "Diagnosis: $code. Finding: $finding. Follow-up: $followup_date.",
        {"code": "H35.32", "finding": "3+ nuclear sclerosis opacity"},
        required_keys=("$code", "$followup_date"),
    )
    assert rendered == "Diagnosis: H35.32. Finding: 3+ nuclear sclerosis opacity. Follow-up: $followup_date."
    assert missing == ["followup_date"]

    rendered_full, missing_full = compile_freetext_template(
        "Diagnosis: $code. Finding: $finding.",
        {"code": "H35.32", "finding": "3+ nuclear sclerosis opacity"},
    )
    assert rendered_full == "Diagnosis: H35.32. Finding: 3+ nuclear sclerosis opacity."
    assert missing_full == []


def test_sandboxed_ast_derived_calculator_and_closure_binding() -> None:
    dosage = evaluate_derived_calculation(
        "annual_dosage", "dose_mg * cycles_per_year", {"dose_mg": 2, "cycles_per_year": 6}, unit="mg"
    )
    assert dosage.value == 12
    assert dosage.formatted_value == "12mg"
    assert dosage.provenance == "derived_calc"

    visits = evaluate_derived_calculation("total_visits", "round(months * 2.5)", {"months": 6})
    assert visits.value == 15
    assert visits.formatted_value == "15"

    deadline = evaluate_derived_calculation(
        "appeal_deadline", "add_days(notice_date, 60)", {"notice_date": "2026-03-01"}
    )
    assert deadline.formatted_value == "2026-04-30"

    # Rejects unsafe AST nodes / arbitrary calls
    for bad_expr in (
        "__import__('os').system('echo pwn')",
        "eval('1+1')",
        "(1).__class__",
        "[x for x in [1, 2]]",
        "2 ** 1000",
    ):
        with pytest.raises(ValueError):
            evaluate_derived_calculation("unsafe", bad_expr, {})

    # Bind derived calculation into dossier and enforce exact presence in closure
    base_dossier = {"primary_code": "H35.32"}
    updated_dossier = bind_derived_calculation(base_dossier, dosage)
    assert updated_dossier["annual_dosage"] == "12mg"
    assert "annual_dosage" not in base_dossier

    ledger = ObligationLedger("calc-ep-1", subject_key="CASE-12")
    ledger.add_obligation("submit_auth", "Submit authorization note")
    ledger.mark_committed(
        "submit_auth", ReadBackProof("api_read", "https://portal.example/auth/12", ["CASE-12", "12mg"])
    )

    wrong_math = verify_completion_closure(
        ledger,
        step_idx=3,
        max_steps=10,
        freetext_notes="Annual dosage requested: 10mg for H35.32.",
        extracted_facts=(ExtractedFact("derived_calc", dosage.formatted_value, "calc", True),),
    )
    assert wrong_math.passed is False
    assert wrong_math.reason == "missing_freetext_facts"

    exact_math = verify_completion_closure(
        ledger,
        step_idx=4,
        max_steps=10,
        freetext_notes="Annual dosage requested: 12mg for H35.32.",
        extracted_facts=(ExtractedFact("derived_calc", dosage.formatted_value, "calc", True),),
    )
    assert exact_math.passed is True


def test_query_view_preconditions_and_reactive_mid_trajectory_policy() -> None:
    # Query-view precondition check blocks wrong-filter read-only answers
    wrong_view = verify_query_view_preconditions(
        {"status": "open", "assignee": "alex", "tab": "billing"},
        current_url="https://tracker.example/issues?status=closed&assignee=alex",
        active_filter_chips=["Status: Closed", "Assignee: Alex"],
        active_tab="Billing",
    )
    assert wrong_view.passed is False
    assert wrong_view.missing_filters == ("status=open",)

    right_view = verify_query_view_preconditions(
        {"status": "open", "assignee": "alex", "sort": "newest", "tab": "billing"},
        current_url="https://tracker.example/issues?status=open&sort=newest",
        active_filter_chips=["Assignee: Alex"],
        active_tab="Billing",
    )
    assert right_view.passed is True
    assert not right_view.missing_filters

    # Mid-trajectory dossier-reactive policy re-evaluation (0 LLM calls)
    specs: List[Dict[str, Any]] = [
        {"kind": "access_management", "deny_patterns": ["*/admin/*"]},
        {
            "kind": "policy_contradiction",
            "rule_id": "external_domain_block",
            "when": {"recipient_scope": "external"},
            "then": "block_export",
        },
        {"kind": "irreversible_actions", "actions": ["wire_transfer"]},
    ]
    table, cfg = compile_policy_rules_to_table(specs, table_id="reactive_v1")

    # Discovered external recipient on page 3 -> blocks send_report immediately
    refused = evaluate_reactive_policy_on_dossier_update(
        table,
        pre_step_config=cfg,
        updated_dossier={"recipient_scope": "external", "account_id": "ACC-1"},
        proposed_action="send_report",
    )
    assert refused.allowed is False
    assert refused.action == "REFUSE"
    assert refused.reason == "reactive_policy_contradiction"

    # Irreversible action triggers PREPARE_ASK mid-trajectory
    irrev = evaluate_reactive_policy_on_dossier_update(
        table,
        pre_step_config=cfg,
        updated_dossier={"account_id": "ACC-1"},
        proposed_action="wire_transfer",
    )
    assert irrev.allowed is False
    assert irrev.action == "PREPARE_ASK"
    assert irrev.reason == "irreversible_action_requires_confirmation"

    # Ungrounded target_value mid-trajectory triggers VETO
    vetoed = evaluate_reactive_policy_on_dossier_update(
        PolicyDecisionTable("empty", (), require_human_if_unmatched=False),
        pre_step_config={**cfg, "grounding_required": True},
        updated_dossier={"account_id": "ACC-1", "balance": "$500.00"},
        proposed_action="fill_balance",
        target_value="$999.00",
        field_name="balance",
        field_type="currency",
    )
    assert vetoed.allowed is False
    assert vetoed.action == "VETO"

    # Clean allowed mid-trajectory action
    allowed = evaluate_reactive_policy_on_dossier_update(
        PolicyDecisionTable("empty", (), require_human_if_unmatched=False),
        pre_step_config=cfg,
        updated_dossier={"account_id": "ACC-1", "balance": "$500.00"},
        proposed_action="fill_balance",
        target_url="https://crm.example/accounts/ACC-1",
        target_value="500.00",
        field_name="balance",
        field_type="currency",
    )
    assert allowed.allowed is True
    assert allowed.action == "ALLOW"
