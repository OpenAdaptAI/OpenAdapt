"""Unit tests for openadapt_flow.wrap CheckpointSpec + Cross-Tab EntityLedger."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from openadapt_flow.wrap import (
    CheckpointEnforcer,
    CheckpointSpec,
    EntityLedger,
    compile_checkpoint_spec,
)
from openadapt_flow.wrap.checkpoint_spec import (
    FORBIDDEN_LAYOUT_KEYS,
    SPEC_SCHEMA,
    assert_layout_independent,
)
from openadapt_flow.wrap.entity_ledger import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_ESCALATE,
    OUTCOME_BLOCKED,
    OUTCOME_COMPLETE,
    OUTCOME_INCOMPLETE,
    evaluate_suite,
)

POLICY_RULES: list[dict[str, Any]] = [
    {
        "id": "policy_01_amount_cap",
        "surface": "portal_b",
        "commit_action": "portal_b:submit_request",
        "field_name": "requested_units",
        "operator": "lte",
        "threshold": 15.0,
        "on_violation": "escalate",
        "description": "Requests above 15.0 units require operator approval.",
    },
    {
        "id": "policy_02_request_type",
        "surface": "portal_b",
        "commit_action": "portal_b:submit_request",
        "field_name": "request_type",
        "operator": "in",
        "threshold": ["standard", "expedited"],
        "on_violation": "block",
        "description": "Request type must be an allowed category.",
    },
]


def _demo_trace() -> dict[str, Any]:
    """Generic two-surface demonstration trace with raw UI coordinates."""
    return {
        "meta": {
            "id": "demo-cross-tab-001",
            "workflow_id": "cross-tab:submit-and-close",
            "viewport": [1440, 900],
            "coordinate_space": "page",
        },
        "events": [
            {
                "i": 0,
                "kind": "click",
                "surface": "source_app",
                "section": "summary",
                "x": 312,
                "y": 198,
                "selector": "#row-CASE-2026-001",
                "read_entities": {
                    "case_id": "CASE-2026-001",
                    "subject_name": "Doe, John",
                    "notes_summary": "0.5 units x 2 x 12 months",
                },
            },
            {
                "i": 1,
                "kind": "click",
                "surface": "source_app",
                "section": "codes",
                "x": 420,
                "y": 140,
                "selector": "button[data-tab='codes']",
                "read_entities": {
                    "category_codes": ["A10.1", "A10.2", "B20.0"],
                },
            },
            {
                "i": 2,
                "kind": "click",
                "surface": "source_app",
                "section": "items",
                "x": 510,
                "y": 140,
                "selector": "button[data-tab='items']",
                "read_entities": {
                    "item_codes": ["10001", "20002"],
                },
            },
            {
                "i": 3,
                "kind": "click",
                "surface": "source_app",
                "section": "account",
                "x": 605,
                "y": 140,
                "selector": "button[data-tab='account']",
                "read_entities": {
                    "account_id": "ACC123456789",
                    "effective_date": "1965-03-15",
                },
            },
            {
                "i": 4,
                "kind": "download",
                "surface": "source_app",
                "section": "attachments",
                "x": 880,
                "y": 410,
                "selector": "button.download-attachment",
                "read_entities": {
                    "supporting_document": "Supporting_Letter.pdf",
                },
            },
            {
                "i": 5,
                "kind": "switch_surface",
                "surface": "portal_b",
                "section": "form",
            },
            {
                "i": 6,
                "kind": "select",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 260,
                "field_name": "request_type",
                "value": "standard",
            },
            {
                "i": 7,
                "kind": "type",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 320,
                "field_rect": [300, 305, 280, 32],
                "field_name": "account_id",
                "text": "ACC123456789",
            },
            {
                "i": 8,
                "kind": "type",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 380,
                "field_rect": [300, 365, 280, 32],
                "field_name": "effective_date",
                "text": "03/15/1965",
            },
            {
                "i": 9,
                "kind": "type",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 440,
                "field_name": "category_codes",
                "value": ["A10.1", "A10.2", "B20.0"],
            },
            {
                "i": 10,
                "kind": "type",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 500,
                "field_name": "item_codes",
                "value": ["10001", "20002"],
            },
            {
                "i": 11,
                "kind": "attach",
                "surface": "portal_b",
                "section": "form",
                "x": 440,
                "y": 560,
                "field_name": "attached_document",
                "value": "Supporting_Letter.pdf",
            },
            {
                "i": 12,
                "kind": "submit",
                "action_type": "irreversible_commit",
                "commit_action": "portal_b:submit_request",
                "surface": "portal_b",
                "section": "form",
                "x": 720,
                "y": 810,
                "payload": {
                    "requested_units": 12.0,
                },
                "response": {
                    "status": 201,
                    "confirmation_id": "CONF-2026-88421",
                },
            },
            {
                "i": 13,
                "kind": "switch_surface",
                "surface": "source_app",
                "section": "notes",
            },
            {
                "i": 14,
                "kind": "note",
                "surface": "source_app",
                "section": "notes",
                "x": 520,
                "y": 600,
                "field_name": "completion_note",
                "text": "Submitted request. Confirmation ID: CONF-2026-88421.",
            },
            {
                "i": 15,
                "kind": "submit",
                "action_type": "irreversible_commit",
                "commit_action": "source_app:close_case",
                "surface": "source_app",
                "section": "queue",
                "x": 910,
                "y": 190,
                "payload": {"case_id": "CASE-2026-001"},
                "response": {"status": 200, "closed": True},
            },
        ],
    }


def _base_events() -> list[dict[str, Any]]:
    return copy.deepcopy(_demo_trace()["events"])


@pytest.fixture(scope="module")
def compiled_spec() -> CheckpointSpec:
    return compile_checkpoint_spec(_demo_trace(), policy_rules=POLICY_RULES)


def test_compile_strips_layout_and_record_literals(
    compiled_spec: CheckpointSpec,
) -> None:
    assert compiled_spec.schema == SPEC_SCHEMA
    assert compiled_spec.surfaces == ("source_app", "portal_b")
    assert compiled_spec.commit_sequence == (
        "portal_b:submit_request",
        "source_app:close_case",
    )
    assert len(compiled_spec.required_source_reads) == 5
    assert len(compiled_spec.cross_tab_invariants) == 5
    assert len(compiled_spec.approval_rules) == 2
    assert len(compiled_spec.confirmation_checks) == 2

    raw_json = compiled_spec.to_json()
    parsed = json.loads(raw_json)
    assert_layout_independent(parsed)
    for forbidden in FORBIDDEN_LAYOUT_KEYS:
        assert f'"{forbidden}"' not in raw_json

    for literal in ("Doe, John", "ACC123456789", "1965-03-15", "CONF-2026-88421"):
        assert literal not in raw_json

    reloaded = CheckpointSpec.from_dict(parsed)
    assert reloaded == compiled_spec

    with pytest.raises(ValueError, match="Layout-dependent key"):
        assert_layout_independent({"nested": {"x": 100}})


def test_infers_date_normalization_and_confirmation_writeback(
    compiled_spec: CheckpointSpec,
) -> None:
    date_invs = [
        inv
        for inv in compiled_spec.cross_tab_invariants
        if inv.dest_field == "effective_date"
    ]
    assert len(date_invs) == 1
    assert date_invs[0].match_mode == "date_iso"

    first_confirm = [
        c
        for c in compiled_spec.confirmation_checks
        if c.commit_action == "portal_b:submit_request"
    ][0]
    assert first_confirm.required_response_fields == ("confirmation_id",)
    assert first_confirm.writeback_surface == "source_app"
    assert first_confirm.writeback_field == "completion_note"
    assert first_confirm.writeback_before_commit == "source_app:close_case"


def test_shifted_layout_and_different_record_complete_cleanly(
    compiled_spec: CheckpointSpec,
) -> None:
    # 1. Shifted coordinates, renamed selectors, extra scroll, ISO date format
    ev_shifted = _base_events()
    for ev in ev_shifted:
        if "x" in ev:
            ev["x"] += 410
            ev["y"] += 220
        if "selector" in ev:
            ev["selector"] = "div.new-layout > button"
        if ev.get("field_name") == "effective_date":
            ev["text"] = "1965-03-15"
    ev_shifted.insert(
        6,
        {
            "i": 99,
            "kind": "scroll",
            "surface": "portal_b",
            "section": "form",
            "dy": 500,
        },
    )
    report_shifted = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "clean_shifted", "events": ev_shifted}
    )
    assert report_shifted.outcome == OUTCOME_COMPLETE
    assert report_shifted.commits_blocked == ()

    # 2. Different record values using the same compiled spec
    ev_other = _base_events()
    ev_other[0]["read_entities"] = {
        "case_id": "CASE-2026-002",
        "subject_name": "Smith, Emily",
        "notes_summary": "1.0 unit monthly",
    }
    ev_other[1]["read_entities"] = {"category_codes": ["C30.5"]}
    ev_other[2]["read_entities"] = {"item_codes": ["30003"]}
    ev_other[3]["read_entities"] = {
        "account_id": "ACC987654321",
        "effective_date": "1958-06-20",
    }
    ev_other[4]["read_entities"] = {"supporting_document": "Case_002_Letter.pdf"}
    ev_other[7]["text"] = "ACC987654321"
    ev_other[8]["text"] = "06/20/1958"
    ev_other[9]["value"] = ["C30.5"]
    ev_other[10]["value"] = ["30003"]
    ev_other[11]["value"] = "Case_002_Letter.pdf"
    ev_other[12]["response"]["confirmation_id"] = "CONF-2026-99104"
    ev_other[14]["text"] = "Closed under CONF-2026-99104."
    ev_other[15]["payload"]["case_id"] = "CASE-2026-002"

    report_other = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "clean_other_record", "events": ev_other}
    )
    assert report_other.outcome == OUTCOME_COMPLETE
    assert report_other.commits_executed == compiled_spec.commit_sequence


def test_blocks_cross_tab_mismatch_and_skipped_reads(
    compiled_spec: CheckpointSpec,
) -> None:
    # Skipped source read
    ev_skip = [ev for ev in _base_events() if ev.get("section") != "summary"]
    r_skip = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_skip", "events": ev_skip}
    )
    assert r_skip.outcome == OUTCOME_BLOCKED
    assert "portal_b:submit_request" in r_skip.commits_blocked
    assert "skipped_source_read" in {v.code for v in r_skip.violations}

    # Wrong-record cross-tab paste
    ev_mismatch = _base_events()
    ev_mismatch[7]["text"] = "ACC999888777"
    r_mismatch = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_mismatch", "events": ev_mismatch}
    )
    assert r_mismatch.outcome == OUTCOME_BLOCKED
    assert "portal_b:submit_request" in r_mismatch.commits_blocked
    assert "cross_tab_entity_mismatch" in {v.code for v in r_mismatch.violations}


def test_policy_thresholds_and_stop_on_uncertainty(
    compiled_spec: CheckpointSpec,
) -> None:
    # Exceeded cap without approval -> blocked/escalated before commit
    ev_over = _base_events()
    ev_over[12]["payload"]["requested_units"] = 24.0
    r_over = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_over_cap", "events": ev_over}
    )
    assert "portal_b:submit_request" in r_over.commits_blocked
    assert "policy_violation" in {v.code for v in r_over.violations}

    # Exceeded cap WITH explicit human approval -> completes
    ev_approved = _base_events()
    ev_approved[12]["payload"]["requested_units"] = 24.0
    ev_approved[12]["human_approved"] = True
    r_approved = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "clean_approved", "events": ev_approved}
    )
    assert r_approved.outcome == OUTCOME_COMPLETE

    # Missing policy field -> stops on uncertainty
    ev_missing = _base_events()
    del ev_missing[12]["payload"]["requested_units"]
    r_missing = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_uncertain", "events": ev_missing}
    )
    assert "portal_b:submit_request" in r_missing.commits_blocked
    assert "uncertain_policy_field" in {v.code for v in r_missing.violations}


def test_confirmation_readback_and_writeback_enforcement(
    compiled_spec: CheckpointSpec,
) -> None:
    # Missing confirmation ID in writeback note blocks closing case
    ev_no_wb = _base_events()
    ev_no_wb[14]["text"] = "Submitted request. Done."
    r_no_wb = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_no_wb", "events": ev_no_wb}
    )
    assert r_no_wb.outcome == OUTCOME_BLOCKED
    assert "source_app:close_case" in r_no_wb.commits_blocked
    assert "confirmation_writeback_mismatch" in {v.code for v in r_no_wb.violations}

    # Upstream HTTP 500 on first commit escalates and blocks downstream commit
    ev_500 = _base_events()
    ev_500[12]["response"] = {"status": 500, "error": "timeout"}
    r_500 = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "fail_500", "events": ev_500}
    )
    assert r_500.outcome == OUTCOME_BLOCKED
    assert "source_app:close_case" in r_500.commits_blocked
    codes = {v.code for v in r_500.violations}
    assert "confirmation_status_failed" in codes
    assert "missing_prior_confirmation" in codes


def test_live_entity_ledger_hooks_and_suite_metrics(
    compiled_spec: CheckpointSpec,
) -> None:
    # Live step-by-step check_before_commit / check_after_commit API
    ledger = EntityLedger()
    enforcer = CheckpointEnforcer(compiled_spec, ledger=ledger)
    events = _base_events()
    for ev in events[:12]:
        enforcer.observe_event(ev)

    gate_ok = enforcer.check_before_commit(
        step_index=12,
        surface="portal_b",
        commit_action="portal_b:submit_request",
        payload={"requested_units": 12.0},
    )
    assert gate_ok.decision == DECISION_ALLOW

    gate_over = enforcer.check_before_commit(
        step_index=12,
        surface="portal_b",
        commit_action="portal_b:submit_request",
        payload={"requested_units": 99.0},
    )
    assert gate_over.decision == DECISION_ESCALATE

    gate_bad_type = enforcer.check_before_commit(
        step_index=12,
        surface="portal_b",
        commit_action="portal_b:submit_request",
        payload={"requested_units": 12.0, "request_type": "unknown"},
    )
    assert gate_bad_type.decision == DECISION_BLOCK

    post_viols = enforcer.check_after_commit(
        step_index=12,
        surface="portal_b",
        commit_action="portal_b:submit_request",
        response={"status": 201, "confirmation_id": "CONF-2026-88421"},
    )
    assert post_viols == ()
    assert ledger.get_confirmation("portal_b:submit_request") == {
        "confirmation_id": "CONF-2026-88421"
    }

    # Incomplete trajectory (stopped before any commits)
    r_inc = CheckpointEnforcer(compiled_spec).enforce_trajectory(
        {"id": "incomplete", "events": events[:5]}
    )
    assert r_inc.outcome == OUTCOME_INCOMPLETE

    # Suite metrics
    ev_bad = _base_events()
    ev_bad[7]["text"] = "ACC000000000"
    metrics = evaluate_suite(
        compiled_spec,
        [
            {"id": "clean_1", "is_clean": True, "events": _base_events()},
            {"id": "bad_1", "is_clean": False, "events": ev_bad},
        ],
    )
    assert metrics.silent_error_rate == 0.0
    assert metrics.auto_completion_rate == 1.0
    assert metrics.escalation_precision == 1.0
