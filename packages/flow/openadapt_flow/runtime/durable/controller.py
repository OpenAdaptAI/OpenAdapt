"""The durable-run controller: the replayer's Tier-3 hook (RFC §5).

``DurableRun`` is the object the ``Replayer`` drives to make a run durable. It
owns the :class:`~.checkpoint.CheckpointStore` and turns each ``StepResult``
into the right durable artifact:

- a VERIFIED step (``result.ok``) -> a :class:`~.checkpoint.RunCheckpoint`
  written under ``run_dir/checkpoints/`` (the resume point advances);
- a HALTED step (``not result.ok``) -> a
  :class:`~.checkpoint.PendingEscalation` written to
  ``run_dir/pending_escalation.json`` that captures WHY the run paused, the
  proposed operator options, and the last verified checkpoint to resume from.

The replayer's coupling to this module is intentionally TINY, so the Phase-2
state-machine interpreter (which rewrites ``replayer.py`` heavily) can keep it
across the rebase. The touch-points are:

1. ``Replayer.__init__`` accepts ``durable: bool = False`` and stores it.
2. ``Replayer.run`` accepts ``resume_from: Optional[int] = None``; when
   durability is on it constructs one ``DurableRun`` and, per step, calls
   :meth:`DurableRun.record` right after the result is appended.
3. A resume skips already-verified steps and pre-loads their results via
   :func:`resumed_step_results`.

Nothing here makes a model call or touches the backend/vision -- durability is
pure bookkeeping over the ``StepResult`` the replayer already produces.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from openadapt_flow.ir import Step, StepResult, Workflow
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    ProgramCheckpoint,
)

if TYPE_CHECKING:
    from openadapt_flow.runtime.durable.attended import TransitionObservation

HUMAN_REQUIRED_MARKERS: tuple[str, ...] = (
    "captcha",
    "verify you are human",
    "verify that you are human",
    "i'm not a robot",
    "i am not a robot",
    "unusual traffic",
    "press and hold",
    "multi-factor",
    "multifactor",
    "two-factor",
    "one-time passcode",
    "one-time code",
    "enter the verification code",
    "enter verification code",
    "code sent to",
    "session expired",
    "sign in again",
    "log in again",
)
_HUMAN_REQUIRED_TOKEN_RE = re.compile(r"(?<![a-z0-9])(?:2fa|mfa)(?![a-z0-9])")


def looks_like_human_required(*texts: Optional[str]) -> bool:
    """Recognize a visible human-presence/authentication interruption.

    The result is only a halt classification.  It never triggers a solver,
    retry, click, key entry, or model call.
    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(marker in lowered for marker in HUMAN_REQUIRED_MARKERS):
            return True
        if _HUMAN_REQUIRED_TOKEN_RE.search(lowered):
            return True
    return False


def classify_halt(step: Optional[Step], result: StepResult) -> tuple[str, list[str]]:
    """Categorize a halt and propose operator options.

    Maps the replayer's halt reason (``result.error`` plus the
    ``result.effect_results`` audit lines and the identity verdict) to a coarse
    machine ``category`` and a list of human-facing ``proposed_options``. The
    options are advisory next actions for the operator reviewing the pause;
    "approve and resume from the last verified checkpoint" and "abort" are
    always offered, alongside cause-specific guidance derived from the halt
    reason / compensation escalation.

    Returns ``(category, proposed_options)``.
    """
    error = result.error or ""
    lower = error.lower()
    effect_lines = result.effect_results or []
    effect_blob = " ".join(effect_lines).lower()

    resume = (
        "Approve and RESUME from the last verified checkpoint (re-runs only "
        "this step onward; already-confirmed steps are not repeated)"
    )
    abort = "Abort the run and discard the pending escalation"

    # CAPTCHA, MFA, and re-authentication are human-presence requirements.
    # OpenAdapt only halts and leaves the live application to the operator.
    if looks_like_human_required(error):
        return "human_required", [
            "Complete the challenge or authentication in the LIVE application "
            "yourself; OpenAdapt never answers, solves, or retries it",
            resume,
            abort,
        ]

    # System-of-record effect halts (the richest signal -- effect_verified is
    # explicitly False and the verdict lines carry the cause).
    if result.effect_verified is False or "system of record" in lower:
        if "operator confirmation" in effect_blob or "placeholder" in lower:
            return "placeholder_effect", [
                "Complete the system-of-record binding the compiler flagged as "
                "app-specific (endpoint / record selector / idempotency key) "
                "and clear the effect's needs_operator_confirmation flag",
                resume,
                abort,
            ]
        if "no effectverifier" in effect_blob or "no effectverifier" in lower:
            return "effect_unverifiable", [
                "Configure an EffectVerifier bound to this deployment's system "
                "of record, then re-run",
                abort,
            ]
        if "escalat" in lower or "escalat" in effect_blob:
            return "effect_escalated", [
                "Inspect the system of record and correct it (the automatic "
                "compensation could not safely undo the fault)",
                resume,
                abort,
            ]
        if "indeterminate" in lower or "indeterminate" in effect_blob:
            return "effect_indeterminate", [
                "Restore reachability of the system of record and confirm "
                "whether the write landed",
                resume,
                abort,
            ]
        if "refuted" in lower or "refuted" in effect_blob:
            return "effect_refuted", [
                "Investigate the system of record: the screen showed success "
                "but the record is missing/duplicated/wrong; correct it",
                resume,
                abort,
            ]
        return "effect_refuted", [
            "Investigate the system-of-record effect that could not be "
            "confirmed and correct the record",
            resume,
            abort,
        ]

    # Phase-1 guard / wait_until precondition halts.
    if (
        "precondition" in lower
        or "guard" in lower
        or "wait_until" in lower
        or ("readiness" in lower)
    ):
        return "unmet_guard", [
            "Satisfy the step's precondition (bring the app to the expected "
            "state), then resume",
            resume,
            abort,
        ]

    # Disambiguation (which entity / which of several matches).
    if "disambigu" in lower or "ambiguous" in lower:
        return "disambiguation", [
            "Choose the intended target/entity for this step, then resume",
            resume,
            abort,
        ]

    # Pre-click identity gate (wrong-entity refusal or unreadable/abstain).
    if result.identity is not None and (
        "identity" in lower or "refusing to act" in lower
    ):
        return "identity", [
            "Confirm the resolved target is the intended entity (the identity "
            "band could not be certified), then resume",
            resume,
            abort,
        ]

    if "postconditions failed" in lower or "semantic drift" in lower:
        return "postcondition", [
            "Verify the app reached the expected screen state; re-run this step "
            "once the drift is understood",
            resume,
            abort,
        ]

    if "could not resolve" in lower or "all resolution rungs failed" in lower:
        return "resolution", [
            "The target could not be located on screen; confirm the app view "
            "matches the recording, then resume",
            resume,
            abort,
        ]

    return "halt", [resume, abort]


class DurableRun:
    """Per-run Tier-3 controller: writes checkpoints and pending escalations.

    Constructed once by the replayer when durability is enabled. It writes a
    :class:`~.checkpoint.RunManifest` up front (so a resume can reconstruct the
    run from ``run_dir`` alone) and then records one artifact per step.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        run_id: str,
        workflow_name: str,
        bundle_dir: Path | str,
        params: dict[str, str],
        worklists: dict[str, list[dict[str, str]]],
        save_healed_to: Optional[Path | str] = None,
        key: Optional[str] = None,
        governed_authorization: Optional[GovernedRunAuthorization] = None,
    ) -> None:
        # ``key`` (None by default) opts the durable artifacts into AES-256-GCM
        # encryption-at-rest; unset => plaintext, exactly as before.
        self.store = CheckpointStore(run_dir, key=key)
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.bundle_dir = Path(bundle_dir).resolve()
        self.governed_authorization = governed_authorization
        self.store.write_manifest(
            RunManifest(
                run_id=run_id,
                workflow_name=workflow_name,
                bundle_dir=str(self.bundle_dir),
                params=dict(params),
                worklists=worklists,
                governed_authorization=governed_authorization,
                save_healed_to=(str(save_healed_to) if save_healed_to else None),
            )
        )

    def record(
        self,
        step_index: int,
        step: Step,
        result: StepResult,
        params: dict[str, str],
        *,
        workflow: Optional[Workflow] = None,
        transition_observation: Optional["TransitionObservation"] = None,
    ) -> None:
        """Persist the durable artifact for one completed step.

        ``result.ok`` -> checkpoint (the resume point advances past this step).
        Otherwise -> a pending escalation capturing the pause. Idempotent: a
        resume that re-verifies a step overwrites its checkpoint rather than
        duplicating it.
        """
        if result.ok:
            self.store.write_checkpoint(
                RunCheckpoint(
                    workflow_name=self.workflow_name,
                    step_index=step_index,
                    step_id=step.id,
                    intent=step.intent,
                    next_step_index=step_index + 1,
                    params=dict(params),
                    effect_verified=result.effect_verified,
                    effect_approved_unverified=result.effect_approved_unverified,
                    effect_contract_hashes=list(result.effect_contract_hashes),
                    governed_authorization_id=(
                        self.governed_authorization.authorization_id
                        if self.governed_authorization is not None
                        else None
                    ),
                    governed_approval_source=(
                        self.governed_authorization.approval_source
                        if self.governed_authorization is not None
                        else None
                    ),
                    postconditions_ok=result.postconditions_ok,
                    skipped=result.skipped,
                    actuation=result.actuation,
                )
            )
            return

        # HALT: durably pause instead of just dying. Resume from the last
        # verified checkpoint (0 when nothing verified yet).
        last = self.store.last_checkpoint()
        resume_from = last.next_step_index if last is not None else 0
        category, options = classify_halt(step, result)
        pending = PendingEscalation(
            workflow_name=self.workflow_name,
            step_index=step_index,
            step_id=step.id,
            intent=step.intent,
            category=category,
            reason=result.error or "",
            detail=list(result.effect_results or []),
            proposed_options=options,
            resume_from_index=resume_from,
            resume_from_step_id=(last.step_id if last is not None else None),
            params=dict(params),
        )
        self.store.write_pending(pending)
        if workflow is not None:
            from openadapt_flow.runtime.durable.attended import (
                issue_attended_capability,
            )

            issue_attended_capability(
                self.store.run_dir,
                store=self.store,
                pending=pending,
                workflow=workflow,
                result=result,
                transition_observation=transition_observation,
            )

    # -- Phase-2 program (state-machine) durability --------------------------

    def record_program_checkpoint(self, checkpoint: ProgramCheckpoint) -> None:
        """Persist one verified-state interpreter checkpoint (Phase-2 program).

        Called by the program interpreter after each ``action`` state that
        VERIFIED (identity + effects + postconditions). The checkpoint captures
        the whole interpreter state (frame stack, loop cursors, bound params,
        completed effect keys) so a resume RESTORES the interpreter rather than
        translating to a step index. Idempotent per ``seq``."""
        self.store.write_program_checkpoint(checkpoint)

    def record_program_halt(
        self,
        *,
        state_id: str,
        intent: str,
        result: StepResult,
        params: dict[str, str],
        workflow: Optional[Workflow] = None,
        transition_observation: Optional["TransitionObservation"] = None,
        program_frames: Optional[list[GraphFrame]] = None,
        program_checkpoint_seq: int = 0,
        program_history_hash: str = "",
    ) -> None:
        """Persist a durable PROGRAM pause (the interpreter HALTED for a human).

        Mirrors :meth:`record` for the state machine: classify WHY it paused,
        propose operator options, and point the resume at the last verified
        interpreter checkpoint (``ProgramCheckpoint``, restored from ``run_dir``
        by :func:`~.resume.resume`). ``resume_from_index``/``resume_from_step_id``
        do NOT apply to a program run (the resume point is an interpreter state,
        not a step index), so they are left at their defaults; ``program=True``
        marks the pause as a state-machine pause."""
        last = self.store.last_program_checkpoint()
        category, options = classify_halt(None, result)
        pending = PendingEscalation(
            workflow_name=self.workflow_name,
            step_index=0,
            step_id=state_id,
            intent=intent,
            state_id=state_id,
            category=category,
            reason=result.error or "",
            detail=list(result.effect_results or []),
            proposed_options=options,
            resume_from_step_id=(last.verified_state_id if last is not None else None),
            params=dict(params),
            program=True,
            program_frames=list(program_frames or []),
            program_checkpoint_seq=program_checkpoint_seq,
            program_history_hash=program_history_hash,
        )
        self.store.write_pending(pending)
        if workflow is not None:
            from openadapt_flow.runtime.durable.attended import (
                issue_attended_capability,
            )

            issue_attended_capability(
                self.store.run_dir,
                store=self.store,
                pending=pending,
                workflow=workflow,
                result=result,
                transition_observation=transition_observation,
            )


def resumed_step_results(
    run_dir: Path | str,
    workflow: Workflow,
    resume_from: int,
    *,
    key: Optional[str] = None,
) -> list[StepResult]:
    """Synthesize ``StepResult``s for the already-verified steps of a resume.

    A resume skips steps ``[0, resume_from)`` -- they were verified in the
    original run and must NOT re-execute (never re-perform a confirmed write).
    But the report's ``success`` accounting counts one result per step, so we
    reconstruct their results from the persisted checkpoints (falling back to
    the workflow definition for any checkpoint the operator pruned). Each is
    marked ``ok=True`` and annotated as resumed, for an honest audit trail.
    """
    store = CheckpointStore(run_dir, key=key)
    by_index = {c.step_index: c for c in store.checkpoints()}
    results: list[StepResult] = []
    for index in range(min(resume_from, len(workflow.steps))):
        checkpoint = by_index.get(index)
        step = workflow.steps[index]
        results.append(
            StepResult(
                step_id=step.id,
                intent=step.intent,
                ok=True,
                skipped=checkpoint.skipped if checkpoint is not None else False,
                effect_verified=(
                    checkpoint.effect_verified if checkpoint is not None else None
                ),
                effect_approved_unverified=(
                    checkpoint.effect_approved_unverified
                    if checkpoint is not None
                    else False
                ),
                effect_contract_hashes=(
                    list(checkpoint.effect_contract_hashes)
                    if checkpoint is not None
                    else []
                ),
                postconditions_ok=(
                    checkpoint.postconditions_ok if checkpoint is not None else None
                ),
                actuation=checkpoint.actuation if checkpoint is not None else None,
                error=None,
            )
        )
    return results
