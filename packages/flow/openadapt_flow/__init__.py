"""openadapt-flow: record, compile, replay deterministically, heal on drift."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "3.4.0"

# Strict no-outbound mode follows the package into child interpreters, so a
# Python child that imports openadapt_flow is refused off-machine connections
# like its parent (docs/OFFLINE_SEALS.md). This stays eager: it runs before any
# other Flow code.
from openadapt_flow import no_outbound as _no_outbound

if TYPE_CHECKING:
    from openadapt_flow.ir import (
        ActionKind,
        Anchor,
        HealEvent,
        Landmark,
        Postcondition,
        PostconditionKind,
        Resolution,
        RunReport,
        Step,
        StepResult,
        UnarmedStep,
        Workflow,
    )


def __getattr__(name: str) -> Any:
    # The ir re-exports load on first use, so importing a small module such as
    # ``openadapt_flow.crypto`` no longer loads the whole schema and everything
    # ``ir`` imports. Each branch is a plain import, so the import-layer checker
    # (tests/test_import_layers.py) sees every edge.
    if name == "ActionKind":
        from openadapt_flow.ir import ActionKind

        return ActionKind
    if name == "Anchor":
        from openadapt_flow.ir import Anchor

        return Anchor
    if name == "HealEvent":
        from openadapt_flow.ir import HealEvent

        return HealEvent
    if name == "Landmark":
        from openadapt_flow.ir import Landmark

        return Landmark
    if name == "Postcondition":
        from openadapt_flow.ir import Postcondition

        return Postcondition
    if name == "PostconditionKind":
        from openadapt_flow.ir import PostconditionKind

        return PostconditionKind
    if name == "Resolution":
        from openadapt_flow.ir import Resolution

        return Resolution
    if name == "RunReport":
        from openadapt_flow.ir import RunReport

        return RunReport
    if name == "Step":
        from openadapt_flow.ir import Step

        return Step
    if name == "StepResult":
        from openadapt_flow.ir import StepResult

        return StepResult
    if name == "UnarmedStep":
        from openadapt_flow.ir import UnarmedStep

        return UnarmedStep
    if name == "Workflow":
        from openadapt_flow.ir import Workflow

        return Workflow
    # Importing the package used to load ``ir`` and the modules it imports, so
    # these submodules were attributes of the package with no import of their
    # own. Keep that working: each loads on first access. ``import a.b as c``
    # sets the package attribute before reading it, so this never recurses.
    # Four of these (approval_mode, ir, locale_invariants, runtime) are listed
    # in import_layers_debt.json: the checker cannot prove what a returned
    # module object exposes.
    if name == "_browser_readiness":
        import openadapt_flow._browser_readiness as browser_readiness_module

        return browser_readiness_module
    if name == "action_evidence":
        import openadapt_flow.action_evidence as action_evidence_module

        return action_evidence_module
    if name == "approval_mode":
        import openadapt_flow.approval_mode as approval_mode_module

        return approval_mode_module
    if name == "assurance":
        import openadapt_flow.assurance as assurance_module

        return assurance_module
    if name == "check_level":
        import openadapt_flow.check_level as check_level_module

        return check_level_module
    if name == "decision_delivery":
        import openadapt_flow.decision_delivery as decision_delivery_module

        return decision_delivery_module
    if name == "execution_profiles":
        import openadapt_flow.execution_profiles as execution_profiles_module

        return execution_profiles_module
    if name == "fhir_read":
        import openadapt_flow.fhir_read as fhir_read_module

        return fhir_read_module
    if name == "identity_signals":
        import openadapt_flow.identity_signals as identity_signals_module

        return identity_signals_module
    if name == "input_evidence":
        import openadapt_flow.input_evidence as input_evidence_module

        return input_evidence_module
    if name == "ir":
        import openadapt_flow.ir as ir_module

        return ir_module
    if name == "job_authoring":
        import openadapt_flow.job_authoring as job_authoring_module

        return job_authoring_module
    if name == "judgment_cases":
        import openadapt_flow.judgment_cases as judgment_cases_module

        return judgment_cases_module
    if name == "locale_invariants":
        import openadapt_flow.locale_invariants as locale_invariants_module

        return locale_invariants_module
    if name == "lookup_capabilities":
        import openadapt_flow.lookup_capabilities as lookup_capabilities_module

        return lookup_capabilities_module
    if name == "network_scope":
        import openadapt_flow.network_scope as network_scope_module

        return network_scope_module
    if name == "private_file":
        import openadapt_flow.private_file as private_file_module

        return private_file_module
    if name == "qualification":
        import openadapt_flow.qualification as qualification_module

        return qualification_module
    if name == "qualification_admission":
        import openadapt_flow.qualification_admission as qualification_admission_module

        return qualification_admission_module
    if name == "qualification_faults":
        import openadapt_flow.qualification_faults as qualification_faults_module

        return qualification_faults_module
    if name == "qualified_business_decisions":
        import openadapt_flow.qualified_business_decisions as qualified_business_decisions_module

        return qualified_business_decisions_module
    if name == "records":
        import openadapt_flow.records as records_module

        return records_module
    if name == "run_invocation":
        import openadapt_flow.run_invocation as run_invocation_module

        return run_invocation_module
    if name == "runtime":
        import openadapt_flow.runtime as runtime_module

        return runtime_module
    if name == "traversal":
        import openadapt_flow.traversal as traversal_module

        return traversal_module
    if name == "verification":
        import openadapt_flow.verification as verification_module

        return verification_module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ActionKind",
    "Anchor",
    "HealEvent",
    "Landmark",
    "Postcondition",
    "PostconditionKind",
    "Resolution",
    "RunReport",
    "Step",
    "StepResult",
    "UnarmedStep",
    "Workflow",
]

_no_outbound.enforce_from_env()
