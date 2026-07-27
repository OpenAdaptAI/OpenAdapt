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
    ActionDeliveryUncertainty,
    EffectJournalEntry,
    EffectVerificationEvidence,
    ExecutionOutcomeEnvelope,
    IdentityCheck,
    IdentitySignalEvidence,
    OutcomeContractCounts,
    RunReport,
    StepResult,
    UnarmedStep,
)
from openadapt_flow.receipt import (
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
    "transaction_outcome",
    "profile",
    "production_eligible",
    "steps_total",
    "steps_ok",
    "heals",
    "model_calls",
    "est_cost_usd",
    "duration_ms",
    "rung_histogram",
    "evidence_classes",
    "effect_tier_reached",
    "authorization_required",
    "authorization_confirmed",
    "identity_required",
    "identity_confirmed",
    "postconditions_required",
    "postconditions_confirmed",
    "effects_required",
    "effects_confirmed",
    "identity_armed",
    "identity_applicable",
    "over_halt_count",
    "substrate",
    "provenance",
    "receipt_builder_version",
    "external_network_calls",
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
        "transaction_platform_fault": False,
        "production_eligible": True,
        "execution_completed": True,
        "execution_target_kind": "web",
        "execution_origin": "http://records.example.internal",
        "execution_entry_url": "http://records.example.internal/?patient=SECRET-MRN",
        "bundle_content_digest": "a" * 64,
        "governed_authorization_id": "authorization-1",
        "governed_approval_source": "openadapt-flow-tutorial",
        "governed_runtime_inputs_digest": "c" * 64,
        "governed_policy_contract_sha256": "d" * 64,
        "governed_minimum_effect_tier": 3,
        "required_identity_step_ids": ["step_000"],
        "params": {"note": "SECRET-NOTE-VALUE"},
        "success": True,
        "rung_counts": {"structural": 4},
        "heal_count": 0,
        "model_calls": 0,
        "est_model_cost_usd": 0.0,
        "total_ms": 4321.6,
        "identity_applicable_steps": 1,
        "identity_armed_steps": 1,
        "results": [
            StepResult(
                step_id="step_000",
                intent="click 'SECRET-BUTTON-LABEL'",
                ok=True,
                identity=IdentityCheck(
                    status="verified",
                    mode="structured",
                    coverage=1.0,
                    expected="SECRET-MRN",
                    observed="SECRET-MRN",
                ),
                postconditions_ok=True,
                effect_verified=True,
                effect_contract_hashes=["sha256:" + "b" * 64],
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
        "effect_journal": [
            EffectJournalEntry(
                step_id="step_000",
                intent="click 'SECRET-BUTTON-LABEL'",
                intended_effect_contract_hashes=["sha256:" + "b" * 64],
                attempt_state="delivered",
                observed_effect="present",
                effect_verified=True,
                verification_performed=True,
            )
        ],
        "outcome_envelope": ExecutionOutcomeEnvelope(
            outcome="VERIFIED",
            profile="standard",
            production_eligible=True,
            execution_completed=True,
            required_contracts=OutcomeContractCounts(
                authorization=1, identity=1, postcondition=1, effect=1
            ),
            passed_contracts=OutcomeContractCounts(
                authorization=1, identity=1, postcondition=1, effect=1
            ),
            evidence_classes=[
                "authorization",
                "effect_tier_1",
                "identity",
                "postcondition",
            ],
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

    receipt = build_receipt(_report())
    payload = json.loads(receipt.canonical_json())
    payload["screenshot"] = "steps/step_000_after.png"
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(payload)


def test_receipt_never_carries_a_phi_carrier_from_the_report() -> None:
    report = _report()
    receipt = build_receipt(report)
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
    receipt = build_receipt(_report())
    assert receipt.schema_version == RECEIPT_SCHEMA
    assert receipt.outcome == "VERIFIED"
    assert receipt.transaction_outcome == "VERIFIED"
    assert receipt.profile == "standard"
    assert receipt.production_eligible is True
    assert receipt.effect_tier_reached == "independent_system"
    assert receipt.authorization_confirmed == receipt.authorization_required == 1
    assert receipt.identity_confirmed == receipt.identity_required == 1
    assert receipt.postconditions_confirmed == receipt.postconditions_required == 1
    assert receipt.effects_confirmed == receipt.effects_required == 1
    assert receipt.identity_armed == receipt.identity_applicable == 1
    assert receipt.model_calls == 0
    assert receipt.duration_ms == 4322
    assert receipt.rung_histogram == {"structural": 4}
    assert receipt.bundle_digest == "a" * 64
    assert receipt.provenance == "production"


def test_generated_at_is_truncated_to_the_hour() -> None:
    """Minute/second resolution is a correlation handle a receipt does not need."""

    receipt = build_receipt(_report())
    assert receipt.generated_at == "2026-07-27T15:00:00Z"


def test_receipt_digest_binds_every_other_field() -> None:
    receipt = build_receipt(_report())
    assert receipt.receipt_digest
    other = build_receipt(
        _report(
            model_calls=3,
            est_model_cost_usd=0.02,
            outcome_envelope=_report().outcome_envelope.model_copy(
                update={
                    "model_calls": 3,
                    "evidence_classes": [
                        "authorization",
                        "effect_tier_1",
                        "identity",
                        "model",
                        "postcondition",
                    ],
                }
            ),
        ),
    )
    assert other.receipt_digest != receipt.receipt_digest


@pytest.mark.parametrize("field", ["label", "launcher_version", "halt_class"])
def test_arbitrary_or_legacy_receipt_fields_are_refused(field: str) -> None:
    receipt = build_receipt(_report())
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(
            {**json.loads(receipt.canonical_json()), field: "SECRET FREE TEXT"}
        )


def test_verified_receipt_refuses_a_retained_over_halt() -> None:
    """A success receipt cannot coexist with a halted retained step."""

    clean = build_receipt(_report())
    assert clean.over_halt_count == 0

    with pytest.raises(ReceiptError, match="failed, halted, or refuted"):
        build_receipt(
            _report(
                results=[
                    StepResult(
                        step_id="step_000",
                        intent="click",
                        ok=False,
                        safety_halt=True,
                        failure_category="safety_halt",
                        identity=IdentityCheck(
                            status="verified",
                            mode="structured",
                            coverage=1.0,
                            expected="SECRET-MRN",
                            observed="SECRET-MRN",
                        ),
                        postconditions_ok=True,
                        effect_verified=True,
                        effect_contract_hashes=["sha256:" + "b" * 64],
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
                ]
            ),
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"transaction_platform_fault": None}, "complete governed VERIFIED"),
        ({"governed_runtime_inputs_digest": None}, "complete governed VERIFIED"),
        ({"governed_policy_contract_sha256": None}, "complete governed VERIFIED"),
        ({"bundle_content_digest": None}, "complete governed VERIFIED"),
        ({"identity_armed_steps": 0}, "complete workflow identity arming"),
    ],
)
def test_receipt_refuses_incomplete_verified_contract(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ReceiptError, match=message):
        build_receipt(
            _report(**override),
        )


def test_receipt_requires_exactly_one_authorization_contract() -> None:
    doubled = OutcomeContractCounts(
        authorization=2, identity=1, postcondition=1, effect=1
    )
    envelope = _report().outcome_envelope
    assert envelope is not None
    envelope = envelope.model_copy(
        update={"required_contracts": doubled, "passed_contracts": doubled}
    )
    with pytest.raises(ReceiptError, match="exactly one passed authorization"):
        build_receipt(_report(outcome_envelope=envelope))


def test_receipt_revalidates_the_complete_run_report_from_json() -> None:
    report = _report()
    assert report.outcome_envelope is not None
    # RunReport models are mutable after construction. This creates a state the
    # envelope validator would have refused at load time.
    report.outcome_envelope.passed_contracts.effect = 2
    with pytest.raises(ReceiptError, match="typed JSON revalidation"):
        build_receipt(report)


@pytest.mark.parametrize("surface", ["list", "mapping", "result", "journal"])
def test_receipt_refuses_every_approved_unverified_mirror(surface: str) -> None:
    report = _report()
    if surface == "list":
        report.approved_unverified_effect_step_ids = ["step_000"]
    elif surface == "mapping":
        report.governed_authorized_effect_contracts = {
            "step_000": ["sha256:" + "b" * 64]
        }
    elif surface == "result":
        report.results[0].effect_approved_unverified = True
    else:
        report.effect_journal[0].approved_unverified = True
    with pytest.raises(ReceiptError, match="approved-unverified"):
        build_receipt(report)


def test_receipt_requires_identity_on_every_effect_bearing_step() -> None:
    envelope = _report().outcome_envelope
    assert envelope is not None
    no_identity = OutcomeContractCounts(
        authorization=1, identity=0, postcondition=1, effect=1
    )
    envelope = envelope.model_copy(
        update={"required_contracts": no_identity, "passed_contracts": no_identity}
    )
    with pytest.raises(ReceiptError, match="every effect-bearing step"):
        build_receipt(_report(required_identity_step_ids=[], outcome_envelope=envelope))


def test_receipt_binds_required_identity_to_workflow_coverage() -> None:
    with pytest.raises(ReceiptError, match="complete workflow identity arming"):
        build_receipt(_report(identity_applicable_steps=0, identity_armed_steps=0))


def test_receipt_refuses_any_retained_unarmed_identity_step() -> None:
    with pytest.raises(ReceiptError, match="unarmed identity step"):
        build_receipt(
            _report(
                identity_unarmed=[
                    UnarmedStep(step_id="outside-required", reason="not armed")
                ]
            )
        )


def _signal_quorum_identity() -> IdentityCheck:
    evidence = [
        IdentitySignalEvidence(
            signal="record_id",
            source="structured",
            verdict="verified",
            evidence_class="application_structured_text",
            match="exact",
        ),
        IdentitySignalEvidence(
            signal="secondary_identifier",
            source="identifier_region",
            verdict="verified",
            evidence_class="recorded_and_live_region",
            match="exact",
        ),
    ]
    return IdentityCheck(
        status="verified",
        mode="signal_quorum",
        signal_evidence=evidence,
        quorum_required=2,
        quorum_verified=2,
    )


def test_receipt_accepts_consistent_signal_quorum_identity() -> None:
    result = (
        _report().results[0].model_copy(update={"identity": _signal_quorum_identity()})
    )
    assert build_receipt(_report(results=[result])).outcome == "VERIFIED"


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_signal",
        "duplicate_source",
        "evidence_class",
        "conflict",
        "count",
        "quorum",
    ],
)
def test_receipt_refuses_inconsistent_signal_quorum_identity(fault: str) -> None:
    identity = _signal_quorum_identity()
    if fault == "duplicate_signal":
        duplicate = identity.signal_evidence[0].model_copy(
            update={"source": "identifier_region"}
        )
        identity.signal_evidence[1] = duplicate
    elif fault == "duplicate_source":
        identity.signal_evidence[1] = identity.signal_evidence[1].model_copy(
            update={
                "source": "structured",
                "evidence_class": "application_structured_text",
            }
        )
    elif fault == "evidence_class":
        identity.signal_evidence[0] = identity.signal_evidence[0].model_copy(
            update={"evidence_class": "recorded_and_live_region"}
        )
    elif fault == "conflict":
        identity.signal_evidence[1].verdict = "conflict"
    elif fault == "count":
        identity.quorum_verified = 1
    else:
        identity.quorum_required = 3
    result = _report().results[0].model_copy(update={"identity": identity})
    with pytest.raises(ReceiptError, match="signal-quorum"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_an_empty_verified_identity_record() -> None:
    result = (
        _report()
        .results[0]
        .model_copy(update={"identity": IdentityCheck(status="verified")})
    )
    with pytest.raises(ReceiptError, match="empty retained identity"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_partial_effect_hash_coverage() -> None:
    result = (
        _report()
        .results[0]
        .model_copy(update={"effect_contract_hashes": ["sha256:" + "e" * 64]})
    )
    with pytest.raises(ReceiptError, match="effect-hash coverage"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_effect_evidence_swapped_between_steps() -> None:
    first = _report().results[0]
    first_evidence = first.effect_evidence[0]
    first_hash = "sha256:" + "b" * 64
    second_hash = "sha256:" + "e" * 64
    first = first.model_copy(
        update={
            "effect_contract_hashes": [first_hash],
            "effect_evidence": [
                first_evidence.model_copy(update={"effect_contract_hash": second_hash})
            ],
        }
    )
    second = first.model_copy(
        update={
            "step_id": "step_001",
            "effect_contract_hashes": [second_hash],
            "effect_evidence": [
                first_evidence.model_copy(update={"effect_contract_hash": first_hash})
            ],
        }
    )
    complete = OutcomeContractCounts(
        authorization=1, identity=2, postcondition=2, effect=2
    )
    envelope = _report().outcome_envelope
    assert envelope is not None
    envelope = envelope.model_copy(
        update={"required_contracts": complete, "passed_contracts": complete}
    )
    journal = [
        _report().effect_journal[0],
        _report()
        .effect_journal[0]
        .model_copy(
            update={
                "step_id": "step_001",
                "intended_effect_contract_hashes": [second_hash],
            }
        ),
    ]
    with pytest.raises(ReceiptError, match="per-step effect-hash"):
        build_receipt(
            _report(
                results=[first, second],
                effect_journal=journal,
                required_identity_step_ids=["step_000", "step_001"],
                identity_applicable_steps=2,
                identity_armed_steps=2,
                outcome_envelope=envelope,
            )
        )


def test_receipt_refuses_missing_retained_postcondition_evidence() -> None:
    result = _report().results[0].model_copy(update={"postconditions_ok": None})
    with pytest.raises(ReceiptError, match="retained postcondition evidence"):
        build_receipt(_report(results=[result]))


def test_receipt_requires_postcondition_on_the_effect_step_not_a_decoy() -> None:
    effect = _report().results[0].model_copy(update={"postconditions_ok": None})
    decoy = StepResult(
        step_id="decoy",
        intent="read-only decoy",
        ok=True,
        postconditions_ok=True,
    )
    with pytest.raises(ReceiptError, match="postcondition coverage"):
        build_receipt(_report(results=[effect, decoy]))


def test_receipt_refuses_effect_verified_false() -> None:
    result = _report().results[0].model_copy(update={"effect_verified": False})
    with pytest.raises(ReceiptError, match="effect_verified=true"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_confirmed_but_absent_effect() -> None:
    evidence = (
        _report()
        .results[0]
        .effect_evidence[0]
        .model_copy(update={"observed_effect": "absent"})
    )
    result = _report().results[0].model_copy(update={"effect_evidence": [evidence]})
    with pytest.raises(ReceiptError, match="observed present"):
        build_receipt(_report(results=[result]))


@pytest.mark.parametrize(
    "evidence_update",
    [
        {"initial_verdict": "indeterminate"},
        {"reconciliation_completed": True},
        {"reconciliation_actions": 1},
    ],
)
def test_receipt_refuses_nonclean_effect_evidence(
    evidence_update: dict[str, object],
) -> None:
    evidence = (
        _report().results[0].effect_evidence[0].model_copy(update=evidence_update)
    )
    result = _report().results[0].model_copy(update={"effect_evidence": [evidence]})
    with pytest.raises(ReceiptError, match="cleanly confirmed|reconciled"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_evidence_below_authorized_minimum() -> None:
    evidence = (
        _report()
        .results[0]
        .effect_evidence[0]
        .model_copy(update={"verification_tier": 3})
    )
    result = _report().results[0].model_copy(update={"effect_evidence": [evidence]})
    envelope = _report().outcome_envelope.model_copy(
        update={
            "evidence_classes": [
                "authorization",
                "effect_tier_3",
                "identity",
                "postcondition",
            ]
        }
    )
    with pytest.raises(ReceiptError, match="authorization minimum"):
        build_receipt(
            _report(
                results=[result],
                outcome_envelope=envelope,
                governed_minimum_effect_tier=1,
            )
        )


@pytest.mark.parametrize(
    "journal",
    [
        [],
        [
            EffectJournalEntry(
                step_id="step_000",
                intent="click",
                intended_effect_contract_hashes=["sha256:" + "b" * 64],
                attempt_state="delivery_uncertain",
                observed_effect="unknown",
                effect_verified=None,
                verification_performed=False,
            )
        ],
    ],
)
def test_receipt_refuses_missing_or_contradictory_transaction_journal(
    journal: list[EffectJournalEntry],
) -> None:
    with pytest.raises(ReceiptError, match="transaction journal"):
        build_receipt(_report(effect_journal=journal))


def test_receipt_accepts_resolved_uncertain_delivery_with_exact_journal() -> None:
    uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=False,
        observed_at="2026-07-27T15:34:57+00:00",
        cause_type="ActionDeliveryUncertain",
        verification_attempted=True,
        postconditions_confirmed=True,
        effects_confirmed=True,
        resolved_by_contract=True,
    )
    result = (
        _report().results[0].model_copy(update={"delivery_uncertainty": uncertainty})
    )
    journal = [
        _report()
        .effect_journal[0]
        .model_copy(
            update={
                "attempt_state": "delivery_uncertain",
                "observed_at": uncertainty.observed_at,
            }
        )
    ]
    receipt = build_receipt(_report(results=[result], effect_journal=journal))
    assert receipt.outcome == "VERIFIED"


def test_receipt_refuses_non_actuated_effect_result() -> None:
    result = _report().results[0].model_copy(update={"delivery_attempted": False})
    journal = [
        _report().effect_journal[0].model_copy(update={"attempt_state": "not_actuated"})
    ]
    with pytest.raises(ReceiptError, match="non-actuated"):
        build_receipt(_report(results=[result], effect_journal=journal))


@pytest.mark.parametrize("kind", ["irreversible", "uncertain"])
def test_receipt_accounts_for_every_transaction_consequential_result(
    kind: str,
) -> None:
    if kind == "irreversible":
        extra = StepResult(
            step_id="extra-write",
            intent="unaccounted write",
            ok=True,
            risk="irreversible",
            delivery_attempted=True,
        )
    else:
        extra = StepResult(
            step_id="extra-uncertain",
            intent="unaccounted uncertain delivery",
            ok=True,
            delivery_uncertainty=ActionDeliveryUncertainty(
                operation="click",
                native=False,
                observed_at="2026-07-27T15:34:57+00:00",
                cause_type="ActionDeliveryUncertain",
                verification_attempted=True,
                postconditions_confirmed=True,
                effects_confirmed=True,
                resolved_by_contract=True,
            ),
        )
    with pytest.raises(ReceiptError, match="transaction-consequential"):
        build_receipt(_report(results=[*_report().results, extra]))


def test_receipt_refuses_equal_length_transaction_effect_result_swap() -> None:
    skipped_effect = _report().results[0].model_copy(update={"skipped": True})
    unaccounted_write = StepResult(
        step_id="unaccounted-write",
        intent="unaccounted write",
        ok=True,
        risk="irreversible",
        delivery_attempted=True,
        identity=_report().results[0].identity,
    )
    misleading_journal = [
        _report()
        .effect_journal[0]
        .model_copy(
            update={
                "step_id": unaccounted_write.step_id,
                "intent": unaccounted_write.intent,
                "intended_effect_contract_hashes": [],
            }
        )
    ]

    with pytest.raises(ReceiptError, match="transaction-consequential"):
        build_receipt(
            _report(
                results=[skipped_effect, unaccounted_write],
                effect_journal=misleading_journal,
                required_identity_step_ids=[unaccounted_write.step_id],
            )
        )


def test_receipt_refuses_uncertain_delivery_with_wrong_observation_time() -> None:
    uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=False,
        observed_at="2026-07-27T15:34:57+00:00",
        cause_type="ActionDeliveryUncertain",
        verification_attempted=True,
        postconditions_confirmed=True,
        effects_confirmed=True,
        resolved_by_contract=True,
    )
    result = (
        _report().results[0].model_copy(update={"delivery_uncertainty": uncertainty})
    )
    journal = [
        _report()
        .effect_journal[0]
        .model_copy(
            update={
                "attempt_state": "delivery_uncertain",
                "observed_at": "2026-07-27T15:34:58+00:00",
            }
        )
    ]
    with pytest.raises(ReceiptError, match="transaction journal"):
        build_receipt(_report(results=[result], effect_journal=journal))


def test_receipt_refuses_unresolved_uncertain_delivery() -> None:
    uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=False,
        observed_at="2026-07-27T15:34:57+00:00",
        cause_type="ActionDeliveryUncertain",
        verification_attempted=True,
        postconditions_confirmed=True,
        effects_confirmed=True,
        resolved_by_contract=False,
    )
    result = (
        _report().results[0].model_copy(update={"delivery_uncertainty": uncertainty})
    )
    journal = [
        _report()
        .effect_journal[0]
        .model_copy(
            update={
                "attempt_state": "delivery_uncertain",
                "observed_at": uncertainty.observed_at,
            }
        )
    ]
    with pytest.raises(ReceiptError, match="unresolved action-delivery"):
        build_receipt(_report(results=[result], effect_journal=journal))


def test_receipt_binds_api_attempt_state_to_the_transaction_classifier() -> None:
    result = _report().results[0].model_copy(update={"actuation": "api"})
    with pytest.raises(ReceiptError, match="transaction journal"):
        build_receipt(_report(results=[result]))
    journal = [
        _report().effect_journal[0].model_copy(update={"attempt_state": "actuated_api"})
    ]
    assert (
        build_receipt(_report(results=[result], effect_journal=journal)).outcome
        == "VERIFIED"
    )


def test_receipt_refuses_uncertainty_hidden_as_delivered() -> None:
    uncertainty = ActionDeliveryUncertainty(
        operation="click",
        native=False,
        observed_at="2026-07-27T15:34:57+00:00",
        cause_type="ActionDeliveryUncertain",
        verification_attempted=True,
        postconditions_confirmed=True,
        effects_confirmed=True,
        resolved_by_contract=True,
    )
    result = (
        _report().results[0].model_copy(update={"delivery_uncertainty": uncertainty})
    )
    with pytest.raises(ReceiptError, match="transaction journal"):
        build_receipt(_report(results=[result]))


def test_receipt_refuses_screen_only_effect_evidence() -> None:
    evidence = (
        _report()
        .results[0]
        .effect_evidence[0]
        .model_copy(update={"verification_tier": 4})
    )
    result = _report().results[0].model_copy(update={"effect_evidence": [evidence]})
    envelope = _report().outcome_envelope.model_copy(
        update={
            "evidence_classes": [
                "authorization",
                "effect_tier_4",
                "identity",
                "postcondition",
            ]
        }
    )
    with pytest.raises(ReceiptError, match="independent verification floor"):
        build_receipt(
            _report(results=[result], outcome_envelope=envelope),
        )


def test_generic_receipt_cannot_claim_synthetic_provenance() -> None:
    receipt = build_receipt(_report(governed_approval_source="openadapt-flow-tutorial"))
    assert receipt.provenance == "production"


def test_receipt_digest_is_revalidated_on_parse() -> None:
    receipt = build_receipt(_report())
    payload = json.loads(receipt.canonical_json())
    payload["duration_ms"] += 1
    with pytest.raises(ValidationError, match="digest does not match"):
        RunReceipt.model_validate(payload)


def test_receipt_digest_is_required_on_parse() -> None:
    payload = json.loads(build_receipt(_report()).canonical_json())
    payload.pop("receipt_digest")
    with pytest.raises(ValidationError):
        RunReceipt.model_validate(payload)


def test_write_receipt_refuses_a_stale_model_copy(tmp_path: Path) -> None:
    receipt = build_receipt(_report()).model_copy(update={"duration_ms": 999})
    with pytest.raises(ReceiptError, match="integrity validation"):
        write_receipt(receipt, tmp_path / "stale")
    assert not (tmp_path / "stale").exists()


def test_receipt_refuses_a_report_with_no_classified_outcome() -> None:
    with pytest.raises(ReceiptError):
        build_receipt(_report(execution_outcome=None))


def test_write_receipt_is_local_and_deterministic(tmp_path: Path) -> None:
    receipt = build_receipt(_report())
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
