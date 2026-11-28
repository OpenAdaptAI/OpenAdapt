"""Unit tests for deterministic typed canonicalizers, semantic slot checks, PREPARE queue, and 4-mutant promotion."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

import pytest

from openadapt_kernel import (
    ControlSchema,
    DossierVariableSchema,
    ObligationLedger,
    PrepareCard,
    PrepareResolution,
    ReadBackFieldSpec,
    ReadBackLens,
    RubberStampViolationError,
    UnresolvedSlot,
    build_prepare_card,
    canonical_equal,
    execute_prepare_resolution,
    generate_ephemeral_ed25519_keypair,
    publish_signed_pack,
    pull_verified_pack,
    qualify_and_promote_pack,
    reconcile_pending_submission,
    scan_pack_for_pii,
    verify_semantic_slot_bindings,
    verify_warm_pack_putget,
)


def _make_ctrl(idx: int, label: str, selector: str) -> ControlSchema:
    return ControlSchema(
        tag="input",
        input_type="text",
        role="textbox",
        label=label,
        placeholder="",
        name="",
        element_id="",
        testid="",
        in_modal=False,
        selector=selector,
        control_index=idx,
    )


def test_canonical_date_formats_and_strict_rejections() -> None:
    assert canonical_equal("1981-04-12", "04/12/1981", "date")
    assert canonical_equal("1981-04-12", "4/12/1981", "date")
    assert canonical_equal("1981-04-12", "04-12-1981", "date")
    assert canonical_equal("1981-04-12", "Apr 12, 1981", "date")
    assert canonical_equal("1981-04-12", "April 12, 1981 (45y)", "date")
    assert canonical_equal("1981-04-12", "Apr. 12, 1981 (Age: 45 yrs)", "date")
    assert canonical_equal("1981-04-12", "1981/04/12", "date")

    # Swapped day/month, transposed digits, invalid calendar dates, and blanks must fail
    assert not canonical_equal("1981-04-12", "12/04/1981", "date")
    assert not canonical_equal("1981-04-12", "1981-04-21", "date")
    assert not canonical_equal("1981-04-12", "21/04/1981", "date")
    assert not canonical_equal("2025-02-28", "02/29/2025", "date")
    assert not canonical_equal("1981-04-12", "", "date")


def test_canonical_currency_cents_and_credit_signs() -> None:
    assert canonical_equal("2450", "$2,450.00", "currency")
    assert canonical_equal("2450.0", "USD 2,450.00", "currency")
    assert canonical_equal("2450.50", "$2,450.50", "currency")
    assert canonical_equal("-100.00", "($100.00)", "currency")
    assert canonical_equal("-$100.00", "(100.00)", "currency")
    assert canonical_equal("$(100.00)", "-100", "currency")

    # 10x drop, sign flip, malformed comma grouping, and extra decimals must fail
    assert not canonical_equal("2450", "$245.00", "currency")
    assert not canonical_equal("2450", "$2,45.00", "currency")
    assert not canonical_equal("-100.00", "$100.00", "currency")
    assert not canonical_equal("100.00", "($100.00)", "currency")
    assert not canonical_equal("2450", "2450.005", "currency")


def test_canonical_phone_e164_and_digit_length_typos() -> None:
    assert canonical_equal("4155550199", "(415) 555-0199", "phone")
    assert canonical_equal("4155550199", "+1-415-555-0199", "phone")
    assert canonical_equal("4155550199", "+1 (415) 555-0199", "phone")
    assert canonical_equal("4155550199", "1-415-555-0199", "phone")
    assert canonical_equal("+44 20 7946 0958", "+442079460958", "phone")

    # 9-digit and 11-digit typos and extensions must fail
    assert not canonical_equal("4155550199", "415555019", "phone")
    assert not canonical_equal("4155550199", "41555501999", "phone")
    assert not canonical_equal("4155550199", "(415) 555-01999", "phone")
    assert not canonical_equal("4155550199", "141-555-01999", "phone")
    assert not canonical_equal("4155550199", "+1-415-555-01999", "phone")
    assert not canonical_equal("4155550199", "(415) 555-0199 ext 4", "phone")


def test_canonical_masked_id_strict_type_and_suffix_guard() -> None:
    assert canonical_equal("123-45-6789", "XXX-XX-6789", "masked_id")
    assert canonical_equal("123-45-6789", "***6789", "masked_id")
    assert canonical_equal("123-45-6789", "***-**-6789", "masked_id")
    assert canonical_equal("123-45-6789", "\u2022\u2022\u2022\u20226789", "masked_id")
    assert canonical_equal("123-45-6789", "123456789", "masked_id")

    # Must NEVER match masked output under other field types
    assert not canonical_equal("123-45-6789", "XXX-XX-6789", "exact")
    assert not canonical_equal("123-45-6789", "XXX-XX-6789", "code_prefix")
    assert not canonical_equal("123-45-6789", "XXX-XX-6789", "casefold_ws")

    # Suffix shorter than min_mask_suffix, wrong suffix, unmasked wrong prefix, or bare suffix must fail
    assert not canonical_equal("123-45-6789", "XXX-XX-X789", "masked_id", min_mask_suffix=4)
    assert not canonical_equal("123-45-6789", "XXX-XX-6780", "masked_id")
    assert not canonical_equal("123-45-6789", "999-99-6789", "masked_id")
    assert not canonical_equal("123-45-6789", "6789", "masked_id")


def test_canonical_code_prefix_and_casefold_ws() -> None:
    assert canonical_equal("M54.50", "M54.50 - Low back pain", "code_prefix")
    assert canonical_equal("m54.50", "M54.50: Low back pain", "code_prefix")
    assert canonical_equal("M54.50", "M54.50 (Low back pain)", "code_prefix")
    assert canonical_equal("99213-25", "99213-25 - Separately identifiable E/M", "code_prefix")
    assert canonical_equal("pol-10042", "POL - 10042", "code_prefix")

    # Subcode truncation, 1-char code swaps, and stripped CPT modifiers must fail
    assert not canonical_equal("M54.50", "M54.51", "code_prefix")
    assert not canonical_equal("M54.50", "M54.51 - Low back pain", "code_prefix")
    assert not canonical_equal("M54.50", "M54.5", "code_prefix")
    assert not canonical_equal("M54.50", "M54.5 - Low back pain", "code_prefix")
    assert not canonical_equal("M54.5", "M54.50 - Low back pain", "code_prefix")
    assert not canonical_equal("99213-25", "99213 - Office visit", "code_prefix")

    assert canonical_equal("Outpatient", "  outpatient  ", "casefold_ws")
    assert not canonical_equal("Outpatient", "Inpatient", "casefold_ws")
    assert not canonical_equal("", "", "exact")
    assert not canonical_equal("  ", "  ", "casefold_ws")


def test_verify_semantic_slot_bindings_catches_swaps_and_ambiguous_labels() -> None:
    schemas = [
        DossierVariableSchema("$subscriber_dob", "date", {"subscriber", "dob"}),
        DossierVariableSchema("$patient_dob", "date", {"patient", "dob"}),
        DossierVariableSchema("$service_date", "date", {"service", "date"}),
        DossierVariableSchema("$billed_amount", "currency", {"billed", "amount"}),
    ]
    controls = {
        0: "Subscriber DOB",
        1: "Patient Date of Birth",
        2: "Date of Service",
        3: "Total Charge",
    }

    clean = verify_semantic_slot_bindings(
        {0: "$subscriber_dob", 1: "$patient_dob", 2: "$service_date", 3: "$billed_amount"},
        schemas,
        controls,
    )
    assert clean.ok
    assert clean.ambiguous_slots == ()

    # Swapped Subscriber DOB and Patient DOB must flag both slots before commit
    swapped = verify_semantic_slot_bindings(
        {0: "$patient_dob", 1: "$subscriber_dob", 2: "$service_date", 3: "$billed_amount"},
        schemas,
        controls,
    )
    assert not swapped.ok
    assert swapped.ambiguous_slots == (0, 1)
    by_idx = {v.control_idx: v for v in swapped.verdicts}
    assert "conflicting_role_tokens:subscriber" in by_idx[0].reason
    assert "conflicting_role_tokens:patient" in by_idx[1].reason
    assert by_idx[0].candidate_vars == ("$patient_dob", "$service_date", "$subscriber_dob")

    # Terse label lacking disambiguating role token when multiple date vars exist
    terse = verify_semantic_slot_bindings(
        {0: "$subscriber_dob", 1: "$patient_dob"},
        schemas,
        {0: "Subscriber DOB", 1: "Birth Date"},
    )
    assert not terse.ok
    assert terse.ambiguous_slots == (1,)
    assert terse.verdicts[1].reason == "missing_disambiguating_role_token"


def test_prepare_anti_rubber_stamping_and_post_human_verification() -> None:
    schemas = [
        DossierVariableSchema("$subscriber_dob", "date", {"subscriber", "dob"}),
        DossierVariableSchema("$patient_dob", "date", {"patient", "dob"}),
        DossierVariableSchema("$billed_amount", "currency", {"billed", "amount"}),
    ]
    controls = [
        _make_ctrl(0, "Subscriber DOB", "#sub-dob"),
        _make_ctrl(1, "DOB", "#pat-dob"),
        _make_ctrl(2, "Billed Amount", "#amount"),
    ]
    # Proposed bindings: slot 1 label is terse "DOB", plus slot 3 requires an enum choice
    card = build_prepare_card(
        card_id="card-001",
        portal_origin="https://portal.example.test",
        proposed_bindings={0: "$subscriber_dob", 1: "$patient_dob", 2: "$billed_amount"},
        variable_schemas=schemas,
        controls=controls,
        unbound_enum_slots=[
            UnresolvedSlot(
                control_idx=3,
                control_label="Care Setting",
                reason="unmapped_enum_control",
                allowed_enum_options=("Outpatient", "Inpatient"),
                expected_field_type="casefold_ws",
            )
        ],
    )
    card.control_labels[3] = "Care Setting"
    card.control_selectors[3] = "#care-setting"
    assert [u.control_idx for u in card.unresolved_slots] == [1, 3]
    assert 1 not in card.staged_bindings

    # 1. Anti-rubber-stamping: approve_all=True MUST fail whenever unresolved_slots is non-empty
    with pytest.raises(RubberStampViolationError, match="approve_all=True is forbidden"):
        card.validate_resolution(
            PrepareResolution(
                card_id="card-001",
                operator_id="op-1",
                slot_bindings={1: "$patient_dob", 3: "Outpatient"},
                approve_all=True,
            ),
            schemas,
        )

    # 2. Out-of-domain free-text human typo in PrepareResolution MUST fail
    with pytest.raises(RubberStampViolationError, match="not in allowed"):
        card.validate_resolution(
            PrepareResolution(
                card_id="card-001",
                operator_id="op-1",
                slot_bindings={1: "$patient_dob", 3: "Outpatinet"},
            ),
            schemas,
        )

    # 3. Post-human verification Part A: operator swaps slot 0 ("Subscriber DOB") -> "$patient_dob"
    wrong_role_res = execute_prepare_resolution(
        card=card,
        resolution=PrepareResolution(
            card_id="card-001",
            operator_id="op-1",
            slot_bindings={0: "$patient_dob", 1: "$subscriber_dob", 3: "Outpatient"},
        ),
        variable_schemas=schemas,
        dossier_values={
            "$subscriber_dob": "1955-08-19",
            "$patient_dob": "1981-04-12",
            "$billed_amount": "2450",
        },
        observed_readback={},
    )
    assert wrong_role_res.outcome == "REFUSED_PRE_COMMIT"
    assert "conflicting_role_tokens:subscriber" in wrong_role_res.reason

    # 4. Post-human verification Part B: valid slot pick, but read-back shows $245.00 instead of 2450
    ledger_bad = ObligationLedger("ep-bad")
    bad_putget_res = execute_prepare_resolution(
        card=card,
        resolution=PrepareResolution(
            card_id="card-001",
            operator_id="op-1",
            slot_bindings={1: "$patient_dob", 3: "Outpatient"},
        ),
        variable_schemas=schemas,
        dossier_values={
            "$subscriber_dob": "1955-08-19",
            "$patient_dob": "1981-04-12",
            "$billed_amount": "2450",
        },
        observed_readback={
            "Subscriber DOB": "08/19/1955",
            "DOB": "04/12/1981 (45y)",
            "Billed Amount": "$245.00",
            "Care Setting": "Outpatient",
        },
        ledger=ledger_bad,
    )
    assert bad_putget_res.outcome == "ESCALATED"
    assert "Billed Amount" in bad_putget_res.mismatched_readback_keys
    assert ledger_bad.status == "ESCALATED"


def test_async_clearinghouse_pending_reconciliation_and_duplicate_guard() -> None:
    schemas = [
        DossierVariableSchema("$member_ssn", "masked_id", {"member", "ssn"}),
        DossierVariableSchema("$icd_code", "code_prefix", {"primary", "icd"}),
    ]
    card = PrepareCard(
        card_id="card-async-1",
        portal_origin="https://clearinghouse.example.test",
        staged_bindings={0: "$member_ssn", 1: "$icd_code"},
        control_labels={0: "Member SSN", 1: "Primary ICD"},
        control_selectors={0: "#ssn", 1: "#icd"},
    )
    lens = ReadBackLens(
        readback_url_pattern="https://clearinghouse.example.test/claims",
        fields=(
            ReadBackFieldSpec("#ssn", "Member SSN", "masked_id"),
            ReadBackFieldSpec("#icd", "Primary ICD", "code_prefix"),
        ),
        receipt_ref_pattern=r"Tracking #:\s*(TRK-[0-9]+)",
    )
    ledger = ObligationLedger("ep-async-1")
    submitted_registry: Dict[str, str] = {}

    # Immediate response has tracking number while table rows have not indexed yet
    pending_res = execute_prepare_resolution(
        card=card,
        resolution=PrepareResolution(card_id="card-async-1", operator_id="op-1"),
        variable_schemas=schemas,
        dossier_values={"$member_ssn": "123-45-6789", "$icd_code": "M54.50"},
        observed_readback={},
        lens=lens,
        receipt_text="Batch accepted. Tracking #: TRK-884920. Table indexing in progress.",
        ledger=ledger,
        submitted_registry=submitted_registry,
    )
    assert pending_res.outcome == "SUBMITTED_PENDING_RECONCILIATION"
    assert pending_res.receipt_ref == "TRK-884920"
    assert pending_res.pending_reconciliation is not None
    assert ledger.pending_ids() == ["reconcile:card-async-1"]

    # Retry before reconciliation MUST NOT double-submit
    dup_res = execute_prepare_resolution(
        card=card,
        resolution=PrepareResolution(card_id="card-async-1", operator_id="op-1"),
        variable_schemas=schemas,
        dossier_values={"$member_ssn": "123-45-6789", "$icd_code": "M54.50"},
        observed_readback={},
        lens=lens,
        submitted_registry=submitted_registry,
    )
    assert dup_res.outcome == "REFUSED_PRE_COMMIT"
    assert "duplicate_submission_blocked:TRK-884920" in dup_res.reason

    # Once table indexing finishes, reconciliation verifies masked SSN and ICD description
    reconciled = reconcile_pending_submission(
        pending_res.pending_reconciliation,
        {"Member SSN": "XXX-XX-6789", "Primary ICD": "M54.50 - Low back pain"},
        ledger=ledger,
    )
    assert reconciled.outcome == "VERIFIED"
    assert ledger.status == "VERIFIED"

    # Generic toast with NO tracking ID on empty table MUST escalate, never pending-reconcile
    toast_only = execute_prepare_resolution(
        card=card,
        resolution=PrepareResolution(card_id="card-async-1", operator_id="op-1"),
        variable_schemas=schemas,
        dossier_values={"$member_ssn": "123-45-6789", "$icd_code": "M54.50"},
        observed_readback={},
        lens=lens,
        receipt_text="Saved successfully!",
    )
    assert toast_only.outcome == "ESCALATED"


def test_four_mutant_qualification_and_cold_to_warm_pack_promotion(tmp_path: Path) -> None:
    schemas = [
        DossierVariableSchema("$subscriber_dob", "date", {"subscriber", "dob"}),
        DossierVariableSchema("$patient_dob", "date", {"patient", "dob"}),
        DossierVariableSchema("$billed_amount", "currency", {"billed", "amount"}),
        DossierVariableSchema("$callback_phone", "phone", {"callback", "phone"}),
        DossierVariableSchema("$member_ssn", "masked_id", {"member", "ssn"}),
        DossierVariableSchema("$icd_code", "code_prefix", {"primary", "icd"}),
    ]
    controls = [
        _make_ctrl(0, "Subscriber DOB", "#sub-dob"),
        _make_ctrl(1, "DOB", "#pat-dob"),
        _make_ctrl(2, "Billed Amount", "#billed-amt"),
        _make_ctrl(3, "Callback Phone", "#cb-phone"),
        _make_ctrl(4, "Member SSN", "#mbr-ssn"),
        _make_ctrl(5, "Primary ICD", "#icd-code"),
    ]
    card = build_prepare_card(
        card_id="card-promote-1",
        portal_origin="https://claims.example.test",
        proposed_bindings={
            0: "$subscriber_dob",
            1: "$patient_dob",
            2: "$billed_amount",
            3: "$callback_phone",
            4: "$member_ssn",
            5: "$icd_code",
        },
        variable_schemas=schemas,
        controls=controls,
    )
    resolution = PrepareResolution(
        card_id="card-promote-1",
        operator_id="op-1",
        slot_bindings={1: "$patient_dob"},
    )
    dossier_1 = {
        "$subscriber_dob": "1955-08-19",
        "$patient_dob": "1981-04-12",
        "$billed_amount": "2450",
        "$callback_phone": "4155550199",
        "$member_ssn": "123-45-6789",
        "$icd_code": "M54.50",
    }
    observed_1 = {
        "Subscriber DOB": "08/19/1955",
        "DOB": "Apr 12, 1981 (45y)",
        "Billed Amount": "$2,450.00",
        "Callback Phone": "(415) 555-0199",
        "Member SSN": "XXX-XX-6789",
        "Primary ICD": "M54.50 - Low back pain",
    }

    # Weak Verifier A: raw exact string diff fails baseline / format variation
    def raw_exact_verifier(exp: Mapping[str, str], obs: Mapping[str, str]) -> bool:
        return all(obs.get(k, "") == v for k, v in exp.items())

    rep_raw = qualify_and_promote_pack(
        pack_id="claims-v1",
        card=card,
        resolution=resolution,
        variable_schemas=schemas,
        dossier_values=dossier_1,
        observed_readback=observed_1,
        putget_verifier=raw_exact_verifier,
    )
    assert not rep_raw.qualified
    assert rep_raw.pack is None

    # Weak Verifier B: unkeyed bag-of-values check fails swapped_same_type_var
    def unkeyed_bag_verifier(exp: Mapping[str, str], obs: Mapping[str, str]) -> bool:
        obs_vals = [v for v in obs.values() if v.strip()]
        if len(obs_vals) < len(exp):
            return False
        for k, w in exp.items():
            matched_any = any(
                canonical_equal(w, ov, "date")
                or canonical_equal(w, ov, "currency")
                or canonical_equal(w, ov, "phone")
                or canonical_equal(w, ov, "masked_id")
                or canonical_equal(w, ov, "code_prefix")
                for ov in obs_vals
            )
            if not matched_any:
                return False
        return True

    rep_bag = qualify_and_promote_pack(
        pack_id="claims-v1",
        card=card,
        resolution=resolution,
        variable_schemas=schemas,
        dossier_values=dossier_1,
        observed_readback=observed_1,
        putget_verifier=unkeyed_bag_verifier,
    )
    assert not rep_bag.qualified
    assert not rep_bag.swapped_same_type_var_refuted
    assert rep_bag.pack is None

    # Weak Verifier C: loose digit-prefix verifier accepts $245.00 for 2450 -> fails Mutant 2 & Mutant 4
    def loose_digit_prefix_verifier(exp: Mapping[str, str], obs: Mapping[str, str]) -> bool:
        for k, w in exp.items():
            ov = obs.get(k, "").strip()
            if not ov:
                return False
            if k == "Billed Amount":
                # Buggy prefix check: "245" is a prefix of "2450"
                od = "".join(c for c in ov if c.isdigit())[:3]
                wd = "".join(c for c in w if c.isdigit())[:3]
                if not od or od != wd:
                    return False
            else:
                if not (
                    canonical_equal(w, ov, "date")
                    or canonical_equal(w, ov, "phone")
                    or canonical_equal(w, ov, "masked_id")
                    or canonical_equal(w, ov, "code_prefix")
                ):
                    return False
        return True

    rep_loose = qualify_and_promote_pack(
        pack_id="claims-v1",
        card=card,
        resolution=resolution,
        variable_schemas=schemas,
        dossier_values=dossier_1,
        observed_readback=observed_1,
        putget_verifier=loose_digit_prefix_verifier,
    )
    assert not rep_loose.qualified
    assert not rep_loose.mutated_amount_or_date_refuted
    assert not rep_loose.format_variation_accepted
    assert rep_loose.pack is None

    # Default Typed Canonical PutGet lens passes all 4 mutants and promotes zero-PII PortalPack
    rep_ok = qualify_and_promote_pack(
        pack_id="claims-v1",
        card=card,
        resolution=resolution,
        variable_schemas=schemas,
        dossier_values=dossier_1,
        observed_readback=observed_1,
    )
    assert rep_ok.qualified
    assert rep_ok.dropped_write_refuted
    assert rep_ok.mutated_amount_or_date_refuted
    assert rep_ok.swapped_same_type_var_refuted
    assert rep_ok.format_variation_accepted
    assert rep_ok.pack is not None
    assert scan_pack_for_pii(rep_ok.pack) == []

    # Sign, publish, pull, and run warm pack on Patient #2 with zero PREPARE handoffs
    priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
    pack_path = publish_signed_pack(rep_ok.pack, priv_pem, tmp_path)
    warm_pack = pull_verified_pack(pack_path, pub_pem)

    dossier_2 = {
        "$subscriber_dob": "1962-11-04",
        "$patient_dob": "1994-07-23",
        "$billed_amount": "1875.5",
        "$callback_phone": "2125550144",
        "$member_ssn": "987-65-4321",
        "$icd_code": "J01.90",
    }
    observed_2 = {
        "Subscriber DOB": "11/04/1962",
        "DOB": "07/23/1994 (32y)",
        "Billed Amount": "$1,875.50",
        "Callback Phone": "+1-212-555-0144",
        "Member SSN": "***4321",
        "Primary ICD": "J01.90 - Acute sinusitis, unspecified",
    }
    ok_warm, mismatches = verify_warm_pack_putget(warm_pack, dossier_2, observed_2)
    assert ok_warm, mismatches

    # Swapping Subscriber DOB and Patient DOB on Patient #2 is immediately caught by warm pack
    observed_2_swapped = {
        **observed_2,
        "Subscriber DOB": "07/23/1994",
        "DOB": "11/04/1962",
    }
    ok_swapped, swap_mismatches = verify_warm_pack_putget(warm_pack, dossier_2, observed_2_swapped)
    assert not ok_swapped
    assert "#sub-dob" in swap_mismatches and "#pat-dob" in swap_mismatches
