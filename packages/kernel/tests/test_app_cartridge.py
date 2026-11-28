"""Tests for Pillar B Coding-Agent AppCartridge Compiler and Offline Mutant Qualification Gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_kernel.app_cartridge import (
    AdversarialMutantSpec,
    AppCartridge,
    SOPBranchSpec,
    check_sop_consistency,
    compile_sop_handbook_to_rules,
    qualify_app_cartridge_offline,
    synthesize_app_cartridge_with_coding_agent,
)
from openadapt_kernel.egress_guard import EgressBlockedError
from openadapt_kernel.stateful_twin import (
    TwinPageSpec,
    TwinRouteSpec,
    synthesize_zero_phi_stateful_twin,
)


def test_compile_sop_handbook_to_rules_and_symbolic_conflict_detection(tmp_path: Path) -> None:
    """compile_sop_handbook_to_rules audits PHI, compiles branches, and catches conflicting dispositions."""
    with pytest.raises(EgressBlockedError):
        compile_sop_handbook_to_rules(
            "IF patient=Maria_Gonzalez THEN disposition=APPROVE",
            ledger_path=tmp_path / "phi_sop.jsonl",
        )

    clean_sop = (
        "IF status=active AND tier=primary THEN disposition=APPROVE READ coverage,policy NOTES code,amount PRIORITY 2\n"
        "IF status=suspended THEN disposition=DENY READ policy NOTES reason PRIORITY 1\n"
    )
    compiled = compile_sop_handbook_to_rules(clean_sop, spec_id="billing_sop", ledger_path=tmp_path / "clean_sop.jsonl")
    assert compiled.consistency_ok is True
    assert compiled.consistency_conflicts == ()
    assert len(compiled.branches) == 2
    assert compiled.branches[0].disposition == "APPROVE"
    assert compiled.branches[0].required_read_artifacts == ("coverage", "policy")
    assert compiled.branches[0].required_note_slots == ("code", "amount")

    # Conflicting same-priority branches (overlapping equality + overlapping numeric intervals)
    conflicting_branches = [
        SOPBranchSpec("b1", {"status": "active", "amount": ">=100"}, "APPROVE", priority=0),
        SOPBranchSpec("b2", {"status": "active", "amount": "<=200"}, "DENY", priority=0),
    ]
    ok, conflicts = check_sop_consistency(conflicting_branches)
    assert ok is False
    assert len(conflicts) == 1
    assert "conflict:b1(APPROVE)_vs_b2(DENY)" in conflicts[0]

    # Disjoint numeric intervals are consistent
    disjoint_branches = [
        SOPBranchSpec("b1", {"amount": "<100"}, "APPROVE", priority=0),
        SOPBranchSpec("b2", {"amount": ">=100"}, "ESCALATE", priority=0),
    ]
    ok_disjoint, conflicts_disjoint = check_sop_consistency(disjoint_branches)
    assert ok_disjoint is True
    assert conflicts_disjoint == ()

    # Conflicting compiled SOP blocks cartridge qualification
    conflict_sop_text = (
        "IF status=active THEN disposition=APPROVE PRIORITY 1\n"
        "IF status=active AND tier=primary THEN disposition=DENY PRIORITY 1\n"
    )
    bad_sop = compile_sop_handbook_to_rules(conflict_sop_text, ledger_path=tmp_path / "conflict_sop.jsonl")
    assert bad_sop.consistency_ok is False
    bad_cartridge = AppCartridge(
        cartridge_id="conflict_cart",
        portal_origin="https://portal.example",
        sop_spec=bad_sop,
        skills=(),
        api_oracles=(),
        mutants=(AdversarialMutantSpec("m1", "active primary", {"status": "active", "tier": "primary"}, "APPROVE"),),
        egress_audit={},
    )
    rep = qualify_app_cartridge_offline(bad_cartridge)
    assert rep.consistency_ok is False
    assert rep.qualified is False


def test_synthesize_app_cartridge_egress_phi_block_ast_rejection_and_har_oracles(tmp_path: Path) -> None:
    """synthesize_app_cartridge_with_coding_agent enforces egress PHI gate, AST safety, and read-only HAR oracles."""
    called = False

    def fail_if_called(_prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    with pytest.raises(EgressBlockedError):
        synthesize_app_cartridge_with_coding_agent(
            cartridge_id="phi_cart",
            portal_origin="https://portal.example",
            sanitized_portal_spec={"page": "Maria Gonzalez 123-45-6789"},
            sop_text="IF status=active THEN disposition=APPROVE",
            coding_agent_fn=fail_if_called,
            ledger_path=tmp_path / "phi_cart.jsonl",
        )
    assert called is False

    # Mutating POST oracle is hard-rejected
    def post_oracle_agent(_prompt: str) -> str:
        return json.dumps(
            {
                "sop_branches": [{"branch_id": "b1", "when": {"status": "active"}, "disposition": "APPROVE"}],
                "skills": [],
                "api_oracles": [{"oracle_id": "bad", "method": "POST", "path_pattern": "/api/records", "read_only": False}],
                "mutants": [],
            }
        )

    with pytest.raises(ValueError, match="api_oracle_must_be_read_only"):
        synthesize_app_cartridge_with_coding_agent(
            cartridge_id="bad_oracle",
            portal_origin="https://portal.example",
            sanitized_portal_spec={"page": "form"},
            sop_text="IF status=active THEN disposition=APPROVE",
            coding_agent_fn=post_oracle_agent,
            ledger_path=tmp_path / "bad_oracle.jsonl",
        )

    # Unsafe skill AST is rejected by strict_ast=True AND by qualify_app_cartridge_offline
    def unsafe_skill_agent(_prompt: str) -> str:
        return json.dumps(
            {
                "sop_branches": [{"branch_id": "b1", "when": {"status": "active"}, "disposition": "APPROVE"}],
                "skills": [
                    {
                        "skill_id": "s_bad",
                        "name": "exfil",
                        "description": "unsafe",
                        "parameters": ["$title"],
                        "code": "import os\ndef repair(ui, vars):\n    ui.click(0)\n",
                    }
                ],
                "api_oracles": [],
                "mutants": [
                    {"mutant_id": "m1", "description": "ok", "dossier": {"status": "active"}, "expected_disposition": "APPROVE"}
                ],
            }
        )

    with pytest.raises(ValueError, match="unsafe_skill_ast"):
        synthesize_app_cartridge_with_coding_agent(
            cartridge_id="unsafe_strict",
            portal_origin="https://portal.example",
            sanitized_portal_spec={"page": "form"},
            sop_text="IF status=active THEN disposition=APPROVE",
            coding_agent_fn=unsafe_skill_agent,
            ledger_path=tmp_path / "unsafe_strict.jsonl",
            strict_ast=True,
        )

    cart_unsafe = synthesize_app_cartridge_with_coding_agent(
        cartridge_id="unsafe_soft",
        portal_origin="https://portal.example",
        sanitized_portal_spec={"page": "form"},
        sop_text="IF status=active THEN disposition=APPROVE",
        coding_agent_fn=unsafe_skill_agent,
        har_schemas=[
            {"method": "GET", "path_pattern": "/api/records", "response_fields": ["status", "code"]},
            {"method": "POST", "path_pattern": "/api/submit", "response_fields": ["status"]},
        ],
        ledger_path=tmp_path / "unsafe_soft.jsonl",
        strict_ast=False,
    )
    assert len(cart_unsafe.api_oracles) == 1
    assert cart_unsafe.api_oracles[0].method == "GET"
    assert cart_unsafe.api_oracles[0].read_only is True
    assert cart_unsafe.egress_audit["sha256_chain_integrity_verified"] is True

    rep_unsafe = qualify_app_cartridge_offline(cart_unsafe)
    assert rep_unsafe.qualified is False
    assert any("forbidden_syntax:Import" in v for v in rep_unsafe.ast_violations)


def test_qualify_app_cartridge_offline_multi_domain_mutants_and_twin(tmp_path: Path) -> None:
    """qualify_app_cartridge_offline passes 100% of mutants across billing/claims, CRM routing, and support SLA."""
    domains = [
        (
            "billing_claims_triage",
            "IF status=active AND tier=primary THEN disposition=APPROVE READ coverage,policy NOTES code\n"
            "IF status=suspended THEN disposition=DENY READ policy NOTES reason\n",
            [
                {"mutant_id": "m_approve", "description": "active primary", "dossier": {"status": "active", "tier": "primary"}, "expected_disposition": "APPROVE"},
                {"mutant_id": "m_unread_block", "description": "unread coverage blocks approve", "dossier": {"status": "active", "tier": "primary"}, "expected_disposition": "APPROVE", "expected_blocked": True, "unread_artifacts": ["coverage"]},
                {"mutant_id": "m_deny", "description": "suspended denied", "dossier": {"status": "suspended"}, "expected_disposition": "DENY"},
                {"mutant_id": "m_unmatched_block", "description": "unknown status blocks", "dossier": {"status": "pending"}, "expected_disposition": "APPROVE", "expected_blocked": True},
            ],
        ),
        (
            "crm_approval_routing",
            "IF tier=primary AND status=valid THEN disposition=APPROVE READ account NOTES summary\n"
            "IF tier=standard THEN disposition=REVIEW READ account NOTES summary\n",
            [
                {"mutant_id": "crm_ok", "description": "primary valid approved", "dossier": {"tier": "primary", "status": "valid"}, "expected_disposition": "APPROVE"},
                {"mutant_id": "crm_review", "description": "standard routed to review", "dossier": {"tier": "standard"}, "expected_disposition": "REVIEW"},
                {"mutant_id": "crm_unread", "description": "unread account blocks", "dossier": {"tier": "standard"}, "expected_disposition": "REVIEW", "expected_blocked": True, "unread_artifacts": ["account"]},
            ],
        ),
        (
            "support_sla_escalation",
            "IF priority=high THEN disposition=ESCALATE READ history NOTES summary\n"
            "IF priority=low THEN disposition=APPROVE READ history NOTES summary\n",
            [
                {"mutant_id": "sla_high", "description": "high priority escalates", "dossier": {"priority": "high"}, "expected_disposition": "ESCALATE"},
                {"mutant_id": "sla_low", "description": "low priority approves", "dossier": {"priority": "low"}, "expected_disposition": "APPROVE"},
                {"mutant_id": "sla_wrong_disp_block", "description": "high priority approve blocked", "dossier": {"priority": "high"}, "expected_disposition": "APPROVE", "expected_blocked": True},
            ],
        ),
    ]

    twin_app = synthesize_zero_phi_stateful_twin(
        [TwinPageSpec(url_path="/form", sanitized_html='<form method="post" action="/submit"><input type="text" name="title" /><button type="submit">Save</button></form>')],
        api_routes=[TwinRouteSpec(method="POST", path_pattern="/submit", mutates_collection="records")],
        ledger_path=tmp_path / "twin_egress.jsonl",
    )

    for domain_id, sop_text, mutant_dicts in domains:
        def _agent(_prompt: str, _muts: list = mutant_dicts) -> str:
            return json.dumps(
                {
                    "sop_branches": [],
                    "skills": [
                        {
                            "skill_id": "submit_skill",
                            "name": "submit_form",
                            "description": "fill title and click save",
                            "parameters": ["$title"],
                            "code": "def repair(ui, vars):\n    ui.fill(0, vars[\"$title\"])\n    ui.click(1)\n",
                            "expected_url_pattern": "/form",
                        }
                    ],
                    "api_oracles": [
                        {"oracle_id": "read_records", "method": "GET", "path_pattern": "/api/records", "expected_fields": ["status"], "read_only": True}
                    ],
                    "mutants": _muts,
                }
            )

        cart = synthesize_app_cartridge_with_coding_agent(
            cartridge_id=domain_id,
            portal_origin="https://portal.example",
            sanitized_portal_spec={"page": "form"},
            sop_text=sop_text,
            coding_agent_fn=_agent,
            ledger_path=tmp_path / f"{domain_id}_egress.jsonl",
            strict_ast=True,
        )
        rep = qualify_app_cartridge_offline(cart, twin_app=twin_app)
        assert rep.qualified is True, f"{domain_id} failed qualification: {rep}"
        assert rep.passed_mutants == len(mutant_dicts)
        assert rep.total_mutants == len(mutant_dicts)
        assert rep.failed_mutant_ids == ()
        assert rep.ast_violations == ()
        assert rep.consistency_ok is True
