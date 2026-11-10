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


# Annotation-only declarations keep deferred names visible to static analysis.
# No value is bound until __getattr__ loads and memoizes that exact export.
ApprovalRecord: Any
ApprovalRequired: Any
BundleMismatch: Any
PauseExpired: Any
ResumeRefused: Any
StateDiverged: Any
approval_pause_digest: Any
enforce_resume_authorization: Any
issue_resume_approval: Any
AttendedActionRefused: Any
AttendedActionRequest: Any
AttendedActionStore: Any
AttendedDecision: Any
AttendedExecutionResult: Any
AttendedPauseCapability: Any
AttendedRelayBinding: Any
BoundAttendedExecutor: Any
SignedTransitionBaseline: Any
TransitionObservation: Any
execute_attended_action: Any
issue_attended_capability: Any
AttendedActionService: Any
AttendedExecutorTimeout: Any
BusinessDecisionPrincipal: Any
BusinessDecisionReceipt: Any
BusinessDecisionRefused: Any
BusinessDecisionRequest: Any
BusinessDecisionStore: Any
BusinessDecisionSubmission: Any
submit_business_decision: Any
CheckpointStore: Any
PendingEscalation: Any
RunCheckpoint: Any
RunManifest: Any
DurableRun: Any
classify_halt: Any
resumed_step_results: Any
TOP_GRAPH_ID: Any
GraphFrame: Any
LoopCursor: Any
ProgramCheckpoint: Any
ProgramTransitionReceipt: Any
bundle_version: Any
control_frames_hash: Any
resume: Any
resume_point: Any


def __getattr__(name: str) -> Any:
    global ApprovalRecord
    global ApprovalRequired
    global BundleMismatch
    global PauseExpired
    global ResumeRefused
    global StateDiverged
    global approval_pause_digest
    global enforce_resume_authorization
    global issue_resume_approval
    global AttendedActionRefused
    global AttendedActionRequest
    global AttendedActionStore
    global AttendedDecision
    global AttendedExecutionResult
    global AttendedPauseCapability
    global AttendedRelayBinding
    global BoundAttendedExecutor
    global SignedTransitionBaseline
    global TransitionObservation
    global execute_attended_action
    global issue_attended_capability
    global AttendedActionService
    global AttendedExecutorTimeout
    global BusinessDecisionPrincipal
    global BusinessDecisionReceipt
    global BusinessDecisionRefused
    global BusinessDecisionRequest
    global BusinessDecisionStore
    global BusinessDecisionSubmission
    global submit_business_decision
    global CheckpointStore
    global PendingEscalation
    global RunCheckpoint
    global RunManifest
    global DurableRun
    global classify_halt
    global resumed_step_results
    global TOP_GRAPH_ID
    global GraphFrame
    global LoopCursor
    global ProgramCheckpoint
    global ProgramTransitionReceipt
    global bundle_version
    global control_frames_hash
    global resume
    global resume_point

    # Literal imports retain deferred loading even when an export shadows
    # its submodule (for example durable.resume). Named globals keep
    # normal package memoization without exposing a globals() mapping.
    from importlib import import_module

    if name == "ApprovalRecord":
        ApprovalRecord = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).ApprovalRecord
        return ApprovalRecord
    if name == "ApprovalRequired":
        ApprovalRequired = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).ApprovalRequired
        return ApprovalRequired
    if name == "BundleMismatch":
        BundleMismatch = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).BundleMismatch
        return BundleMismatch
    if name == "PauseExpired":
        PauseExpired = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).PauseExpired
        return PauseExpired
    if name == "ResumeRefused":
        ResumeRefused = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).ResumeRefused
        return ResumeRefused
    if name == "StateDiverged":
        StateDiverged = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).StateDiverged
        return StateDiverged
    if name == "approval_pause_digest":
        approval_pause_digest = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).approval_pause_digest
        return approval_pause_digest
    if name == "enforce_resume_authorization":
        enforce_resume_authorization = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).enforce_resume_authorization
        return enforce_resume_authorization
    if name == "issue_resume_approval":
        issue_resume_approval = import_module(
            "openadapt_flow.runtime.durable.approval"
        ).issue_resume_approval
        return issue_resume_approval
    if name == "AttendedActionRefused":
        AttendedActionRefused = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedActionRefused
        return AttendedActionRefused
    if name == "AttendedActionRequest":
        AttendedActionRequest = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedActionRequest
        return AttendedActionRequest
    if name == "AttendedActionStore":
        AttendedActionStore = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedActionStore
        return AttendedActionStore
    if name == "AttendedDecision":
        AttendedDecision = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedDecision
        return AttendedDecision
    if name == "AttendedExecutionResult":
        AttendedExecutionResult = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedExecutionResult
        return AttendedExecutionResult
    if name == "AttendedPauseCapability":
        AttendedPauseCapability = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedPauseCapability
        return AttendedPauseCapability
    if name == "AttendedRelayBinding":
        AttendedRelayBinding = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).AttendedRelayBinding
        return AttendedRelayBinding
    if name == "BoundAttendedExecutor":
        BoundAttendedExecutor = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).BoundAttendedExecutor
        return BoundAttendedExecutor
    if name == "SignedTransitionBaseline":
        SignedTransitionBaseline = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).SignedTransitionBaseline
        return SignedTransitionBaseline
    if name == "TransitionObservation":
        TransitionObservation = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).TransitionObservation
        return TransitionObservation
    if name == "execute_attended_action":
        execute_attended_action = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).execute_attended_action
        return execute_attended_action
    if name == "issue_attended_capability":
        issue_attended_capability = import_module(
            "openadapt_flow.runtime.durable.attended"
        ).issue_attended_capability
        return issue_attended_capability
    if name == "AttendedActionService":
        AttendedActionService = import_module(
            "openadapt_flow.runtime.durable.attended_service"
        ).AttendedActionService
        return AttendedActionService
    if name == "AttendedExecutorTimeout":
        AttendedExecutorTimeout = import_module(
            "openadapt_flow.runtime.durable.attended_service"
        ).AttendedExecutorTimeout
        return AttendedExecutorTimeout
    if name == "BusinessDecisionPrincipal":
        BusinessDecisionPrincipal = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionPrincipal
        return BusinessDecisionPrincipal
    if name == "BusinessDecisionReceipt":
        BusinessDecisionReceipt = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionReceipt
        return BusinessDecisionReceipt
    if name == "BusinessDecisionRefused":
        BusinessDecisionRefused = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionRefused
        return BusinessDecisionRefused
    if name == "BusinessDecisionRequest":
        BusinessDecisionRequest = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionRequest
        return BusinessDecisionRequest
    if name == "BusinessDecisionStore":
        BusinessDecisionStore = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionStore
        return BusinessDecisionStore
    if name == "BusinessDecisionSubmission":
        BusinessDecisionSubmission = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).BusinessDecisionSubmission
        return BusinessDecisionSubmission
    if name == "submit_business_decision":
        submit_business_decision = import_module(
            "openadapt_flow.runtime.durable.business_decision"
        ).submit_business_decision
        return submit_business_decision
    if name == "CheckpointStore":
        CheckpointStore = import_module(
            "openadapt_flow.runtime.durable.checkpoint"
        ).CheckpointStore
        return CheckpointStore
    if name == "PendingEscalation":
        PendingEscalation = import_module(
            "openadapt_flow.runtime.durable.checkpoint"
        ).PendingEscalation
        return PendingEscalation
    if name == "RunCheckpoint":
        RunCheckpoint = import_module(
            "openadapt_flow.runtime.durable.checkpoint"
        ).RunCheckpoint
        return RunCheckpoint
    if name == "RunManifest":
        RunManifest = import_module(
            "openadapt_flow.runtime.durable.checkpoint"
        ).RunManifest
        return RunManifest
    if name == "DurableRun":
        DurableRun = import_module(
            "openadapt_flow.runtime.durable.controller"
        ).DurableRun
        return DurableRun
    if name == "classify_halt":
        classify_halt = import_module(
            "openadapt_flow.runtime.durable.controller"
        ).classify_halt
        return classify_halt
    if name == "resumed_step_results":
        resumed_step_results = import_module(
            "openadapt_flow.runtime.durable.controller"
        ).resumed_step_results
        return resumed_step_results
    if name == "TOP_GRAPH_ID":
        TOP_GRAPH_ID = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).TOP_GRAPH_ID
        return TOP_GRAPH_ID
    if name == "GraphFrame":
        GraphFrame = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).GraphFrame
        return GraphFrame
    if name == "LoopCursor":
        LoopCursor = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).LoopCursor
        return LoopCursor
    if name == "ProgramCheckpoint":
        ProgramCheckpoint = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).ProgramCheckpoint
        return ProgramCheckpoint
    if name == "ProgramTransitionReceipt":
        ProgramTransitionReceipt = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).ProgramTransitionReceipt
        return ProgramTransitionReceipt
    if name == "bundle_version":
        bundle_version = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).bundle_version
        return bundle_version
    if name == "control_frames_hash":
        control_frames_hash = import_module(
            "openadapt_flow.runtime.durable.program_checkpoint"
        ).control_frames_hash
        return control_frames_hash
    if name == "resume":
        resume = import_module("openadapt_flow.runtime.durable.resume").resume
        return resume
    if name == "resume_point":
        resume_point = import_module(
            "openadapt_flow.runtime.durable.resume"
        ).resume_point
        return resume_point
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
