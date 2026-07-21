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

from openadapt_flow.runtime.durable.approval import (  # noqa: F401
    ApprovalRecord,
    ApprovalRequired,
    BundleMismatch,
    PauseExpired,
    ResumeRefused,
    StateDiverged,
    enforce_resume_authorization,
)
from openadapt_flow.runtime.durable.attended import (  # noqa: F401
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    AttendedDecision,
    AttendedExecutionResult,
    AttendedPauseCapability,
    BoundAttendedExecutor,
    SignedTransitionBaseline,
    TransitionObservation,
    execute_attended_action,
    issue_attended_capability,
)
from openadapt_flow.runtime.durable.attended_service import (  # noqa: F401
    AttendedActionService,
    AttendedExecutorTimeout,
)
from openadapt_flow.runtime.durable.checkpoint import (  # noqa: F401
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.controller import (  # noqa: F401
    DurableRun,
    classify_halt,
    resumed_step_results,
)
from openadapt_flow.runtime.durable.program_checkpoint import (  # noqa: F401
    TOP_GRAPH_ID,
    GraphFrame,
    LoopCursor,
    ProgramCheckpoint,
    ProgramTransitionReceipt,
    bundle_version,
    control_frames_hash,
)
from openadapt_flow.runtime.durable.resume import (  # noqa: F401
    resume,
    resume_point,
)

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
    "AttendedPauseCapability",
    "BoundAttendedExecutor",
    "SignedTransitionBaseline",
    "TransitionObservation",
    "execute_attended_action",
    "issue_attended_capability",
    "enforce_resume_authorization",
    "DurableRun",
    "classify_halt",
    "resumed_step_results",
    "resume",
    "resume_point",
]
