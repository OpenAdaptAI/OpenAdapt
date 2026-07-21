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
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import State, StateKind, Step, StepResult, Workflow
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    BundleMismatch,
    PauseExpired,
    ResumeRefused,
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
    bundle_version,
    control_frames_hash,
)

CAPABILITY_FILENAME = "attended_capability.json"
CAPABILITY_HISTORY_FILENAME = "attended_capability_history.json"
CAPABILITY_KEY_FILENAME = ".attended_capability.key"
DECISIONS_FILENAME = "attended_decisions.json"
LEASE_FILENAME = ".attended_action.lease"
PROGRAM_RECEIPTS_DIRNAME = ".attended_program_receipts"
DEFAULT_CAPABILITY_TTL_S = 24 * 3600.0
DEFAULT_LEASE_TTL_S = 15 * 60.0


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
    issued_at: str
    expires_at: str
    allowed_actions: tuple[Literal["continue", "skip", "teach", "escalate"], ...] = (
        "teach",
        "escalate",
    )
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        return self.model_dump(exclude={"signature"}, mode="json")

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
    action: Literal["continue", "skip", "teach", "escalate"]
    disposition: Optional[
        Literal[
            "completed_by_operator",
            "not_applicable",
            "cannot_complete",
            "needs_assistance",
            "teach_requested",
        ]
    ] = None


class AttendedExecutionResult(BaseModel):
    """Outcome returned by a deployment-bound continue/skip executor."""

    status: Literal["completed", "refused", "halted"]
    message: str
    report_success: Optional[bool] = None
    resumed_from: Optional[str] = None
    next_transition: Optional[str] = None
    transition_receipt_digest: Optional[str] = None


class AttendedDecision(BaseModel):
    """Append-only audit record for an admitted or refused operator decision."""

    schema_version: int = 1
    decision_id: str = Field(default_factory=lambda: secrets.token_hex(16))
    pause_id: str
    capability_digest: str
    request_digest: str
    idempotency_key: str
    action: Literal["continue", "skip", "teach", "escalate"]
    operator: str
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
    ]
    message: str
    created_at: str = Field(default_factory=lambda: _iso(_now()))
    report_success: Optional[bool] = None
    next_transition: Optional[str] = None
    transition_receipt_digest: Optional[str] = None


class AttendedDecisionLog(BaseModel):
    schema_version: int = 1
    decisions: list[AttendedDecision] = Field(default_factory=list)


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
) -> tuple[Literal["continue", "skip", "teach", "escalate"], ...]:
    """Derive mutation authority from the exact workflow step semantics."""
    actions: list[Literal["continue", "skip", "teach", "escalate"]] = [
        "teach",
        "escalate",
    ]
    step: Optional[Step]
    if pending.program:
        state = _program_pause_state(workflow, pending)
        step = state.step if state is not None else None
    elif 0 <= pending.step_index < len(workflow.steps):
        step = workflow.steps[pending.step_index]
        if step.id != pending.step_id:
            step = None
    else:
        step = None
    if step is None:
        return tuple(actions)

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
        for effect in step.effects
    )
    if (
        bool(step.expect or step.effects)
        and has_relative_baseline
        and not has_unsupported_effect
    ):
        actions.insert(0, "continue")

    if (
        step.risk != "irreversible"
        and not step.effects
        and step.guard is not None
        and step.guard.on_unmet == "skip"
    ):
        actions.insert(1 if actions[0] == "continue" else 0, "skip")
    return tuple(actions)


class AttendedActionStore:
    """Capability, single-flight lease, and append-only decision persistence."""

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.capability_path = self.run_dir / CAPABILITY_FILENAME
        self.capability_history_path = self.run_dir / CAPABILITY_HISTORY_FILENAME
        self.key_path = self.run_dir / CAPABILITY_KEY_FILENAME
        self.decisions_path = self.run_dir / DECISIONS_FILENAME
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

    def _sign(self, capability: AttendedPauseCapability, *, create_key: bool) -> str:
        return (
            "hmac-sha256:"
            + hmac.new(
                self._key(create=create_key),
                _canonical(capability.unsigned()),
                hashlib.sha256,
            ).hexdigest()
        )

    def _receipt_path(self, pause_id: str) -> Path:
        if len(pause_id) != 32 or any(ch not in "0123456789abcdef" for ch in pause_id):
            raise AttendedActionRefused("the program receipt pause id is invalid")
        return self.run_dir / PROGRAM_RECEIPTS_DIRNAME / f"{pause_id}.json"

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
        pause_digest = _digest(pending)
        # Creating the HMAC key before digesting transition values gives the
        # baseline and capability signature one stable per-run trust root.
        self._key(create=True)
        baseline = self._transition_baseline(transition_observation)
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
        now = _now()
        transition = _transition_payload(
            run_id=manifest.run_id,
            workflow_name=pending.workflow_name,
            bundle_revision=revision,
            pending=pending,
            expected_next_transition=expected,
        )
        capability = AttendedPauseCapability(
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
            delivery_state=_delivery_state(result),
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=max(1.0, ttl_s))),
            allowed_actions=_allowed_actions(workflow, pending, baseline),
        )
        capability.signature = self._sign(capability, create_key=False)
        self._atomic_write(
            self.capability_path,
            capability.model_dump_json(indent=2).encode("utf-8"),
        )
        return capability

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
        if _digest(pending) != capability.pause_digest:
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
        if not self.decisions_path.is_file():
            return AttendedDecisionLog()
        try:
            return AttendedDecisionLog.model_validate_json(
                self.decisions_path.read_text()
            )
        except ValueError as exc:
            raise AttendedActionRefused(
                "the attended decision audit log is invalid"
            ) from exc

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

    def append(self, decision: AttendedDecision) -> None:
        log = self._read_log()
        log.decisions.append(decision)
        self._atomic_write(
            self.decisions_path, log.model_dump_json(indent=2).encode("utf-8")
        )

    @contextmanager
    def lease(
        self,
        request: AttendedActionRequest,
        *,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
        now: Optional[datetime] = None,
    ) -> Iterator[None]:
        """Acquire one per-run action lease with no silent stale takeover."""
        now = now or _now()
        lease = {
            "request_digest": _digest(request),
            "idempotency_key": request.idempotency_key,
            "acquired_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=max(1.0, ttl_s))),
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                existing = json.loads(self.lease_path.read_text())
                expired = _parse(str(existing["expires_at"])) < now
            except (OSError, ValueError, KeyError):
                expired = False
            if expired:
                raise AttendedActionBusy(
                    "a prior action lease expired without a recorded outcome; "
                    "delivery is uncertain and must be reconciled before retry"
                ) from None
            raise AttendedActionBusy(
                "another attended action is already in progress"
            ) from None
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(lease, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent(self.lease_path)
            yield
        finally:
            try:
                self.lease_path.unlink()
                self._fsync_parent(self.lease_path)
            except FileNotFoundError:
                pass


def validate_attended_program_receipt(
    run_dir: Path | str,
    *,
    checkpoint: ProgramCheckpoint,
    pending: Optional[PendingEscalation],
    manifest: Any,
    live_bundle_version: str,
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
    ):
        raise AttendedActionRefused(
            "the attended program receipt does not match its signed "
            "run/bundle/pause/state/frame lineage"
        )
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
    }


def _approval(
    capability: AttendedPauseCapability,
    *,
    operator: str,
    resolution: str,
    run_dir: Path,
) -> ApprovalRecord:
    if not operator.strip():
        raise ApprovalRequired("attended actions require an authenticated operator")
    return ApprovalRecord(
        approver=operator,
        resolution=resolution,
        bundle_version=capability.bundle_version,
        workflow_name=capability.workflow_name,
        run_dir=str(run_dir),
    )


def execute_attended_action(
    run_dir: Path | str,
    request: AttendedActionRequest,
    *,
    operator: str,
    executor: Optional[AttendedActionExecutor] = None,
    key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AttendedDecision:
    """Admit and execute one attended decision under exact binding."""
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
    }
    if request.disposition not in expected_dispositions[request.action]:
        raise AttendedActionRefused(
            "the disposition does not match the requested attended action"
        )
    actions = AttendedActionStore(run_dir)
    prior = actions.prior(request)
    if prior is not None:
        if prior.status in {"delivery_started", "delivery_uncertain"}:
            raise AttendedActionRefused(
                "the prior request may have crossed the delivery boundary; "
                "automatic retry is refused until an audited reconciliation"
            )
        if prior.status != "prepared":
            return prior

    checkpoints = CheckpointStore(run_dir, key=key)
    pending = checkpoints.read_pending()
    manifest = checkpoints.read_manifest()
    if pending is None or manifest is None:
        raise AttendedActionRefused("the run is not durably paused")
    capability = actions.validate(request, pending=pending, manifest=manifest, now=now)

    with actions.lease(request, now=now):
        # Repeat the complete validation under the lease: the bundle/pause may
        # have changed between page load and lock acquisition.
        pending = checkpoints.read_pending()
        manifest = checkpoints.read_manifest()
        if pending is None or manifest is None:
            raise AttendedActionRefused("the run is no longer durably paused")
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
                return prior
        request_digest = _digest(request)
        unresolved = actions.unresolved_delivery(capability.pause_id)
        if unresolved is not None and request.action in {"continue", "skip"}:
            raise AttendedActionRefused(
                "another request for this pause may have crossed the delivery "
                "boundary; reconcile its live state before continuing or skipping"
            )

        if request.action == "teach":
            decision = AttendedDecision(
                pause_id=capability.pause_id,
                capability_digest=capability.digest,
                request_digest=request_digest,
                idempotency_key=request.idempotency_key,
                action=request.action,
                operator=operator,
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
            actions.append(decision)
            return decision

        if request.action == "escalate":
            decision = AttendedDecision(
                pause_id=capability.pause_id,
                capability_digest=capability.digest,
                request_digest=request_digest,
                idempotency_key=request.idempotency_key,
                action=request.action,
                operator=operator,
                disposition=request.disposition or "needs_assistance",
                status="escalated",
                message=(
                    "Escalation recorded. The durable pause remains intact and "
                    "can be continued after a qualified operator resolves it."
                ),
                next_transition=capability.expected_next_transition,
            )
            actions.append(decision)
            return decision

        if executor is None:
            raise AttendedActionRefused(
                "this console has no deployment-bound attended executor; start "
                "it with the qualified backend/effect configuration"
            )

        resolution = (
            "operator completed the live-app task; verify and continue"
            if request.action == "continue"
            else "operator requested policy-scoped skip"
        )
        approval = _approval(
            capability, operator=operator, resolution=resolution, run_dir=run_dir
        )
        prepared = AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=request_digest,
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
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
        try:
            result = (
                executor.continue_run(run_dir, capability, approval)
                if request.action == "continue"
                else executor.skip_run(run_dir, capability, approval)
            )
        except Exception:
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
            raise
        decision = AttendedDecision(
            pause_id=capability.pause_id,
            capability_digest=capability.digest,
            request_digest=request_digest,
            idempotency_key=request.idempotency_key,
            action=request.action,
            operator=operator,
            disposition=request.disposition,
            status=result.status,
            message=result.message,
            report_success=result.report_success,
            next_transition=result.next_transition,
            transition_receipt_digest=result.transition_receipt_digest,
        )
        actions.append(decision)
        return decision


def checkpoint_human_completed_step(
    run_dir: Path | str,
    *,
    capability: AttendedPauseCapability,
    result: StepResult,
    params: dict[str, str],
    key: Optional[str] = None,
) -> RunCheckpoint:
    """Advance a linear resume point after outcome verification, without acting."""
    if not result.ok or result.postconditions_ok is False:
        raise AttendedActionRefused(
            "the human-completed step did not pass outcome verification"
        )
    if result.effect_verified is False:
        raise AttendedActionRefused(
            "the human-completed step's independent effect was not confirmed"
        )
    checkpoint = RunCheckpoint(
        workflow_name=capability.workflow_name,
        step_index=capability.step_index,
        step_id=capability.step_id,
        intent=result.intent,
        next_step_index=capability.step_index + 1,
        params=dict(params),
        effect_verified=result.effect_verified,
        effect_approved_unverified=result.effect_approved_unverified,
        effect_contract_hashes=list(result.effect_contract_hashes),
        postconditions_ok=result.postconditions_ok,
        skipped=False,
        actuation="human_attended",
    )
    CheckpointStore(run_dir, key=key).write_checkpoint(checkpoint)
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
        if workflow.program is not None:
            pending = store.read_pending()
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
    ) -> AttendedExecutionResult:
        # Fresh verification can take long enough for an independent durable
        # CLI/operator process to replace or clear the pause.  Re-bind the
        # exact signed pause immediately before committing the human-completed
        # checkpoint; never approve a newer pause under an older capability.
        pending = store.read_pending()
        if pending is None or _digest(pending) != capability.pause_digest:
            raise AttendedActionRefused(
                "the exact attended pause changed before checkpoint commit"
            )
        checkpoint = RunCheckpoint(
            workflow_name=capability.workflow_name,
            step_index=capability.step_index,
            step_id=capability.step_id,
            intent=result.intent,
            next_step_index=capability.step_index + 1,
            params=dict(manifest.params),
            effect_verified=result.effect_verified,
            effect_approved_unverified=result.effect_approved_unverified,
            effect_contract_hashes=list(result.effect_contract_hashes),
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
            postconditions_ok=result.postconditions_ok,
            skipped=skipped,
            actuation="human_attended_skip" if skipped else "human_attended",
        )
        store.write_checkpoint(checkpoint)
        store.write_approval(approval)
        store.write_pending(pending.model_copy(update={"status": "approved"}))

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
    ) -> AttendedExecutionResult:
        if state.step is None or not pending.program_frames:
            raise AttendedActionRefused("the paused program action is unavailable")
        source_seq = pending.program_checkpoint_seq
        cursor_digest = capability.program_cursor_digest
        if cursor_digest is None:
            raise AttendedActionRefused("the program cursor is not signed")
        receipt = ProgramTransitionReceipt(
            run_id=capability.run_id,
            workflow_name=capability.workflow_name,
            bundle_version=capability.bundle_version,
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
        resolved_effects = (
            [
                effect.model_dump(mode="json")
                for effect in resume_replayer._resolve_effects(
                    state.step.effects, params
                )
            ]
            if result.effect_verified is True and state.step.effects
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
            workflow_name=capability.workflow_name,
            seq=source_seq + 1,
            verified_state_id=state.id,
            intent=state.step.intent,
            frames=list(pending.program_frames),
            bound_params=params,
            new_effect_keys=(
                list(result.effect_contract_hashes)
                if result.effect_verified is True
                else []
            ),
            new_effects=resolved_effects,
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
            transition_history_hash=pending.program_history_hash,
            bundle_version=capability.bundle_version,
            attended_transition=receipt,
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
        if live_pending is None or _digest(live_pending) != capability.pause_digest:
            raise AttendedActionRefused(
                "the exact attended program pause changed before transition commit"
            )
        receipt = action_store.write_program_receipt(receipt)
        checkpoint = checkpoint.model_copy(update={"attended_transition": receipt})
        if existing_seq == source_seq:
            store.write_program_checkpoint(checkpoint)
        store.write_approval(approval)
        live_pending = store.read_pending()
        if live_pending is None or _digest(live_pending) != capability.pause_digest:
            raise AttendedActionRefused("the program pause changed before resume")
        store.write_pending(live_pending.model_copy(update={"status": "approved"}))

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
            if (
                step.risk == "irreversible"
                or step.effects
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
