"""Unit tests for DirtyBufferMonitor (LTL_f) and SsaIdentifierGuard (SSA)."""

from __future__ import annotations

from openadapt_flow.wrap import DirtyBufferMonitor, SsaIdentifierGuard
from openadapt_flow.wrap.dirty_and_provenance import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    extract_opaque_identifiers,
)

# --- DirtyBufferMonitor (LTL_f No-Dirty-Exit Automaton) tests -------------------


def test_dirty_buffer_blocks_done_until_all_tabs_submitted_or_discarded() -> None:
    monitor = DirtyBufferMonitor()

    # Initially clean
    initial = monitor.check_exit()
    assert initial.decision == DECISION_ALLOW
    assert initial.code == "all_buffers_clean"
    assert initial.dirty_by_surface == ()

    # Typing on two different tabs marks both dirty
    monitor.observe_event({"surface": "tab_a", "kind": "type", "field_name": "notes"})
    monitor.observe_event(
        {"surface": "tab_b", "kind": "fill", "field_name": "member_id"}
    )
    monitor.observe_event(
        {"surface": "tab_b", "kind": "select", "field_name": "request_type"}
    )

    blocked = monitor.check_exit()
    assert blocked.decision == DECISION_BLOCK
    assert blocked.code == "dirty_unsubmitted_fields"
    assert blocked.dirty_by_surface == (
        ("tab_a", ("notes",)),
        ("tab_b", ("member_id", "request_type")),
    )

    # Submitting tab_b clears tab_b, but tab_a is still dirty -> DONE still blocked
    monitor.observe_event({"surface": "tab_b", "kind": "submit"})
    still_blocked = monitor.check_exit()
    assert still_blocked.decision == DECISION_BLOCK
    assert still_blocked.dirty_by_surface == (("tab_a", ("notes",)),)

    # Saving or discarding tab_a clears the final dirty field -> DONE allowed
    monitor.observe_event({"surface": "tab_a", "kind": "save"})
    allowed = monitor.check_exit()
    assert allowed.decision == DECISION_ALLOW
    assert allowed.code == "all_buffers_clean"
    assert allowed.dirty_by_surface == ()


def test_dirty_buffer_partial_field_clear_and_discard() -> None:
    monitor = DirtyBufferMonitor()
    monitor.mark_dirty("editor", "title")
    monitor.mark_dirty("editor", "body")

    monitor.clear_surface("editor", fields=["title"])
    mid = monitor.check_exit()
    assert mid.decision == DECISION_BLOCK
    assert mid.dirty_by_surface == (("editor", ("body",)),)

    monitor.observe_event({"surface": "editor", "kind": "discard"})
    assert monitor.check_exit().decision == DECISION_ALLOW


# --- SsaIdentifierGuard (SSA Opaque-ID Provenance Conservation) tests -----------


def test_extract_opaque_identifiers_canonicalizes_dates_phones_codes_and_ids() -> None:
    text = (
        "Record MRN12345678 DOB 03/15/1965 NPI 1234567890 CPT 67028 J2778 "
        "ICD H35.32 Fax (408) 555-0199 Auth PA-2025-88421"
    )
    extracted = extract_opaque_identifiers(text)
    canonicals = {c for c, _raw, _kind in extracted}
    assert "ID:MRN12345678" in canonicals
    assert "DATE:1965-03-15" in canonicals
    assert "NUM:1234567890" in canonicals
    assert "NUM:67028" in canonicals
    assert "ID:J2778" in canonicals
    assert "CODE:H35.32" in canonicals
    assert "PHONE:4085550199" in canonicals
    assert "ID:PA-2025-88421" in canonicals


def test_ssa_guard_allows_conserved_identifiers_across_formats() -> None:
    guard = SsaIdentifierGuard()
    guard.record_source_entities(
        "source_tab",
        {
            "record_id": "MRN12345678",
            "birth_date": "1965-03-15",
            "provider_npi": "1234567890",
            "diagnosis_codes": ["H35.32", "H35.31"],
            "procedure_codes": ["67028", "J2778"],
            "fax": "408-555-0199",
        },
        subject_key="subj_A",
    )
    guard.record_trusted_prompt("Use tracking code TRK-2026-0042 for submission.")

    decision = guard.check_before_submit(
        "dest_tab",
        {
            "record_id": "MRN12345678",
            "birth_date": "03/15/1965",  # US format matches ISO source
            "provider_npi": "1234567890",
            "diagnosis_codes": ["H35.32", "H35.31"],
            "procedure_codes": ["67028", "J2778"],
            "fax": "(408) 555-0199",  # Formatted phone matches hyphenated source
            "tracking_code": "TRK-2026-0042",  # Sourced from trusted user prompt
        },
        active_subject_key="subj_A",
    )
    assert decision.decision == DECISION_ALLOW
    assert decision.code == "provenance_conserved"
    assert decision.violations == ()
    assert decision.checked_tokens == 9


def test_ssa_guard_blocks_unsourced_hallucinated_ids() -> None:
    guard = SsaIdentifierGuard()
    guard.record_source_entities(
        "source_tab",
        {"record_id": "MRN12345678", "procedure_code": "67028"},
        subject_key="subj_A",
    )

    decision = guard.check_before_submit(
        "dest_tab",
        {
            "record_id": "MRN12345678",
            "procedure_code": "99214",  # Hallucinated CPT not observed anywhere
            "auth_number": "PA-2025-99999",  # Hallucinated auth ID
        },
        active_subject_key="subj_A",
    )
    assert decision.decision == DECISION_BLOCK
    assert decision.code == "unsourced_identifier"
    bad_canonicals = {v.canonical for v in decision.violations}
    assert bad_canonicals == {"NUM:99214", "ID:PA-2025-99999"}


def test_ssa_guard_blocks_cross_subject_tab_bleed() -> None:
    guard = SsaIdentifierGuard()
    guard.record_source_entities(
        "source_tab_1",
        {"record_id": "MRN11111111", "birth_date": "1965-03-15"},
        subject_key="subj_A",
    )
    guard.record_source_entities(
        "source_tab_2",
        {"record_id": "MRN22222222", "birth_date": "1958-06-20"},
        subject_key="subj_B",
    )

    # 1. Explicit active_subject_key mismatch
    bleed_explicit = guard.check_before_submit(
        "dest_tab",
        {"record_id": "MRN22222222", "birth_date": "03/15/1965"},
        active_subject_key="subj_A",
    )
    assert bleed_explicit.decision == DECISION_BLOCK
    assert bleed_explicit.code == "cross_subject_bleed"
    assert {v.canonical for v in bleed_explicit.violations} == {"ID:MRN22222222"}

    # 2. Implicit multi-subject mix (even without active_subject_key)
    bleed_implicit = guard.check_before_submit(
        "dest_tab",
        {"record_id": "MRN11111111", "birth_date": "06/20/1958"},
    )
    assert bleed_implicit.decision == DECISION_BLOCK
    assert bleed_implicit.code == "cross_subject_bleed"
    assert len(bleed_implicit.violations) == 2
