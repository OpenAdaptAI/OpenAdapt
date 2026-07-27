"""The Run Receipt's allow-list is the privacy boundary; pin it exactly.

The receipt is safe because it is GENERATED from a closed declaration, not
REDACTED from the operator's report.  These tests pin that property: the field
set is exact, unknown keys are refused rather than dropped, and no free-text or
record-bearing field of a run report can reach the artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import (
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    IdentityCheck,
    OutcomeContractCounts,
    RunReport,
    StepResult,
)
from openadapt_flow.receipt import (
    MAX_LABEL_CHARS,
    RECEIPT_SCHEMA,
    ReceiptError,
    RunReceipt,
    build_receipt,
    render_receipt_markdown,
    write_receipt,
)

#: The complete set of keys a receipt may ever serialize.  Adding one is a
#: privacy-boundary change and must be a deliberate edit to this list.
ALLOWED_FIELDS = {
    "schema_version",
    "outcome",
    "halt_class",
    "billable",
    "label",
    "steps_total",
    "steps_ok",
    "heals",
    "model_calls",
    "est_cost_usd",
    "duration_ms",
    "rung_histogram",
    "evidence_classes",
    "effect_tier_reached",
    "effects_required",
    "effects_confirmed",
    "identity_armed",
    "identity_applicable",
    "over_halt_count",
    "substrate",
    "provenance",
    "flow_version",
    "launcher_version",
    "bundle_digest",
    "receipt_digest",
    "generated_at",
}


def _report(**overrides: object) -> RunReport:
    """A VERIFIED report whose every free-text field is a tracer value."""

    defaults: dict[str, object] = {
        "workflow_name": "SECRET-WORKFLOW-NAME",
        "started_at": "2026-07-27T15:34:56.789012+00:00",
        "execution_profile": "standard",
        "execution_outcome": "VERIFIED",
        "transaction_outcome": "VERIFIED",
        "transaction_billable": True,
        "production_eligible": True,
        "execution_completed": True,
        "execution_target_kind": "web",
        "execution_origin": "http://records.example.internal",
        "execution_entry_url": "http://records.example.internal/?patient=SECRET-MRN",
        "bundle_content_digest": "a" * 64,
        "params": {"note": "SECRET-NOTE-VALUE"},
        "success": True,
        "rung_counts": {"structural": 4},
        "heal_count": 0,
        "model_calls": 0,
        "est_model_cost_usd": 0.0,
        "total_ms": 4321.6,
        "identity_applicable_steps": 4,
        "identity_armed_steps": 4,
        "results": [
            StepResult(
                step_id="step_000",
                intent="click 'SECRET-BUTTON-LABEL'",
                ok=True,
                identity=IdentityCheck(status="verified"),
                before_png="steps/step_000_before.png",
                after_png="steps/step_000_after.png",
                effect_evidence=[
                    EffectVerificationEvidence(
                        substrate="rest",
                        initial_verdict="confirmed",
                        final_verdict="confirmed",
                        observed_effect="present",
                        verification_tier=1,
                        effect_contract_hash="sha256:" + "b" * 64,
                    )
                ],
            )
        ],
        "outcome_envelope": ExecutionOutcomeEnvelope(
            outcome="VERIFIED",
            profile="standard",
            production_eligible=True,
            execution_completed=True,
            required_contracts=OutcomeContractCounts(
                authorization=1, identity=1, postcondition=2, effect=2
            ),
            passed_contracts=OutcomeContractCounts(
                authorization=1, identity=1, postcondition=2, effect=2
            ),
            evidence_classes=["authorization", "effect_tier_1", "identity"],
            model_calls=0,
            external_network_calls="observed",
        ),
    }
    defaults.update(overrides)
    envelope = defaults.get("outcome_envelope")
    if isinstance(envelope, ExecutionOutcomeEnvelope):
        # The report refuses an envelope that contradicts its own outcome, so
        # keep the two consistent for whatever the test overrode.
        defaults["outcome_envelope"] = envelope.model_copy(
            update={
                "outcome": defaults["execution_outcome"],
                "model_calls": defaults["model_calls"],
            }
        )
        if defaults["execution_outcome"] is None:
            defaults["outcome_envelope"] = None
    return RunReport.model_validate(defaults)


def test_receipt_field_set_is_exactly_the_allow_list() -> None:
    assert set(RunReceipt.model_fields) == ALLOWED_FIELDS


def test_unknown_keys_are_refused_not_dropped() -> None:
    """A dropped key is a silent widening; a refused key is a review."""

    receipt = build_receipt(_report(), provenance="synthetic-tutorial")
    payload = json.loads(receipt.canonical_json())
    payload["screenshot"] = "steps/step_000_after.png"
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(payload)


def test_receipt_never_carries_a_phi_carrier_from_the_report() -> None:
    report = _report()
    receipt = build_receipt(report, provenance="synthetic-tutorial")
    text = receipt.canonical_json().decode("utf-8") + render_receipt_markdown(receipt)
    for tracer in (
        "SECRET-WORKFLOW-NAME",
        "SECRET-NOTE-VALUE",
        "SECRET-BUTTON-LABEL",
        "SECRET-MRN",
        "records.example.internal",
        "step_000_after.png",
        "b" * 64,  # a resolved effect-contract hash
    ):
        assert tracer not in text, f"receipt leaked {tracer!r}"


def test_receipt_reports_the_evidence_it_claims() -> None:
    receipt = build_receipt(_report(), provenance="synthetic-tutorial")
    assert receipt.schema_version == RECEIPT_SCHEMA
    assert receipt.outcome == "VERIFIED"
    assert receipt.halt_class == "VERIFIED"
    assert receipt.billable is True
    assert receipt.effect_tier_reached == "independent_system"
    assert receipt.effects_required == 2
    assert receipt.effects_confirmed == 2
    assert receipt.identity_armed == 4
    assert receipt.identity_applicable == 4
    assert receipt.model_calls == 0
    assert receipt.duration_ms == 4322
    assert receipt.rung_histogram == {"structural": 4}
    assert receipt.bundle_digest == "a" * 64
    assert receipt.provenance == "synthetic-tutorial"


def test_generated_at_is_truncated_to_the_hour() -> None:
    """Minute/second resolution is a correlation handle a receipt does not need."""

    receipt = build_receipt(_report(), provenance="synthetic-tutorial")
    assert receipt.generated_at == "2026-07-27T15:00:00Z"


def test_receipt_digest_binds_every_other_field() -> None:
    receipt = build_receipt(_report(), provenance="synthetic-tutorial")
    assert receipt.receipt_digest
    other = build_receipt(
        _report(model_calls=3, est_model_cost_usd=0.02),
        provenance="synthetic-tutorial",
    )
    assert other.receipt_digest != receipt.receipt_digest


def test_label_is_operator_typed_and_bounded() -> None:
    receipt = build_receipt(
        _report(), provenance="synthetic-tutorial", label="  My first run  "
    )
    assert receipt.label == "My first run"
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(
            {
                **json.loads(receipt.canonical_json()),
                "label": "x" * (MAX_LABEL_CHARS + 1),
            }
        )
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(
            {**json.loads(receipt.canonical_json()), "label": "two\nlines"}
        )


def test_over_halt_count_is_always_present_and_counts_refused_confirmed_effects() -> (
    None
):
    """The counter-metric is not optional, and it is computed, not asserted."""

    clean = build_receipt(_report(), provenance="synthetic-tutorial")
    assert clean.over_halt_count == 0

    over_halted = build_receipt(
        _report(
            results=[
                StepResult(
                    step_id="step_000",
                    intent="click",
                    ok=False,
                    safety_halt=True,
                    failure_category="safety_halt",
                    effect_evidence=[
                        EffectVerificationEvidence(
                            substrate="rest",
                            initial_verdict="confirmed",
                            final_verdict="confirmed",
                            observed_effect="present",
                            verification_tier=1,
                            effect_contract_hash="sha256:" + "c" * 64,
                        )
                    ],
                )
            ]
        ),
        provenance="synthetic-tutorial",
    )
    assert over_halted.over_halt_count == 1


def test_receipt_refuses_a_report_with_no_classified_outcome() -> None:
    with pytest.raises(ReceiptError):
        build_receipt(_report(execution_outcome=None), provenance="production")


def test_write_receipt_is_local_and_deterministic(tmp_path: Path) -> None:
    receipt = build_receipt(_report(), provenance="synthetic-tutorial")
    paths = write_receipt(receipt, tmp_path / "share")
    assert set(paths) == {"json", "markdown", "png"}
    for path in paths.values():
        assert path.is_file()
    first = paths["json"].read_bytes()
    write_receipt(receipt, tmp_path / "share")
    assert paths["json"].read_bytes() == first


def test_demo_profile_still_cannot_require_or_report_verified_effects() -> None:
    """Guard the gate this work must NOT have moved.

    The receipt became reachable because the tutorial gained real effect
    evidence, not because the Demo contract changed.
    """

    demo = execution_profile_contract(ExecutionProfile.DEMO)
    assert demo.require_effect_contracts is False
    assert demo.minimum_effect_tier is None
    assert demo.production is False

    standard = execution_profile_contract(ExecutionProfile.STANDARD)
    assert standard.require_effect_contracts is True
    assert standard.require_certification is True
    assert standard.require_identity_coverage is True
    assert int(standard.minimum_effect_tier) == 3
