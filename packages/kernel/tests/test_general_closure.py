"""Multi-domain unit tests for general-purpose closure primitives.

Covers PolicyDecisionTable routing, non-waivable step-limit default-deny,
required dossier key coverage, triage evidence citation, ControlSignature,
ephemeral selector detection, and cold-trace-to-pack lifting across
ERP, insurance underwriting, clinical triage, and SaaS UI frameworks.
"""

from __future__ import annotations

from typing import Any, Dict, List

from openadapt_kernel import (
    ControlSchema,
    ControlSignature,
    FieldType,
    ObligationLedger,
    PolicyDecisionTable,
    PolicyRule,
    ReadBackProof,
    evaluate_policy_decision,
    is_ephemeral_selector,
    lift_cold_trace_to_pack,
    parameterize_value_against_dossier,
    verify_completion_closure,
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
