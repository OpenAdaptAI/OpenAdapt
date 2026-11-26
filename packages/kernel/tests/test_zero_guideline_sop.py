"""Tests for Zero-Guideline Enum Probing, Case-Tab Discovery, Counterfactual Gate, and Auto-SOP Induction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from openadapt_kernel.app_cartridge import (
    AdversarialMutantSpec,
    CounterfactualEnumVerdict,
    EnumAffordanceProbe,
    discover_case_evidence_tabs,
    evaluate_counterfactual_enum_gate,
    induce_sop_from_shadow_traces,
    probe_enum_affordances_in_twin,
    render_sop_signoff_summary,
)
from openadapt_kernel.egress_guard import EgressBlockedError, EgressPolicy, LocalEgressGuard
from openadapt_kernel.fsm import evaluate_policy_decision
from openadapt_kernel.ledger import (
    EpistemicReadTracker,
    ObligationLedger,
    ReadBackProof,
    verify_completion_closure,
)
from openadapt_kernel.stateful_twin import StatefulTwinApp, TwinPageSpec


def test_discover_case_evidence_tabs_blocks_until_all_tabs_read() -> None:
    """discover_case_evidence_tabs registers case tabs/links and blocks commit when EOB or inquiry is unread."""
    dom_controls = [
        {"role": "tab", "tag": "button", "label": "Case Summary"},
        {"role": "tab", "tag": "button", "label": "Remittance EOB"},
        {"role": "tab", "tag": "button", "label": "Payer Inquiry History"},
        {"role": "link", "tag": "a", "label": "Supporting Attachments (2)", "href": "/case/attachments"},
        {"role": "region", "tag": "section", "aria-label": "Plan Coverage Details"},
        {"role": "button", "tag": "button", "label": "Submit Disposition"},
        {"role": "button", "tag": "button", "label": "Cancel"},
    ]

    partial_tabs = discover_case_evidence_tabs(dom_controls, visited_labels=["Case Summary"])
    labels = [a.label for a in partial_tabs]
    assert labels == [
        "Case Summary",
        "Remittance EOB",
        "Payer Inquiry History",
        "Supporting Attachments (2)",
        "Plan Coverage Details",
    ]

    tracker = EpistemicReadTracker()
    for art in partial_tabs:
        tracker.register_artifact(
            art.artifact_id,
            art.kind,
            art.label,
            required_before_commit=art.required_before_commit,
            read=art.read,
        )
    assert len(tracker.unread_required_artifacts()) == 4

    ledger = ObligationLedger("ep-case-tabs")
    ledger.add_obligation("disp_commit", "Record claim disposition")
    ledger.mark_committed(
        "disp_commit",
        ReadBackProof(channel="fresh_reload", url_or_locator="/case/1", cited_substrings=["Recorded"]),
    )

    blocked = verify_completion_closure(
        ledger,
        step_idx=3,
        max_steps=10,
        epistemic_artifacts=list(tracker.artifacts.values()),
    )
    assert blocked.passed is False
    assert blocked.outcome == "ESCALATED"
    assert "tab:remittance_eob" in blocked.unread_epistemic_artifacts
    assert "tab:payer_inquiry_history" in blocked.unread_epistemic_artifacts

    # Marking all remaining tabs/attachments read allows completion closure to verify
    all_read_tabs = discover_case_evidence_tabs(
        dom_controls,
        current_url="https://portal.example/case/attachments",
        visited_labels=[
            "Case Summary",
            "Remittance EOB",
            "Payer Inquiry History",
            "Plan Coverage Details",
        ],
    )
    assert all(a.read for a in all_read_tabs)

    verified = verify_completion_closure(
        ledger,
        step_idx=5,
        max_steps=10,
        epistemic_artifacts=all_read_tabs,
    )
    assert verified.passed is True
    assert verified.outcome == "VERIFIED"


def test_probe_enum_affordances_in_twin_live_offline_playwright(tmp_path: Path) -> None:
    """probe_enum_affordances_in_twin discovers conditional inputs/help offline with 0 network calls and 0 B PHI."""
    triage_html = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Twin Triage</title></head>
<body>
  <form method="post" action="/submit">
    <label for="case-ref">Case Reference</label>
    <input id="case-ref" name="reference" type="text" required />

    <label for="disp-select">Disposition Action</label>
    <select id="disp-select" name="action">
      <option value="">Select</option>
      <option value="RESUBMIT_MODIFIER">Resubmit With Modifier</option>
      <option value="ROUTE_INTERNAL">Route Internal Queue</option>
      <option value="WRITE_OFF_AUTH">Write Off Expired Auth</option>
    </select>

    <div id="panel-modifier" style="display: none">
      <label for="mod-input">Modifier Code</label>
      <input id="mod-input" name="modifier" type="text" aria-label="Modifier Code" />
      <p id="help-modifier">Provide valid billing modifier code before claim resubmission</p>
    </div>

    <div id="panel-internal" style="display: none">
      <label for="assignee-input">Internal Assignee</label>
      <input id="assignee-input" name="assignee" type="text" aria-label="Internal Assignee" />
      <p id="help-internal">Select internal specialist queue owner</p>
    </div>

    <div id="panel-writeoff" style="display: none">
      <label for="reason-input">Adjustment Reason</label>
      <input id="reason-input" name="reason" type="text" aria-label="Adjustment Reason" />
      <p id="help-writeoff">Record expired authorization reference code</p>
    </div>

    <button type="submit">Submit Case</button>
  </form>
  <script>
    const sel = document.getElementById('disp-select');
    const modPanel = document.getElementById('panel-modifier');
    const modInput = document.getElementById('mod-input');
    const intPanel = document.getElementById('panel-internal');
    const intInput = document.getElementById('assignee-input');
    const woPanel = document.getElementById('panel-writeoff');
    const woInput = document.getElementById('reason-input');
    sel.addEventListener('change', () => {
      modPanel.style.display = sel.value === 'RESUBMIT_MODIFIER' ? 'block' : 'none';
      modInput.required = sel.value === 'RESUBMIT_MODIFIER';
      intPanel.style.display = sel.value === 'ROUTE_INTERNAL' ? 'block' : 'none';
      intInput.required = sel.value === 'ROUTE_INTERNAL';
      woPanel.style.display = sel.value === 'WRITE_OFF_AUTH' ? 'block' : 'none';
      woInput.required = false;
    });
  </script>
</body>
</html>"""

    guard = LocalEgressGuard(
        tmp_path / "twin_probe_egress.jsonl",
        policy=EgressPolicy(
            extra_allowed_tokens=frozenset(
                {
                    "adjustment", "assignee", "auth", "expired", "internal", "modifier",
                    "off", "owner", "path", "resubmit", "resubmission", "route",
                    "specialist", "triage", "writeoff",
                }
            )
        ),
    )
    verdict = guard.check_and_record(
        {"page_path": "/triage", "options": ["RESUBMIT_MODIFIER", "ROUTE_INTERNAL", "WRITE_OFF_AUTH"]},
        destination="twin_affordance_probe",
    )
    assert verdict.allowed is True

    twin_app = StatefulTwinApp(
        pages={"/triage": TwinPageSpec(url_path="/triage", sanitized_html=triage_html)},
        route_specs=(),
        egress_guard=guard,
    )

    probes = probe_enum_affordances_in_twin(
        twin_app,
        "#disp-select",
        ["RESUBMIT_MODIFIER", "ROUTE_INTERNAL", "WRITE_OFF_AUTH"],
        page_path="/triage",
    )
    assert len(probes) == 3
    by_opt = {p.option_value: p for p in probes}

    assert by_opt["RESUBMIT_MODIFIER"].revealed_inputs == ("Modifier Code",)
    assert by_opt["RESUBMIT_MODIFIER"].required_fields == ("Modifier Code",)
    assert "modifier code" in by_opt["RESUBMIT_MODIFIER"].revealed_help_text.lower()

    assert by_opt["ROUTE_INTERNAL"].revealed_inputs == ("Internal Assignee",)
    assert by_opt["ROUTE_INTERNAL"].required_fields == ("Internal Assignee",)
    assert "internal specialist" in by_opt["ROUTE_INTERNAL"].revealed_help_text.lower()

    assert by_opt["WRITE_OFF_AUTH"].revealed_inputs == ("Adjustment Reason",)
    assert by_opt["WRITE_OFF_AUTH"].required_fields == ()
    assert "expired authorization" in by_opt["WRITE_OFF_AUTH"].revealed_help_text.lower()

    # Zero live network calls, zero mutating requests, and verified zero-PHI egress chain
    assert twin_app.live_network_requests_attempted == 0
    assert all(r["method"] == "GET" for r in twin_app.handled_twin_requests)
    audit = twin_app.egress_audit()
    assert audit["blocked_count"] == 0
    assert audit["sha256_chain_integrity_verified"] is True


def test_evaluate_counterfactual_enum_gate_unanimous_vs_split_and_affordance_match() -> None:
    """evaluate_counterfactual_enum_gate allows unanimous votes and routes 2-vs-1 splits to PREPARE_REQUIRED."""
    candidates = ["RESUBMIT_MODIFIER", "ROUTE_INTERNAL", "WRITE_OFF_AUTH"]
    probes = (
        EnumAffordanceProbe("RESUBMIT_MODIFIER", ("Modifier Code",), "Provide billing modifier code", ("Modifier Code",)),
        EnumAffordanceProbe("ROUTE_INTERNAL", ("Internal Assignee",), "Choose internal assignee", ("Internal Assignee",)),
        EnumAffordanceProbe("WRITE_OFF_AUTH", ("Adjustment Reason",), "Expired authorization write-off", ()),
    )

    # 1. Unanimous 3-of-3 across rotated option orders -> ALLOW
    unanimous: CounterfactualEnumVerdict = evaluate_counterfactual_enum_gate(
        candidates,
        context_facts={"denial_reason": "missing_modifier", "modifier_code": "25"},
        affordance_probes=probes,
        contrast_fn=lambda _opts, _facts: "RESUBMIT_MODIFIER",
        k_samples=3,
        min_agreement=1.0,
    )
    assert unanimous.decision == "ALLOW"
    assert unanimous.selected_option == "RESUBMIT_MODIFIER"
    assert unanimous.agreement_ratio == 1.0
    assert unanimous.votes == {"RESUBMIT_MODIFIER": 3}
    assert unanimous.affordance_matched is True

    # 2. Position-biased / 2-vs-1 split -> PREPARE_REQUIRED (0% silent guessing)
    def split_contrast(sample_idx: int, _opts: Sequence[str], _facts: dict, _probes: Sequence[EnumAffordanceProbe]) -> str:
        return "RESUBMIT_MODIFIER" if sample_idx < 2 else "ROUTE_INTERNAL"

    split_verdict = evaluate_counterfactual_enum_gate(
        candidates,
        context_facts={"denial_reason": "ambiguous_note"},
        affordance_probes=probes,
        contrast_fn=split_contrast,
        k_samples=3,
        min_agreement=1.0,
    )
    assert split_verdict.decision == "PREPARE_REQUIRED"
    assert split_verdict.selected_option == ""
    assert split_verdict.agreement_ratio == pytest.approx(2.0 / 3.0, abs=1e-3)
    assert split_verdict.votes == {"RESUBMIT_MODIFIER": 2, "ROUTE_INTERNAL": 1}

    # 3. Deterministic zero-LLM affordance matcher (contrast_fn=None)
    det_allow = evaluate_counterfactual_enum_gate(
        candidates,
        context_facts={"modifier_code": "59", "claim_note": "missing modifier"},
        affordance_probes=probes,
        contrast_fn=None,
        k_samples=3,
    )
    assert det_allow.decision == "ALLOW"
    assert det_allow.selected_option == "RESUBMIT_MODIFIER"
    assert det_allow.affordance_matched is True

    det_ambiguous = evaluate_counterfactual_enum_gate(
        candidates,
        context_facts={"unmapped_flag": "unknown"},
        affordance_probes=probes,
        contrast_fn=None,
        k_samples=3,
    )
    assert det_ambiguous.decision == "PREPARE_REQUIRED"
    assert det_ambiguous.selected_option == ""


def test_induce_sop_from_shadow_traces_and_plain_english_signoff_summary(tmp_path: Path) -> None:
    """induce_sop_from_shadow_traces audits 0 B PHI, induces 3 branches from 6 resolutions, and renders sign-off."""
    # PHI in operator traces MUST be blocked by LocalEgressGuard before induction
    with pytest.raises(EgressBlockedError):
        induce_sop_from_shadow_traces(
            [
                {
                    "features": {"patient_name": "Maria Gonzalez", "ssn": "123-45-6789"},
                    "disposition": "WRITE_OFF",
                }
            ],
            ledger_path=tmp_path / "phi_blocked.jsonl",
        )

    # 6 scrubbed Zero-PHI operator resolutions across 3 triage dispositions (+ varying tier noise)
    resolutions = [
        {
            "features": {
                "denial_category": "missing_modifier",
                "auth_status": "valid",
                "coverage_status": "active",
                "tier": "primary",
            },
            "disposition": "RESUBMIT_WITH_MODIFIER",
            "observed_read_artifacts": ["remittance_eob", "claim_detail"],
            "observed_note_slots": ["modifier", "code"],
        },
        {
            "features": {
                "denial_category": "missing_modifier",
                "auth_status": "valid",
                "coverage_status": "active",
                "tier": "secondary",
            },
            "disposition": "RESUBMIT_WITH_MODIFIER",
            "observed_read_artifacts": ["remittance_eob", "claim_detail"],
            "observed_note_slots": ["modifier", "code"],
        },
        {
            "features": {
                "denial_category": "expired_authorization",
                "auth_status": "expired",
                "coverage_status": "active",
                "tier": "primary",
            },
            "disposition": "WRITE_OFF",
            "observed_read_artifacts": ["remittance_eob", "prior_auth"],
            "observed_note_slots": ["authorization", "reason"],
        },
        {
            "features": {
                "denial_category": "expired_authorization",
                "auth_status": "expired",
                "coverage_status": "active",
                "tier": "secondary",
            },
            "disposition": "WRITE_OFF",
            "observed_read_artifacts": ["remittance_eob", "prior_auth"],
            "observed_note_slots": ["authorization", "reason"],
        },
        {
            "features": {
                "denial_category": "coverage_exclusion",
                "auth_status": "none",
                "coverage_status": "excluded",
                "tier": "primary",
            },
            "disposition": "TRANSFER_PATIENT_RESPONSIBILITY",
            "observed_read_artifacts": ["remittance_eob", "plan_benefits"],
            "observed_note_slots": ["coverage", "explanation"],
        },
        {
            "features": {
                "denial_category": "coverage_exclusion",
                "auth_status": "none",
                "coverage_status": "excluded",
                "tier": "secondary",
            },
            "disposition": "TRANSFER_PATIENT_RESPONSIBILITY",
            "observed_read_artifacts": ["remittance_eob", "plan_benefits"],
            "observed_note_slots": ["coverage", "explanation"],
        },
    ]

    ledger_path = tmp_path / "induced_egress.jsonl"
    induced_sop = induce_sop_from_shadow_traces(
        resolutions,
        spec_id="denial_triage_sop",
        ledger_path=ledger_path,
    )
    assert induced_sop.consistency_ok is True
    assert induced_sop.consistency_conflicts == ()
    assert len(induced_sop.branches) == 3

    # Verify non-discriminative 'tier' noise was dropped and every branch routes accurately
    for branch in induced_sop.branches:
        assert "tier" not in branch.when

    d_mod = evaluate_policy_decision(
        induced_sop.decision_table,
        {"denial_category": "missing_modifier", "auth_status": "valid", "coverage_status": "active", "tier": "custom"},
        "RESUBMIT_WITH_MODIFIER",
    )
    assert d_mod.allowed is True

    d_wo = evaluate_policy_decision(
        induced_sop.decision_table,
        {"denial_category": "expired_authorization", "auth_status": "expired", "coverage_status": "active"},
        "WRITE_OFF",
    )
    assert d_wo.allowed is True

    d_tr = evaluate_policy_decision(
        induced_sop.decision_table,
        {"denial_category": "coverage_exclusion", "auth_status": "none", "coverage_status": "excluded"},
        "TRANSFER_PATIENT_RESPONSIBILITY",
    )
    assert d_tr.allowed is True

    # Unseen category must route to human PREPARE rather than guessing
    d_unseen = evaluate_policy_decision(
        induced_sop.decision_table,
        {"denial_category": "rate_mismatch", "auth_status": "valid", "coverage_status": "active"},
        "WRITE_OFF",
    )
    assert d_unseen.allowed is False
    assert d_unseen.requires_prepare is True

    # Render Plain-English Manager Sign-Off Summary + SHA-256 SOP Hash
    sample_mutants = (
        AdversarialMutantSpec(
            mutant_id="m_unread_eob",
            description="Unread remittance EOB tab",
            dossier={"denial_category": "missing_modifier", "auth_status": "valid"},
            expected_disposition="RESUBMIT_WITH_MODIFIER",
            expected_blocked=True,
            unread_artifacts=("remittance_eob",),
        ),
        AdversarialMutantSpec(
            mutant_id="m_unseen_rate",
            description="Unseen contract rate mismatch",
            dossier={"denial_category": "rate_mismatch"},
            expected_blocked=True,
        ),
    )
    summary = render_sop_signoff_summary(induced_sop, sample_mutants=sample_mutants)
    summary_repeat = render_sop_signoff_summary(induced_sop, sample_mutants=sample_mutants)
    assert len(summary["sop_hash"]) == 64
    assert summary["sop_hash"] == summary_repeat["sop_hash"]
    assert summary["consistency_ok"] is True
    assert len(summary["rules"]) == 3
    assert len(summary["boundary_examples"]) == 2

    md_table = summary["markdown_table"]
    assert "| Rule | If Conditions | Then Disposition | Must Read First | Required Note Fields |" in md_table
    assert "Resubmit with modifier" in md_table
    assert "Write off" in md_table
    assert "Transfer patient responsibility" in md_table
    assert "Remittance eob" in md_table

    # Zero Python/Z3 syntax leaked to clinic managers
    for forbidden_syntax in ("==", "!=", ">=", "<=", "{", "}", "z3.", "SOPBranchSpec", "lambda"):
        assert forbidden_syntax not in md_table
        for row in summary["rules"]:
            assert forbidden_syntax not in row["if_conditions"]

    # Also verify optional coding_agent_fn induction path
    def mock_inducer_agent(_prompt: str) -> str:
        return json.dumps(
            {
                "branches": [
                    {
                        "branch_id": "agent_b1",
                        "when": {"auth_status": "expired"},
                        "disposition": "WRITE_OFF",
                        "required_read_artifacts": ["remittance_eob"],
                        "required_note_slots": ["reason"],
                        "priority": 1,
                    }
                ]
            }
        )

    agent_sop = induce_sop_from_shadow_traces(
        resolutions[:4],
        spec_id="agent_induced_sop",
        coding_agent_fn=mock_inducer_agent,
        ledger_path=tmp_path / "agent_induce.jsonl",
    )
    assert agent_sop.consistency_ok is True
    assert agent_sop.branches[0].branch_id == "agent_b1"
