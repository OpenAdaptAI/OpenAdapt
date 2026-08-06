"""Governed actions for a durably paused, staff-attended run.

The browser console is only a presentation surface.  This module owns the
engine contract that makes an attended action safe:

* an engine-issued capability is bound to the exact run, bundle revision,
  paused step/state, resume point, and expected next transition;
* an authenticated operator must present that exact capability before acting;
* one filesystem lease serializes decisions and one idempotency key makes a
  retried HTTP request replay its recorded result instead of acting twice;
* a human-completed step is *observed and verified*, never actuated again;
* delivery evidence is never accepted as outcome evidence;
* every accepted, refused, deferred, and escalated decision is auditable.

CAPTCHA, MFA, re-authentication, and other human-presence challenges are
deliberately outside the automation path.  A person completes them in the live
application and then asks OpenAdapt to verify the resulting state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openadapt_flow.ir import (
    AttendedProgramTransitionEvidence,
    IdentityCheck,
    ProgramExecutionScopeFrame,
    State,
    StateKind,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.policy import (
    StepSafetyProjection,
    effects_for_actuation,
    project_step_safety,
)
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    BundleMismatch,
    PauseExpired,
    ResumeRefused,
    StateDiverged,
    approval_pause_digest,
    enforce_resume_authorization,
    issue_resume_approval,
    pause_is_expired,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    ProgramCheckpoint,
    ProgramTransitionReceipt,
    bound_params_sha256,
    bundle_version,
    control_frames_hash,
)

CAPABILITY_FILENAME = "attended_capability.json"
CAPABILITY_HISTORY_FILENAME = "attended_capability_history.json"
CAPABILITY_KEY_FILENAME = ".attended_capability.key"
DECISIONS_FILENAME = "attended_decisions.json"
DECISIONS_LOCK_FILENAME = ".attended_decisions.lock"
LEASE_FILENAME = ".attended_action.lease"
PROGRAM_RECEIPTS_DIRNAME = ".attended_program_receipts"
RECONCILIATION_RECEIPTS_DIRNAME = ".attended_reconciliation_receipts"
RELAY_ACK_RECORD_DOMAIN = b"openadapt:relay-ack-record-v1\0"
RELAY_ACK_WORKFLOW_DOMAIN = b"openadapt:relay-ack-workflow-v1\0"
DEFAULT_CAPABILITY_TTL_S = 24 * 3600.0
DEFAULT_LEASE_TTL_S = 15 * 60.0
DEFAULT_DECISION_LOG_LOCK_TIMEOUT_S = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(payload: Any) -> bytes:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


class AttendedActionRefused(ResumeRefused):
    """An attended mutation was refused before any workflow actuation."""


class AttendedActionBusy(AttendedActionRefused):
    """Another operator request currently owns the run's single-flight lease."""


def _committed_transition_receipt_digest(
    run_dir: Path, capability: "AttendedPauseCapability", *, key: Optional[str]
) -> Optional[str]:
    """Return only the exact durable receipt bound to this attended pause."""

    store = CheckpointStore(run_dir, key=key)
    program = [
        checkpoint
        for checkpoint in store.program_checkpoints()
        if checkpoint.attended_capability_digest == capability.digest
        and checkpoint.attended_transition is not None
    ]
    linear = [
        checkpoint
        for checkpoint in store.checkpoints()
        if checkpoint.attended_capability_digest == capability.digest
    ]
    if len(program) + len(linear) != 1:
        return None
    if program:
        return _digest(program[0].attended_transition)
    return _digest(linear[0])


def _matches_reconciliation_checkpoint(
    checkpoint: Any,
    *,
    capability: "AttendedPauseCapability",
    request_digest: str,
    delivery_state: Literal["delivered", "unknown"],
    effect_contract_hashes: tuple[str, ...],
) -> bool:
    """Return whether one immutable checkpoint binds this reconciliation.

    The reconciliation fields are duplicated from the independently verified
    result into the committed checkpoint.  Requiring their exact values here
    prevents a later ordinary checkpoint, or a different reconciliation for
    the same pause, from being projected as this request's receipt.
    """

    return (
        checkpoint.attended_capability_digest == capability.digest
        and checkpoint.attended_reconciliation_request_digest == request_digest
        and checkpoint.attended_reconciliation_expected_transition_digest
        == capability.expected_transition_digest
        and checkpoint.attended_reconciliation_delivery_state == delivery_state
        and bool(checkpoint.attended_reconciliation_effect_contract_hashes)
        and tuple(checkpoint.attended_reconciliation_effect_contract_hashes)
        == effect_contract_hashes
        and checkpoint.attended_reconciliation_at == capability.issued_at
    )


class AttendedActionExecutor(Protocol):
    """Deployment-bound bridge used only after the engine admits a decision."""

    def continue_run(
        self,
        run_dir: Path,
        capability: "AttendedPauseCapability",
        approval: ApprovalRecord,
    ) -> "AttendedExecutionResult":
        """Verify the human-completed outcome and resume deterministically."""

    def skip_run(
        self,
        run_dir: Path,
        capability: "AttendedPauseCapability",
        approval: ApprovalRecord,
    ) -> "AttendedExecutionResult":
        """Apply declared skip semantics and resume, or refuse."""

    def reconcile_run(
        self,
        run_dir: Path,
        capability: "AttendedPauseCapability",
        approval: ApprovalRecord,
        request_digest: str,
    ) -> "AttendedExecutionResult":
        """Read the live effect and resume only when it is independently proven.

        This method must not dispatch the paused action again.
        """


class TransitionObservation(BaseModel):
    """Ephemeral pre-human browser state; never serialized to the run."""

    model_config = ConfigDict(extra="forbid")

    url: Optional[str] = None
    page_title: Optional[str] = None
    page_count: Optional[int] = Field(default=None, ge=0)


class SignedTransitionBaseline(BaseModel):
    """PHI-safe structural baseline bound into a signed pause capability."""

    schema_version: int = 1
    url_digest: Optional[str] = None
    title_digest: Optional[str] = None
    page_count: Optional[int] = Field(default=None, ge=0)


class AttendedPauseCapability(BaseModel):
    """Exact authority the engine grants for one durable pause."""

    schema_version: int = 1
    #: Monotonic within one run's replaced pause-capability history. This is a
    #: presentation/event-order binding, not a substitute for the random pause
    #: id, signed pause digest, or exact durable-state validation.
    event_sequence: int = Field(default=1, ge=1)
    pause_id: str
    run_id: str
    workflow_name: str
    bundle_version: str
    step_index: int
    step_id: str
    state_id: Optional[str] = None
    resume_from_index: int
    resume_from_step_id: Optional[str] = None
    pause_digest: str
    expected_next_transition: Optional[str] = None
    expected_transition_digest: str
    program_cursor_digest: Optional[str] = None
    transition_baseline: SignedTransitionBaseline = Field(
        default_factory=SignedTransitionBaseline
    )
    delivery_state: Literal["not_delivered", "delivered", "unknown"] = "unknown"
    #: Source-record identity captured by the engine at the original halt.
    #: It is not supplied or attested by the operator, and it is distinct from
    #: any identity check for the next continuation target.
    source_identity_required: bool = False
    source_identity: Optional[IdentityCheck] = None
    issued_at: str
    expires_at: str
    allowed_actions: tuple[
        Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"], ...
    ] = (
        "reject",
        "teach",
        "escalate",
    )
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        exclude = {"signature"}
        # Capabilities issued before event ordering was added used schema v1.
        # Preserve their exact signing payload so an in-flight durable pause
        # remains resumable across the package upgrade.
        if self.schema_version < 2:
            exclude.add("event_sequence")
        if self.schema_version < 3:
            exclude.update({"source_identity_required", "source_identity"})
        return self.model_dump(exclude=exclude, mode="json")

    @property
    def digest(self) -> str:
        """Public, non-authorizing fingerprint used for stale-UI binding."""
        return _digest(self.unsigned())


class AttendedActionRequest(BaseModel):
    """One browser decision, bound to a capability and retry key."""

    model_config = ConfigDict(extra="forbid")

    capability_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    action: Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"]
    #: Closed cause, never free text. ``rejected_by_operator`` is the only
    #: cause of a rejection today. A richer disagreement taxonomy would make
    #: the answer distribution more informative, but there is no evidence yet
    #: for what its members should be -- the reject rate is the data that would
    #: design them -- and adding members here later is additive, whereas a
    #: free-text reason could never be closed again.
    disposition: Optional[
        Literal[
            "completed_by_operator",
            "not_applicable",
            "cannot_complete",
            "needs_assistance",
            "teach_requested",
            "rejected_by_operator",
            "reconciliation_requested",
        ]
    ] = None


class AttendedExecutionResult(BaseModel):
    """Outcome returned by a deployment-bound continue/skip executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed", "refused", "halted"]
    message: str
    report_success: Optional[bool] = None
    resumed_from: Optional[str] = None
    next_transition: Optional[str] = None
    transition_receipt_digest: Optional[str] = None


class AttendedReconciliationReceipt(BaseModel):
    """Private, signed proof for one no-re-dispatch reconciliation transition.

    The portable receipt exports only this object's digest. The full record
    remains on the customer-controlled runner and binds the completed result to
    the exact pause authority, operator request, and source action.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    pause_id: str
    capability_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_transition_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    #: Digest of the durable linear checkpoint or signed program transition.
    #: This is the execution receipt exported to callers.  The digest of this
    #: reconciliation record is only a local audit reference.
    transition_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action: Literal["reconcile"] = "reconcile"
    delivery_state: Literal["delivered", "unknown"]
    effect_contract_hashes: tuple[str, ...] = ()
    report_success: Literal[True] = True
    reconciled_at: str
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(exclude={"signature"}, mode="json")

    @property
    def digest(self) -> str:
        return _digest(self)


class AttendedDecision(BaseModel):
    """Append-only audit record for an admitted or refused operator decision."""

    schema_version: Literal[1, 2] = 2
    decision_id: str = Field(default_factory=lambda: secrets.token_hex(16))
    pause_id: str
    capability_digest: str
    request_digest: str
    idempotency_key: str
    action: Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"]
    operator: str
    #: Trusted route attribution. This is separate from ``operator``, which
    #: identifies the principal but not the class of decider attributed by the
    #: route. It is not proof of physical human presence. Legacy and undeclared
    #: decisions remain ``unknown`` and must not count as human decisions.
    decided_by: Literal["human", "automation", "unknown"] = "unknown"
    disposition: Optional[str] = None
    status: Literal[
        "prepared",
        "delivery_started",
        "delivery_uncertain",
        "completed",
        "refused",
        "halted",
        "needs_demonstration",
        "escalated",
        "rejected",
    ]
    message: str
    created_at: str = Field(default_factory=lambda: _iso(_now()))
    report_success: Optional[bool] = None
    next_transition: Optional[str] = None
    transition_receipt_digest: Optional[str] = None

    @model_validator(mode="after")
    def _schema_matches_provenance(self) -> "AttendedDecision":
        if self.schema_version == 1 and self.decided_by != "unknown":
            raise ValueError(
                "attended decision schema v1 cannot assert decider provenance"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_schema(self, handler: Any) -> dict[str, Any]:
        """Keep schema-v1 journal entries byte-semantically compatible."""
        payload: dict[str, Any] = handler(self)
        if self.schema_version == 1:
            payload.pop("decided_by", None)
        return payload


def attended_decision_payload(decision: AttendedDecision) -> dict[str, Any]:
    """Return the canonical payload for the decision's declared schema."""
    payload = decision.model_dump(mode="json")
    if decision.schema_version == 1:
        # Pydantic supplies the safe ``unknown`` default when a v1 record is
        # read. The field was not part of v1, so it must not change that
        # record's existing engine or portable digest.
        payload.pop("decided_by", None)
    return payload


class AttendedRelayBinding(BaseModel):
    """Exact signed hosted decision bound to one retained engine outcome."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    decision_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    relay_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    relay_signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    capability_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    event_sequence: int = Field(ge=1)
    action: Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"]


RelayOutcomeStatus = Literal[
    "completed",
    "refused",
    "halted",
    "needs_demonstration",
    "escalated",
    "rejected",
]


class AttendedRelayAcknowledgement(AttendedRelayBinding):
    """Durable hosted acknowledgement linked to one journaled decision."""

    engine_ack_result: Literal["accepted", "refused"]
    run_id: str
    workflow_digest: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    bundle_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pause_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retained_decision_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retained_decision_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retained_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retained_status: RelayOutcomeStatus
    confirmed: bool = False
    created_at: str = Field(default_factory=lambda: _iso(_now()))
    confirmed_at: Optional[str] = None
    record_mac: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(exclude={"record_mac"}, mode="json")

    def binding(self) -> AttendedRelayBinding:
        return AttendedRelayBinding.model_validate(
            self.model_dump(
                include={
                    "schema_version",
                    "decision_id",
                    "relay_digest",
                    "relay_signature",
                    "idempotency_key",
                    "capability_digest",
                    "event_sequence",
                    "action",
                }
            )
        )


class AttendedDecisionLog(BaseModel):
    schema_version: int = 1
    decisions: list[AttendedDecision] = Field(default_factory=list)
    relay_acknowledgements: list[AttendedRelayAcknowledgement] = Field(
        default_factory=list
    )


def _delivery_state(
    result: StepResult,
) -> Literal["not_delivered", "delivered", "unknown"]:
    """Classify delivery without ever implying outcome success."""
    error = (result.error or "").lower()
    if result.delivery_receipt is not None or result.actuation == "api":
        return "delivered"
    if (
        result.identity is not None
        or "could not resolve" in error
        or "refusing to act" in error
        or "precondition" in error
        or "guard" in error
    ):
        return "not_delivered"
    return "unknown"


def _expected_transition(
    workflow: Workflow, pending: PendingEscalation
) -> Optional[str]:
    if pending.program:
        # A guarded successor can be selected only after fresh human-completion
        # verification. The engine persists that one target in a receipt before
        # resume instead of guessing it at capability-issuance time.
        return "<program-transition-receipt>"
    next_index = pending.step_index + 1
    if 0 <= next_index < len(workflow.steps):
        return workflow.steps[next_index].id
    return "<complete>"


def _transition_payload(
    *,
    run_id: str,
    workflow_name: str,
    bundle_revision: str,
    pending: PendingEscalation,
    expected_next_transition: Optional[str],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "workflow_name": workflow_name,
        "bundle_version": bundle_revision,
        "step_index": pending.step_index,
        "step_id": pending.step_id,
        "state_id": pending.state_id,
        "resume_from_index": pending.resume_from_index,
        "resume_from_step_id": pending.resume_from_step_id,
        "program_cursor_digest": _program_cursor_digest(pending),
        "expected_next_transition": expected_next_transition,
    }


def _program_cursor_digest(pending: PendingEscalation) -> Optional[str]:
    if not pending.program or not pending.program_frames:
        return None
    return _digest(
        {
            "state_id": pending.state_id,
            "checkpoint_seq": pending.program_checkpoint_seq,
            "history_hash": pending.program_history_hash,
            "control_frames_hash": control_frames_hash(pending.program_frames),
        }
    )


def _program_pause_state(
    workflow: Workflow, pending: PendingEscalation
) -> Optional[State]:
    if (
        workflow.program is None
        or not pending.program
        or not pending.program_frames
        or pending.state_id is None
    ):
        return None
    leaf = pending.program_frames[-1]
    graph = (
        workflow.program
        if leaf.graph_id == "__program__"
        else workflow.subflows.get(leaf.graph_id)
    )
    if graph is None or leaf.state_id != pending.state_id:
        return None
    state = graph.states.get(leaf.state_id)
    if state is None or state.kind is not StateKind.ACTION or state.step is None:
        return None
    return state


def _source_step(workflow: Workflow, pending: PendingEscalation) -> Optional[Step]:
    """Return the exact action step represented by one durable pause."""

    if pending.program:
        state = _program_pause_state(workflow, pending)
        return state.step if state is not None else None
    if 0 <= pending.step_index < len(workflow.steps):
        step = workflow.steps[pending.step_index]
        if step.id == pending.step_id:
            return step
    return None


def _source_identity_required(step: Step, manifest: Any) -> bool:
    authorization = getattr(manifest, "governed_authorization", None)
    return bool(
        step.identity_armed
        or (
            authorization is not None
            and authorization.requires_verified_identity(step.id)
        )
    )


def _identity_status(identity: Optional[IdentityCheck]) -> Optional[str]:
    value = getattr(identity, "status", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _relative_postcondition_kinds(step: Any) -> set[str]:
    return {
        pc.kind.value if hasattr(pc.kind, "value") else str(pc.kind)
        for pc in step.expect
        if (pc.kind.value if hasattr(pc.kind, "value") else str(pc.kind))
        in {"url_changed", "title_changed", "new_tab_opened"}
    }


def _allowed_actions(
    workflow: Workflow,
    pending: PendingEscalation,
    baseline: SignedTransitionBaseline,
    manifest: Any,
    source_identity: Optional[IdentityCheck],
    delivery_state: Literal["not_delivered", "delivered", "unknown"],
) -> tuple[
    Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"], ...
]:
    """Derive mutation authority from the exact workflow step semantics.

    ``reject`` is offered at every pause EXCEPT one where the runtime
    POSITIVELY recorded that this step may already have actuated, and that
    exception is the whole of its gating.

    It is deliberately not gated on halt category. Rejecting asserts something
    about THIS RUN -- that it must not proceed -- which an operator looking at
    the live application can conclude at a resolution halt as readily as at an
    effect halt. Gating on category would remove it from exactly the halts
    where ``continue`` IS offered (those with postconditions and a usable
    baseline) and leave it only where ``continue`` often is not, which inverts
    the reason it exists: the pressure toward the agreeable one-tap answer
    lives wherever the agreeable answer is one tap.

    An uncertain delivery is different in kind. The action may already have
    landed, so a button reading "stop this" would imply a write was prevented
    that may not have been, and the operator's task there is reconciliation
    rather than termination. ``escalate`` keeps the pause and hands it to
    someone who can reconcile; that is the correct path, so ``reject`` is
    withheld.

    Note what this does NOT read. ``_delivery_state`` is fail-closed and
    returns ``unknown`` for most halts simply because nothing proved
    non-delivery -- gating on it would withhold ``reject`` from nearly every
    pause and quietly undo the whole point. The gate is
    ``pending.delivery_uncertainty``, which exists only where the runtime
    recorded a real post-delivery uncertainty. :func:`execute_attended_action`
    adds the matching live-journal check, because a delivery can become
    uncertain AFTER this capability was issued.
    """
    actions: list[
        Literal["continue", "skip", "reject", "teach", "escalate", "reconcile"]
    ] = [
        "teach",
        "escalate",
    ]
    # Reconciliation observes the current application and the independent
    # effect verifier. It never re-dispatches this action. A capability for a
    # proved pre-delivery halt must not advertise it: there is nothing to
    # reconcile, and allowing it would manufacture a success-shaped path.
    if pending.delivery_uncertainty is not None and delivery_state in {
        "delivered",
        "unknown",
    }:
        actions.insert(0, "reconcile")
    # Reject needs no step semantics: it neither actuates nor resumes, so it is
    # available even where the pause carries no resolvable action step at all
    # (a non-action program pause), unlike continue and skip below.
    if pending.delivery_uncertainty is None:
        actions.insert(0, "reject")
    step = _source_step(workflow, pending)
    if step is None:
        return tuple(actions)

    projection = _attended_step_safety(step, workflow, manifest)
    gui_effects = dict(projection.effect_paths).get("gui", ())

    relative = _relative_postcondition_kinds(step)
    has_relative_baseline = (
        ("url_changed" not in relative or baseline.url_digest is not None)
        and ("title_changed" not in relative or baseline.title_digest is not None)
        and ("new_tab_opened" not in relative or baseline.page_count is not None)
    )
    has_unsupported_effect = any(
        effect.needs_operator_confirmation
        or effect.count_new_only
        or effect.forbid_collateral_loss
        for effect in gui_effects
    )
    if (
        bool(step.expect or gui_effects)
        and has_relative_baseline
        and not has_unsupported_effect
        and _identity_status(source_identity) != "mismatch"
        and (
            not _source_identity_required(step, manifest)
            or _identity_status(source_identity) == "verified"
        )
    ):
        actions.insert(0, "continue")

    if (
        not projection.consequential
        and step.guard is not None
        and step.guard.on_unmet == "skip"
    ):
        actions.insert(1 if actions[0] == "continue" else 0, "skip")
    return tuple(actions)


def _attended_step_safety(
    step: Step,
    workflow: Workflow,
    manifest: Any,
) -> StepSafetyProjection:
    """Project one step through the exact admitted qualification authority.

    A bundle-local risk downgrade is not enough to grant an attended skip. A
    production downgrade is authoritative only when the durable run carries
    the exact policy digest whose current qualification can be reproduced.
    Without that authority the canonical projection remains conservative.
    """

    authorization = getattr(manifest, "governed_authorization", None)
    policy_digest = (
        getattr(authorization, "admitted_policy_contract_sha256", None)
        if authorization is not None
        else None
    )
    return project_step_safety(
        step,
        workflow,
        require_current_certification=True,
        certifying_policy_sha256=policy_digest,
    )


class AttendedActionStore:
    """Capability, single-flight lease, and append-only decision persistence."""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.capability_path = self.run_dir / CAPABILITY_FILENAME
        self.capability_history_path = self.run_dir / CAPABILITY_HISTORY_FILENAME
        self.key_path = self.run_dir / CAPABILITY_KEY_FILENAME
        self.decisions_path = self.run_dir / DECISIONS_FILENAME
        self.decisions_lock_path = self.run_dir / DECISIONS_LOCK_FILENAME
        self.lease_path = self.run_dir / LEASE_FILENAME

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        """Persist a replace/create directory entry on POSIX."""
        if os.name == "nt":
            return
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            AttendedActionStore._fsync_parent(path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _key(self, *, create: bool) -> bytes:
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise AttendedActionRefused(
                    "the pause capability key is missing; refusing an "
                    "unverifiable operator action"
                ) from None
            self.run_dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            try:
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return self._key(create=False)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent(self.key_path)
        if os.name != "nt" and self.key_path.stat().st_mode & 0o077:
            raise AttendedActionRefused(
                "the pause capability key permissions are too broad; refusing"
            )
        if len(key) != 32:
            raise AttendedActionRefused(
                "the pause capability key has an invalid length; refusing"
            )
        return key

    @contextmanager
    def _decision_log_lock(
        self,
        *,
        timeout_s: float = DEFAULT_DECISION_LOG_LOCK_TIMEOUT_S,
    ) -> Iterator[None]:
        """Serialize cross-process decision-journal read-modify-write cycles.

        There is no stale automatic takeover. If a process dies while it owns
        this lock, an operator must reconcile and remove the private lock file.
        Guessing that a journal writer died would permit two writers to replace
        one another's retained outcomes.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fd = os.open(
                    self.decisions_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                break
            except FileExistsError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AttendedActionBusy(
                        "the attended decision journal is being updated or "
                        "requires operator reconciliation"
                    ) from None
                time.sleep(min(0.01, remaining))
        owner_nonce = secrets.token_bytes(32)
        owner_stat = os.fstat(fd)
        try:
            if os.write(fd, owner_nonce) != len(owner_nonce):
                raise OSError("the decision journal lock nonce write was incomplete")
            os.fsync(fd)
            self._fsync_parent(self.decisions_lock_path)
            yield
        finally:
            cleanup_error: Optional[AttendedActionBusy] = None
            try:
                current_lstat = os.lstat(self.decisions_lock_path)
                same_entry = (
                    stat.S_ISREG(current_lstat.st_mode)
                    and current_lstat.st_dev == owner_stat.st_dev
                    and current_lstat.st_ino == owner_stat.st_ino
                )
                if not same_entry:
                    raise OSError("the lock path no longer names the owner file")
                current_fd = os.open(
                    self.decisions_lock_path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    current_fstat = os.fstat(current_fd)
                    current_nonce = os.read(current_fd, len(owner_nonce) + 1)
                finally:
                    os.close(current_fd)
                if (
                    not stat.S_ISREG(current_fstat.st_mode)
                    or current_fstat.st_dev != owner_stat.st_dev
                    or current_fstat.st_ino != owner_stat.st_ino
                    or not hmac.compare_digest(current_nonce, owner_nonce)
                ):
                    raise OSError("the lock owner nonce or file identity changed")
                final_lstat = os.lstat(self.decisions_lock_path)
                if (
                    final_lstat.st_dev != owner_stat.st_dev
                    or final_lstat.st_ino != owner_stat.st_ino
                ):
                    raise OSError("the lock path changed before release")
                self.decisions_lock_path.unlink()
                self._fsync_parent(self.decisions_lock_path)
            except OSError:
                cleanup_error = AttendedActionBusy(
                    "the attended decision journal lock changed while owned; "
                    "the replacement lock was retained for reconciliation"
                )
            finally:
                os.close(fd)
            if cleanup_error is not None:
                raise cleanup_error

    def _sign(self, capability: AttendedPauseCapability, *, create_key: bool) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=create_key),
                _canonical(capability.unsigned()),
                hashlib.sha256,
            ).hexdigest()
        )

    def _workflow_digest(self, workflow_name: str) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=False),
                RELAY_ACK_WORKFLOW_DOMAIN + workflow_name.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

    def _relay_ack_mac(self, record: AttendedRelayAcknowledgement) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=False),
                RELAY_ACK_RECORD_DOMAIN + _canonical(record.unsigned()),
                hashlib.sha256,
            ).hexdigest()
        )

    def _verify_relay_ack_mac(self, record: AttendedRelayAcknowledgement) -> None:
        expected = self._relay_ack_mac(record)
        if not hmac.compare_digest(record.record_mac, expected):
            raise AttendedActionRefused(
                "the retained relay acknowledgement HMAC does not verify"
            )

    def _signed_capabilities(self) -> list[AttendedPauseCapability]:
        """Read and authenticate the current and historical pause authorities."""
        if (
            self.capability_path.is_symlink()
            or self.capability_history_path.is_symlink()
        ):
            raise AttendedActionRefused(
                "the attended capability path must not be a symlink"
            )
        capabilities: list[AttendedPauseCapability] = []
        if self.capability_path.is_file():
            try:
                capabilities.append(
                    AttendedPauseCapability.model_validate_json(
                        self.capability_path.read_text()
                    )
                )
            except ValueError as exc:
                raise AttendedActionRefused(
                    "the current attended capability is invalid"
                ) from exc
        if self.capability_history_path.is_file():
            try:
                raw_history = json.loads(self.capability_history_path.read_text())
                if not isinstance(raw_history, list):
                    raise ValueError("history is not a list")
                capabilities.extend(
                    AttendedPauseCapability.model_validate(raw) for raw in raw_history
                )
            except (OSError, ValueError) as exc:
                raise AttendedActionRefused(
                    "the attended capability history is invalid"
                ) from exc
        if not capabilities:
            raise AttendedActionRefused(
                "no signed attended capability can authenticate this relay outcome"
            )
        for capability in capabilities:
            expected = self._sign(capability, create_key=False)
            if not hmac.compare_digest(capability.signature, expected):
                raise AttendedActionRefused(
                    "an attended capability signature does not verify"
                )
        return capabilities

    def _capability_for_relay_binding(
        self,
        binding: AttendedRelayBinding,
    ) -> AttendedPauseCapability:
        matches = [
            capability
            for capability in self._signed_capabilities()
            if capability.digest == binding.capability_digest
            and capability.event_sequence == binding.event_sequence
        ]
        if len(matches) != 1:
            raise AttendedActionRefused(
                "the exact signed pause capability for this relay is missing "
                "or ambiguous"
            )
        return matches[0]

    def signed_capability_for_digest(
        self,
        capability_digest: str,
    ) -> AttendedPauseCapability:
        """Return one authenticated current or historical capability."""

        matches = [
            capability
            for capability in self._signed_capabilities()
            if capability.digest == capability_digest
        ]
        if len(matches) != 1:
            raise AttendedActionRefused(
                "the exact signed attended capability is missing or ambiguous"
            )
        return matches[0]

    def _verify_relay_ack_context(
        self,
        record: AttendedRelayAcknowledgement,
        *,
        key: Optional[str] = None,
    ) -> AttendedPauseCapability:
        """Verify the record against its signed pause and live run manifest."""
        capability = self._capability_for_relay_binding(record.binding())
        if (
            capability.run_id != record.run_id
            or capability.bundle_version != record.bundle_version
            or capability.pause_id != record.pause_id
            or capability.event_sequence != record.event_sequence
            or capability.digest != record.capability_digest
            or self._workflow_digest(capability.workflow_name) != record.workflow_digest
        ):
            raise AttendedActionRefused(
                "the retained relay acknowledgement does not match its signed "
                "pause capability"
            )

        from openadapt_flow import crypto as _crypto

        manifest = CheckpointStore(
            self.run_dir, key=_crypto.resolve_key(key)
        ).read_manifest()
        if manifest is None:
            raise AttendedActionRefused(
                "the live run manifest is missing for relay acknowledgement recovery"
            )
        try:
            live_bundle_version = bundle_version(manifest.bundle_dir)
        except (OSError, ValueError) as exc:
            raise AttendedActionRefused(
                "the live bundle version cannot be verified for relay recovery"
            ) from exc
        if (
            manifest.run_id != record.run_id
            or live_bundle_version != record.bundle_version
            or self._workflow_digest(manifest.workflow_name) != record.workflow_digest
        ):
            raise AttendedActionRefused(
                "the live run manifest does not match the retained relay "
                "acknowledgement"
            )
        return capability

    def seal_human_decision_task(self, unsigned: dict[str, Any]) -> dict[str, Any]:
        """Sign one PHI-free task projection with a separate HMAC domain.

        The result is presentation integrity, never execution authority. The
        engine still requires the separately signed pause capability and all
        normal attended-action admission checks.
        """
        try:
            from openadapt_types import sign_human_decision_task_hmac

            task = sign_human_decision_task_hmac(
                key=self._key(create=False),
                fields=unsigned,
            )
        except (ImportError, ValueError) as exc:
            raise AttendedActionRefused(
                "the shared human decision task contract is unavailable or invalid"
            ) from exc
        return task.model_dump(mode="json")

    def verify_human_decision_task(self, task: dict[str, Any]) -> bool:
        """Verify a projected task without treating it as pause authority."""
        try:
            from openadapt_types import HumanDecisionTaskV1

            validated = HumanDecisionTaskV1.model_validate(task)
            return validated.verify_hmac(self._key(create=False))
        except (ImportError, ValueError, AttendedActionRefused):
            return False

    def _receipt_path(self, pause_id: str) -> Path:
        if len(pause_id) != 32 or any(ch not in "0123456789abcdef" for ch in pause_id):
            raise AttendedActionRefused("the program receipt pause id is invalid")
        return self.run_dir / PROGRAM_RECEIPTS_DIRNAME / f"{pause_id}.json"

    def _reconciliation_receipt_path(self, pause_id: str) -> Path:
        if len(pause_id) != 32 or any(ch not in "0123456789abcdef" for ch in pause_id):
            raise AttendedActionRefused(
                "the reconciliation receipt pause id is invalid"
            )
        return self.run_dir / RECONCILIATION_RECEIPTS_DIRNAME / f"{pause_id}.json"

    def _sign_reconciliation_receipt(
        self, receipt: AttendedReconciliationReceipt
    ) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=False),
                b"openadapt-attended-reconciliation-receipt/v1\0"
                + _canonical(receipt.unsigned()),
                hashlib.sha256,
            ).hexdigest()
        )

    def write_reconciliation_receipt(
        self, receipt: AttendedReconciliationReceipt
    ) -> AttendedReconciliationReceipt:
        """Atomically retain one signed reconciliation proof for a pause.

        A second write is idempotent only when every bound value is identical.
        This prevents a stale request from replacing the result for the same
        delivery-uncertain action.
        """

        sealed = receipt.model_copy(update={"signature": ""})
        sealed = sealed.model_copy(
            update={"signature": self._sign_reconciliation_receipt(sealed)}
        )
        path = self._reconciliation_receipt_path(sealed.pause_id)
        if path.parent.is_symlink() or path.is_symlink():
            raise AttendedActionRefused(
                "the reconciliation receipt path must not be a symlink"
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        if path.is_file():
            existing = self.read_reconciliation_receipt(sealed.pause_id)
            if existing != sealed:
                raise AttendedActionRefused(
                    "a different reconciliation receipt already exists for this pause"
                )
            return existing
        self._atomic_write(path, sealed.model_dump_json(indent=2).encode("utf-8"))
        return sealed

    def read_reconciliation_receipt(
        self, pause_id: str
    ) -> AttendedReconciliationReceipt:
        path = self._reconciliation_receipt_path(pause_id)
        if path.parent.is_symlink() or path.is_symlink():
            raise AttendedActionRefused(
                "the reconciliation receipt path must not be a symlink"
            )
        try:
            receipt = AttendedReconciliationReceipt.model_validate_json(
                path.read_text()
            )
        except (FileNotFoundError, ValueError) as exc:
            raise AttendedActionRefused(
                "the exact reconciliation receipt is missing or invalid"
            ) from exc
        expected = self._sign_reconciliation_receipt(receipt)
        if not hmac.compare_digest(receipt.signature, expected):
            raise AttendedActionRefused(
                "the reconciliation receipt signature does not verify"
            )
        return receipt

    def _sign_program_receipt(self, receipt: ProgramTransitionReceipt) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=False),
                _canonical(receipt.unsigned()),
                hashlib.sha256,
            ).hexdigest()
        )

    def seal_program_receipt(
        self, receipt: ProgramTransitionReceipt
    ) -> ProgramTransitionReceipt:
        """Bind an exact interpreter transition to the signed per-run trust root."""
        sealed = receipt.model_copy(update={"signature": ""})
        return sealed.model_copy(
            update={"signature": self._sign_program_receipt(sealed)}
        )

    def write_program_receipt(
        self, receipt: ProgramTransitionReceipt
    ) -> ProgramTransitionReceipt:
        """Atomically persist one private, HMAC-authenticated transition receipt."""
        sealed = self.seal_program_receipt(receipt)
        path = self._receipt_path(sealed.pause_id)
        if path.parent.is_symlink() or path.is_symlink():
            raise AttendedActionRefused(
                "the private program receipt path must not be a symlink"
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        if path.is_file():
            existing = self.read_program_receipt(sealed.pause_id)
            if existing != sealed:
                raise AttendedActionRefused(
                    "a different program transition receipt already exists for "
                    "this pause"
                )
            return existing
        self._atomic_write(path, sealed.model_dump_json(indent=2).encode("utf-8"))
        return sealed

    def read_program_receipt(self, pause_id: str) -> ProgramTransitionReceipt:
        """Read and authenticate one exact program transition receipt."""
        path = self._receipt_path(pause_id)
        if path.parent.is_symlink() or path.is_symlink():
            raise AttendedActionRefused(
                "the private program receipt path must not be a symlink"
            )
        try:
            receipt = ProgramTransitionReceipt.model_validate_json(path.read_text())
        except (FileNotFoundError, ValueError) as exc:
            raise AttendedActionRefused(
                "the exact program transition receipt is missing or invalid"
            ) from exc
        expected = self._sign_program_receipt(receipt)
        if not hmac.compare_digest(receipt.signature, expected):
            raise AttendedActionRefused(
                "the program transition receipt signature does not verify"
            )
        return receipt

    def transition_value_digest(self, field: str, value: str) -> str:
        """Keyed digest for one transient URL/title observation."""
        if field not in {"url", "page_title"}:
            raise ValueError("transition digest field must be url or page_title")
        payload = f"openadapt-attended-transition-v1:{field}:".encode() + value.encode(
            "utf-8"
        )
        return (
            "hmac-sha256:"
            + hmac.new(self._key(create=False), payload, hashlib.sha256).hexdigest()
        )

    def _transition_baseline(
        self, observation: Optional[TransitionObservation]
    ) -> SignedTransitionBaseline:
        observation = observation or TransitionObservation()
        return SignedTransitionBaseline(
            url_digest=(
                self.transition_value_digest("url", observation.url)
                if observation.url is not None
                else None
            ),
            title_digest=(
                self.transition_value_digest("page_title", observation.page_title)
                if observation.page_title is not None
                else None
            ),
            page_count=observation.page_count,
        )

    def issue(
        self,
        *,
        manifest: Any,
        pending: PendingEscalation,
        workflow: Workflow,
        result: StepResult,
        transition_observation: Optional[TransitionObservation] = None,
        ttl_s: float = DEFAULT_CAPABILITY_TTL_S,
    ) -> AttendedPauseCapability:
        """Issue once for a new pause; re-reads an existing valid capability."""
        revision = bundle_version(manifest.bundle_dir)
        expected = _expected_transition(workflow, pending)
        pause_digest = approval_pause_digest(pending)
        # Creating the HMAC key before digesting transition values gives the
        # baseline and capability signature one stable per-run trust root.
        self._key(create=True)
        baseline = self._transition_baseline(transition_observation)
        delivery_state = _delivery_state(result)
        source_step = _source_step(workflow, pending)
        if source_step is not None and result.step_id != source_step.id:
            raise AttendedActionRefused(
                "the halt identity does not match its source workflow step"
            )
        source_identity = (
            result.identity.model_copy(deep=True)
            if result.identity is not None
            else None
        )
        source_identity_required = bool(
            source_step is not None and _source_identity_required(source_step, manifest)
        )
        allowed_actions = _allowed_actions(
            workflow,
            pending,
            baseline,
            manifest,
            source_identity,
            delivery_state,
        )
        event_sequence = 1
        if self.capability_path.is_file():
            existing = self.read()
            if (
                existing.pause_digest == pause_digest
                and existing.step_id == pending.step_id
                and existing.step_index == pending.step_index
                and existing.state_id == pending.state_id
                and existing.resume_from_index == pending.resume_from_index
                and existing.resume_from_step_id == pending.resume_from_step_id
                and existing.expected_next_transition == expected
                and existing.bundle_version == revision
                and existing.run_id == manifest.run_id
                and existing.workflow_name == pending.workflow_name
                and existing.transition_baseline == baseline
                and existing.allowed_actions == allowed_actions
                and existing.source_identity_required == source_identity_required
                and existing.source_identity == source_identity
            ):
                return existing
            # A resumed run may halt again before the first request's terminal
            # HTTP response is written. Preserve the old signed capability in
            # an append-only history and let the engine issue the new pause;
            # browser callers still present the exact current digest.
            history: list[dict[str, Any]] = []
            if self.capability_history_path.is_file():
                try:
                    raw_history = json.loads(self.capability_history_path.read_text())
                    if isinstance(raw_history, list):
                        history = [
                            item for item in raw_history if isinstance(item, dict)
                        ]
                except (OSError, ValueError):
                    raise AttendedActionRefused(
                        "the attended capability history is invalid"
                    ) from None
            history.append(existing.model_dump(mode="json"))
            self._atomic_write(
                self.capability_history_path,
                json.dumps(history, indent=2, sort_keys=True).encode("utf-8"),
            )
            event_sequence = existing.event_sequence + 1
        elif self.capability_history_path.is_file():
            historical = self._signed_capabilities()
            event_sequence = max(item.event_sequence for item in historical) + 1
        now = _now()
        transition = _transition_payload(
            run_id=manifest.run_id,
            workflow_name=pending.workflow_name,
            bundle_revision=revision,
            pending=pending,
            expected_next_transition=expected,
        )
        capability = AttendedPauseCapability(
            schema_version=3,
            event_sequence=event_sequence,
            pause_id=secrets.token_hex(16),
            run_id=manifest.run_id,
            workflow_name=pending.workflow_name,
            bundle_version=transition["bundle_version"],
            step_index=pending.step_index,
            step_id=pending.step_id,
            state_id=pending.state_id,
            resume_from_index=pending.resume_from_index,
            resume_from_step_id=pending.resume_from_step_id,
            pause_digest=pause_digest,
            expected_next_transition=expected,
            expected_transition_digest=_digest(transition),
            program_cursor_digest=_program_cursor_digest(pending),
            transition_baseline=baseline,
            delivery_state=delivery_state,
            source_identity_required=source_identity_required,
            source_identity=source_identity,
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=max(1.0, ttl_s))),
            allowed_actions=allowed_actions,
        )
        capability.signature = self._sign(capability, create_key=False)
        self._atomic_write(
            self.capability_path,
            capability.model_dump_json(indent=2).encode("utf-8"),
        )
        return capability

    def retire_current(self) -> None:
        """Archive an obsolete operational capability and remove its pointer.

        A typed business-decision pause has a different authority contract. A
        capability from an earlier operational pause must remain auditable but
        must not remain visible as the current operator action surface.
        """

        if not self.capability_path.is_file():
            return
        existing = self.read()
        history: list[dict[str, Any]] = []
        if self.capability_history_path.is_file():
            try:
                raw_history = json.loads(self.capability_history_path.read_text())
                if not isinstance(raw_history, list) or not all(
                    isinstance(item, dict) for item in raw_history
                ):
                    raise ValueError("history is not a list of objects")
                history = list(raw_history)
            except (OSError, ValueError) as exc:
                raise AttendedActionRefused(
                    "the attended capability history is invalid"
                ) from exc
        existing_payload = existing.model_dump(mode="json")
        if existing_payload not in history:
            history.append(existing_payload)
            self._atomic_write(
                self.capability_history_path,
                json.dumps(history, indent=2, sort_keys=True).encode("utf-8"),
            )
        self.capability_path.unlink()
        self._fsync_parent(self.capability_path)

    def read(self) -> AttendedPauseCapability:
        try:
            capability = AttendedPauseCapability.model_validate_json(
                self.capability_path.read_text()
            )
        except (FileNotFoundError, ValueError) as exc:
            raise AttendedActionRefused(
                "the run has no valid engine-issued attended capability"
            ) from exc
        expected = self._sign(capability, create_key=False)
        if not hmac.compare_digest(capability.signature, expected):
            raise AttendedActionRefused(
                "the attended capability signature does not verify"
            )
        return capability

    def validate(
        self,
        request: AttendedActionRequest,
        *,
        pending: PendingEscalation,
        manifest: Any,
        now: Optional[datetime] = None,
    ) -> AttendedPauseCapability:
        capability = self.read()
        now = now or _now()
        if request.capability_digest != capability.digest:
            raise AttendedActionRefused(
                "the operator page is stale or the pause capability changed"
            )
        if request.action not in capability.allowed_actions:
            raise AttendedActionRefused("the capability does not allow this action")
        if _parse(capability.expires_at) < now or pause_is_expired(pending, now):
            raise PauseExpired(
                "the attended pause expired; reload and re-qualify live state"
            )
        live_version = bundle_version(manifest.bundle_dir)
        if live_version != capability.bundle_version:
            raise BundleMismatch("the bundle revision changed after the attended pause")
        if approval_pause_digest(pending) != capability.pause_digest:
            raise AttendedActionRefused(
                "the exact durable pause changed after capability issuance"
            )
        transition = _transition_payload(
            run_id=manifest.run_id,
            workflow_name=pending.workflow_name,
            bundle_revision=live_version,
            pending=pending,
            expected_next_transition=capability.expected_next_transition,
        )
        if _digest(transition) != capability.expected_transition_digest:
            raise AttendedActionRefused(
                "the expected attended transition binding no longer verifies"
            )
        if capability.program_cursor_digest != _program_cursor_digest(pending):
            raise AttendedActionRefused(
                "the exact program interpreter cursor no longer verifies"
            )
        if (
            pending.step_id != capability.step_id
            or pending.step_index != capability.step_index
            or pending.resume_from_index != capability.resume_from_index
            or pending.resume_from_step_id != capability.resume_from_step_id
            or manifest.run_id != capability.run_id
            or manifest.workflow_name != capability.workflow_name
        ):
            raise AttendedActionRefused(
                "the durable pause no longer matches its issued capability"
            )
        return capability

    def _read_log(self) -> AttendedDecisionLog:
        log, _head = self._read_log_with_head()
        return log

    def _read_log_with_head(self) -> tuple[AttendedDecisionLog, str]:
        """Read the external monotonic journal and repair its local projection."""

        from openadapt_flow.runtime.durable.authority import (
            JOURNAL_GENESIS_DIGEST,
            DurableAuthority,
            DurableAuthorityBusy,
        )

        try:
            capability = self.read()
            authority = DurableAuthority(
                self.run_dir,
                CheckpointStore(self.run_dir),
            )
            snapshot, head = authority.read_attended_snapshot(
                expected_run_id=capability.run_id
            )
        except DurableAuthorityBusy as exc:
            raise AttendedActionRefused(
                "the external attended decision journal is unavailable or invalid"
            ) from exc
        if snapshot is None:
            if self.decisions_path.is_file():
                try:
                    local = AttendedDecisionLog.model_validate_json(
                        self.decisions_path.read_text()
                    )
                except (OSError, ValueError) as exc:
                    raise AttendedActionRefused(
                        "the local attended decision projection is invalid"
                    ) from exc
                if local.decisions or local.relay_acknowledgements:
                    raise AttendedActionRefused(
                        "the local attended decision history has no external "
                        "monotonic authority"
                    )
            return AttendedDecisionLog(), JOURNAL_GENESIS_DIGEST
        try:
            log = AttendedDecisionLog.model_validate_json(snapshot)
        except ValueError as exc:
            raise AttendedActionRefused(
                "the authenticated attended decision snapshot is invalid"
            ) from exc
        payload = snapshot.encode("utf-8")
        try:
            local_matches = (
                self.decisions_path.is_file()
                and not self.decisions_path.is_symlink()
                and self.decisions_path.read_bytes() == payload
            )
        except OSError:
            local_matches = False
        if not local_matches:
            # The JSON file is a recoverable local projection. The append-only,
            # HMAC-chained SQLite journal outside the run directory is authority.
            self._atomic_write(self.decisions_path, payload)
        return log, head

    def _append_log_snapshot(
        self,
        *,
        log: AttendedDecisionLog,
        expected_head: str,
    ) -> None:
        """Commit authority first, then publish the recoverable local projection."""

        from openadapt_flow.runtime.durable.authority import (
            DurableAuthority,
            DurableAuthorityBusy,
        )

        capability = self.read()
        payload = log.model_dump_json(indent=2)
        try:
            DurableAuthority(
                self.run_dir,
                CheckpointStore(self.run_dir),
            ).append_attended_snapshot(
                expected_run_id=capability.run_id,
                expected_head_digest=expected_head,
                snapshot_json=payload,
            )
        except DurableAuthorityBusy as exc:
            raise AttendedActionRefused(
                "the external attended decision journal changed or is unavailable"
            ) from exc
        self._atomic_write(self.decisions_path, payload.encode("utf-8"))

    def prior(self, request: AttendedActionRequest) -> Optional[AttendedDecision]:
        request_digest = _digest(request)
        for decision in reversed(self._read_log().decisions):
            if decision.idempotency_key != request.idempotency_key:
                continue
            if decision.request_digest != request_digest:
                raise AttendedActionRefused(
                    "the idempotency key was already used for a different request"
                )
            return decision
        return None

    def unresolved_delivery(self, pause_id: str) -> Optional[AttendedDecision]:
        """Return a request whose latest journal state crossed delivery.

        This is pause-wide, not merely idempotency-key-wide. A caller must not
        bypass an uncertain delivery by generating a fresh browser retry key.
        """
        latest: dict[str, AttendedDecision] = {}
        for decision in self._read_log().decisions:
            if decision.pause_id == pause_id:
                latest[decision.request_digest] = decision
        for decision in reversed(list(latest.values())):
            if decision.status in {"delivery_started", "delivery_uncertain"}:
                return decision
        return None

    def _relay_acknowledgement(
        self,
        binding: AttendedRelayBinding,
        decision: AttendedDecision,
        *,
        key: Optional[str] = None,
    ) -> AttendedRelayAcknowledgement:
        if decision.status in {"prepared", "delivery_started", "delivery_uncertain"}:
            raise AttendedActionRefused(
                "a non-terminal engine decision cannot authorize a relay "
                "acknowledgement"
            )
        capability = self._capability_for_relay_binding(binding)
        record = AttendedRelayAcknowledgement(
            **binding.model_dump(),
            engine_ack_result=(
                "refused" if decision.status == "refused" else "accepted"
            ),
            run_id=capability.run_id,
            workflow_digest=self._workflow_digest(capability.workflow_name),
            bundle_version=capability.bundle_version,
            pause_id=capability.pause_id,
            retained_decision_id=decision.decision_id,
            retained_decision_digest=_digest(attended_decision_payload(decision)),
            retained_request_digest=decision.request_digest,
            retained_status=cast(RelayOutcomeStatus, decision.status),
            record_mac="hmac-sha256:" + ("0" * 64),
        )
        record = record.model_copy(update={"record_mac": self._relay_ack_mac(record)})
        self._verify_relay_ack_context(record, key=key)
        return record

    def _add_relay_acknowledgement(
        self,
        log: AttendedDecisionLog,
        binding: AttendedRelayBinding,
        decision: AttendedDecision,
        *,
        key: Optional[str] = None,
    ) -> None:
        acknowledgement = self._relay_acknowledgement(binding, decision, key=key)
        existing = [
            record
            for record in log.relay_acknowledgements
            if record.decision_id == binding.decision_id
        ]
        if len(existing) > 1:
            raise AttendedActionRefused(
                "the relay decision has multiple retained acknowledgement records"
            )
        if existing:
            record = existing[0]
            self._verify_relay_ack_mac(record)
            self._verify_relay_ack_context(record, key=key)
            if (
                record.binding() != binding
                or record.engine_ack_result != acknowledgement.engine_ack_result
                or record.retained_decision_id != acknowledgement.retained_decision_id
                or record.retained_decision_digest
                != acknowledgement.retained_decision_digest
                or record.retained_request_digest
                != acknowledgement.retained_request_digest
                or record.retained_status != acknowledgement.retained_status
            ):
                raise AttendedActionRefused(
                    "the relay decision id is already bound to a different "
                    "signed decision or retained engine outcome"
                )
            return
        log.relay_acknowledgements.append(acknowledgement)

    def append(
        self,
        decision: AttendedDecision,
        *,
        relay_binding: Optional[AttendedRelayBinding] = None,
        key: Optional[str] = None,
    ) -> None:
        with self._decision_log_lock():
            log, head = self._read_log_with_head()
            log.decisions.append(decision)
            if relay_binding is not None:
                self._add_relay_acknowledgement(log, relay_binding, decision, key=key)
            self._append_log_snapshot(log=log, expected_head=head)

    def retain_relay_acknowledgement(
        self,
        binding: AttendedRelayBinding,
        decision: AttendedDecision,
        *,
        key: Optional[str] = None,
    ) -> None:
        """Bind a prior exact engine result for an idempotent remote replay."""
        with self._decision_log_lock():
            log, head = self._read_log_with_head()
            retained = [
                candidate
                for candidate in log.decisions
                if candidate.decision_id == decision.decision_id
            ]
            if len(retained) != 1 or retained[0] != decision:
                raise AttendedActionRefused(
                    "the retained engine outcome for this relay cannot be proved"
                )
            before = len(log.relay_acknowledgements)
            self._add_relay_acknowledgement(log, binding, decision, key=key)
            if len(log.relay_acknowledgements) != before:
                self._append_log_snapshot(log=log, expected_head=head)

    def _validated_relay_acknowledgement(
        self,
        log: AttendedDecisionLog,
        binding: AttendedRelayBinding,
        *,
        key: Optional[str] = None,
    ) -> Optional[tuple[AttendedRelayAcknowledgement, AttendedDecision]]:
        records = [
            record
            for record in log.relay_acknowledgements
            if record.decision_id == binding.decision_id
        ]
        if not records:
            orphaned = [
                decision
                for decision in log.decisions
                if decision.idempotency_key == binding.idempotency_key
                and decision.capability_digest == binding.capability_digest
                and decision.action == binding.action
                and decision.status not in {"prepared", "delivery_started"}
            ]
            if orphaned:
                raise AttendedActionRefused(
                    "a retained engine outcome has no authenticated relay "
                    "acknowledgement record"
                )
            return None
        if len(records) != 1:
            raise AttendedActionRefused(
                "the relay decision has multiple retained acknowledgement records"
            )
        record = records[0]
        self._verify_relay_ack_mac(record)
        self._verify_relay_ack_context(record, key=key)
        if record.binding() != binding:
            raise AttendedActionRefused(
                "a re-delivered decision changed its exact signed or "
                "idempotency binding"
            )
        retained = [
            decision
            for decision in log.decisions
            if decision.decision_id == record.retained_decision_id
        ]
        if len(retained) != 1:
            raise AttendedActionRefused(
                "the engine outcome retained for this relay is missing or ambiguous"
            )
        outcome = retained[0]
        if (
            _digest(attended_decision_payload(outcome))
            != record.retained_decision_digest
            or outcome.request_digest != record.retained_request_digest
            or outcome.idempotency_key != record.idempotency_key
            or outcome.capability_digest != record.capability_digest
            or outcome.action != record.action
            or outcome.status != record.retained_status
            or ("refused" if outcome.status == "refused" else "accepted")
            != record.engine_ack_result
        ):
            raise AttendedActionRefused(
                "the engine outcome retained for this relay no longer verifies"
            )
        return record, outcome

    def relay_acknowledgement(
        self,
        binding: AttendedRelayBinding,
        *,
        key: Optional[str] = None,
    ) -> Optional[tuple[AttendedRelayAcknowledgement, AttendedDecision]]:
        """Return an exact retained result, or refuse a changed relay binding."""
        return self._validated_relay_acknowledgement(self._read_log(), binding, key=key)

    def confirm_relay_acknowledgement(
        self,
        binding: AttendedRelayBinding,
        *,
        key: Optional[str] = None,
    ) -> None:
        """Mark the exact relay record only after Cloud accepted the ACK."""
        with self._decision_log_lock():
            log, head = self._read_log_with_head()
            matched = self._validated_relay_acknowledgement(log, binding, key=key)
            if matched is None:
                raise AttendedActionRefused(
                    "the relay acknowledgement has no retained engine outcome"
                )
            record, _outcome = matched
            if record.confirmed:
                return
            index = next(
                index
                for index, candidate in enumerate(log.relay_acknowledgements)
                if candidate == record
            )
            candidate = record.model_copy(
                update={
                    "confirmed": True,
                    "confirmed_at": _iso(_now()),
                    "record_mac": "hmac-sha256:" + ("0" * 64),
                }
            )
            candidate = candidate.model_copy(
                update={"record_mac": self._relay_ack_mac(candidate)}
            )
            log.relay_acknowledgements[index] = candidate
            self._append_log_snapshot(log=log, expected_head=head)

    @contextmanager
    def lease(
        self,
        request: AttendedActionRequest,
        *,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
        now: Optional[datetime] = None,
        wait_s: float = 0.0,
        key: Optional[str] = None,
    ) -> Iterator[None]:
        """Acquire the shared direct/attended continuation lease."""
        from openadapt_flow.runtime.durable.continuation import (
            ContinuationBusy,
            ContinuationCoordinator,
        )

        try:
            with ContinuationCoordinator(self.run_dir, key=key).lease(
                operation=request.action,
                ttl_s=ttl_s,
                now=now,
                wait_s=wait_s,
            ):
                yield
        except ContinuationBusy as exc:
            raise AttendedActionBusy(str(exc)) from exc


def validate_attended_program_receipt(
    run_dir: Path | str,
    *,
    checkpoint: ProgramCheckpoint,
    pending: Optional[PendingEscalation],
    manifest: Any,
    workflow: Workflow,
    live_bundle_version: str,
    historical: bool = False,
) -> ProgramTransitionReceipt:
    """Authenticate and bind a receipt before interpreter restoration."""
    receipt = checkpoint.attended_transition
    if receipt is None:
        raise AttendedActionRefused("the attended program checkpoint has no receipt")
    actions = AttendedActionStore(run_dir)
    stored = actions.read_program_receipt(receipt.pause_id)
    if stored != receipt:
        raise AttendedActionRefused(
            "the program checkpoint does not match its atomic transition receipt"
        )
    if not checkpoint.frames:
        raise AttendedActionRefused(
            "the attended program checkpoint has no interpreter frame"
        )
    from openadapt_flow.runtime.durable.program_checkpoint import TOP_GRAPH_ID

    graph_id = checkpoint.frames[-1].graph_id
    graph = (
        workflow.program
        if graph_id == TOP_GRAPH_ID
        else workflow.subflows.get(graph_id)
    )
    source_state = (
        graph.states.get(receipt.source_state_id) if graph is not None else None
    )
    source_step = source_state.step if source_state is not None else None
    if source_step is None:
        raise AttendedActionRefused("the attended program source action is unavailable")
    validate_attended_checkpoint_identity(
        run_dir,
        checkpoint=checkpoint,
        step=source_step,
        manifest=manifest,
        live_bundle_version=live_bundle_version,
        state_id=receipt.source_state_id,
    )
    from openadapt_flow.qualification import workflow_contract_sha256

    authorization = manifest.governed_authorization
    expected_runtime_inputs_digest = (
        authorization.runtime_inputs_digest if authorization is not None else None
    )
    if (
        not checkpoint.frames
        or receipt.run_id != manifest.run_id
        or receipt.workflow_name != manifest.workflow_name
        or receipt.workflow_name != checkpoint.workflow_name
        or receipt.bundle_version != live_bundle_version
        or receipt.bundle_version != checkpoint.bundle_version
        or checkpoint.seq != receipt.source_checkpoint_seq + 1
        or checkpoint.frames[-1].graph_id != receipt.source_graph_id
        or checkpoint.frames[-1].state_id != receipt.source_state_id
        or checkpoint.verified_state_id != receipt.source_state_id
        or receipt.control_frames_hash != control_frames_hash(checkpoint.frames)
        or receipt.workflow_contract_sha256 != workflow_contract_sha256(workflow)
        or receipt.governed_runtime_inputs_digest != expected_runtime_inputs_digest
        or receipt.bound_params_sha256 != bound_params_sha256(checkpoint.bound_params)
    ):
        raise AttendedActionRefused(
            "the attended program receipt does not match its signed "
            "run/bundle/pause/state/frame lineage"
        )
    if historical:
        # The signed per-run receipt is the durable authority for a prior
        # attended transition. Its original pending file has since been
        # replaced by later progress and a newer active pause.
        return receipt
    is_current_pause = (
        pending is not None
        and pending.program
        and bool(pending.program_frames)
        and pending.program_checkpoint_seq == receipt.source_checkpoint_seq
        and pending.program_frames[-1].graph_id == receipt.source_graph_id
        and pending.program_frames[-1].state_id == receipt.source_state_id
        and pending.state_id == receipt.source_state_id
    )
    if is_current_pause:
        assert pending is not None
        capability = actions.read()
        if (
            receipt.pause_id != capability.pause_id
            or receipt.pause_digest != capability.pause_digest
            or receipt.action not in capability.allowed_actions
            or receipt.control_frames_hash
            != control_frames_hash(pending.program_frames)
            or receipt.cursor_digest != _program_cursor_digest(pending)
            or receipt.cursor_digest != capability.program_cursor_digest
            or checkpoint.transition_history_hash != pending.program_history_hash
            or checkpoint.transition_parent_hash != pending.program_history_hash
            or checkpoint.transition_delta
            or checkpoint.transition_history != pending.program_history
            or capability.run_id != manifest.run_id
            or capability.workflow_name != manifest.workflow_name
            or capability.bundle_version != live_bundle_version
            or capability.state_id != pending.state_id
        ):
            raise AttendedActionRefused(
                "the attended program receipt does not match its current signed "
                "pause and interpreter cursor"
            )
    elif (
        pending is None
        or not pending.program
        or pending.program_checkpoint_seq != checkpoint.seq
    ):
        raise AttendedActionRefused(
            "the durable program pause does not continue from the receipt's "
            "exact checkpoint lineage"
        )
    return receipt


def validate_attended_checkpoint_identity(
    run_dir: Path | str,
    *,
    checkpoint: RunCheckpoint | ProgramCheckpoint,
    step: Step,
    manifest: Any,
    live_bundle_version: str,
    state_id: Optional[str] = None,
) -> Optional[AttendedPauseCapability]:
    """Bind a human-attended checkpoint to its signed source identity.

    The operator can request a continuation. The operator cannot create or
    replace identity evidence. The only accepted identity is the engine-owned
    result retained in the exact signed pause capability.
    """

    attended = checkpoint.actuation in {
        "human_attended",
        "human_attended_skip",
    }
    capability_digest = checkpoint.attended_capability_digest
    if not attended:
        if capability_digest is not None:
            raise AttendedActionRefused(
                "a non-attended checkpoint carries attended identity authority"
            )
        return None
    if not capability_digest:
        raise AttendedActionRefused(
            "the attended checkpoint has no signed source capability"
        )
    capability = AttendedActionStore(run_dir).signed_capability_for_digest(
        capability_digest
    )
    expected_capability_step_id = state_id or step.id
    if (
        capability.run_id != manifest.run_id
        or capability.workflow_name != manifest.workflow_name
        or capability.bundle_version != live_bundle_version
        or checkpoint.run_id != manifest.run_id
        or checkpoint.workflow_name != manifest.workflow_name
        or checkpoint.bundle_version != live_bundle_version
        or capability.step_id != expected_capability_step_id
        or capability.state_id != state_id
        or checkpoint.step_id != step.id
    ):
        raise AttendedActionRefused(
            "the attended source capability does not match the checkpoint lineage"
        )

    required = _source_identity_required(step, manifest)
    if capability.schema_version < 3:
        if required:
            raise AttendedActionRefused(
                "the attended source identity predates identity-bound continuation"
            )
        source_identity = None
    else:
        if capability.source_identity_required != required:
            raise AttendedActionRefused(
                "the signed source identity requirement changed"
            )
        source_identity = capability.source_identity

    source_status = _identity_status(source_identity)
    if source_status == "mismatch":
        raise AttendedActionRefused(
            "the original attended halt retained a conflicting source identity"
        )
    skipped = checkpoint.actuation == "human_attended_skip"
    if skipped:
        if checkpoint.identity is not None:
            raise AttendedActionRefused(
                "a skipped attended action cannot carry source identity proof"
            )
        return capability
    if required and source_status != "verified":
        raise AttendedActionRefused(
            "the attended source action lacks verified identity evidence"
        )
    if checkpoint.identity != source_identity:
        raise AttendedActionRefused(
            "the attended checkpoint identity differs from its signed source proof"
        )
    return capability


def issue_attended_capability(
    run_dir: Path | str,
    *,
    store: CheckpointStore,
    pending: PendingEscalation,
    workflow: Workflow,
    result: StepResult,
    transition_observation: Optional[TransitionObservation] = None,
) -> AttendedPauseCapability:
    manifest = store.read_manifest()
    if manifest is None or not manifest.run_id:
        raise AttendedActionRefused(
            "the durable manifest has no stable run identity; cannot issue "
            "an attended mutation capability"
        )
    return AttendedActionStore(run_dir).issue(
        manifest=manifest,
        pending=pending,
        workflow=workflow,
        result=result,
        transition_observation=transition_observation,
    )


def attended_capability_summary(
    run_dir: Path | str,
) -> Optional[dict[str, Any]]:
    """Browser-safe capability metadata; the HMAC and local paths stay private."""
    try:
        capability = AttendedActionStore(run_dir).read()
    except AttendedActionRefused:
        return None
    return {
        "digest": capability.digest,
        "expires_at": capability.expires_at,
        "allowed_actions": list(capability.allowed_actions),
        "delivery_state": capability.delivery_state,
        "source_identity_required": capability.source_identity_required,
        "source_identity_status": _identity_status(capability.source_identity),
    }


def _refuse_rejected_pause(pending: PendingEscalation) -> None:
    """Refuse every attended action on a pause an operator already rejected.

    A rejection is terminal, so a second answer -- from a stale phone tab, a
    retried relay, or a different operator -- must not reopen the run. This is
    checked before and again under the single-flight lease, because the first
    read happens before the lock is held.
    """
    if getattr(pending, "status", "pending") == "rejected":
        raise AttendedActionRefused(
            "this run was rejected by an operator and is terminal; no further "
            "attended action can be taken on it"
        )


def _terminate_rejected_run(
    run_dir: Path,
    checkpoints: CheckpointStore,
    pending: PendingEscalation,
) -> Optional[str]:
    """End a run an operator rejected, and record the terminal outcome.

    Two durable artifacts, and both matter.

    The PAUSE is rewritten with ``status="rejected"`` rather than cleared.
    Clearing it would make the run look like one that was never paused, and the
    reason it stopped is precisely what an auditor needs; keeping it also makes
    the refusal in :func:`~.approval.enforce_resume_authorization` reachable, so
    "terminal" is enforced rather than merely reported.

    The REPORT gets ``canceled=True`` and a re-derived ``transaction_outcome``.
    This is the consequential design call, and the important part is what it
    does NOT do: it does not force ``CANCELED``. It states the human intent and
    lets :func:`~openadapt_flow.transaction.classify_transaction_outcome` decide
    what the evidence supports. A rejection over a run whose consequential steps
    are positively proven effect-free is ``CANCELED``. A rejection over one that
    may have written is still ``RECONCILIATION_REQUIRED`` -- an operator tapping
    "stop" must never be able to convert an unreconciled write into a clean bill
    of health, and the absence-proof gate that prevents it is the same one every
    other absence-asserting outcome passes through.

    ``execution_outcome`` is deliberately untouched. The run really did HALT;
    ``transaction_outcome`` refines what is known about the business effect
    without rewriting the coarse lifecycle, which is the contract the
    transaction module documents.

    Returns the terminal transaction outcome, or ``None`` when no readable
    report exists. A missing report does not block the rejection: the run is
    terminated by the pause status either way, and refusing to let an operator
    stop a run because its report is unreadable would be the wrong failure.
    """
    from openadapt_flow.ir import RunReport
    from openadapt_flow.transaction import classify_transaction_outcome

    checkpoints.cas_pending(
        checkpoints.model_digest(pending),
        pending.model_copy(update={"status": "rejected"}),
    )

    # Read the report directly rather than through the console's loader: the
    # console is an optional extra and a presentation layer, and the runtime
    # must not acquire a dependency on it to end a run.
    try:
        report = RunReport.model_validate_json(
            (run_dir / "report.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    report.canceled = True
    outcome = classify_transaction_outcome(report)
    report.transaction_outcome = outcome.value
    report.transaction_billable = outcome.is_billable
    report.transaction_platform_fault = outcome.is_platform_fault
    # Atomic, because a torn report.json would lose the only machine-readable
    # record that this run is over.
    target = run_dir / "report.json"
    tmp = target.with_suffix(".json.rejecting")
    tmp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return outcome.value


def _approval(
    capability: AttendedPauseCapability,
    *,
    pending: PendingEscalation,
    operator: str,
    resolution: str,
    run_dir: Path,
) -> ApprovalRecord:
    return issue_resume_approval(
        pending,
        approver=operator,
        resolution=resolution,
        bundle_version=capability.bundle_version,
        workflow_name=capability.workflow_name,
        run_id=capability.run_id,
        run_dir=run_dir,
    )


def _audit_only_decision(
    capability: AttendedPauseCapability,
    request: AttendedActionRequest,
    *,
    operator: str,
    decided_by: Literal["human", "automation", "unknown"],
) -> AttendedDecision:
    """Create a non-actuating decision that leaves the live pause intact."""

    if request.action == "teach":
        return AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=_digest(request),
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
            decided_by=decided_by,
            disposition=request.disposition or "teach_requested",
            status="needs_demonstration",
            message=(
                "Record the corrective demonstration, then run the existing "
                "governed teach command. Its regression/revision gate decides "
                "accepted, banked-progress, or refused; identity-evidence "
                "changes are never auto-promoted."
            ),
            next_transition=capability.expected_next_transition,
        )
    if request.action == "escalate":
        return AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=_digest(request),
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
            decided_by=decided_by,
            disposition=request.disposition or "needs_assistance",
            status="escalated",
            message=(
                "Escalation recorded. The durable pause remains intact and "
                "can be continued after a qualified operator resolves it."
            ),
            next_transition=capability.expected_next_transition,
        )
    raise AttendedActionRefused("this attended action is not audit-only")


def execute_attended_action(
    run_dir: Path | str,
    request: AttendedActionRequest,
    *,
    operator: str,
    decided_by: Literal["human", "automation", "unknown"] = "unknown",
    executor: Optional[AttendedActionExecutor] = None,
    relay_binding: Optional[AttendedRelayBinding] = None,
    key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AttendedDecision:
    """Admit and execute one attended decision under exact binding.

    ``decided_by`` is trusted caller provenance. An undeclared caller remains
    ``unknown``. The untrusted request cannot supply this value.
    """
    from openadapt_flow import crypto as _crypto

    key = _crypto.resolve_key(key)
    run_dir = Path(run_dir)
    if not operator.strip():
        raise ApprovalRequired("attended actions require an authenticated operator")
    expected_dispositions = {
        "continue": {None, "completed_by_operator"},
        "skip": {None, "not_applicable"},
        "teach": {None, "teach_requested"},
        "escalate": {None, "cannot_complete", "needs_assistance"},
        "reject": {None, "rejected_by_operator"},
        "reconcile": {None, "reconciliation_requested"},
    }
    if request.disposition not in expected_dispositions[request.action]:
        raise AttendedActionRefused(
            "the disposition does not match the requested attended action"
        )
    checkpoints = CheckpointStore(run_dir, key=key)
    manifest = checkpoints.read_manifest()
    if manifest is None:
        raise AttendedActionRefused("the run is not durably paused")
    try:
        checkpoints._validate_local_namespace(manifest)  # noqa: SLF001
    except StateDiverged as exc:
        raise AttendedActionRefused(
            "the durable run identity or canonical path changed"
        ) from exc

    actions = AttendedActionStore(run_dir)
    prior = actions.prior(request)
    if prior is not None:
        if prior.status in {"delivery_started", "delivery_uncertain"}:
            recovery = getattr(executor, "recover_reconciliation_receipt", None)
            capability = actions.signed_capability_for_digest(request.capability_digest)
            if (
                request.action == "reconcile"
                and prior.action == "reconcile"
                and prior.capability_digest == capability.digest
                and callable(recovery)
            ):
                from openadapt_flow.runtime.durable.continuation import (
                    ContinuationCoordinator,
                )

                if ContinuationCoordinator(run_dir, key=key).prove_completed_pause(
                    source_pause_binding=capability.pause_digest
                ):
                    result = AttendedExecutionResult.model_validate(
                        recovery(run_dir, capability, _digest(request))
                    )
                    if (
                        result.status == "completed"
                        and result.report_success is True
                        and result.transition_receipt_digest is not None
                    ):
                        decision = AttendedDecision(
                            pause_id=capability.pause_id,
                            capability_digest=capability.digest,
                            request_digest=_digest(request),
                            idempotency_key=request.idempotency_key,
                            action="reconcile",
                            operator=operator,
                            decided_by=decided_by,
                            disposition=request.disposition,
                            status="completed",
                            message=result.message,
                            report_success=True,
                            next_transition=result.next_transition,
                            transition_receipt_digest=result.transition_receipt_digest,
                        )
                        actions.append(decision, relay_binding=relay_binding, key=key)
                        return decision
            raise AttendedActionRefused(
                "the prior request may have crossed the delivery boundary; "
                "automatic retry is refused until an audited reconciliation"
            )
        if prior.status != "prepared":
            if relay_binding is not None:
                actions.retain_relay_acknowledgement(relay_binding, prior, key=key)
            return prior

    pending = checkpoints.read_pending()
    if pending is None:
        raise AttendedActionRefused("the run is not durably paused")
    _refuse_rejected_pause(pending)
    capability = actions.validate(request, pending=pending, manifest=manifest, now=now)
    unresolved_before_lease = actions.unresolved_delivery(capability.pause_id)
    if unresolved_before_lease is not None:
        if request.action in {"teach", "escalate"}:
            # An uncertain input edge must prevent another action, but it must
            # not prevent the operator from preserving a demonstration request
            # or escalating the retained pause. These records do not resume,
            # retry, reject, or otherwise actuate the workflow.
            decision = _audit_only_decision(
                capability,
                request,
                operator=operator,
                decided_by=decided_by,
            )
            actions.append(decision, relay_binding=relay_binding, key=key)
            return decision
        if request.action == "reject":
            raise AttendedActionRefused(
                "this action may already have been delivered; ending the run "
                "cannot un-send it and would remove the pause needed to "
                "reconcile. Escalate it instead"
            )
        if request.action in {"continue", "skip"}:
            raise AttendedActionRefused(
                "another request for this pause may have crossed the delivery "
                "boundary; reconcile its live state before continuing or skipping"
            )
    try:
        checkpoints.validate_namespace(manifest)
    except StateDiverged as exc:
        raise AttendedActionRefused(
            "the durable pause changed outside its monotonic authority"
        ) from exc

    reject_wait_s = 0.0
    if request.action == "reject":
        from openadapt_flow.runtime.durable.continuation import (
            ContinuationCoordinator,
        )

        preemption = ContinuationCoordinator(run_dir, key=key).request_reject(
            expected_run_id=capability.run_id,
            expected_pause_binding=capability.pause_digest,
        )
        if preemption == "uncertain":
            raise AttendedActionRefused(
                "continuation delivery already started; rejection now requires "
                "effect reconciliation and cannot claim a clean cancellation"
            )
        if preemption == "preempted":
            reject_wait_s = 5.0

    with actions.lease(
        request,
        now=now,
        wait_s=reject_wait_s,
        key=key,
    ):
        # Repeat the complete validation under the lease: the bundle/pause may
        # have changed between page load and lock acquisition.
        pending = checkpoints.read_pending()
        manifest = checkpoints.read_manifest()
        if pending is None or manifest is None:
            raise AttendedActionRefused("the run is no longer durably paused")
        checkpoints.validate_namespace(manifest)
        _refuse_rejected_pause(pending)
        capability = actions.validate(
            request, pending=pending, manifest=manifest, now=now
        )
        prior = actions.prior(request)
        if prior is not None:
            if prior.status in {"delivery_started", "delivery_uncertain"}:
                raise AttendedActionRefused(
                    "the prior request may have crossed the delivery boundary; "
                    "automatic retry is refused until reconciliation"
                )
            if prior.status != "prepared":
                if relay_binding is not None:
                    actions.retain_relay_acknowledgement(relay_binding, prior, key=key)
                return prior
        request_digest = _digest(request)
        unresolved = actions.unresolved_delivery(capability.pause_id)
        if unresolved is not None and request.action in {"continue", "skip"}:
            raise AttendedActionRefused(
                "another request for this pause may have crossed the delivery "
                "boundary; reconcile its live state before continuing or skipping"
            )

        if request.action == "reject":
            # Two authorities, and neither subsumes the other.
            #
            # The SEALED one is already enforced above by
            # `AttendedActionStore.validate`: a pause issued over an uncertain
            # delivery never carried `reject` in its signed action set, so the
            # request never reaches here.
            #
            # This is the LIVE one. A delivery can become uncertain AFTER the
            # capability was sealed, when another request for this same pause
            # crosses the boundary without returning a terminal receipt. The
            # signed capability cannot see that, so the journal is re-read.
            # Dropping this check would leave a real pause unguarded.
            # `_allowed_actions` explains why an uncertain delivery is the one
            # pause reject is withheld on.
            if unresolved is not None:
                raise AttendedActionRefused(
                    "this action may already have been delivered; ending the "
                    "run cannot un-send it and would remove the pause needed "
                    "to reconcile. Escalate it instead"
                )
            terminal = _terminate_rejected_run(run_dir, checkpoints, pending)
            decision = AttendedDecision(
                pause_id=capability.pause_id,
                capability_digest=capability.digest,
                request_digest=request_digest,
                idempotency_key=request.idempotency_key,
                action=request.action,
                operator=operator,
                decided_by=decided_by,
                disposition=request.disposition or "rejected_by_operator",
                status="rejected",
                message=(
                    "Rejection recorded and the run is over. Nothing was "
                    "actuated, the durable pause is retained as the audit "
                    "record of what was rejected, and no approval can resume "
                    "it. The terminal transaction outcome is "
                    f"{terminal or 'unrecorded (no readable run report)'}. "
                    "Start a fresh run if the workflow should be attempted "
                    "again."
                ),
                next_transition=capability.expected_next_transition,
            )
            actions.append(decision, relay_binding=relay_binding, key=key)
            return decision

        if request.action in {"teach", "escalate"}:
            decision = _audit_only_decision(
                capability,
                request,
                operator=operator,
                decided_by=decided_by,
            )
            actions.append(decision, relay_binding=relay_binding, key=key)
            return decision

        if executor is None:
            raise AttendedActionRefused(
                "this console has no deployment-bound attended executor; start "
                "it with the qualified backend/effect configuration"
            )

        resolution = (
            "operator completed the live-app task; verify and continue"
            if request.action == "continue"
            else (
                "operator requested read-only effect reconciliation; never re-dispatch"
                if request.action == "reconcile"
                else "operator requested policy-scoped skip"
            )
        )
        approval = _approval(
            capability,
            pending=pending,
            operator=operator,
            resolution=resolution,
            run_dir=run_dir,
        )
        from openadapt_flow.runtime.durable.continuation import (
            ContinuationCoordinator,
            current_continuation_token,
        )

        continuation_token = current_continuation_token()
        if continuation_token is None:
            raise AttendedActionRefused(
                "the attended decision lost its continuation authority"
            )
        ContinuationCoordinator(run_dir, key=key).bind_approval(
            continuation_token,
            approval,
        )
        prepared = AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=request_digest,
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
            decided_by=decided_by,
            disposition=request.disposition,
            status="prepared",
            message="request admitted; no delivery attempted",
            next_transition=capability.expected_next_transition,
        )
        if prior is None:
            actions.append(prepared)
        started = prepared.model_copy(
            update={
                "decision_id": secrets.token_hex(16),
                "status": "delivery_started",
                "message": (
                    "deployment-bound verification/resume started; a crash "
                    "after this record makes delivery uncertain"
                ),
                "created_at": _iso(_now()),
            }
        )
        actions.append(started)
        executor_returned = False
        coordinator = ContinuationCoordinator(run_dir, key=key)
        try:
            raw_result = (
                executor.continue_run(run_dir, capability, approval)
                if request.action == "continue"
                else (
                    executor.reconcile_run(
                        run_dir, capability, approval, request_digest
                    )
                    if request.action == "reconcile"
                    else executor.skip_run(run_dir, capability, approval)
                )
            )
            executor_returned = True
            result = AttendedExecutionResult.model_validate(raw_result)
            coordinator.attest_executor_outcome(
                continuation_token,
                status=result.status,
                report_success=result.report_success,
                source_pause_binding=capability.pause_digest,
            )
        except Exception as exc:
            proven = coordinator.prove_executor_outcome(
                continuation_token,
                source_pause_binding=capability.pause_digest,
            )
            if proven is not None and proven[0] in {"completed", "halted"}:
                proven_status, proven_success = proven
                recovery = getattr(executor, "recover_reconciliation_receipt", None)
                if request.action == "reconcile" and proven_status == "completed":
                    if not callable(recovery):
                        raise AttendedActionRefused(
                            "the durable reconciliation completed but this executor "
                            "cannot recover its exact transition receipt"
                        ) from exc
                    result = AttendedExecutionResult.model_validate(
                        recovery(run_dir, capability, request_digest)
                    )
                    if (
                        result.status != "completed"
                        or result.report_success is not True
                        or result.transition_receipt_digest is None
                    ):
                        raise AttendedActionRefused(
                            "the recovered reconciliation receipt is not a completed "
                            "durable transition"
                        ) from exc
                else:
                    durable_receipt = (
                        _committed_transition_receipt_digest(
                            run_dir, capability, key=key
                        )
                        if proven_status == "completed"
                        else None
                    )
                    if proven_status == "completed" and durable_receipt is None:
                        raise AttendedActionRefused(
                            "the recovered completed outcome has no exact durable "
                            "transition receipt"
                        ) from exc
                    result = AttendedExecutionResult(
                        status=proven_status,
                        message=(
                            "The durable run completed and its exact report was "
                            "verified after the executor transport failed."
                            if proven_status == "completed"
                            else (
                                "The durable continuation halted at a new exact pause; "
                                "the executor transport did not carry its valid receipt."
                                if proven_status == "halted"
                                else "The durable state proves that no continuation "
                                "delivery was admitted."
                            )
                        ),
                        report_success=proven_success,
                        resumed_from=capability.step_id,
                        next_transition=capability.expected_next_transition,
                        transition_receipt_digest=durable_receipt,
                    )
            else:
                try:
                    coordinator.mark_executor_uncertain(continuation_token)
                except Exception as fence_exc:
                    raise AttendedActionRefused(
                        "the executor outcome was invalid and its continuation "
                        "authority could not be fenced"
                    ) from fence_exc
                uncertain = started.model_copy(
                    update={
                        "decision_id": secrets.token_hex(16),
                        "status": "delivery_uncertain",
                        "message": (
                            "the deployment-bound action did not return a terminal "
                            "receipt; reconcile live state before any retry"
                        ),
                        "created_at": _iso(_now()),
                    }
                )
                actions.append(uncertain)
                if not executor_returned:
                    raise
                if isinstance(exc, AttendedActionRefused):
                    raise
                raise AttendedActionRefused(
                    "the executor did not return an outcome proven by durable state"
                ) from exc
        if result.status == "completed" and (
            result.report_success is not True
            or result.transition_receipt_digest is None
        ):
            raise AttendedActionRefused(
                "a completed attended result requires report_success=true and an "
                "exact durable transition receipt before it can enter the decision "
                "journal"
            )
        decision = AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=request_digest,
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
            decided_by=decided_by,
            disposition=request.disposition,
            status=result.status,
            message=result.message,
            report_success=result.report_success,
            next_transition=result.next_transition,
            transition_receipt_digest=result.transition_receipt_digest,
        )
        actions.append(decision, relay_binding=relay_binding, key=key)
        return decision


def _validated_attended_result(
    step: Step,
    result: StepResult,
    *,
    identity: Optional[IdentityCheck],
    skipped: bool,
    params: dict[str, str],
    manifest: Any,
) -> StepResult:
    """Return the exact evidence shape emitted by an attended completion."""

    retained = result.model_copy(
        deep=True,
        update={
            "ok": True,
            "skipped": skipped,
            "actuation": "human_attended_skip" if skipped else "human_attended",
            "identity": None if skipped else identity,
            "delivery_attempted": False,
            # Fresh revalidation can use the normal resolver and settled-state
            # machinery to prove the postcondition and business effect.  Those
            # observations do not mean that the engine delivered the action.
            # Retain the human path exactly and do not convert revalidation
            # evidence into a synthetic GUI receipt.
            "delivery_receipt": None,
            "delivery_uncertainty": None,
            "resolution": None,
            "drag_end_resolution": None,
            "starting_state_settled": None,
            "input_verified": None,
            "input_retried": False,
            "fresh_actuation_events": [],
        },
    )
    from openadapt_flow.action_evidence import action_evidence_error

    authorization = manifest.governed_authorization
    evidence_error = action_evidence_error(
        step,
        retained,
        params=params,
        identity_required=bool(
            not skipped
            and (
                step.identity_armed
                or (
                    authorization is not None
                    and authorization.requires_verified_identity(step.id)
                )
            )
        ),
        strict_production=(
            authorization is not None
            and authorization.execution_profile in {"standard", "regulated"}
        ),
    )
    if evidence_error is not None:
        raise AttendedActionRefused(
            "the human-completed checkpoint has invalid action evidence: "
            f"{evidence_error}"
        )
    return retained


def checkpoint_human_completed_step(
    run_dir: Path | str,
    *,
    capability: AttendedPauseCapability,
    approval: ApprovalRecord,
    result: StepResult,
    params: dict[str, str],
    key: Optional[str] = None,
) -> RunCheckpoint:
    """Advance a linear resume point after outcome verification, without acting."""
    store = CheckpointStore(run_dir, key=key)
    manifest = store.read_manifest()
    if manifest is None:
        raise AttendedActionRefused(
            "the human-completed checkpoint has no durable run manifest"
        )
    workflow = Workflow.load(manifest.bundle_dir, key=key)
    if (
        capability.step_index < 0
        or capability.step_index >= len(workflow.steps)
        or workflow.steps[capability.step_index].id != capability.step_id
    ):
        raise AttendedActionRefused(
            "the human-completed checkpoint does not match the workflow step"
        )
    source_step = workflow.steps[capability.step_index]
    if not result.ok or result.postconditions_ok is False:
        raise AttendedActionRefused(
            "the human-completed step did not pass outcome verification"
        )
    if result.effect_verified is False:
        raise AttendedActionRefused(
            "the human-completed step's independent effect was not confirmed"
        )
    authenticated = AttendedActionStore(run_dir).signed_capability_for_digest(
        capability.digest
    )
    if authenticated != capability:
        raise AttendedActionRefused(
            "the human-completed checkpoint does not use the signed capability"
        )
    pending = store.read_pending()
    if pending is None or approval_pause_digest(pending) != capability.pause_digest:
        raise AttendedActionRefused(
            "the human-completed checkpoint does not match the active pause"
        )
    try:
        approved = enforce_resume_authorization(
            pending,
            approval,
            bundle_version=capability.bundle_version,
            run_id=capability.run_id,
            workflow_name=capability.workflow_name,
            run_dir=run_dir,
        )
    except ResumeRefused as exc:
        raise AttendedActionRefused(
            "the human-completed checkpoint has invalid approval authority"
        ) from exc
    if capability.source_identity_required and (
        capability.schema_version < 3
        or _identity_status(capability.source_identity) != "verified"
    ):
        raise AttendedActionRefused(
            "the human-completed source lacks verified identity evidence"
        )
    if _identity_status(capability.source_identity) == "mismatch":
        raise AttendedActionRefused(
            "the human-completed source retained a conflicting identity"
        )
    retained_result = _validated_attended_result(
        source_step,
        result,
        identity=capability.source_identity,
        skipped=False,
        params=params,
        manifest=manifest,
    )
    checkpoint = RunCheckpoint(
        run_id=capability.run_id,
        workflow_name=capability.workflow_name,
        bundle_version=capability.bundle_version,
        step_index=capability.step_index,
        step_id=capability.step_id,
        intent=result.intent,
        next_step_index=capability.step_index + 1,
        params=dict(params),
        effect_verified=retained_result.effect_verified,
        effect_approved_unverified=retained_result.effect_approved_unverified,
        effect_contract_hashes=list(retained_result.effect_contract_hashes),
        effect_evidence=list(retained_result.effect_evidence),
        identity=capability.source_identity,
        input_verified=retained_result.input_verified,
        starting_state_settled=retained_result.starting_state_settled,
        delivery_attempted=retained_result.delivery_attempted,
        delivery_receipt=retained_result.delivery_receipt,
        drag_end_resolution=retained_result.drag_end_resolution,
        fresh_actuation_events=list(retained_result.fresh_actuation_events),
        postconditions_ok=retained_result.postconditions_ok,
        expected_postconditions=[
            condition.model_copy(deep=True) for condition in source_step.expect
        ],
        skipped=False,
        actuation="human_attended",
        delivery_uncertainty=retained_result.delivery_uncertainty,
        resolution=retained_result.resolution,
        governed_authorization_id=(
            manifest.governed_authorization.authorization_id
            if manifest.governed_authorization is not None
            else None
        ),
        governed_approval_source=(
            manifest.governed_authorization.approval_source
            if manifest.governed_authorization is not None
            else None
        ),
        attended_capability_digest=capability.digest,
    )
    from openadapt_flow.runtime.durable.continuation import (
        ContinuationCoordinator,
        current_continuation_token,
    )

    token = current_continuation_token()
    if token is None:
        raise AttendedActionRefused(
            "the human-completed checkpoint lost its continuation authority"
        )
    coordinator = ContinuationCoordinator(run_dir, key=key)
    coordinator.bind_approval(token, approved)
    coordinator.before_delivery(token)
    store.write_checkpoint(checkpoint)
    store.commit_approval_transition(
        expected_pending=pending,
        approval=approved,
        target_status="approved",
    )
    coordinator.acknowledge_progress(token, external_delivery=True)
    return checkpoint


class BoundAttendedExecutor:
    """Real engine executor constructed from a deployment-bound Replayer factory.

    The factory must return a fresh Replayer wired to the qualified live backend,
    effect verifier, policy authorization, and egress posture.  The console
    never accepts backend credentials or challenge answers in an HTTP payload.
    """

    def __init__(
        self,
        replayer_factory: Callable[[Any], Any],
        *,
        key: Optional[str] = None,
    ) -> None:
        from openadapt_flow import crypto as _crypto

        self.replayer_factory = replayer_factory
        self.key = _crypto.resolve_key(key)
        # Per-run filesystem leases prevent duplicate decisions for one pause.
        # The executor additionally owns one shared live backend/session, so
        # actions for different runs must not observe or drive it concurrently.
        self._live_session_lock = threading.Lock()

    @contextmanager
    def _exclusive_live_session(self) -> Iterator[None]:
        if not self._live_session_lock.acquire(blocking=False):
            raise AttendedActionBusy(
                "the qualified live application session is serving another "
                "attended action; reload after that decision completes"
            )
        try:
            yield
        finally:
            self._live_session_lock.release()

    @staticmethod
    def _expected(workflow: Workflow, step_index: int) -> str:
        next_index = step_index + 1
        return (
            workflow.steps[next_index].id
            if next_index < len(workflow.steps)
            else "<complete>"
        )

    def _load(
        self, run_dir: Path, capability: AttendedPauseCapability
    ) -> tuple[CheckpointStore, Any, Workflow]:
        store = CheckpointStore(run_dir, key=self.key)
        manifest = store.read_manifest()
        if manifest is None:
            raise AttendedActionRefused("durable manifest missing")
        if manifest.run_id != capability.run_id:
            raise AttendedActionRefused("run identity changed after pause")
        if bundle_version(manifest.bundle_dir) != capability.bundle_version:
            raise BundleMismatch("bundle changed after attended capability issuance")
        workflow = Workflow.load(manifest.bundle_dir, key=self.key)
        if workflow.name != capability.workflow_name:
            raise AttendedActionRefused(
                "workflow identity changed after attended capability issuance"
            )
        authenticated = AttendedActionStore(run_dir).signed_capability_for_digest(
            capability.digest
        )
        if authenticated != capability:
            raise AttendedActionRefused(
                "the attended executor did not receive the signed pause capability"
            )
        pending = store.read_pending()
        source_step = _source_step(workflow, pending) if pending is not None else None
        if (
            pending is None
            or source_step is None
            or approval_pause_digest(pending) != capability.pause_digest
            or capability.source_identity_required
            != _source_identity_required(source_step, manifest)
            or (
                capability.source_identity_required
                and (
                    capability.schema_version < 3
                    or _identity_status(capability.source_identity) != "verified"
                )
            )
            or _identity_status(capability.source_identity) == "mismatch"
        ):
            raise AttendedActionRefused(
                "the signed attended source identity no longer matches the run"
            )
        if workflow.program is not None:
            state = (
                _program_pause_state(workflow, pending) if pending is not None else None
            )
            if (
                pending is None
                or state is None
                or capability.program_cursor_digest is None
                or capability.program_cursor_digest != _program_cursor_digest(pending)
                or capability.state_id != state.id
                or capability.expected_next_transition != "<program-transition-receipt>"
            ):
                raise AttendedActionRefused(
                    "the exact attended interpreter cursor no longer matches "
                    "the qualified program action"
                )
        else:
            if (
                not 0 <= capability.step_index < len(workflow.steps)
                or workflow.steps[capability.step_index].id != capability.step_id
            ):
                raise AttendedActionRefused(
                    "paused step identity no longer matches the qualified workflow"
                )
            if self._expected(workflow, capability.step_index) != (
                capability.expected_next_transition
            ):
                raise AttendedActionRefused(
                    "the expected next transition no longer matches the workflow"
                )
        return store, manifest, workflow

    def recover_reconciliation_receipt(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        request_digest: str,
    ) -> AttendedExecutionResult:
        """Rebuild a missing local receipt from one committed transition.

        This path is used only after durable authority proves terminal success.
        It never opens the target application and it never repeats the source
        action.  The checkpoint/program receipt is the source of truth; the
        local reconciliation record is a signed, idempotent projection of it.
        """

        if capability.delivery_state == "not_delivered":
            raise AttendedActionRefused(
                "positive non-delivery evidence cannot produce reconciliation"
            )
        delivery_state = capability.delivery_state
        store = CheckpointStore(run_dir, key=self.key)
        manifest = store.read_manifest()
        if manifest is None or manifest.run_id != capability.run_id:
            raise AttendedActionRefused(
                "the durable reconciliation run identity is unavailable"
            )
        if bundle_version(manifest.bundle_dir) != capability.bundle_version:
            raise BundleMismatch("bundle changed after attended capability issuance")
        workflow = Workflow.load(manifest.bundle_dir, key=self.key)
        if workflow.name != capability.workflow_name:
            raise AttendedActionRefused(
                "workflow identity changed after attended capability issuance"
            )
        if (
            AttendedActionStore(run_dir).signed_capability_for_digest(capability.digest)
            != capability
        ):
            raise AttendedActionRefused(
                "the reconciliation recovery capability is not signed"
            )
        checkpoint: Any
        if workflow.program is not None:
            program_candidates: list[ProgramCheckpoint] = [
                item
                for item in store.program_checkpoints()
                if item.attended_transition is not None
                and _matches_reconciliation_checkpoint(
                    item,
                    capability=capability,
                    request_digest=request_digest,
                    delivery_state=delivery_state,
                    effect_contract_hashes=tuple(item.new_effect_keys),
                )
            ]
            checkpoint = program_candidates[0] if len(program_candidates) == 1 else None
            durable_receipt = (
                _digest(checkpoint.attended_transition)
                if checkpoint is not None
                else None
            )
        else:
            linear_candidates: list[RunCheckpoint] = [
                item
                for item in store.checkpoints()
                if _matches_reconciliation_checkpoint(
                    item,
                    capability=capability,
                    request_digest=request_digest,
                    delivery_state=delivery_state,
                    effect_contract_hashes=tuple(item.effect_contract_hashes),
                )
            ]
            checkpoint = linear_candidates[0] if len(linear_candidates) == 1 else None
            durable_receipt = _digest(checkpoint) if checkpoint is not None else None
        if checkpoint is None or durable_receipt is None:
            raise AttendedActionRefused(
                "the completed durable transition does not bind this reconciliation"
            )
        AttendedActionStore(run_dir).write_reconciliation_receipt(
            AttendedReconciliationReceipt(
                pause_id=capability.pause_id,
                capability_digest=capability.digest,
                request_digest=request_digest,
                expected_transition_digest=capability.expected_transition_digest,
                transition_receipt_digest=durable_receipt,
                delivery_state=delivery_state,
                effect_contract_hashes=tuple(
                    checkpoint.attended_reconciliation_effect_contract_hashes
                ),
                reconciled_at=capability.issued_at,
            )
        )
        return AttendedExecutionResult(
            status="completed",
            message=(
                "The durable reconciliation transition completed and its local "
                "receipt was recovered without re-dispatching the source action."
            ),
            report_success=True,
            resumed_from=capability.step_id,
            next_transition=capability.expected_next_transition,
            transition_receipt_digest=durable_receipt,
        )

    @staticmethod
    def _bind_authorization(replayer: Any, manifest: Any) -> None:
        if manifest.governed_authorization is not None:
            existing = getattr(replayer, "governed_authorization", None)
            if existing is not None and existing != manifest.governed_authorization:
                raise BundleMismatch(
                    "attended Replayer carries a different governed authorization"
                )
            replayer.governed_authorization = manifest.governed_authorization
            replayer.governed_continuation = True

    def _resume(
        self,
        *,
        run_dir: Path,
        store: CheckpointStore,
        manifest: Any,
        workflow: Workflow,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
        result: StepResult,
        skipped: bool,
        resume_replayer: Any,
        reconciliation_request_digest: Optional[str] = None,
        reconciliation_delivery_state: Optional[Literal["delivered", "unknown"]] = None,
        reconciliation_effect_contract_hashes: tuple[str, ...] = (),
    ) -> AttendedExecutionResult:
        # Fresh verification can take long enough for an independent durable
        # CLI/operator process to replace or clear the pause.  Re-bind the
        # exact signed pause immediately before committing the human-completed
        # checkpoint; never approve a newer pause under an older capability.
        pending = store.read_pending()
        if pending is None or approval_pause_digest(pending) != capability.pause_digest:
            raise AttendedActionRefused(
                "the exact attended pause changed before checkpoint commit"
            )
        source_step = workflow.steps[capability.step_index]
        retained_result = _validated_attended_result(
            source_step,
            result,
            identity=(None if skipped else capability.source_identity),
            skipped=skipped,
            params=dict(manifest.params),
            manifest=manifest,
        )
        checkpoint = RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=capability.workflow_name,
            bundle_version=capability.bundle_version,
            step_index=capability.step_index,
            step_id=capability.step_id,
            intent=result.intent,
            next_step_index=capability.step_index + 1,
            params=dict(manifest.params),
            effect_verified=retained_result.effect_verified,
            effect_approved_unverified=retained_result.effect_approved_unverified,
            effect_contract_hashes=list(retained_result.effect_contract_hashes),
            effect_evidence=list(retained_result.effect_evidence),
            governed_authorization_id=(
                manifest.governed_authorization.authorization_id
                if manifest.governed_authorization is not None
                else None
            ),
            governed_approval_source=(
                manifest.governed_authorization.approval_source
                if manifest.governed_authorization is not None
                else None
            ),
            input_verified=retained_result.input_verified,
            starting_state_settled=retained_result.starting_state_settled,
            delivery_attempted=retained_result.delivery_attempted,
            delivery_receipt=retained_result.delivery_receipt,
            drag_end_resolution=retained_result.drag_end_resolution,
            fresh_actuation_events=list(retained_result.fresh_actuation_events),
            postconditions_ok=retained_result.postconditions_ok,
            expected_postconditions=(
                [
                    condition.model_copy(deep=True)
                    for condition in workflow.steps[capability.step_index].expect
                ]
                if not skipped
                else []
            ),
            skipped=skipped,
            actuation="human_attended_skip" if skipped else "human_attended",
            identity=(None if skipped else capability.source_identity),
            delivery_uncertainty=retained_result.delivery_uncertainty,
            resolution=retained_result.resolution,
            attended_capability_digest=capability.digest,
            attended_reconciliation_request_digest=reconciliation_request_digest,
            attended_reconciliation_expected_transition_digest=(
                capability.expected_transition_digest
                if reconciliation_request_digest is not None
                else None
            ),
            attended_reconciliation_delivery_state=(
                reconciliation_delivery_state
                if reconciliation_request_digest is not None
                else None
            ),
            attended_reconciliation_effect_contract_hashes=list(
                reconciliation_effect_contract_hashes
            ),
            attended_reconciliation_at=(
                capability.issued_at
                if reconciliation_request_digest is not None
                else None
            ),
        )
        from openadapt_flow.runtime.durable.continuation import (
            ContinuationCoordinator,
            current_continuation_token,
        )

        token = current_continuation_token()
        if token is None:
            raise AttendedActionRefused(
                "the attended completion lost its continuation authority"
            )
        # A human has already completed the source action, but Flow must not
        # commit that continuation into durable local state until the same
        # exact delivery sequence has a server-owned permit.  This makes a
        # missing or refused production authority leave the checkpoint and
        # approval untouched for reconciliation.
        coordinator = ContinuationCoordinator(run_dir, key=self.key)
        coordinator.before_delivery(token)
        store.write_checkpoint(checkpoint)
        store.commit_approval_transition(
            expected_pending=pending,
            approval=approval,
            target_status="approved",
        )
        coordinator.acknowledge_progress(
            token,
            external_delivery=True,
        )

        # Import lazily to avoid a durable-module cycle.
        from openadapt_flow.runtime.durable.resume import resume

        resumed = resume(
            run_dir,
            resume_replayer,
            approval=approval,
            key=self.key,
        )
        return AttendedExecutionResult(
            status="completed" if resumed.success else "halted",
            message=(
                "Human-completed outcome verified; resumed after the attended "
                "step without re-actuating it."
                if resumed.success and not skipped
                else (
                    "Declared optional step skipped; resumed without actuation."
                    if resumed.success
                    else "The deterministic continuation halted and remains auditable."
                )
            ),
            report_success=resumed.success,
            resumed_from=capability.step_id,
            next_transition=capability.expected_next_transition,
            # The durable checkpoint is the linear transition commitment. It
            # records the exact source pause capability and the verified result
            # before ``resume`` can execute any successor step.
            transition_receipt_digest=(
                _digest(checkpoint) if resumed.success else None
            ),
        )

    @staticmethod
    def _program_context(
        store: CheckpointStore,
        workflow: Workflow,
        capability: AttendedPauseCapability,
    ) -> tuple[PendingEscalation, State, dict[str, str]]:
        pending = store.read_pending()
        state = _program_pause_state(workflow, pending) if pending is not None else None
        if (
            pending is None
            or state is None
            or not pending.program_frames
            or capability.program_cursor_digest is None
            or capability.program_cursor_digest != _program_cursor_digest(pending)
        ):
            raise AttendedActionRefused(
                "the exact attended interpreter cursor is unavailable or changed"
            )
        return pending, state, dict(pending.program_frames[-1].params)

    def _resume_program(
        self,
        *,
        run_dir: Path,
        store: CheckpointStore,
        manifest: Any,
        workflow: Workflow,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
        pending: PendingEscalation,
        state: State,
        params: dict[str, str],
        result: StepResult,
        skipped: bool,
        target_state_id: Optional[str],
        resume_replayer: Any,
        reconciliation_request_digest: Optional[str] = None,
        reconciliation_delivery_state: Optional[Literal["delivered", "unknown"]] = None,
        reconciliation_effect_contract_hashes: tuple[str, ...] = (),
    ) -> AttendedExecutionResult:
        if state.step is None or not pending.program_frames:
            raise AttendedActionRefused("the paused program action is unavailable")
        source_seq = pending.program_checkpoint_seq
        cursor_digest = capability.program_cursor_digest
        if cursor_digest is None:
            raise AttendedActionRefused("the program cursor is not signed")
        from openadapt_flow.qualification import workflow_contract_sha256

        receipt = ProgramTransitionReceipt(
            run_id=capability.run_id,
            workflow_name=capability.workflow_name,
            bundle_version=capability.bundle_version,
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            governed_runtime_inputs_digest=(
                manifest.governed_authorization.runtime_inputs_digest
                if manifest.governed_authorization is not None
                else None
            ),
            bound_params_sha256=bound_params_sha256(params),
            pause_id=capability.pause_id,
            pause_digest=capability.pause_digest,
            action="skip" if skipped else "continue",
            source_checkpoint_seq=source_seq,
            source_graph_id=pending.program_frames[-1].graph_id,
            source_state_id=state.id,
            target_state_id=target_state_id,
            control_frames_hash=control_frames_hash(pending.program_frames),
            cursor_digest=cursor_digest,
            created_at=capability.issued_at,
        )
        action_store = AttendedActionStore(run_dir)
        receipt = action_store.seal_program_receipt(receipt)
        attended_effects = effects_for_actuation(state.step, "gui")
        retained_result = _validated_attended_result(
            state.step,
            result,
            identity=(None if skipped else capability.source_identity),
            skipped=skipped,
            params=params,
            manifest=manifest,
        )
        resolved_effects = (
            [
                effect.model_dump(mode="json")
                for effect in resume_replayer._resolve_effects(attended_effects, params)
            ]
            if (
                retained_result.effect_verified is True
                or retained_result.effect_approved_unverified
            )
            and attended_effects
            else []
        )
        expected_texts = (
            [
                condition.text
                for condition in state.step.expect
                if (
                    condition.kind.value
                    if hasattr(condition.kind, "value")
                    else str(condition.kind)
                )
                == "text_present"
                and condition.text
            ]
            if not skipped
            else []
        )
        checkpoint = ProgramCheckpoint(
            run_id=manifest.run_id,
            workflow_name=capability.workflow_name,
            seq=source_seq + 1,
            verified_state_id=state.id,
            intent=state.step.intent,
            frames=list(pending.program_frames),
            bound_params=params,
            new_effect_keys=(
                list(retained_result.effect_contract_hashes)
                if retained_result.effect_verified is True
                else []
            ),
            new_effects=(
                resolved_effects if retained_result.effect_verified is True else []
            ),
            new_effect_evidence=(
                list(retained_result.effect_evidence)
                if retained_result.effect_verified is True
                else []
            ),
            new_unverified_effect_keys=(
                list(retained_result.effect_contract_hashes)
                if retained_result.effect_approved_unverified
                else []
            ),
            new_unverified_effects=(
                resolved_effects if retained_result.effect_approved_unverified else []
            ),
            step_id=state.step.id,
            # The live verifier's result describes the next continuation
            # target. The signed capability retains the already human-actuated
            # source identity used for the completed source step.
            identity=(None if skipped else capability.source_identity),
            input_verified=retained_result.input_verified,
            starting_state_settled=retained_result.starting_state_settled,
            delivery_attempted=retained_result.delivery_attempted,
            delivery_receipt=retained_result.delivery_receipt,
            drag_end_resolution=retained_result.drag_end_resolution,
            fresh_actuation_events=list(retained_result.fresh_actuation_events),
            postconditions_ok=retained_result.postconditions_ok,
            skipped=skipped,
            actuation="human_attended_skip" if skipped else "human_attended",
            delivery_uncertainty=retained_result.delivery_uncertainty,
            resolution=retained_result.resolution,
            attended_capability_digest=capability.digest,
            governed_authorization_id=(
                manifest.governed_authorization.authorization_id
                if manifest.governed_authorization is not None
                else None
            ),
            governed_approval_source=(
                manifest.governed_authorization.approval_source
                if manifest.governed_authorization is not None
                else None
            ),
            expected_texts=expected_texts,
            expected_postconditions=(
                [condition.model_copy(deep=True) for condition in state.step.expect]
                if not skipped
                else []
            ),
            transition_history_hash=pending.program_history_hash,
            visited_states_delta=list(pending.program_history_delta),
            program_transition_evidence_delta=list(
                pending.program_transition_evidence_delta
            ),
            business_decision_evidence_delta=list(
                pending.business_decision_evidence_delta
            ),
            program_exception_evidence_delta=list(
                pending.program_exception_evidence_delta
            ),
            transition_parent_hash=pending.program_history_hash,
            transition_delta=[],
            transition_history=list(pending.program_history),
            bundle_version=capability.bundle_version,
            attended_transition=receipt,
            attended_reconciliation_request_digest=reconciliation_request_digest,
            attended_reconciliation_expected_transition_digest=(
                capability.expected_transition_digest
                if reconciliation_request_digest is not None
                else None
            ),
            attended_reconciliation_delivery_state=(
                reconciliation_delivery_state
                if reconciliation_request_digest is not None
                else None
            ),
            attended_reconciliation_effect_contract_hashes=list(
                reconciliation_effect_contract_hashes
            ),
            attended_reconciliation_at=(
                capability.issued_at
                if reconciliation_request_digest is not None
                else None
            ),
            created_at=capability.issued_at,
        )
        existing = store.last_program_checkpoint()
        existing_seq = existing.seq if existing is not None else 0
        if existing_seq != source_seq and (
            existing_seq != checkpoint.seq or existing != checkpoint
        ):
            raise AttendedActionRefused(
                "the program checkpoint sequence advanced differently; refusing "
                "a non-idempotent attended transition"
            )
        # Live verification and guarded edge selection can take long enough for
        # an independent durable CLI/operator process to replace the pause.
        # Re-bind the exact signed pause before writing any receipt, checkpoint,
        # or approval; a newer pause must remain completely untouched.
        live_pending = store.read_pending()
        if (
            live_pending is None
            or approval_pause_digest(live_pending) != capability.pause_digest
        ):
            raise AttendedActionRefused(
                "the exact attended program pause changed before transition commit"
            )
        from openadapt_flow.runtime.durable.continuation import (
            ContinuationCoordinator,
            current_continuation_token,
        )

        token = current_continuation_token()
        if token is None:
            raise AttendedActionRefused(
                "the attended program completion lost its continuation authority"
            )
        # Commit the signed program receipt, checkpoint, and approval only
        # after the server-owned delivery permit accepts this exact sequence.
        # A refusal leaves the program continuation available for safe
        # reconciliation instead of creating local-only progress.
        coordinator = ContinuationCoordinator(run_dir, key=self.key)
        coordinator.before_delivery(token)
        receipt = action_store.write_program_receipt(receipt)
        prior_decision_indexes = (
            [
                item.decision_index
                for prior_checkpoint in store.program_checkpoints()
                for item in prior_checkpoint.program_transition_evidence_delta
            ]
            + [
                item.decision_index
                for prior_checkpoint in store.program_checkpoints()
                if prior_checkpoint.attended_transition_evidence is not None
                for item in [prior_checkpoint.attended_transition_evidence]
            ]
            + [
                item.decision_index
                for prior_checkpoint in store.program_checkpoints()
                for item in prior_checkpoint.business_decision_evidence_delta
            ]
            + [
                item.decision_index
                for prior_checkpoint in store.program_checkpoints()
                for item in prior_checkpoint.program_exception_evidence_delta
            ]
            + [
                item.decision_index
                for item in pending.program_transition_evidence_delta
            ]
            + [item.decision_index for item in pending.business_decision_evidence_delta]
            + [item.decision_index for item in pending.program_exception_evidence_delta]
        )
        receipt_path = action_store._receipt_path(receipt.pause_id)
        receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        control_payload = json.dumps(
            [frame.model_dump(mode="json") for frame in pending.program_frames],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        control_sha256 = hashlib.sha256(control_payload).hexdigest()
        control_ref = f"private/program-transition-controls/{control_sha256}.json"
        control_path = Path(run_dir) / control_ref
        control_dir = control_path.parent
        run_root = Path(run_dir).resolve()
        if control_dir.is_symlink() or control_path.is_symlink():
            raise AttendedActionRefused(
                "the attended transition control path must not be a symlink"
            )
        control_dir.mkdir(parents=True, exist_ok=True)
        if control_dir.is_symlink() or not control_dir.resolve().is_relative_to(
            run_root
        ):
            raise AttendedActionRefused(
                "the attended transition control path leaves the run directory"
            )
        if control_path.is_file():
            if control_path.read_bytes() != control_payload:
                raise AttendedActionRefused(
                    "the attended transition control digest has other bytes"
                )
        else:
            action_store._atomic_write(control_path, control_payload)
        scope = [
            ProgramExecutionScopeFrame(
                graph_id=frame.graph_id,
                loop_state_id=(
                    frame.loop.loop_state_id if frame.loop is not None else None
                ),
                relation=frame.loop.relation if frame.loop is not None else None,
                row_index=frame.loop.row_index if frame.loop is not None else None,
            )
            for frame in pending.program_frames
        ]
        attended_evidence = AttendedProgramTransitionEvidence(
            decision_index=max(prior_decision_indexes, default=-1) + 1,
            graph_id=receipt.source_graph_id,
            state_id=receipt.source_state_id,
            program_scope=scope,
            target_state_id=receipt.target_state_id,
            action=receipt.action,
            receipt_pause_id=receipt.pause_id,
            receipt_sha256=receipt_sha256,
            receipt_inventory_ref=(
                f"{PROGRAM_RECEIPTS_DIRNAME}/{receipt.pause_id}.json"
            ),
            control_frames_sha256=control_sha256,
            control_frames_inventory_ref=control_ref,
            governed_runtime_inputs_digest=(
                manifest.governed_authorization.runtime_inputs_digest
                if manifest.governed_authorization is not None
                else None
            ),
        )
        checkpoint = checkpoint.model_copy(
            update={
                "attended_transition": receipt,
                "attended_transition_evidence": attended_evidence,
            }
        )
        if existing_seq == source_seq:
            store.write_program_checkpoint(checkpoint)
        live_pending = store.read_pending()
        if (
            live_pending is None
            or approval_pause_digest(live_pending) != capability.pause_digest
        ):
            raise AttendedActionRefused("the program pause changed before resume")
        store.commit_approval_transition(
            expected_pending=live_pending,
            approval=approval,
            target_status="approved",
        )
        coordinator.acknowledge_progress(
            token,
            external_delivery=True,
        )

        from openadapt_flow.runtime.durable.resume import resume

        resumed = resume(
            run_dir,
            resume_replayer,
            approval=approval,
            key=self.key,
        )
        receipt_digest = _digest(receipt)
        target = target_state_id or "<return>"
        return AttendedExecutionResult(
            status="completed" if resumed.success else "halted",
            message=(
                "Human-completed program action verified; exact interpreter "
                "transition receipt committed and resumed without re-actuation."
                if resumed.success and not skipped
                else (
                    "Declared optional program action skipped; exact interpreter "
                    "transition receipt committed without actuation."
                    if resumed.success
                    else "The exact program continuation halted and remains auditable."
                )
            ),
            report_success=resumed.success,
            resumed_from=state.id,
            next_transition=target,
            transition_receipt_digest=receipt_digest,
        )

    def continue_run(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        try:
            with self._exclusive_live_session():
                return self._continue_run_locked(run_dir, capability, approval)
        except AttendedActionBusy as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

    def _continue_run_locked(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        program_context: Optional[
            tuple[PendingEscalation, State, dict[str, str], Optional[str]]
        ] = None
        try:
            store, manifest, workflow = self._load(run_dir, capability)
            replayer = self.replayer_factory(manifest)
            self._bind_authorization(replayer, manifest)
            attended_store = AttendedActionStore(run_dir)
            if workflow.program is not None:
                pending, state, params = self._program_context(
                    store, workflow, capability
                )
                leaf = pending.program_frames[-1]
                result, target = replayer.revalidate_attended_program_completion(
                    workflow,
                    graph_id=leaf.graph_id,
                    state_id=state.id,
                    params=params,
                    bundle_dir=Path(manifest.bundle_dir),
                    run_dir=run_dir,
                    run_id=manifest.run_id,
                    transition_baseline=capability.transition_baseline,
                    transition_digest=attended_store.transition_value_digest,
                )
                program_context = (pending, state, params, target)
            else:
                result = replayer.revalidate_attended_completion(
                    workflow,
                    step_index=capability.step_index,
                    params=dict(manifest.params),
                    bundle_dir=Path(manifest.bundle_dir),
                    run_dir=run_dir,
                    run_id=manifest.run_id,
                    transition_baseline=capability.transition_baseline,
                    transition_digest=attended_store.transition_value_digest,
                )
            if not result.ok:
                return AttendedExecutionResult(
                    status="refused",
                    message=result.error or "attended outcome verification refused",
                    report_success=False,
                    resumed_from=capability.step_id,
                    next_transition=capability.expected_next_transition,
                )
        except ResumeRefused as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        except Exception:
            # Loading, attaching to the live session, and fresh verification
            # are observation-only. A failure here cannot be outcome evidence,
            # but it also has not mutated workflow state.
            return AttendedExecutionResult(
                status="refused",
                message=(
                    "fresh attended verification was unavailable before "
                    "resume; no workflow continuation was admitted"
                ),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        try:
            if program_context is not None:
                pending, state, params, target = program_context
                return self._resume_program(
                    run_dir=run_dir,
                    store=store,
                    manifest=manifest,
                    workflow=workflow,
                    capability=capability,
                    approval=approval,
                    pending=pending,
                    state=state,
                    params=params,
                    result=result,
                    skipped=False,
                    target_state_id=target,
                    resume_replayer=replayer,
                )
            return self._resume(
                run_dir=run_dir,
                store=store,
                manifest=manifest,
                workflow=workflow,
                capability=capability,
                approval=approval,
                result=result,
                skipped=False,
                resume_replayer=replayer,
            )
        except ResumeRefused as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

    def reconcile_run(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
        request_digest: str,
    ) -> AttendedExecutionResult:
        """Re-read a possibly completed action without delivering it again."""

        try:
            with self._exclusive_live_session():
                return self._reconcile_run_locked(
                    run_dir, capability, approval, request_digest
                )
        except AttendedActionBusy as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

    def _reconcile_run_locked(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
        request_digest: str,
    ) -> AttendedExecutionResult:
        """Perform the read-only half of attended reconciliation.

        This deliberately shares the fresh state, next-target identity, and
        independent-effect verifier with attended completion. It does not call
        ``_run_step`` or any backend delivery method. A failed read leaves the
        original durable pause intact for further reconciliation or escalation.
        """

        if capability.delivery_state == "not_delivered":
            return AttendedExecutionResult(
                status="refused",
                message=(
                    "This pause has positive non-delivery evidence; there is no "
                    "possible action to reconcile."
                ),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        reconciliation_delivery_state = capability.delivery_state
        program_context: Optional[
            tuple[PendingEscalation, State, dict[str, str], Optional[str]]
        ] = None
        try:
            store, manifest, workflow = self._load(run_dir, capability)
            pending = store.read_pending()
            if (
                pending is None
                or approval_pause_digest(pending) != capability.pause_digest
            ):
                raise AttendedActionRefused(
                    "the exact attended pause changed before reconciliation"
                )
            if pending.delivery_uncertainty is None:
                raise AttendedActionRefused(
                    "this pause has no recorded uncertain delivery to reconcile"
                )
            replayer = self.replayer_factory(manifest)
            self._bind_authorization(replayer, manifest)
            attended_store = AttendedActionStore(run_dir)
            if workflow.program is not None:
                pending, state, params = self._program_context(
                    store, workflow, capability
                )
                leaf = pending.program_frames[-1]
                result, target = replayer.revalidate_attended_program_completion(
                    workflow,
                    graph_id=leaf.graph_id,
                    state_id=state.id,
                    params=params,
                    bundle_dir=Path(manifest.bundle_dir),
                    run_dir=run_dir,
                    run_id=manifest.run_id,
                    transition_baseline=capability.transition_baseline,
                    transition_digest=attended_store.transition_value_digest,
                )
                program_context = (pending, state, params, target)
            else:
                result = replayer.revalidate_attended_completion(
                    workflow,
                    step_index=capability.step_index,
                    params=dict(manifest.params),
                    bundle_dir=Path(manifest.bundle_dir),
                    run_dir=run_dir,
                    run_id=manifest.run_id,
                    transition_baseline=capability.transition_baseline,
                    transition_digest=attended_store.transition_value_digest,
                )
        except Exception:
            return AttendedExecutionResult(
                status="refused",
                message=(
                    "Fresh reconciliation evidence was unavailable. The run remains "
                    "paused and no workflow action was delivered."
                ),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

        # Reconciliation is stricter than ordinary attended continuation: an
        # action that may already have landed must prove a declared independent
        # effect. A screen-only postcondition cannot settle an uncertain write.
        if (
            not result.ok
            or result.effect_verified is not True
            or not result.effect_contract_hashes
        ):
            return AttendedExecutionResult(
                status="refused",
                message=(
                    "The current application and independent verifier did not prove "
                    "the possibly delivered effect. The run remains paused for "
                    "reconciliation."
                ),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

        try:
            if program_context is not None:
                pending, state, params, target = program_context
                resumed = self._resume_program(
                    run_dir=run_dir,
                    store=store,
                    manifest=manifest,
                    workflow=workflow,
                    capability=capability,
                    approval=approval,
                    pending=pending,
                    state=state,
                    params=params,
                    result=result,
                    skipped=False,
                    target_state_id=target,
                    resume_replayer=replayer,
                    reconciliation_request_digest=request_digest,
                    reconciliation_delivery_state=reconciliation_delivery_state,
                    reconciliation_effect_contract_hashes=tuple(
                        result.effect_contract_hashes
                    ),
                )
            else:
                resumed = self._resume(
                    run_dir=run_dir,
                    store=store,
                    manifest=manifest,
                    workflow=workflow,
                    capability=capability,
                    approval=approval,
                    result=result,
                    skipped=False,
                    resume_replayer=replayer,
                    reconciliation_request_digest=request_digest,
                    reconciliation_delivery_state=reconciliation_delivery_state,
                    reconciliation_effect_contract_hashes=tuple(
                        result.effect_contract_hashes
                    ),
                )
        except ResumeRefused as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        if resumed.status != "completed" or resumed.report_success is not True:
            return resumed

        if resumed.transition_receipt_digest is None:
            raise AttendedActionRefused(
                "the completed reconciliation has no durable transition receipt"
            )
        attended_store.write_reconciliation_receipt(
            AttendedReconciliationReceipt(
                pause_id=capability.pause_id,
                capability_digest=capability.digest,
                request_digest=request_digest,
                expected_transition_digest=capability.expected_transition_digest,
                transition_receipt_digest=resumed.transition_receipt_digest,
                delivery_state=reconciliation_delivery_state,
                effect_contract_hashes=tuple(result.effect_contract_hashes),
                reconciled_at=capability.issued_at,
            )
        )
        return resumed.model_copy(
            update={
                "message": (
                    "The independent effect readback proved the original action. "
                    "OpenAdapt resumed without re-dispatching it."
                )
            }
        )

    def skip_run(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        try:
            with self._exclusive_live_session():
                return self._skip_run_locked(run_dir, capability, approval)
        except AttendedActionBusy as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

    def _skip_run_locked(
        self,
        run_dir: Path,
        capability: AttendedPauseCapability,
        approval: ApprovalRecord,
    ) -> AttendedExecutionResult:
        program_context: Optional[
            tuple[PendingEscalation, State, dict[str, str], Optional[str]]
        ] = None
        try:
            store, manifest, workflow = self._load(run_dir, capability)
            if workflow.program is not None:
                pending, state, params = self._program_context(
                    store, workflow, capability
                )
                assert state.step is not None
                step = state.step
            else:
                pending = None
                state = None
                params = dict(manifest.params)
                step = workflow.steps[capability.step_index]
            projection = _attended_step_safety(step, workflow, manifest)
            if (
                projection.consequential
                or step.guard is None
                or step.guard.on_unmet != "skip"
            ):
                return AttendedExecutionResult(
                    status="refused",
                    message=(
                        "Skip is not declared by this workflow, or the step is "
                        "consequential/effectful. A non-success disposition may "
                        "be escalated, but it cannot be turned into success."
                    ),
                    report_success=False,
                    resumed_from=capability.step_id,
                    next_transition=capability.expected_next_transition,
                )
            replayer = self.replayer_factory(manifest)
            self._bind_authorization(replayer, manifest)
            frame = replayer.vision.wait_settled(replayer.backend)
            if replayer._predicate_holds(
                step.guard.predicate,
                frame,
                Path(manifest.bundle_dir),
                params,
            ):
                return AttendedExecutionResult(
                    status="refused",
                    message=(
                        "The declared skip guard currently holds, so normal "
                        "workflow semantics require executing this step."
                    ),
                    report_success=False,
                    resumed_from=capability.step_id,
                    next_transition=capability.expected_next_transition,
                )
            result = StepResult(
                step_id=step.id,
                intent=step.intent,
                ok=True,
                skipped=True,
                postconditions_ok=None,
                actuation="human_attended_skip",
            )
            if pending is not None and state is not None:
                target = replayer.select_attended_program_transition(
                    workflow,
                    graph_id=pending.program_frames[-1].graph_id,
                    state_id=state.id,
                    params=params,
                    bundle_dir=Path(manifest.bundle_dir),
                )
                program_context = (pending, state, params, target)
        except ResumeRefused as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        except Exception:
            return AttendedExecutionResult(
                status="refused",
                message=(
                    "fresh skip-policy validation was unavailable before "
                    "resume; no workflow continuation was admitted"
                ),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
        try:
            if program_context is not None:
                pending, state, params, target = program_context
                return self._resume_program(
                    run_dir=run_dir,
                    store=store,
                    manifest=manifest,
                    workflow=workflow,
                    capability=capability,
                    approval=approval,
                    pending=pending,
                    state=state,
                    params=params,
                    result=result,
                    skipped=True,
                    target_state_id=target,
                    resume_replayer=replayer,
                )
            return self._resume(
                run_dir=run_dir,
                store=store,
                manifest=manifest,
                workflow=workflow,
                capability=capability,
                approval=approval,
                result=result,
                skipped=True,
                resume_replayer=replayer,
            )
        except ResumeRefused as exc:
            return AttendedExecutionResult(
                status="refused",
                message=str(exc),
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )
