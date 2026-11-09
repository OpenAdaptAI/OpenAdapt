"""Governed repair promotion lifecycle (roadmap Section 9).

A proposed repair (a healed or taught bundle) is never the active bundle just
because it was written to disk. It enters an explicit, auditable lifecycle:

    candidate -> reviewed -> replay_passed -> fault_passed -> approved
              -> staged -> canary -> active

with hard, fail-closed gates between the states:

- a :class:`RepairCandidate` records WHAT changed (the binding diff), WHY the
  old binding failed (privacy-safe failure fingerprints plus evidence refs),
  and what evidence supports the new binding. It never auto-activates.
- a candidate must pass a REPLAY campaign (healthy cases, the deterministic
  drift battery) and a FAULT campaign (ambiguity, wrong entity, stale target,
  unexpected dialog, verifier failure) before it may be approved.
- a candidate may NEVER weaken the identity, effect, risk, environment, or
  policy contract without an explicit new qualification revision
  (:mod:`openadapt_flow.repair.invariants`; hard refusal, fail closed).
- promotion requires HUMAN approval; activation is an atomic bundle swap by
  content hash with retained lineage and one-command rollback
  (:mod:`openadapt_flow.repair.lifecycle`).
- the CANARY state bounds the newly activated bundle to a first-N runs window
  with per-run verification; any silent-incorrect or verification regression
  automatically halts back to ``staged`` and reverts the active pointer.
- a runtime MODEL SUGGESTION can never actuate a repair or promote itself:
  every lifecycle transition asserts the acting party is not a model
  (:func:`openadapt_flow.repair.lifecycle.assert_not_model_actor`).

See ``docs/REPAIR_LIFECYCLE.md`` for the state machine and CLI reference.
"""

from openadapt_flow.repair.campaign import (  # noqa: F401
    FAULT_BATTERY_KINDS,
    FaultCase,
    FaultKind,
    fault_battery,
    run_fault_campaign,
    run_replay_campaign,
)
from openadapt_flow.repair.candidate import (  # noqa: F401
    Actor,
    ApprovalRecord,
    BindingChange,
    CampaignCaseResult,
    CampaignResult,
    CanaryConfig,
    CanaryMetrics,
    CanaryRunObservation,
    ContractWeakening,
    FailureFingerprint,
    RepairCandidate,
    RepairState,
    TransitionRecord,
    candidate_id_for,
)
from openadapt_flow.repair.invariants import (  # noqa: F401
    RepairInvariantError,
    check_contract_invariants,
    enforce_contract_invariants,
)
from openadapt_flow.repair.lifecycle import (  # noqa: F401
    ActivePointer,
    ModelActuationError,
    RepairLifecycleError,
    RepairStore,
    assert_not_model_actor,
    observation_from_report,
)
from openadapt_flow.repair.registration import (  # noqa: F401
    DETACHED_CANDIDATE_RELPATH,
    detached_candidate_path,
    register_bundle_candidate,
)

__all__ = [
    # candidate
    "RepairCandidate",
    "RepairState",
    "Actor",
    "ApprovalRecord",
    "BindingChange",
    "CampaignCaseResult",
    "CampaignResult",
    "CanaryConfig",
    "CanaryMetrics",
    "CanaryRunObservation",
    "ContractWeakening",
    "FailureFingerprint",
    "TransitionRecord",
    "candidate_id_for",
    # invariants
    "RepairInvariantError",
    "check_contract_invariants",
    "enforce_contract_invariants",
    # campaigns
    "FaultKind",
    "FaultCase",
    "FAULT_BATTERY_KINDS",
    "fault_battery",
    "run_replay_campaign",
    "run_fault_campaign",
    # lifecycle
    "RepairStore",
    "ActivePointer",
    "RepairLifecycleError",
    "ModelActuationError",
    "assert_not_model_actor",
    "observation_from_report",
    # registration
    "register_bundle_candidate",
    "detached_candidate_path",
    "DETACHED_CANDIDATE_RELPATH",
]
