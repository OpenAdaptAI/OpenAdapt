"""Tests for 3-layer surgical selector healing and fleet patch propagation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from openadapt_verify.surgical_heal import (
    ElementTarget,
    FleetPatchError,
    FleetSelectorPatch,
    SurgicalHealCandidate,
    SurgicalSelectorHealer,
    apply_fleet_patch,
    apply_fleet_patch_to_packs,
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
        # Hidden element with identical label must be ignored fail-closed.
        SurgicalHealCandidate(
            selector="input#hidden-effective-date",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
            visible=False,
        ),
    ]


def test_deterministic_aria_heal_and_fleet_propagation_across_30_agencies() -> None:
    target = _drifted_quote_target()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    oracle_calls: list[str] = []

    def readback_oracle(cand: SurgicalHealCandidate) -> tuple[bool, str]:
        oracle_calls.append(cand.selector)
        return True, "a" * 64

    result = healer.heal_and_verify(target, candidates, readback_oracle=readback_oracle)

    assert result.verdict == "HEALED"
    assert result.readback_verified is True
    assert result.selected is not None
    assert (
        result.selected.candidate.selector
        == "input[data-testid='coverage-effective-date-v2']"
    )
    assert result.selected.score == 1.0
    assert result.margin >= 0.15
    assert oracle_calls == ["input[data-testid='coverage-effective-date-v2']"]
    assert result.patch is not None
    assert result.patch.readback_verified is True
    assert result.patch.verify_integrity() is True

    # JSON round-trip preserves content-addressed integrity.
    roundtripped = FleetSelectorPatch.from_json(result.patch.to_json())
    assert roundtripped == result.patch
    assert roundtripped.verify_integrity() is True

    # Propagate the single verified patch across 32 agency workflow packs.
    agency_packs = {
        f"agency_{idx:02d}": [
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
        for idx in range(32)
    }

    propagated = apply_fleet_patch_to_packs(agency_packs, roundtripped)
    assert len(propagated) == 32
    for idx in range(32):
        aid = f"agency_{idx:02d}"
        # Original packs are never mutated in place.
        assert agency_packs[aid][1]["selector"] == "input.nw-eff-date-v1"
        assert (
            propagated[aid][1]["selector"]
            == "input[data-testid='coverage-effective-date-v2']"
        )
        assert propagated[aid][1]["healed_by_patch_sha256"] == roundtripped.patch_sha256
        # Untouched steps stay identical.
        assert propagated[aid][0]["selector"] == "input#applicant-name"


def test_abstain_halt_on_ambiguous_tie_candidates() -> None:
    target = _drifted_quote_target()
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
        target, tied_candidates, readback_oracle=readback_oracle
    )

    assert result.verdict == "ABSTAIN_HALT"
    assert "ambiguous" in result.reason
    assert result.margin == 0.0
    assert result.selected is None
    assert result.patch is None
    assert oracle_called is False


def test_abstain_halt_below_min_score_threshold() -> None:
    target = _drifted_quote_target()
    weak_candidates = [
        SurgicalHealCandidate(
            selector="button#cancel-quote",
            aria_role="button",
            accessible_name="Cancel Quote",
            form_section="Footer Actions",
            input_type="button",
        )
    ]
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)
    result = healer.rank_and_select(target, weak_candidates)

    assert result.verdict == "ABSTAIN_HALT"
    assert "below min_score" in result.reason
    assert result.selected is None
    assert result.patch is None


def test_failed_readback_refuses_to_emit_fleet_patch() -> None:
    target = _drifted_quote_target()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    result = healer.heal_and_verify(
        target, candidates, readback_oracle=lambda _cand: False
    )

    assert result.verdict == "ABSTAIN_HALT"
    assert result.readback_verified is False
    assert result.patch is None
    assert "read-back" in result.reason


def test_visual_locator_callback_can_break_near_tie_and_fails_closed_on_error() -> None:
    target = _drifted_quote_target()
    near_tie = [
        SurgicalHealCandidate(
            selector="input#eff-date-top",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input#eff-date-bottom",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
    ]

    def visual_cb(_target: ElementTarget, cand: SurgicalHealCandidate) -> float:
        return 1.0 if cand.selector == "input#eff-date-top" else 0.1

    healer = SurgicalSelectorHealer(
        min_score=0.75,
        min_margin=0.15,
        visual_locator=visual_cb,
        visual_weight=0.25,
    )
    ok_res = healer.rank_and_select(target, near_tie)
    assert ok_res.verdict == "HEALED"
    assert ok_res.selected is not None
    assert ok_res.selected.candidate.selector == "input#eff-date-top"
    assert ok_res.margin == pytest.approx(0.225, abs=1e-6)

    # Out-of-range visual score must fail closed to ABSTAIN_HALT.
    bad_healer = SurgicalSelectorHealer(
        visual_locator=lambda _t, _c: float("nan"),
        visual_weight=0.2,
    )
    bad_res = bad_healer.rank_and_select(target, near_tie)
    assert bad_res.verdict == "ABSTAIN_HALT"
    assert "visual locator callback failed" in bad_res.reason


def test_apply_fleet_patch_rejects_tampered_sha_unverified_and_mismatched_steps() -> (
    None
):
    patch = FleetSelectorPatch.create(
        workflow_id="wf_policy_quote_submit",
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input[data-testid='coverage-effective-date-v2']",
        readback_verified=True,
        confidence=0.98,
        margin=0.35,
    )
    pack = [
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_effective_date",
            "selector": "input.nw-eff-date-v1",
        }
    ]

    # Tampered selector with original sha256 -> refused.
    tampered_selector = replace(patch, new_selector="button#delete-policy")
    assert tampered_selector.verify_integrity() is False
    with pytest.raises(FleetPatchError, match="integrity check failed"):
        apply_fleet_patch(pack, tampered_selector)

    # Tampered sha256 -> refused.
    tampered_sha = replace(patch, patch_sha256="0" * 64)
    with pytest.raises(FleetPatchError, match="integrity check failed"):
        apply_fleet_patch(pack, tampered_sha)

    # Unverified patch -> refused both at create() and apply_fleet_patch().
    with pytest.raises(FleetPatchError, match="readback_verified=True"):
        FleetSelectorPatch.create(
            workflow_id="wf_policy_quote_submit",
            step_id="step_effective_date",
            old_selector="input.nw-eff-date-v1",
            new_selector="input#v2",
            readback_verified=False,
            confidence=0.95,
            margin=0.4,
        )
    unverified = replace(patch, readback_verified=False)
    with pytest.raises(FleetPatchError, match="readback_verified != True"):
        apply_fleet_patch(pack, unverified)

    # Selector mismatch in target pack -> refused without mutating pack.
    drifted_pack = [
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_effective_date",
            "selector": "input.other-selector",
        }
    ]
    with pytest.raises(FleetPatchError, match="selector mismatch"):
        apply_fleet_patch(drifted_pack, patch)
    assert drifted_pack[0]["selector"] == "input.other-selector"
