"""Durable tiered runtime (RFC §5, Tier 3): checkpoint + pause/approve/resume.

The escalation tier of the Workflow-Program IR runtime
(``docs/design/WORKFLOW_PROGRAM_IR.md`` §5). Where the deterministic fast path
and the bounded local recovery cannot safely proceed, a production run must not
just die: it must **durably pause at the last verified checkpoint, persist why
it paused and the proposed operator options, and RESUME from that checkpoint**
after AUTHENTICATED approval -- never from step 0, and never by handing the
remaining workflow to a free-form agent.

Two checkpoint flavors:

- Linear ``steps`` runs checkpoint on a step index (:class:`RunCheckpoint`).
- Phase-2 PROGRAM runs checkpoint the whole INTERPRETER STATE -- the frame
  stack, loop cursors, bound params, and completed effect keys
  (:class:`ProgramCheckpoint`) -- so a resume RESTORES the interpreter rather
  than translating to a step index (which cannot express a loop cursor).

Resume is an authenticated approval workflow (P0-5): it requires an
:class:`ApprovalRecord` (approver / timestamp / resolution / bundle version),
revalidates the live app is still in the checkpoint's expected state, and
refuses a stale (expired) pause.

Import-light by design (pydantic + json + pathlib): no vision, no backend, no
model call.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    name: "openadapt_flow.runtime.durable.approval"
    for name in (
        "ApprovalRecord",
        "ApprovalRequired",
        "BundleMismatch",
        "PauseExpired",
        "ResumeRefused",
        "StateDiverged",
        "approval_pause_digest",
        "enforce_resume_authorization",
        "issue_resume_approval",
    )
}
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.attended"
        for name in (
            "AttendedActionRefused",
            "AttendedActionRequest",
            "AttendedActionStore",
            "AttendedDecision",
            "AttendedExecutionResult",
            "AttendedPauseCapability",
            "AttendedRelayBinding",
            "BoundAttendedExecutor",
            "SignedTransitionBaseline",
            "TransitionObservation",
            "execute_attended_action",
            "issue_attended_capability",
        )
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.attended_service"
        for name in ("AttendedActionService", "AttendedExecutorTimeout")
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.business_decision"
        for name in (
            "BusinessDecisionPrincipal",
            "BusinessDecisionReceipt",
            "BusinessDecisionRefused",
            "BusinessDecisionRequest",
            "BusinessDecisionStore",
            "BusinessDecisionSubmission",
            "submit_business_decision",
        )
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.checkpoint"
        for name in (
            "CheckpointStore",
            "PendingEscalation",
            "RunCheckpoint",
            "RunManifest",
        )
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.controller"
        for name in ("DurableRun", "classify_halt", "resumed_step_results")
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.program_checkpoint"
        for name in (
            "TOP_GRAPH_ID",
            "GraphFrame",
            "LoopCursor",
            "ProgramCheckpoint",
            "ProgramTransitionReceipt",
            "bundle_version",
            "control_frames_hash",
        )
    }
)
_LAZY_EXPORTS.update(
    {
        name: "openadapt_flow.runtime.durable.resume"
        for name in ("resume", "resume_point")
    }
)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "CheckpointStore",
    "PendingEscalation",
    "RunCheckpoint",
    "RunManifest",
    "ProgramCheckpoint",
    "ProgramTransitionReceipt",
    "GraphFrame",
    "LoopCursor",
    "TOP_GRAPH_ID",
    "bundle_version",
    "control_frames_hash",
    "ApprovalRecord",
    "ApprovalRequired",
    "BundleMismatch",
    "PauseExpired",
    "ResumeRefused",
    "StateDiverged",
    "AttendedActionRefused",
    "AttendedActionRequest",
    "AttendedActionStore",
    "AttendedDecision",
    "AttendedExecutionResult",
    "AttendedActionService",
    "AttendedExecutorTimeout",
    "BusinessDecisionPrincipal",
    "BusinessDecisionReceipt",
    "BusinessDecisionRefused",
    "BusinessDecisionRequest",
    "BusinessDecisionStore",
    "BusinessDecisionSubmission",
    "AttendedPauseCapability",
    "BoundAttendedExecutor",
    "SignedTransitionBaseline",
    "TransitionObservation",
    "execute_attended_action",
    "issue_attended_capability",
    "submit_business_decision",
    "approval_pause_digest",
    "enforce_resume_authorization",
    "issue_resume_approval",
    "DurableRun",
    "classify_halt",
    "resumed_step_results",
    "resume",
    "resume_point",
]
