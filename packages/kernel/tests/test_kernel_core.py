"""Unit tests for ObligationLedger, CommitInterceptor, ShadowObserver, offline mutants, and receipts."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_kernel import (
    CommitInterceptor,
    MicroHandoffSession,
    NetworkEvent,
    ObligationLedger,
    ReadBackProof,
    ShadowObserver,
    ShadowTrace,
    generate_ephemeral_ed25519_keypair,
    mint_receipt,
    qualify_verifier_offline,
    sanitize_action_string,
    verify_receipt,
)


def test_default_deny_on_step_limit_and_timeout() -> None:
    ledger = ObligationLedger("ep-hab-1", subject_key="P-100")
    ledger.add_obligation("ob-1", "Submit prior authorization", target_system="payer")

    for trigger in ("step_limit", "timeout", "error", "manual_abort"):
        decision = ledger.finalize(trigger=trigger)  # type: ignore[arg-type]
        assert decision.outcome == "ESCALATED"
        assert ledger.status == "ESCALATED"
        assert trigger in decision.reason


def test_default_deny_requires_readback_and_clean_dirty_buffer() -> None:
    ledger = ObligationLedger("ep-hab-2", subject_key="P-100")
    ledger.add_obligation("ob-1", "Submit prior auth", target_system="payer")

    # Cannot finalize while obligation is pending
    d1 = ledger.finalize(trigger="done")
    assert d1.outcome == "ESCALATED"
    assert d1.pending_obligations == ["ob-1"]

    # Mark committed with independent read-back proof
    ledger.mark_committed(
        "ob-1",
        ReadBackProof(
            channel="cross_tab",
            url_or_locator="https://portal.example/submitted",
            cited_substrings=["PA-9921", "AET123456789"],
            external_ref="PA-9921",
        ),
    )

    # Dirty form field still blocks VERIFIED
    d2 = ledger.finalize(trigger="done", dirty_fields=["triage-note-input"])
    assert d2.outcome == "ESCALATED"
    assert d2.dirty_fields == ["triage-note-input"]

    # Clean dirty buffer + valid proof -> VERIFIED
    d3 = ledger.finalize(trigger="done", dirty_fields=[])
    assert d3.outcome == "VERIFIED"
    assert d3.committed_refs == {"ob-1": "PA-9921"}


def test_capability_ledger_preserves_consent_constraints_and_ssa_provenance() -> None:
    ledger = ObligationLedger("ep-seam-1", subject_key="PATIENT-A")
    ledger.record_consent("update_stock", "Yes, add 5 units", turn=1)
    ledger.record_constraint("allergy", "Patient allergic to penicillin", source_window="tab-2", turn=4)
    ledger.record_ssa_literal("member_id", "AET123456789", source_window="tab-1", turn=2, subject_key="PATIENT-A")
    ledger.record_ssa_literal("member_id", "AET999999999", source_window="tab-3", turn=3, subject_key="PATIENT-B")

    assert ledger.has_consent("update_stock")
    ctx = ledger.render_auditor_context()
    assert len(ctx["MANDATORY_CONSTRAINTS"]) == 1
    assert len(ctx["VERIFIED_CAPABILITIES"]["consents"]) == 1

    # Valid SSA literal for PATIENT-A passes
    ok = ledger.verify_ssa_provenance({"member_id": "AET123456789"}, opaque_fields=["member_id"])
    assert ok == []

    # Hallucinated ID blocked
    hallucinated = ledger.verify_ssa_provenance({"member_id": "AET000000000"}, opaque_fields=["member_id"])
    assert len(hallucinated) == 1
    assert hallucinated[0].reason == "unsourced_opaque_identifier"

    # Cross-patient bleed blocked
    bleed = ledger.verify_ssa_provenance({"member_id": "AET999999999"}, opaque_fields=["member_id"])
    assert len(bleed) == 1
    assert "cross_subject_bleed" in bleed[0].reason


def test_saga_checkpoint_skips_already_committed_portal(tmp_path: Path) -> None:
    saga_path = tmp_path / "saga.json"
    ledger = ObligationLedger("ep-saga-1", subject_key="P-200")
    ledger.add_obligation("portal_a", "Submit payer auth", target_system="payer")
    ledger.add_obligation("portal_b", "Write auth note", target_system="ehr")

    ledger.mark_committed(
        "portal_a",
        ReadBackProof(
            channel="api_read",
            url_or_locator="/api/auths/PA-42",
            cited_substrings=["PA-42"],
            external_ref="PA-42",
        ),
    )
    ledger.save_saga(saga_path)

    resumed = ObligationLedger.load_saga(saga_path)
    assert resumed.is_committed("portal_a")
    assert not resumed.is_committed("portal_b")
    assert resumed.pending_ids() == ["portal_b"]


def test_interceptor_ignores_nav_links_and_search_boxes_but_catches_http_post() -> None:
    ic = CommitInterceptor()

    # Search box never dirties form buffer
    assert ic.mark_field_edited("1385", "Winter Jacket", aria_role="searchbox") is False
    assert ic.mark_field_edited("filter-input", "2026", label="Search products") is False
    assert ic.dirty_fields == {}

    # Clicking nav link whose ID contains "submit" with no POST is read_only_nav
    ic.begin_step()
    ic.record_network_event(NetworkEvent(method="GET", url="https://portal.example/payer/dashboard", status=200))
    c_nav = ic.classify_step(element_tag="a", element_role="link")
    assert c_nav.is_commit is False
    assert c_nav.kind == "read_only_nav"

    # Unlabeled icon click that sends POST IS a commit
    ic.begin_step()
    ic.record_network_event(NetworkEvent(method="POST", url="https://portal.example/api/orders/save", status=201))
    c_post = ic.classify_step(element_tag="button", element_role="button")
    assert c_post.is_commit is True
    assert c_post.kind == "http_mutation"

    # Escalation payload escapes nested quotes safely
    escaped = sanitize_action_string("[OPENADAPT_ESCALATE]", "DIRTY_BUFFER: ['1385'] not saved")
    assert "\\'" not in escaped
    assert escaped.startswith('"') and escaped.endswith('"')


def test_shadow_induction_4_mutant_qualification_and_micro_handoff() -> None:
    traces = [
        ShadowTrace(
            trace_id="t1",
            portal_origin="https://carrier.example",
            source_dossier={"policy_id": "POL-101", "zip": "94107", "roof_year": "2018"},
            submitted_fields={"policy_number": "POL-101", "postal_code": "94107", "state_code": "CA"},
            readback_url="https://carrier.example/quotes/summary?id=1",
            readback_text="Quote Q-101 confirmed for POL-101 ZIP 94107",
            confirmation_ref="Q-101",
        ),
        ShadowTrace(
            trace_id="t2",
            portal_origin="https://carrier.example",
            source_dossier={"policy_id": "POL-202", "zip": "90210", "roof_year": "2020"},
            submitted_fields={"policy_number": "POL-202", "postal_code": "90210", "state_code": "CA"},
            readback_url="https://carrier.example/quotes/summary?id=2",
            readback_text="Quote Q-202 confirmed for POL-202 ZIP 90210",
            confirmation_ref="Q-202",
        ),
    ]
    pack = ShadowObserver.induce_portal_pack("carrier-home-v1", traces)
    assert pack.static_defaults == {"state_code": "CA"}
    assert set(pack.variable_fields) == {"policy_number", "postal_code"}
    assert pack.provenance_map == {"policy_number": "policy_id", "postal_code": "zip"}

    # Tautological toast verifier MUST fail 4-mutant qualification
    def bad_toast_verifier(fields: dict[str, str], text: str) -> bool:
        return "saved successfully" in text.lower() or all(v in text for v in fields.values())

    bad_report = qualify_verifier_offline(
        expected_fields={"policy_number": "POL-101", "postal_code": "94107"},
        pre_write_text="Draft form for new quote",
        post_write_text="Quote Q-101 confirmed for POL-101 ZIP 94107",
        verifier_fn=bad_toast_verifier,
    )
    assert bad_report.qualified is False
    assert bad_report.toast_only_refuted is False

    # True token-boundary field-checking verifier passes all 4 mutants
    import re

    def good_readback_verifier(fields: dict[str, str], text: str) -> bool:
        compact = " ".join(text.split())
        return all(bool(re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact)) for v in fields.values())

    good_report = qualify_verifier_offline(
        expected_fields={"policy_number": "POL-101", "postal_code": "94107"},
        pre_write_text="Draft form for new quote",
        post_write_text="Quote Q-101 confirmed for POL-101 ZIP 94107",
        verifier_fn=good_readback_verifier,
    )
    assert good_report.qualified is True

    # Micro-Handoff assimilates 1 human click so Run 2 is 100% autonomous
    steps = [
        {"step_id": "s1", "ok": True},
        {"step_id": "s2", "branch_key": "roof_dropdown", "ok": False, "prompt": "Select roof type"},
        {"step_id": "s3", "ok": True},
    ]
    sess1 = MicroHandoffSession(pack, steps)
    status1, req = sess1.run_until_blocked(lambda s: bool(s.get("ok")))
    assert status1 == "AWAITING_MICRO_HANDOFF"
    assert req is not None and req.step_id == "s2" and req.staged_count == 1
    sess1.resolve_micro_handoff({"ok": True, "selected": "Wood Shake"})
    status1_end, _ = sess1.run_until_blocked(lambda s: bool(s.get("ok")))
    assert status1_end == "COMPLETED"

    # Run 2 uses learned pack branch with 0 human stops
    sess2 = MicroHandoffSession(pack, steps)
    status2, req2 = sess2.run_until_blocked(lambda s: bool(s.get("ok")))
    assert status2 == "COMPLETED" and req2 is None


def test_ed25519_receipt_blocks_unverified_and_detects_tampering() -> None:
    priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
    ledger = ObligationLedger("ep-rec-1")
    ledger.add_obligation("ob-1", "Save order")

    with pytest.raises(ValueError):
        mint_receipt(ledger, "order-entry", priv_pem)

    ledger.mark_committed(
        "ob-1",
        ReadBackProof(channel="fresh_reload", url_or_locator="/orders", cited_substrings=["ORD-7"]),
    )
    assert ledger.finalize("done").outcome == "VERIFIED"

    receipt = mint_receipt(ledger, "order-entry", priv_pem)
    assert verify_receipt(receipt, pub_pem) is True

    # Tampering invalidates verification
    receipt["payload"]["outcome"] = "ESCALATED"
    assert verify_receipt(receipt, pub_pem) is False
