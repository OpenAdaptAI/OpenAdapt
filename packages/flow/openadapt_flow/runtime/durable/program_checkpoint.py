"""Durable checkpoint for the Phase-2 ProgramGraph interpreter (RFC §5, Tier 3).

The linear replayer's :class:`~.checkpoint.RunCheckpoint` keys a resume on a
single ``step_index`` -- adequate for a straight-line ``steps`` list, useless for
the Phase-2 STATE MACHINE (``docs/design/WORKFLOW_PROGRAM_IR.md`` §2), whose
"where am I" is not an integer but an INTERPRETER STATE: which state is current,
the stack of subflow / loop-body graphs we descended through, each loop's cursor
into its worklist, the parameter bindings in scope, and which consequential
effects have already CONFIRMED (so a resume never re-performs a write).

This module adds the durable form of that interpreter state so a program run can
pause at its last verified state and RESUME by RESTORING the interpreter -- not
by translating to a linear step index (which cannot represent a loop cursor or a
subflow return).

Import-light (pydantic + hashlib + pathlib): no vision, no backend, no model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: The synthetic ``graph_id`` of the top-level ``Workflow.program`` graph (every
#: OTHER graph is a named entry in ``Workflow.subflows`` -- including a loop
#: body, whose id is ``LoopSpec.body``). Resume resolves a frame's graph by this
#: sentinel or by subflow name.
TOP_GRAPH_ID = "__program__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bundle_version(bundle_dir: Path | str) -> str:
    """A stable content hash of a bundle's ``workflow.json``.

    Recorded on every :class:`ProgramCheckpoint` and re-computed at resume time:
    a bundle edited between pause and resume changes this digest, so resume can
    REFUSE to restore an interpreter state captured against a different program
    (RFC §5: resume is deterministic against the SAME compiled program).

    For an ENCRYPTED bundle (``workflow.json.enc``, no plaintext ``workflow.json``)
    the digest is taken over the on-disk ciphertext container: it is stable for
    an unchanged bundle across a pause/resume cycle (nothing re-saves it in
    between), and any re-save -- which is the only way the program changes --
    reseals it to a new container, so the change-detection semantics hold without
    needing the decryption key here.
    """
    bundle = Path(bundle_dir)
    path = bundle / "workflow.json"
    if not path.is_file():
        path = bundle / "workflow.json.enc"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def history_hash(visited_states: list[str]) -> str:
    """A rolling digest of the ordered state ids visited so far.

    Persisted so an auditor (and a resume) can tell that the interpreter walked
    the demonstrated control-flow path up to the pause -- a divergent transition
    history yields a different digest.
    """
    joined = "␟".join(visited_states)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


class LoopCursor(BaseModel):
    """A ``loop`` state's position in its worklist at checkpoint time (RFC §2.3).

    Carried on the loop-body :class:`GraphFrame` so a resume that paused mid-loop
    finishes the IN-PROGRESS row (the leaf frame) and then runs the REMAINING
    rows (``rows[row_index + 1:]``) -- never re-running an already-completed
    row's consequential writes, and never dropping the tail of the queue.
    """

    #: Id of the ``loop`` state in the PARENT graph.
    loop_state_id: str
    #: The worklist relation name (audit / diagnostics).
    relation: str
    #: Index (0-based) of the row whose body is executing at checkpoint time.
    row_index: int
    #: The fully-resolved worklist rows the loop is iterating. Frozen at loop
    #: entry so a resume replays the SAME queue (a run-time worklist is not
    #: otherwise reconstructible), keeping iteration bounded and deterministic.
    rows: list[dict[str, str]] = Field(default_factory=list)


class GraphFrame(BaseModel):
    """One frame of the interpreter's graph/subflow/loop stack.

    Frames are ordered OUTER -> INNER (index 0 is the top ``program`` graph). The
    LEAF frame's :attr:`state_id` is the last VERIFIED state (the resume
    re-drives from its successor); each ANCESTOR frame's :attr:`state_id` is the
    ``subflow_call`` / ``loop`` state whose body the next inner frame is running
    (the resume continues the parent AFTER that call/loop once the child
    completes).
    """

    #: :data:`TOP_GRAPH_ID` for the top program, else the subflow name (a
    #: ``loop`` body is a subflow, keyed by ``LoopSpec.body``).
    graph_id: str
    #: The active state id within this graph (see class docstring).
    state_id: str
    #: The parameter bindings in scope for this graph frame (a loop body frame's
    #: scope is the parent's params merged with the current row).
    params: dict[str, str] = Field(default_factory=dict)
    #: Present iff this frame is a loop-body iteration -- the loop's cursor.
    loop: Optional[LoopCursor] = None


def control_frames_hash(frames: list[GraphFrame]) -> str:
    """Stable digest of an exact interpreter cursor/control-frame stack."""
    payload = [frame.model_dump(mode="json") for frame in frames]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class ProgramTransitionReceipt(BaseModel):
    """Exact, idempotent attended transition admitted at a program pause.

    The human-completed action is represented by a new verified-state
    checkpoint instead of being re-actuated. This receipt binds that checkpoint
    to the signed pause/cursor and to one selected successor (including an
    explicit ``None`` fall-off/return).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    workflow_name: str
    bundle_version: str
    pause_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    pause_digest: str
    action: Literal["continue", "skip"]
    source_checkpoint_seq: int = Field(ge=0)
    source_graph_id: str
    source_state_id: str
    target_state_id: Optional[str] = None
    control_frames_hash: str
    cursor_digest: str
    created_at: str = Field(default_factory=_now)
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        """Canonical HMAC payload; never includes raw UI observations."""
        return self.model_dump(exclude={"signature"}, mode="json")


class ProgramCheckpoint(BaseModel):
    """A durable marker that a program STATE verified and the run may resume.

    Written after each ``action`` state whose identity passed, whose effects
    CONFIRMED, and whose postconditions passed. Unlike the linear
    :class:`~.checkpoint.RunCheckpoint` it captures the whole INTERPRETER STATE
    (the frame stack, loop cursors, bound params, completed effect keys) so a
    resume RESTORES the interpreter rather than translating to a step index.
    """

    schema_version: int = 1
    workflow_name: str
    #: Monotonic per-run sequence (checkpoint ordering; the highest is the resume
    #: point). Distinct from any state id, which can repeat across loop rows.
    seq: int
    #: The state that just VERIFIED (the leaf frame points here; resume re-drives
    #: from its successor transition).
    verified_state_id: str
    intent: str = ""
    #: The interpreter's graph/subflow/loop stack, OUTER -> INNER (see
    #: :class:`GraphFrame`). ``frames[-1]`` is the leaf (the verified state).
    frames: list[GraphFrame] = Field(default_factory=list)
    #: The parameter bindings in scope at the leaf (resume re-binds these).
    bound_params: dict[str, str] = Field(default_factory=dict)
    #: Contract hashes (``Effect.contract_hash``) of the effects CONFIRMED AT
    #: THIS state -- appended to the run's completed-effect ledger. Union across
    #: all checkpoints = every already-performed consequential write, so a resume
    #: can refuse to re-perform one (idempotency ledger).
    new_effect_keys: list[str] = Field(default_factory=list)
    #: The resolved effect contracts CONFIRMED at this state (``Effect`` dumps),
    #: so a resume can RE-VERIFY (read-only) that the already-confirmed writes
    #: still hold before continuing. Consistent with the run manifest already
    #: persisting the run's params; a run directory is sensitive at rest.
    new_effects: list[dict] = Field(default_factory=list)
    #: Already-performed effects admitted under explicit approval rather than
    #: independently confirmed. Kept separate so resume never promotes them to
    #: CONFIRMED while still preventing duplicate re-execution.
    new_unverified_effect_keys: list[str] = Field(default_factory=list)
    new_unverified_effects: list[dict] = Field(default_factory=list)
    governed_authorization_id: Optional[str] = None
    governed_approval_source: Optional[str] = None
    #: On-screen text expected at the resume point (this state's TEXT_PRESENT
    #: postconditions). Resume revalidates the live app still shows them before
    #: restoring -- an app that drifted off the checkpoint's state is refused.
    expected_texts: list[str] = Field(default_factory=list)
    #: Rolling digest of the visited-state history up to and including this state.
    transition_history_hash: str = ""
    #: Present only when staff completed/skipped the action represented by this
    #: checkpoint. Resume consumes its exact target instead of re-evaluating a
    #: guarded edge or re-actuating the source action.
    attended_transition: Optional[ProgramTransitionReceipt] = None
    #: Content hash of the bundle this checkpoint was captured against.
    bundle_version: str = ""
    created_at: str = Field(default_factory=_now)
