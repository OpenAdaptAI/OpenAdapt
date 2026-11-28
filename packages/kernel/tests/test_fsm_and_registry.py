"""Tests for PortalFSM static safety verification, zero-PII PortalPack registry, and HTML audit card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_kernel import (
    FSMState,
    FSMTransition,
    ObligationLedger,
    PortalFSM,
    PortalPack,
    ReadBackProof,
    ShadowTrace,
    generate_ephemeral_ed25519_keypair,
    mint_receipt,
    publish_signed_pack,
    pull_verified_pack,
    render_audit_card_html,
    scan_pack_for_pii,
    verify_fsm_safety,
)


def test_fsm_static_safety_proves_safe_and_blocks_double_commit_consent_and_taint() -> None:
    trace = ShadowTrace(
        trace_id="t1",
        portal_origin="https://portal.example",
        source_dossier={"mrn": "MRN-1001"},
        submitted_fields={"mrn": "MRN-1001", "state": "TX"},
        readback_url="https://portal.example/records",
        readback_text="MRN-1001 TX",
    )
    fsm = PortalFSM.compile_from_traces("claims-fsm", [trace], required_consent_scope="submit_claim")

    # Missing consent -> rejected statically before any click
    ledger = ObligationLedger(episode_id="ep-1", subject_key="patient-A")
    ledger.record_ssa_literal("mrn", "MRN-1001", source_window="ehr", turn=0)
    proof_no_consent = verify_fsm_safety(
        fsm,
        ledger=ledger,
        planned_bindings={"mrn": "MRN-1001"},
        opaque_fields=["mrn"],
        active_subject_key="patient-A",
        literal_subjects={"mrn": "patient-A"},
    )
    assert not proof_no_consent.safe
    assert any(v.property_name == "consent_dominance" for v in proof_no_consent.violations)

    # Grant consent -> safe
    ledger.record_consent("submit_claim", "User approved claim submission", turn=0)
    proof_ok = verify_fsm_safety(
        fsm,
        ledger=ledger,
        planned_bindings={"mrn": "MRN-1001"},
        opaque_fields=["mrn"],
        active_subject_key="patient-A",
        literal_subjects={"mrn": "patient-A"},
    )
    assert proof_ok.safe

    # Cross-patient taint -> rejected statically
    proof_taint = verify_fsm_safety(
        fsm,
        ledger=ledger,
        planned_bindings={"mrn": "MRN-1001"},
        opaque_fields=["mrn"],
        active_subject_key="patient-A",
        literal_subjects={"mrn": "patient-B"},
    )
    assert not proof_taint.safe
    assert any(v.property_name == "subject_isolation" for v in proof_taint.violations)

    # Double-commit cycle without idempotency key -> rejected statically
    cyclic = PortalFSM(fsm_id="cyclic-fsm", initial_state="S0")
    cyclic.add_state(FSMState("S0", "https://portal.example", "/form"))
    cyclic.add_state(FSMState("S1", "https://portal.example", "/done"))
    cyclic.add_transition(FSMTransition("submit", "S0", "S1", "submit", "POST", is_commit=True))
    cyclic.add_transition(FSMTransition("retry", "S1", "S0", "back", "GET", is_commit=False))
    proof_double = verify_fsm_safety(cyclic)
    assert not proof_double.safe
    assert any(v.property_name == "no_double_commit" for v in proof_double.violations)


def test_zero_pii_signed_pack_registry_and_offline_audit_card(tmp_path: Path) -> None:
    priv, pub = generate_ephemeral_ed25519_keypair()

    dirty_pack = PortalPack(
        pack_id="dirty-v1",
        portal_origin="https://portal.example",
        variable_fields=["policy_id"],
        static_defaults={"contact": "alice@example.com"},
        provenance_map={"policy_id": "policy_id"},
        readback_url_pattern="https://portal.example/list",
    )
    assert "email" in scan_pack_for_pii(dirty_pack)
    with pytest.raises(ValueError, match="PII"):
        publish_signed_pack(dirty_pack, priv, tmp_path / "reg")

    clean_pack = PortalPack(
        pack_id="clean-v2",
        portal_origin="https://portal.example",
        variable_fields=["policy_id"],
        static_defaults={"state": "TX"},
        provenance_map={"policy_id": "policy_id"},
        readback_url_pattern="https://portal.example/list",
        stage="AUTOPILOT",
        shadow_passes=5,
    )
    published = publish_signed_pack(clean_pack, priv, tmp_path / "reg")
    pulled = pull_verified_pack(published, pub)
    assert pulled.pack_id == "clean-v2"
    assert pulled.static_defaults == {"state": "TX"}

    # Tampered pack must fail digest/signature verification
    tampered = json.loads(published.read_text(encoding="utf-8"))
    tampered["pack"]["static_defaults"]["state"] = "CA"
    published.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        pull_verified_pack(published, pub)

    # Offline HTML audit card
    ledger = ObligationLedger(episode_id="ep-audit-1")
    ledger.add_obligation("ob1", "Submit quote")
    ledger.mark_committed(
        "ob1",
        ReadBackProof(
            channel="fresh_reload",
            url_or_locator="https://portal.example/list",
            cited_substrings=["POL-42"],
        ),
    )
    assert ledger.finalize("done").outcome == "VERIFIED"
    receipt = mint_receipt(ledger, "clean-v2", priv)
    html_ok = render_audit_card_html(receipt, pub)
    assert "SIGNATURE VERIFIED" in html_ok
    assert "POL-42" not in html_ok  # zero-PHI redacted by default
    import hashlib
    assert hashlib.sha256(b"POL-42").hexdigest() in html_ok

    receipt["payload"]["pack_id"] = "tampered-pack"
    html_bad = render_audit_card_html(receipt, pub)
    assert "SIGNATURE INVALID" in html_bad
