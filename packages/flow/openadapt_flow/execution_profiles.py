"""Named execution profiles over OpenAdapt's existing governed runtime.

Profiles do not implement a second policy or replay path.  They select which
requirements the existing run gate must enforce, whether the shared replayer
must be durable, and how the resulting report may be described.

The low-level controls remain available for embedding and backwards
compatibility.  Production callers can choose one reviewed profile instead of
assembling a potentially contradictory collection of permissive flags.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Workflow


class ExecutionProfile(str, Enum):
    """The supported runtime postures."""

    DEMO = "demo"
    STANDARD = "standard"
    REGULATED = "regulated"


class ExecutionOutcome(str, Enum):
    """Precise result of applying a profile's evidence contract."""

    VERIFIED = "VERIFIED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    HALTED = "HALTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class ExecutionProfileContract:
    """Requirements a named profile applies to the existing runtime."""

    profile: ExecutionProfile
    production: bool
    require_certification: bool
    require_identity_coverage: bool
    require_effect_contracts: bool
    minimum_effect_tier: VerificationTier | None
    require_approval_for_unverified_effects: bool
    allow_unverified_write_approval: bool
    require_encryption: bool
    strict_templates: bool
    require_durable: bool
    require_settled: bool
    default_policy: str | None


_CONTRACTS = {
    ExecutionProfile.DEMO: ExecutionProfileContract(
        profile=ExecutionProfile.DEMO,
        production=False,
        require_certification=False,
        require_identity_coverage=False,
        require_effect_contracts=False,
        minimum_effect_tier=None,
        require_approval_for_unverified_effects=True,
        allow_unverified_write_approval=True,
        require_encryption=False,
        strict_templates=False,
        require_durable=False,
        require_settled=False,
        default_policy=None,
    ),
    ExecutionProfile.STANDARD: ExecutionProfileContract(
        profile=ExecutionProfile.STANDARD,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=False,
        strict_templates=False,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
    ),
    ExecutionProfile.REGULATED: ExecutionProfileContract(
        profile=ExecutionProfile.REGULATED,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=True,
        strict_templates=True,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
    ),
}


def resolve_execution_profile(
    value: ExecutionProfile | str | None,
    *,
    default: ExecutionProfile = ExecutionProfile.REGULATED,
) -> ExecutionProfile:
    """Resolve a profile name or fail loudly on an unknown value."""

    if value is None:
        return default
    if isinstance(value, ExecutionProfile):
        return value
    try:
        return ExecutionProfile(str(value).strip().lower())
    except ValueError as exc:
        choices = ", ".join(profile.value for profile in ExecutionProfile)
        raise ValueError(
            f"unknown execution profile {value!r}; expected one of: {choices}"
        ) from exc


def execution_profile_contract(
    value: ExecutionProfile | str,
) -> ExecutionProfileContract:
    """Return the immutable contract for ``value``."""

    return _CONTRACTS[resolve_execution_profile(value)]


def required_effect_tier(
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> VerificationTier | None:
    """Return the strongest profile/project minimum for this workflow."""

    contract = execution_profile_contract(profile)
    required = contract.minimum_effect_tier
    if not contract.production:
        return required
    project_minimum = getattr(workflow.qualification, "minimum_effect_tier", None)
    if project_minimum is not None:
        candidate = VerificationTier(project_minimum)
        if required is None or int(candidate) < int(required):
            required = candidate
    return required


def classify_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> ExecutionOutcome:
    """Classify a completed report without changing legacy ``success``.

    Demo success is always visibly non-production.  Standard and Regulated
    success becomes ``VERIFIED`` only when every executed consequential action
    has a confirmed effect at or above the workflow's required evidence tier.
    Therefore an approved-unverified or immediate-screen-only result can never
    be reported as ``VERIFIED`` under either production profile.
    """

    resolved = resolve_execution_profile(profile)
    execution_completed = (
        report.execution_completed
        if report.execution_completed is not None
        else report.success
    )
    if _completed_compensation_actions(report) > 0:
        return ExecutionOutcome.ROLLED_BACK
    if not execution_completed:
        refusal_step_ids = {"<authorization>", "<params>", "<profile>"}
        governed_halt = (
            report.terminal_outcome in {"halt", "escalate"}
            or any(result.safety_halt for result in report.results)
            or any(
                result.failure_category in {"governed_refusal", "safety_halt"}
                for result in report.results
            )
            or any(result.step_id in refusal_step_ids for result in report.results)
        )
        return ExecutionOutcome.HALTED if governed_halt else ExecutionOutcome.FAILED

    if resolved is ExecutionProfile.DEMO:
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    if not (report.governed_authorization_id and report.governed_runtime_inputs_digest):
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    # Import lazily: run_gate imports this module for the profile contract.
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    consequential = {
        step.id
        for step in iter_workflow_steps(workflow)
        if is_consequential(
            step,
            workflow,
            certifying_policy_sha256=report.governed_policy_contract_sha256,
        )
    }
    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    if workflow.program is None:
        expected_results = Counter(step.id for step in workflow.steps)
        observed_results = Counter(
            result.step_id
            for result in report.results
            if result.step_id in expected_results
        )
        if observed_results != expected_results:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
    required_identity_ids = set(report.required_identity_step_ids)
    if any(
        result.identity is None or result.identity.status != "verified"
        for result in report.results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    minimum = required_effect_tier(workflow, resolved)
    assert minimum is not None
    for result in report.results:
        if result.skipped or result.exception_handled:
            continue
        step = steps_by_id.get(result.step_id)
        if step is None:
            continue
        if not result.ok:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if step.expect and result.postconditions_ok is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if result.step_id not in consequential:
            continue
        if result.effect_approved_unverified or result.effect_verified is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        from openadapt_flow.policy import effects_for_actuation

        effects = effects_for_actuation(step, result.actuation)
        if len(result.effect_contract_hashes) != len(effects):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        evidence_hashes = Counter(
            item.effect_contract_hash
            for item in result.effect_evidence
            if item.final_verdict == "confirmed"
            and item.verification_tier is not None
            and VerificationTier(item.verification_tier).satisfies(minimum)
        )
        if not result.effect_contract_hashes or evidence_hashes != Counter(
            result.effect_contract_hashes
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
    return ExecutionOutcome.VERIFIED


def stamp_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> ExecutionOutcome:
    """Write the profile and precise outcome into ``report``."""

    resolved = resolve_execution_profile(profile)
    report.external_network_calls = _external_network_call_state(report)
    if report.execution_completed is None:
        report.execution_completed = report.success
    outcome = classify_execution_outcome(report, workflow, resolved)
    report.execution_profile = resolved.value
    report.execution_outcome = outcome.value
    report.production_eligible = bool(
        execution_profile_contract(resolved).production
        and outcome is ExecutionOutcome.VERIFIED
    )
    if execution_profile_contract(resolved).production:
        report.success = outcome is ExecutionOutcome.VERIFIED
    elif outcome is ExecutionOutcome.ROLLED_BACK:
        report.success = False
    report.outcome_envelope = build_outcome_envelope(report, workflow)
    # Section 3: refine the coarse outcome into a first-class terminal
    # transaction outcome + effect journal. Additive -- reads the fields set
    # above and never mutates them (leaf import; see openadapt_flow.transaction).
    from openadapt_flow.transaction import stamp_transaction_outcome

    stamp_transaction_outcome(report, workflow)
    return outcome


def _completed_compensation_actions(report: RunReport) -> int:
    """Count only compensations that completed and were re-verified."""

    return sum(
        evidence.reconciliation_actions
        for result in report.results
        for evidence in result.effect_evidence
        if (
            evidence.reconciliation_completed
            and evidence.reconciliation_actions > 0
            and evidence.final_verdict == "confirmed"
        )
    )


def build_outcome_envelope(report: RunReport, workflow: Workflow):
    """Build the versioned PHI-free evidence summary for ``report``.

    Counts are derived from typed workflow/report fields only.  Free-text
    intents, parameters, identifiers, effect hashes, and observed values never
    enter the envelope.
    """

    from openadapt_flow.ir import (
        ExecutionOutcomeEnvelope,
        OutcomeContractCounts,
        OutcomeEvidenceClass,
    )
    from openadapt_flow.traversal import iter_workflow_steps

    if report.execution_outcome is None:
        raise ValueError("execution outcome must be classified before enveloping")

    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    production = report.execution_profile in {"standard", "regulated"}

    required_authorization = 1 if production else 0
    passed_authorization = int(
        bool(
            required_authorization
            and report.governed_authorization_id
            and report.governed_runtime_inputs_digest
        )
    )

    required_identity_ids = set(report.required_identity_step_ids)
    identity_results = [
        result
        for result in report.results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ]
    required_identity = len(identity_results)
    passed_identity = sum(
        result.identity is not None and result.identity.status == "verified"
        for result in identity_results
    )

    required_postconditions = 0
    passed_postconditions = 0
    required_effects = 0
    passed_effects = 0
    evidence_classes: set[OutcomeEvidenceClass] = set()
    effect_class_by_tier: dict[int, OutcomeEvidenceClass] = {
        1: "effect_tier_1",
        2: "effect_tier_2",
        3: "effect_tier_3",
        4: "effect_tier_4",
    }
    minimum_effect_tier = (
        required_effect_tier(workflow, report.execution_profile)
        if report.execution_profile is not None
        else None
    )
    compensation_actions = _completed_compensation_actions(report)
    for result in report.results:
        if result.skipped or result.exception_handled:
            continue
        step = steps_by_id.get(result.step_id)
        if step is not None:
            postcondition_count = len(step.expect)
            required_postconditions += postcondition_count
            if result.postconditions_ok is True:
                passed_postconditions += postcondition_count
            from openadapt_flow.policy import effects_for_actuation

            effects = effects_for_actuation(step, result.actuation)
            required_effects += len(effects)
        if result.effect_verified is True:
            sufficient_evidence = Counter(
                evidence.effect_contract_hash
                for evidence in result.effect_evidence
                if evidence.final_verdict == "confirmed"
                and (
                    minimum_effect_tier is None
                    or (
                        evidence.verification_tier is not None
                        and VerificationTier(evidence.verification_tier).satisfies(
                            minimum_effect_tier
                        )
                    )
                )
            )
            passed_effects += sum(
                (sufficient_evidence & Counter(result.effect_contract_hashes)).values()
            )
        if result.identity is not None and result.identity.status == "verified":
            evidence_classes.add("identity")
        if result.postconditions_ok is True and step is not None and step.expect:
            evidence_classes.add("postcondition")
        for evidence in result.effect_evidence:
            if (
                evidence.final_verdict == "confirmed"
                and evidence.verification_tier is not None
            ):
                evidence_class = effect_class_by_tier.get(evidence.verification_tier)
                if evidence_class is not None:
                    evidence_classes.add(evidence_class)
            if (
                evidence.reconciliation_completed
                and evidence.reconciliation_actions > 0
                and evidence.final_verdict == "confirmed"
            ):
                evidence_classes.add("compensation")

    required = OutcomeContractCounts(
        authorization=required_authorization,
        identity=required_identity,
        postcondition=required_postconditions,
        effect=required_effects,
    )
    passed = OutcomeContractCounts(
        authorization=passed_authorization,
        identity=min(passed_identity, required_identity),
        postcondition=min(passed_postconditions, required_postconditions),
        effect=min(passed_effects, required_effects),
    )
    if passed.authorization:
        evidence_classes.add("authorization")
    if report.model_calls:
        evidence_classes.add("model")

    return ExecutionOutcomeEnvelope(
        outcome=report.execution_outcome,
        profile=report.execution_profile,
        production_eligible=report.production_eligible,
        execution_completed=bool(report.execution_completed),
        required_contracts=required,
        passed_contracts=passed,
        evidence_classes=sorted(evidence_classes),
        model_calls=report.model_calls,
        external_network_calls=report.external_network_calls,
        compensation_actions=compensation_actions,
    )


def _external_network_call_state(
    report: RunReport,
) -> Literal["none", "observed", "unknown"]:
    """Report observed egress without turning absence of instrumentation into 0."""

    if report.external_network_calls == "observed" or report.model_calls > 0:
        return "observed"
    if report.execution_origin or report.execution_entry_url:
        return "observed"
    if report.execution_target_kind in {"web", "rdp", "citrix"}:
        return "observed"

    local_substrates = {
        "onscreen",
        "file",
        "document_hash",
        "snapshot",
        "test",
        "fake",
    }
    for result in report.results:
        if result.actuation == "api":
            return "observed"
        for evidence in result.effect_evidence:
            substrate = evidence.substrate.strip().lower()
            if substrate in {"rest", "fhir", "sftp", "http", "https"}:
                return "observed"
            if substrate not in local_substrates:
                return "unknown"
    # A native target says where input was delivered, not whether this process
    # or one of its integrations opened a socket. Until an explicit network
    # observer proves the negative, absence of an observed call remains unknown.
    return report.external_network_calls
