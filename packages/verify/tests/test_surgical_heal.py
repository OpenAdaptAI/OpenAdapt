"""Tests for 3-layer surgical selector healing and signed candidate patch generation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_verify.surgical_heal import (
    CandidatePatchedProgram,
    ElementTarget,
    FleetPatchError,
    FleetSelectorPatch,
    PatchScope,
    PinnedPatchKey,
    SurgicalHealCandidate,
    SurgicalSelectorHealer,
    apply_fleet_patch,
    apply_fleet_patch_to_packs,
    compute_program_sha256,
    load_pinned_patch_keys,
    write_pinned_patch_keys,
)


def _sample_steps() -> list[dict[str, str]]:
    return [
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_applicant_name",
            "selector": "input#applicant-name",
        },
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_effective_date",
            "selector": "input.nw-eff-date-v1",
        },
    ]


def _sample_scope(
    steps: list[dict[str, str]] | None = None,
    *,
    tenant_id: str = "tenant_alpha",
    program_id: str = "prog_quote_v1",
    app_origin: str = "https://portal.example.com",
    workflow_id: str = "wf_policy_quote_submit",
) -> PatchScope:
    source_steps = steps if steps is not None else _sample_steps()
    return PatchScope(
        tenant_id=tenant_id,
        program_id=program_id,
        program_sha256=compute_program_sha256(source_steps),
        app_origin=app_origin,
        workflow_id=workflow_id,
    )


def _drifted_quote_target() -> ElementTarget:
    return ElementTarget(
        workflow_id="wf_policy_quote_submit",
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        aria_role="textbox",
        accessible_name="Policy Effective Date",
        form_section="Coverage Dates",
        input_type="date",
    )


def _drifted_portal_candidates() -> list[SurgicalHealCandidate]:
    return [
        SurgicalHealCandidate(
            selector="input[data-testid='coverage-effective-date-v2']",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input[data-testid='coverage-expiration-date-v2']",
            aria_role="textbox",
            accessible_name="Policy Expiration Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input[data-testid='applicant-dob-v2']",
            aria_role="textbox",
            accessible_name="Date of Birth",
            form_section="Applicant Information",
            input_type="date",
        ),
    ]


def test_identity_anchored_heal_emits_signed_candidate_verified_by_pinned_trust_store(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()
    trust_store = write_pinned_patch_keys(
        tmp_path / "pinned_keys.json",
        [
            PinnedPatchKey(
                key_id="key-primary-2026",
                public_key=signing_key.public_key(),
                allowed_tenants=frozenset({"tenant_alpha"}),
            )
        ],
    )
    pinned_keys = load_pinned_patch_keys(trust_store)

    steps = _sample_steps()
    scope = _sample_scope(steps)
    target = _drifted_quote_target()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    oracle_calls: list[str] = []

    def readback_oracle(cand: SurgicalHealCandidate) -> tuple[bool, str]:
        oracle_calls.append(cand.selector)
        return True, "a" * 64

    result = healer.heal_and_verify(
        target,
        candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-primary-2026",
        pre_action_identity_check=lambda _t, _c: True,
        readback_oracle=readback_oracle,
    )

    assert result.verdict == "HEALED"
    assert result.readback_verified is True
    assert result.selected is not None
    assert (
        result.selected.candidate.selector
        == "input[data-testid='coverage-effective-date-v2']"
    )
    assert result.selected.score == 1.0
    assert oracle_calls == ["input[data-testid='coverage-effective-date-v2']"]
    assert result.patch is not None
    assert result.patch.verify_signature(pinned_keys) is True

    roundtripped = FleetSelectorPatch.from_json(result.patch.to_json())
    assert roundtripped == result.patch
    assert roundtripped.verify_signature(pinned_keys) is True

    candidate_prog = apply_fleet_patch(
        steps,
        roundtripped,
        pinned_keys_path=trust_store,
        scope=scope,
    )
    assert isinstance(candidate_prog, CandidatePatchedProgram)
    assert candidate_prog.status == "PENDING_QUALIFICATION"
    assert candidate_prog.requires_re_qualification is True
    assert candidate_prog.requires_admission_approval is True
    assert (
        candidate_prog.candidate_program_sha256 != candidate_prog.source_program_sha256
    )
    # Original steps are never mutated in place.
    assert steps[1]["selector"] == "input.nw-eff-date-v1"
    assert (
        candidate_prog.steps[1]["selector"]
        == "input[data-testid='coverage-effective-date-v2']"
    )
    assert (
        candidate_prog.steps[1]["healed_by_patch_sha256"] == roundtripped.patch_sha256
    )


def test_hard_anchors_section_name_only_and_hidden_ambiguity_halt() -> None:
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    # 1. Renamed label ("Patient last name" vs "Patient first name" / "Surname") halts.
    target = ElementTarget(
        workflow_id="wf_intake",
        step_id="step_last_name",
        old_selector="#last_name",
        aria_role="textbox",
        accessible_name="Patient last name",
        form_section="Demographics",
        input_type="text",
    )
    drifted = [
        SurgicalHealCandidate(
            selector="#fname",
            aria_role="textbox",
            accessible_name="Patient first name",
            form_section="Demographics",
            input_type="text",
        ),
        SurgicalHealCandidate(
            selector="#surname",
            aria_role="textbox",
            accessible_name="Surname",
            form_section="Demographics",
            input_type="text",
        ),
    ]
    res_renamed = healer.rank_and_select(target, drifted)
    assert res_renamed.verdict == "ABSTAIN_HALT"
    assert "identity anchor" in res_renamed.reason

    # 2. Lone candidate with same label in the WRONG form_section (correct one hidden) halts!
    addr_target = ElementTarget(
        workflow_id="wf_order",
        step_id="step_billing_addr",
        old_selector="#bill_addr",
        aria_role="textbox",
        accessible_name="Street Address",
        form_section="Billing Address",
        input_type="text",
    )
    wrong_section_only = [
        SurgicalHealCandidate(
            selector="#bill_addr_v2",
            aria_role="textbox",
            accessible_name="Street Address",
            form_section="Billing Address",
            input_type="text",
            visible=False,
        ),
        SurgicalHealCandidate(
            selector="#ship_addr",
            aria_role="textbox",
            accessible_name="Street Address",
            form_section="Shipping Address",
            input_type="text",
            visible=True,
        ),
    ]
    res_section = healer.rank_and_select(addr_target, wrong_section_only)
    assert res_section.verdict == "ABSTAIN_HALT"
    assert "form_section" in res_section.reason

    # 3. Hidden/disabled same-identity candidate counts toward ambiguity and halts!
    hidden_same = [
        SurgicalHealCandidate(
            selector="#eff-visible",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
            visible=True,
        ),
        SurgicalHealCandidate(
            selector="#eff-hidden",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
            visible=False,
        ),
    ]
    res_hidden = healer.rank_and_select(_drifted_quote_target(), hidden_same)
    assert res_hidden.verdict == "ABSTAIN_HALT"
    assert "ambiguous" in res_hidden.reason

    # 4. Name-only target (missing aria_role / input_type / form_section) halts!
    name_only_target = ElementTarget(
        workflow_id="wf_admin",
        step_id="step_del",
        old_selector="#del-row",
        accessible_name="Delete",
    )
    res_name_only = healer.rank_and_select(
        name_only_target,
        [
            SurgicalHealCandidate(
                selector="#del-all",
                aria_role="button",
                accessible_name="Delete",
                form_section="Admin",
            )
        ],
    )
    assert res_name_only.verdict == "ABSTAIN_HALT"
    assert "secondary identity anchor" in res_name_only.reason


def test_abstain_halt_on_ambiguous_tie_candidates() -> None:
    signing_key = Ed25519PrivateKey.generate()
    target = _drifted_quote_target()
    scope = _sample_scope()
    tied_candidates = [
        SurgicalHealCandidate(
            selector="input#eff-date-primary",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input#eff-date-secondary",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
    ]
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)
    oracle_called = False

    def readback_oracle(_cand: SurgicalHealCandidate) -> bool:
        nonlocal oracle_called
        oracle_called = True
        return True

    result = healer.heal_and_verify(
        target,
        tied_candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-1",
        pre_action_identity_check=lambda _t, _c: True,
        readback_oracle=readback_oracle,
    )

    assert result.verdict == "ABSTAIN_HALT"
    assert "ambiguous" in result.reason
    assert result.margin == 0.0
    assert result.selected is None
    assert result.patch is None
    assert oracle_called is False


def test_pre_action_check_and_non_bool_oracle_verdicts_withhold_patch() -> None:
    signing_key = Ed25519PrivateKey.generate()
    target = _drifted_quote_target()
    scope = _sample_scope()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    oracle_called = False

    def oracle(_cand: SurgicalHealCandidate) -> bool:
        nonlocal oracle_called
        oracle_called = True
        return True

    pre_fail = healer.heal_and_verify(
        target,
        candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-1",
        pre_action_identity_check=lambda _t, _c: False,
        readback_oracle=oracle,
    )
    assert pre_fail.verdict == "ABSTAIN_HALT"
    assert "pre-action identity check" in pre_fail.reason
    assert pre_fail.patch is None
    assert oracle_called is False

    # Truthy non-bool oracle results ("false", dicts, ints) must NEVER sign a patch!
    for bad_verdict in (
        ("false", ""),
        {"error": "timeout"},
        "false",
        1,
        False,
    ):
        res = healer.heal_and_verify(
            target,
            candidates,
            scope=scope,
            signing_key=signing_key,
            signer_key_id="key-1",
            pre_action_identity_check=lambda _t, _c: True,
            readback_oracle=lambda _cand, v=bad_verdict: v,  # type: ignore[arg-type, return-value]
        )
        assert res.verdict == "ABSTAIN_HALT", (
            f"expected ABSTAIN_HALT for {bad_verdict!r}"
        )
        assert res.readback_verified is False
        assert res.patch is None


def test_pinned_trust_store_tenant_key_binding_and_tamper_guards(
    tmp_path: Path,
) -> None:
    key_a = Ed25519PrivateKey.generate()
    key_b = Ed25519PrivateKey.generate()
    attacker_key = Ed25519PrivateKey.generate()

    trust_store = write_pinned_patch_keys(
        tmp_path / "pinned_keys.json",
        [
            PinnedPatchKey(
                key_id="key-tenant-a",
                public_key=key_a.public_key(),
                allowed_tenants=frozenset({"tenant_a"}),
            ),
            PinnedPatchKey(
                key_id="key-tenant-b",
                public_key=key_b.public_key(),
                allowed_tenants=frozenset({"tenant_b"}),
            ),
        ],
    )

    steps_a = _sample_steps()
    scope_a = _sample_scope(steps_a, tenant_id="tenant_a")
    scope_b = _sample_scope(steps_a, tenant_id="tenant_b")

    valid_patch = FleetSelectorPatch.create(
        scope=scope_a,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input[data-testid='coverage-effective-date-v2']",
        readback_verified=True,
        confidence=1.0,
        margin=0.4,
        signing_key=key_a,
        signer_key_id="key-tenant-a",
    )

    # 1. Missing trust store fails closed.
    with pytest.raises(FleetPatchError, match="not found"):
        apply_fleet_patch(
            steps_a,
            valid_patch,
            pinned_keys_path=tmp_path / "missing.json",
            scope=scope_a,
        )

    # 2. Pinned key for tenant_b CANNOT sign a patch for tenant_a!
    cross_key_patch = FleetSelectorPatch.create(
        scope=scope_a,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input#hijack",
        readback_verified=True,
        confidence=1.0,
        margin=0.4,
        signing_key=key_b,
        signer_key_id="key-tenant-b",
    )
    with pytest.raises(FleetPatchError, match="verification failed"):
        apply_fleet_patch(
            steps_a,
            cross_key_patch,
            pinned_keys_path=trust_store,
            scope=scope_a,
        )

    # 3. Forged attacker-signed patch -> refused.
    forged_patch = FleetSelectorPatch.create(
        scope=scope_a,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="#evil",
        readback_verified=True,
        confidence=1.0,
        margin=0.4,
        signing_key=attacker_key,
        signer_key_id="key-tenant-a",
    )
    with pytest.raises(FleetPatchError, match="verification failed"):
        apply_fleet_patch(
            steps_a,
            forged_patch,
            pinned_keys_path=trust_store,
            scope=scope_a,
        )

    # 4. JSON string "false" for readback_verified -> refused at parse time.
    bad_json_dict = valid_patch.to_dict()
    bad_json_dict["readback_verified"] = "false"
    with pytest.raises(FleetPatchError, match="JSON boolean"):
        FleetSelectorPatch.from_json(json.dumps(bad_json_dict))

    # 5. Cross-tenant batch application fails closed.
    with pytest.raises(FleetPatchError, match="scope mismatch on tenant_id"):
        apply_fleet_patch_to_packs(
            {"pack_a": steps_a, "pack_b": steps_a},
            valid_patch,
            pinned_keys_path=trust_store,
            pack_scopes={"pack_a": scope_a, "pack_b": scope_b},
        )

    # 6. Step missing workflow_id -> refused.
    missing_wf_steps = [
        {"step_id": "step_effective_date", "selector": "input.nw-eff-date-v1"}
    ]
    missing_wf_scope = _sample_scope(missing_wf_steps, tenant_id="tenant_a")
    missing_wf_patch = FleetSelectorPatch.create(
        scope=missing_wf_scope,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input#v2",
        readback_verified=True,
        confidence=1.0,
        margin=0.5,
        signing_key=key_a,
        signer_key_id="key-tenant-a",
    )
    with pytest.raises(FleetPatchError, match="missing required workflow_id"):
        apply_fleet_patch(
            missing_wf_steps,
            missing_wf_patch,
            pinned_keys_path=trust_store,
            scope=missing_wf_scope,
        )

    # 7. Tampered selector with original signature -> refused.
    tampered = replace(valid_patch, new_selector="button#delete")
    with pytest.raises(FleetPatchError, match="verification failed"):
        apply_fleet_patch(
            steps_a,
            tampered,
            pinned_keys_path=trust_store,
            scope=scope_a,
        )
