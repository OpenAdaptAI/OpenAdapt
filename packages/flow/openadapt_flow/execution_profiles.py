"""Named execution profiles over OpenAdapt's existing governed runtime.

Profiles do not implement a second policy or replay path.  They select which
requirements the existing run gate must enforce, whether the shared replayer
must be durable, and how the resulting report may be described.

The low-level controls remain available for embedding and backwards
compatibility.  Production callers can choose one reviewed profile instead of
assembling a potentially contradictory collection of permissive flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

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
    require_independent_effects: bool
    require_approval_for_unverified_effects: bool
    allow_unverified_write_approval: bool
    require_encryption: bool
    strict_templates: bool
    require_durable: bool
    default_policy: str | None


_CONTRACTS = {
    ExecutionProfile.DEMO: ExecutionProfileContract(
        profile=ExecutionProfile.DEMO,
        production=False,
        require_certification=False,
        require_identity_coverage=False,
        require_effect_contracts=False,
        require_independent_effects=False,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=True,
        require_encryption=False,
        strict_templates=False,
        require_durable=False,
        default_policy=None,
    ),
    ExecutionProfile.STANDARD: ExecutionProfileContract(
        profile=ExecutionProfile.STANDARD,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        require_independent_effects=True,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=False,
        strict_templates=False,
        require_durable=True,
        default_policy="clinical-write",
    ),
    ExecutionProfile.REGULATED: ExecutionProfileContract(
        profile=ExecutionProfile.REGULATED,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        require_independent_effects=True,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=True,
        strict_templates=True,
        require_durable=True,
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


def classify_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> ExecutionOutcome:
    """Classify a completed report without changing legacy ``success``.

    Demo success is always visibly non-production.  Standard and Regulated
    success becomes ``VERIFIED`` only when every executed consequential action
    has an independently confirmed effect.  Therefore an approved-unverified
    or screen-only consequential result can never be reported as ``VERIFIED``
    under either production profile.
    """

    resolved = resolve_execution_profile(profile)
    if not report.success:
        refusal_step_ids = {"<authorization>", "<params>", "<profile>"}
        governed_halt = (
            report.halt is not None
            or report.terminal_outcome in {"halt", "escalate"}
            or any(result.safety_halt for result in report.results)
            or any(result.step_id in refusal_step_ids for result in report.results)
        )
        return ExecutionOutcome.HALTED if governed_halt else ExecutionOutcome.FAILED

    if resolved is ExecutionProfile.DEMO:
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    # Import lazily: run_gate imports this module for the profile contract.
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    consequential = {
        step.id for step in iter_workflow_steps(workflow) if is_consequential(step)
    }
    for result in report.results:
        if result.skipped or result.step_id not in consequential:
            continue
        if result.effect_approved_unverified or result.effect_verified is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
    return ExecutionOutcome.VERIFIED


def stamp_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> ExecutionOutcome:
    """Write the profile and precise outcome into ``report``."""

    resolved = resolve_execution_profile(profile)
    outcome = classify_execution_outcome(report, workflow, resolved)
    report.execution_profile = resolved.value
    report.execution_outcome = outcome.value
    report.production_eligible = bool(
        execution_profile_contract(resolved).production
        and outcome is ExecutionOutcome.VERIFIED
    )
    return outcome
