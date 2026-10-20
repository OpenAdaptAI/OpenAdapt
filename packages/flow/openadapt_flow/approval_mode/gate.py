"""The pre-save gate (P1 to P5) and the per-item finish (R1, R2, switch-off).

:class:`ApprovalGate` is the runtime object a caller hands to the
:class:`~openadapt_flow.runtime.replayer.Replayer`. The replayer asks it
before every consequential save, at the last point before input delivery and
after the target and the right-record check have resolved, and again when the
run's outcome is stamped. One gate instance serves one item (one run).

Modes:

- ``not_gated``: every save proceeds, and the run report says so.
- ``each_item``: every save waits for a person. Before the save step runs,
  the gate writes an approval request with a screenshot of the filled form
  (:mod:`openadapt_flow.approval_mode.item_approval`) and waits for a person's
  answer. After an approval the step takes a fresh frame and resolves its
  target and right-record check again, and at the last point before the
  save the gate re-reads the form and refuses if any field changed while
  waiting. A decline, a refused answer or no answer in time stops the item
  before the save. Without an approval store every save stops before
  delivery, so nothing is ever saved without a person.
- ``problems_only``: a save proceeds without a person only when the item
  checks (design 7.2) and P1 to P5 (design 7.3) all pass. Any failure stops
  before the save and sends the item to a person: per-item approval for an
  item check, a switch problem or a changed screen, or "stop before saving"
  for the signed-in user, the right record or the record check. After the
  run, R1 and R2 decide whether the item counts as done without a person; any
  other result goes to a person, and the switch-off rules in design 7.6 are
  written to the runner's switch log.

Every failure, including an exception inside the gate itself, fails toward a
person. Nothing here retries a save.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol

from openadapt_flow.approval_mode.approver import (
    DEFAULT_APPROVER_POLICY,
    Approver,
    ApproverPolicyV1,
    step_class_for,
)
from openadapt_flow.approval_mode.item_approval import (
    DEFAULT_APPROVAL_WAIT_S,
    ApiRequestRef,
    ItemApprovalRefused,
    ItemApprovalRequest,
    ItemApprovalStore,
    SaveField,
    wait_for_answer,
)
from openadapt_flow.approval_mode.model import (
    ITEM_APPROVAL_PAUSE_MARKER,
    ITEM_DECLINED_MARKER,
    UI_READBACK_PILOT,
    ApprovalMode,
    ApprovalModeItemEvidence,
    CheckOutcome,
    ItemApprovalRef,
    ItemContext,
    ItemDecisionKind,
    OffReason,
    resolve_approval_mode,
)
from openadapt_flow.approval_mode.post import (
    ItemDecision,
    check_verified,
    check_whole_run_clean,
    decide_problems_only,
    step_screen_problem,
    too_many_problems,
)
from openadapt_flow.approval_mode.switch import (
    StatusRejected,
    StatusRequest,
    SwitchLog,
    SwitchLogError,
    SwitchLogState,
    SwitchRecord,
    SwitchStatus,
    verify_status,
)

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Step, StepResult, Workflow

SignedInVerdict = Literal["match", "mismatch", "unreadable"]


class SignedInUserCheck(Protocol):
    """Reads the signed-in user from the application's own screen (P2)."""

    #: ``login_id`` where the application shows one; otherwise a display
    #: name that runner setup attested belongs only to the OpenAdapt user.
    kind: Literal["login_id", "display_name_unique_by_attestation"]

    def check(self, frame_png: bytes) -> SignedInVerdict: ...

    def reference(self, key: bytes) -> str:
        """``hmac-sha256:<hex>`` of the expected identifier under ``key``."""
        ...


def dedicated_user_reference(identifier: str, key: bytes) -> str:
    """The HMAC a switch record stores for the dedicated user's identifier."""

    digest = hmac.new(key, identifier.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


class ScreenTextSignedInUser:
    """P2 by OCR: the dedicated user's identifier appears as a whole token.

    ``expected`` comes from customer-controlled configuration (for example an
    environment variable), never from a recording. The value is held in memory
    only; evidence keeps the closed verdict.
    """

    def __init__(
        self,
        vision: Any,
        expected: str,
        *,
        kind: Literal["login_id", "display_name_unique_by_attestation"] = "login_id",
        region: Optional[tuple[int, int, int, int]] = None,
    ) -> None:
        if not expected or not expected.strip():
            raise ValueError("the dedicated user's identifier is required")
        self._vision = vision
        self._expected = expected.strip()
        self.kind = kind
        self._region = region

    def reference(self, key: bytes) -> str:
        return dedicated_user_reference(self._expected, key)

    def check(self, frame_png: bytes) -> SignedInVerdict:
        try:
            lines = self._vision.ocr(frame_png, region=self._region)
        except Exception:  # noqa: BLE001 - an unreadable screen is not a match
            return "unreadable"
        texts = [str(getattr(line, "text", "") or "") for line in lines or []]
        if not any(text.strip() for text in texts):
            return "unreadable"
        expected = self._expected.casefold()
        for text in texts:
            for token in text.split():
                if token.strip(".,;:()[]{}<>\"'").casefold() == expected:
                    return "match"
        return "mismatch"


#: ``fetch_status(request)`` returns Cloud's signed status (model, dict, JSON).
StatusFetcher = Callable[[StatusRequest], Any]
#: Returns ``(kind, "sha256:<hex>")`` for the form's current field state, or
#: ``None`` when it can't be read. Given the frame to read when it's known.
FormStateReader = Callable[[Optional[bytes]], Optional[tuple[str, str]]]


def _no_form_state(_frame: Optional[bytes]) -> Optional[tuple[str, str]]:
    return None


#: Returns the values the save writes, for the phone, or ``None`` when the
#: run can't name them. The phone then shows the screenshot alone.
SaveFieldsReader = Callable[[], Optional[Sequence[SaveField]]]


def _no_save_fields() -> Optional[Sequence[SaveField]]:
    return None


@dataclass(frozen=True)
class StepContext:
    """What the replayer knows when a save step starts, before it resolves."""

    step: "Step"
    workflow: "Workflow"
    #: The step's own result object. An approval binds to exactly this
    #: execution of the step; a later run of the same step asks again.
    result: "StepResult"
    step_index: int
    run_id: str
    #: The current settled frame, shown to the person as the filled form.
    frame: Callable[[], Optional[bytes]]
    form_state: FormStateReader = _no_form_state
    #: True at the fault step of a qualification drill. A drill must stop
    #: there by itself, so the gate never asks a person at that step.
    drill_fault_step: bool = False
    qualification_case_kind: Optional[str] = None
    runtime_inputs_digest: Optional[str] = None
    #: The values the save writes, shown on the phone (see
    #: :data:`SaveFieldsReader`). Bound into the request digest.
    save_fields: SaveFieldsReader = _no_save_fields
    #: The request an API step sends, when the save is one. ``frame`` then
    #: returns an image of that request and ``form_state`` its operation
    #: digest, so the person approves the request rather than a screenshot.
    api_request: Optional[ApiRequestRef] = None


@dataclass(frozen=True)
class SaveContext:
    """What the replayer knows at the last point before one save."""

    step: "Step"
    workflow: "Workflow"
    #: The save step's own result (not yet appended to the report).
    result: "StepResult"
    #: Results of the steps before this one in the same run.
    prior_results: Sequence["StepResult"]
    effect_verifier: Any
    governed_authorization: Any
    #: Model calls made so far in this run.
    model_calls: int
    #: The frame the save's target and identity were resolved on, if any.
    frame: Callable[[], Optional[bytes]]
    #: Re-reads the form's field state now (see :data:`FormStateReader`).
    form_state: FormStateReader = _no_form_state


@dataclass(frozen=True)
class GateVerdict:
    """The gate's answer for one save."""

    proceed: bool
    decision: Optional[ItemDecision]
    checks: tuple[CheckOutcome, ...]

    def refusal_message(self) -> Optional[str]:
        """Plain-words reason for the halt. Codes only; never a value."""

        if self.proceed or self.decision is None:
            return None
        failed = next((check for check in self.checks if not check.passed), None)
        detail = f" ({failed.code}: {failed.detail})" if failed is not None else ""
        if self.decision.kind is ItemDecisionKind.DECLINED:
            reason = self.decision.reason or "declined"
            return (
                f"{ITEM_DECLINED_MARKER} ({reason}). This save was not made. "
                "Nothing was retried."
            )
        if self.decision.kind is ItemDecisionKind.STOP_BEFORE_SAVING:
            return (
                f"Approval mode: this item stopped before saving{detail}. "
                "Nothing was retried."
            )
        return f"{ITEM_APPROVAL_PAUSE_MARKER}{detail}. Nothing was retried."


class ApprovalGate:
    """Pre-save gate and post-run finish for one item under an approval mode."""

    def __init__(
        self,
        mode: ApprovalMode | str | None,
        *,
        record: Optional[SwitchRecord] = None,
        log: SwitchLog | Path | str | None = None,
        pinned_status_keys: Optional[Mapping[str, str]] = None,
        fetch_status: Optional[StatusFetcher] = None,
        environment_digest: Optional[Callable[[], Optional[str]]] = None,
        runner_id: Optional[str] = None,
        signed_in_user: Optional[SignedInUserCheck] = None,
        dedicated_user_key: Optional[bytes] = None,
        item: Optional[ItemContext] = None,
        monotonic: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        approval_store: ItemApprovalStore | Path | str | None = None,
        approval_wait_s: float = DEFAULT_APPROVAL_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        approval_poll_s: float = 0.5,
        approver_policy: Optional["ApproverPolicyV1"] = None,
        approver: Optional["Approver"] = None,
        policy_name: Optional[str] = None,
    ) -> None:
        self.mode = resolve_approval_mode(mode)
        if approval_store is None or isinstance(approval_store, ItemApprovalStore):
            self.approval_store = approval_store
        else:
            self.approval_store = ItemApprovalStore(approval_store)
        # Who may answer a save. Person-only unless the deployment widened it.
        # The run's store records the policy at run start, always, so every
        # reader applies the same one, and this gate applies only that one: a
        # policy file that appears or changes later is refused or ignored.
        self.approver_policy = approver_policy
        self.approver = approver
        self._policy_name = policy_name
        if self.approval_store is not None:
            self.approval_store.write_policy(
                approver_policy
                if approver_policy is not None
                else DEFAULT_APPROVER_POLICY
            )
        self._approval_wait_s = float(approval_wait_s)
        self._approval_poll_s = float(approval_poll_s)
        self._sleep = sleep
        self.record = record
        if log is None or isinstance(log, SwitchLog):
            self.log = log
        else:
            self.log = SwitchLog(log)
        self._pinned_keys = dict(pinned_status_keys or {})
        self._fetch_status = fetch_status
        self._environment_digest = environment_digest
        self._runner_id = runner_id
        self._signed_in_user = signed_in_user
        self._dedicated_user_key = dedicated_user_key
        # The item context is single-use: begin_item consumes it, so a reused
        # gate never applies one item's list facts to the next item.
        self._item_pending: Optional[ItemContext] = item
        self._monotonic = monotonic
        self._nonce_factory = nonce_factory
        self._reset_item_state()

    # -- per-item state --------------------------------------------------------
    def next_item(self, item: ItemContext) -> None:
        """Load the context for the next item when one gate is reused."""

        self._item_pending = item

    def _reset_item_state(self) -> None:
        self._item: ItemContext = ItemContext()
        self._run_ref: Optional[str] = None
        self._bundle_digest: Optional[str] = None
        self._item_checks: list[CheckOutcome] = []
        self._item_fallback: Optional[ItemDecision] = None
        self._pre_checks: list[CheckOutcome] = []
        self._decision: Optional[ItemDecision] = None
        self._gated_step_id: Optional[str] = None
        self._saves_without_person = 0
        # Saves that passed every pre-check, keyed by id() and holding the
        # result itself: the reference keeps the id from being reused by a
        # later result object, and the step id must match too.
        self._passed_results: dict[int, tuple[str, "StepResult"]] = {}
        self._item_started_logged = False
        # Saves a person approved, keyed like ``_passed_results`` and holding
        # the request whose form state the save must still show.
        self._approved: dict[int, tuple[str, "StepResult", ItemApprovalRequest]] = {}
        self._approved_edge_checked: set[int] = set()
        self._approvals: list[ItemApprovalRef] = []
        self._declined: Optional[ItemApprovalRef] = None
        self._run_id: Optional[str] = None
        self._workflow: Optional["Workflow"] = None
        self._off_logged: Optional[OffReason] = None
        self._status: Optional[SwitchStatus] = None
        self._status_fetched_at: Optional[float] = None
        self._finished: Optional[ApprovalModeItemEvidence] = None
        self._began = False

    def begin_item(
        self,
        *,
        run_id: str,
        bundle_content_digest: Optional[str],
        profile: Optional[str],
        inputs_from_recording: Sequence[str] = (),
        durable_resume: bool = False,
        model_egress_possible: bool = False,
    ) -> Optional[str]:
        """Start one item. Returns a refusal message, or ``None``.

        In a gated mode, a run in which an input came from the recording is
        refused before anything happens: values must arrive already approved.
        """

        self._reset_item_state()
        if self._item_pending is not None:
            self._item = self._item_pending
        self._item_pending = None
        self._began = True
        self._run_id = run_id
        self._run_ref = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self._bundle_digest = bundle_content_digest
        if self.mode is ApprovalMode.NOT_GATED:
            return None
        refusal: Optional[str] = None
        recorded = sorted(set(inputs_from_recording))
        self._item_checks.append(
            CheckOutcome(
                code="inputs_supplied",
                passed=not recorded,
                detail="ok" if not recorded else "input_from_recording",
            )
        )
        if recorded:
            refusal = (
                "Approval mode: every declared input must be supplied for this "
                f"item; {len(recorded)} input(s) would come from the recording. "
                "Nothing was typed or saved."
            )
            self._item_fallback = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="inputs_supplied",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
        if self.mode is ApprovalMode.PROBLEMS_ONLY:
            self._evaluate_item_checks(
                profile=profile,
                durable_resume=durable_resume,
                model_egress_possible=model_egress_possible,
            )
        return refusal

    def _evaluate_item_checks(
        self,
        *,
        profile: Optional[str],
        durable_resume: bool,
        model_egress_possible: bool,
    ) -> None:
        item = self._item
        record = self.record
        expected_channel = record.channel if record is not None else None
        checks = [
            CheckOutcome(
                code="channel",
                passed=item.channel is not None and item.channel == expected_channel,
                detail=(
                    "ok"
                    if item.channel is not None and item.channel == expected_channel
                    else "channel_not_covered"
                ),
            ),
            CheckOutcome(
                code="inputs_strict",
                passed=item.header_maps_every_input
                and item.rows_well_formed
                and item.values_pass_types,
                detail=(
                    "ok"
                    if item.header_maps_every_input
                    and item.rows_well_formed
                    and item.values_pass_types
                    else "inputs_not_strictly_parsed"
                ),
            ),
            CheckOutcome(
                code="not_a_retry",
                passed=not item.is_retry and not item.allow_repeat,
                detail=(
                    "ok"
                    if not item.is_retry and not item.allow_repeat
                    else "retry_or_repeat"
                ),
            ),
            CheckOutcome(
                code="record_key_not_recent",
                passed=item.record_key_seen_recently is False,
                detail=(
                    "ok"
                    if item.record_key_seen_recently is False
                    else (
                        "record_key_unknown"
                        if item.record_key_seen_recently is None
                        else "record_key_entered_recently"
                    )
                ),
            ),
            CheckOutcome(
                code="deployment_config",
                passed=not item.caller_supplied_configuration
                and record is not None
                and profile == record.profile
                and not model_egress_possible,
                detail=(
                    "caller_supplied_configuration"
                    if item.caller_supplied_configuration
                    else "profile_not_covered"
                    if record is None or profile != record.profile
                    else "model_egress_possible"
                    if model_egress_possible
                    else "ok"
                ),
            ),
            CheckOutcome(
                code="switch_lease",
                passed=item.holds_switch_lease,
                detail="ok" if item.holds_switch_lease else "lease_not_held",
            ),
            CheckOutcome(
                code="not_resumed",
                passed=not durable_resume,
                detail="ok" if not durable_resume else "resumed_run",
            ),
        ]
        self._item_checks.extend(checks)
        failed = next((check for check in checks if not check.passed), None)
        if failed is not None and self._item_fallback is None:
            self._item_fallback = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason=failed.code,
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )

    # -- when a save step starts ------------------------------------------------
    def before_step(self, ctx: StepContext) -> GateVerdict:
        """Ask a person before a save step runs, when the mode requires it.

        Called before the step resolves its target, so that after an approval
        the step takes a fresh frame and resolves its target and right-record
        check again. Returns ``proceed`` when no person is needed yet (the
        last-point gate still decides), when a person approved, or when no
        approval store is configured (the last-point gate then refuses).
        """

        try:
            return self._before_step(ctx)
        except Exception:  # noqa: BLE001 - every gate failure goes to a person
            decision = ItemDecision(
                kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                reason="gate_error",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="gate_error",
                passed=False,
                detail="gate_error",
                step_id=ctx.step.id,
            )
            self._halt_step(ctx.step.id, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

    def _halt_step(
        self, step_id: str, decision: ItemDecision, checks: list[CheckOutcome]
    ) -> None:
        self._pre_checks.extend(checks)
        # A person's decline, or a stop, replaces a pending request for
        # approval as the item's decision. Nothing replaces a decline.
        if self._decision is None or (
            self._decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
            and decision.kind is not ItemDecisionKind.APPROVAL_REQUIRED
        ):
            self._decision = decision
            self._gated_step_id = step_id
        if decision.switch_off is not None:
            self._log_switch_off(decision.switch_off)

    def _needs_approval(self) -> bool:
        if self.mode is ApprovalMode.EACH_ITEM:
            return True
        if self.mode is ApprovalMode.PROBLEMS_ONLY:
            return self._item_fallback is not None or (
                self._decision is not None
                and self._decision.kind is ItemDecisionKind.APPROVAL_REQUIRED
            )
        return False

    def _before_step(self, ctx: StepContext) -> GateVerdict:
        if not self._began:
            raise RuntimeError("begin_item was not called for this run")
        if self.mode is ApprovalMode.NOT_GATED:
            return GateVerdict(proceed=True, decision=None, checks=())
        self._workflow = ctx.workflow
        if not self._is_save(ctx):
            return GateVerdict(proceed=True, decision=None, checks=())
        if self._declined is not None:
            decision = ItemDecision(
                kind=ItemDecisionKind.DECLINED,
                reason=self._declined.decline_reason,
                needs_person=False,
                counts_toward_problems=False,
                switch_off=None,
            )
            return GateVerdict(proceed=False, decision=decision, checks=())
        if ctx.drill_fault_step:
            # A drill must stop at its fault step by itself. Nobody is asked,
            # and the last-point gate refuses any save there without an
            # approval, so a drill that fails to stop still saves nothing.
            return GateVerdict(proceed=True, decision=None, checks=())
        if not self._needs_approval():
            return GateVerdict(proceed=True, decision=None, checks=())
        store = self.approval_store
        if store is None or self._run_id is None:
            # No way to ask a person here: the last-point gate refuses.
            return GateVerdict(proceed=True, decision=None, checks=())

        def wait_refusal(detail: str) -> GateVerdict:
            if self._item_fallback is not None:
                reason: Optional[str] = self._item_fallback.reason
            elif self._decision is not None:
                reason = self._decision.reason
            else:
                reason = ApprovalMode.EACH_ITEM.value
            decision = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason=reason,
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="each_item", passed=False, detail=detail, step_id=ctx.step.id
            )
            self._halt_step(ctx.step.id, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

        found = store.find_unused_approval(
            run_id=self._run_id, step_id=ctx.step.id, step_index=ctx.step_index
        )
        if found is not None:
            # A resumed run: the person already approved this exact save.
            request, answer = found
        else:
            frame = ctx.frame()
            if not frame:
                return wait_refusal("frame_unavailable")
            form = ctx.form_state(frame)
            if form is None:
                return wait_refusal("form_state_unreadable")
            try:
                request = store.open_request(
                    run_id=self._run_id,
                    workflow_name=ctx.workflow.name,
                    step_id=ctx.step.id,
                    step_index=ctx.step_index,
                    step_intent=ctx.step.intent or "",
                    mode=self.mode,
                    bundle_content_digest=self._bundle_digest,
                    runtime_inputs_digest=ctx.runtime_inputs_digest,
                    frame_png=frame,
                    form_state=form,
                    qualification_case_kind=ctx.qualification_case_kind,
                    step_class=step_class_for(ctx.step, policy_name=self._policy_name),
                    fields=ctx.save_fields(),
                    api_request=ctx.api_request,
                )
            except (OSError, ValueError, ItemApprovalRefused):
                return wait_refusal("approval_request_unwritable")
            waited, code = wait_for_answer(
                store,
                request,
                timeout_s=self._approval_wait_s,
                poll_s=self._approval_poll_s,
                monotonic=self._monotonic,
                sleep=self._sleep,
                approver=self.approver,
            )
            if waited is None:
                return wait_refusal(code or "approval_wait_timed_out")
            answer = waited
        reference = answer.reference(request)
        if answer.decision == "decline":
            self._approvals.append(reference)
            self._declined = reference
            decision = ItemDecision(
                kind=ItemDecisionKind.DECLINED,
                reason=answer.decline_reason,
                needs_person=False,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="each_item",
                passed=False,
                detail=(
                    "declined_by_person"
                    if answer.decided_by == "human"
                    else "declined_by_delegate"
                ),
                step_id=ctx.step.id,
            )
            self._halt_step(ctx.step.id, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))
        if not store.consume(request.request_id):
            return wait_refusal("approval_already_used")
        self._approvals.append(reference)
        self._approved[id(ctx.result)] = (ctx.step.id, ctx.result, request)
        return GateVerdict(proceed=True, decision=None, checks=())

    # -- before each save ------------------------------------------------------
    def before_save(self, ctx: SaveContext) -> GateVerdict:
        """Decide whether this save may proceed without a person."""

        try:
            return self._before_save(ctx)
        except Exception:  # noqa: BLE001 - every gate failure goes to a person
            decision = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="gate_error",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="gate_error",
                passed=False,
                detail="gate_error",
                step_id=ctx.step.id,
            )
            self._halt(ctx, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

    def _halt(
        self, ctx: SaveContext, decision: ItemDecision, checks: list[CheckOutcome]
    ) -> None:
        self._pre_checks.extend(checks)
        if self._decision is None:
            self._decision = decision
            self._gated_step_id = ctx.step.id
        if decision.switch_off is not None:
            self._log_switch_off(decision.switch_off)

    def _before_save(self, ctx: SaveContext) -> GateVerdict:
        if not self._began:
            raise RuntimeError("begin_item was not called for this run")
        if self.mode is ApprovalMode.NOT_GATED:
            return GateVerdict(proceed=True, decision=None, checks=())
        self._workflow = ctx.workflow
        if not self._is_save(ctx):
            # The runtime also asks before an identity-checked navigation
            # click (opening the patient's chart). That isn't a save: no record
            # check names it and nothing is gated on it. The engine's own
            # identity check still applies to it.
            return GateVerdict(proceed=True, decision=None, checks=())

        approved = self._approved.get(id(ctx.result))
        if (
            approved is not None
            and approved[1] is ctx.result
            and approved[0] == ctx.step.id
        ):
            return self._approved_save(ctx, approved[2])
        if self._declined is not None:
            decision = ItemDecision(
                kind=ItemDecisionKind.DECLINED,
                reason=self._declined.decline_reason,
                needs_person=False,
                counts_toward_problems=False,
                switch_off=None,
            )
            return GateVerdict(proceed=False, decision=decision, checks=())

        if self._decision is not None:
            # A gate that already sent the item to a person never reopens.
            return GateVerdict(proceed=False, decision=self._decision, checks=())
        if self._item_fallback is not None:
            failed = next(c for c in self._item_checks if not c.passed)
            check = failed.model_copy(update={"step_id": ctx.step.id})
            self._halt(ctx, self._item_fallback, [])
            return GateVerdict(
                proceed=False, decision=self._item_fallback, checks=(check,)
            )
        if self.mode is ApprovalMode.EACH_ITEM:
            decision = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="each_item",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="each_item",
                passed=False,
                detail="approval_required",
                step_id=ctx.step.id,
            )
            self._halt(ctx, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

        passed = self._passed_results.get(id(ctx.result))
        if passed is not None and passed[1] is ctx.result and passed[0] == ctx.step.id:
            # Another input edge of a save that already passed (for example a
            # focus click before typing). Re-read the switch log, which must
            # hold before every input edge, and re-check the screens.
            repeat: list[CheckOutcome] = []
            for check, failure in (
                self._check_switch_on(ctx, refresh_status=False),
                self._check_same_screens(ctx),
            ):
                repeat.append(check)
                if failure is not None:
                    self._halt(ctx, failure, repeat)
                    return GateVerdict(
                        proceed=False, decision=failure, checks=tuple(repeat)
                    )
            return GateVerdict(proceed=True, decision=None, checks=tuple(repeat))

        checks: list[CheckOutcome] = []
        for evaluate in (
            self._check_switch_on,
            self._check_signed_in_user,
            self._check_right_record,
            self._check_same_screens,
            self._check_record_check_ready,
        ):
            check, failure = evaluate(ctx)
            checks.append(check)
            if failure is not None:
                self._halt(ctx, failure, checks)
                return GateVerdict(
                    proceed=False, decision=failure, checks=tuple(checks)
                )
        # Every pre-check passed. Record that a save without a person is about
        # to start, so an interrupted item is found by the next one.
        if not self._item_started_logged:
            try:
                if self.log is None or self._run_ref is None:
                    raise SwitchLogError("no switch log for this item")
                self.log.append("item_started", run_ref=self._run_ref)
            except (SwitchLogError, OSError):
                check = CheckOutcome(
                    code="switch_log",
                    passed=False,
                    detail="switch_log_unwritable",
                    step_id=ctx.step.id,
                )
                decision = ItemDecision(
                    kind=ItemDecisionKind.APPROVAL_REQUIRED,
                    reason="switch_log",
                    needs_person=True,
                    counts_toward_problems=False,
                    switch_off=None,
                )
                checks.append(check)
                self._halt(ctx, decision, checks)
                return GateVerdict(
                    proceed=False, decision=decision, checks=tuple(checks)
                )
            self._item_started_logged = True
        self._pre_checks.extend(checks)
        self._passed_results[id(ctx.result)] = (ctx.step.id, ctx.result)
        self._saves_without_person += 1
        return GateVerdict(proceed=True, decision=None, checks=tuple(checks))

    def _approved_save(
        self, ctx: SaveContext, request: ItemApprovalRequest
    ) -> GateVerdict:
        """A save a person approved: proceed only if the form is unchanged."""

        key = id(ctx.result)
        if key in self._approved_edge_checked:
            # Another input edge of the same approved save (for example the
            # focus click before typing). The form changes as it is typed.
            return GateVerdict(proceed=True, decision=None, checks=())
        try:
            frame = ctx.frame()
        except Exception:  # noqa: BLE001
            frame = None
        try:
            current = ctx.form_state(frame)
        except Exception:  # noqa: BLE001
            current = None
        expected = (request.form_state_kind, request.form_state_sha256)
        if current is None or current != expected:
            detail = (
                "form_state_unreadable"
                if current is None
                else "form_changed_after_approval"
            )
            decision = ItemDecision(
                kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                reason="each_item",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="each_item", passed=False, detail=detail, step_id=ctx.step.id
            )
            # The approval is spent: a changed form needs a new run and a new
            # approval of what the person can see.
            self._approved.pop(key, None)
            self._decision = decision
            self._gated_step_id = ctx.step.id
            self._pre_checks.append(check)
            return GateVerdict(proceed=False, decision=decision, checks=(check,))
        self._approved_edge_checked.add(key)
        check = CheckOutcome(
            code="each_item",
            passed=True,
            detail="approved_by_person",
            step_id=ctx.step.id,
        )
        self._pre_checks.append(check)
        return GateVerdict(proceed=True, decision=None, checks=(check,))

    def _certifying_policy(self) -> Any:
        """The policy the bundle is certified under, when the gate names one."""

        if not self._policy_name:
            return None
        cached = getattr(self, "_policy_cache", None)
        if cached is None:
            from openadapt_flow.policy import load_policy

            try:
                cached = load_policy(self._policy_name)
            except Exception:  # noqa: BLE001 - an unknown policy proves nothing
                cached = False
            self._policy_cache = cached
        return cached or None

    def _is_consequential(self, step: Any, workflow: Any) -> bool:
        """The run gate's fail-closed consequential-step classifier.

        Given the certifying policy, an operator's qualified risk
        classification counts only while the bundle's certification under
        that policy is current, exactly as in the run gate. Without it the
        conservative heuristic decides, so a step the qualification
        classified as reversible can still be treated as a save.
        """

        from openadapt_flow.run_gate import is_consequential

        return is_consequential(
            step, workflow, certifying_policy=self._certifying_policy()
        )

    def _is_save(self, ctx: SaveContext | StepContext) -> bool:
        """Whether this step is a consequential save.

        The same classifier the run gate uses, which the record-check coverage
        uses for its windows too, so the gate and coverage agree on which
        steps are saves.
        """

        return self._is_consequential(ctx.step, ctx.workflow)

    # -- P1 --------------------------------------------------------------------
    def _check_switch_on(
        self, ctx: SaveContext, refresh_status: bool = True
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(
            detail: str, off: Optional[OffReason] = None
        ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="switch_on", passed=False, detail=detail, step_id=ctx.step.id
                ),
                ItemDecision(
                    kind=ItemDecisionKind.APPROVAL_REQUIRED,
                    reason="switch_on",
                    needs_person=True,
                    counts_toward_problems=False,
                    switch_off=off,
                ),
            )

        record = self.record
        if record is None or self.log is None:
            return fail("no_switch_record")
        try:
            state = self.log.state()
        except (SwitchLogError, OSError):
            return fail("switch_log_unreadable")
        if (
            state.switch_id != record.switch_id
            or state.switch_record_sha256 != record.digest()
        ):
            return fail("switch_log_for_other_record")
        if state.is_off:
            return fail("switched_off")
        if any(
            ref != self._run_ref or not self._item_started_logged
            for ref in state.open_items
        ):
            # An item started without a person and never finished. Only this
            # gate's own ``item_started`` may be open: a run that reuses an
            # interrupted run's id must not hide it.
            return fail("interrupted_item", OffReason.UNCERTAIN_RESULT)
        if self._runner_id is None:
            # A caller that didn't say which runner this is proves nothing.
            return fail("runner_unknown")
        if self._runner_id != record.runner_id:
            return fail("runner_changed", OffReason.ENVIRONMENT_CHANGED)
        if self._bundle_digest is None:
            return fail("bundle_unsealed")
        if self._bundle_digest != record.bundle_content_digest:
            return fail("version_changed", OffReason.VERSION_CHANGED)
        environment: Optional[str]
        try:
            environment = (
                self._environment_digest() if self._environment_digest else None
            )
        except Exception:  # noqa: BLE001 - an unreadable environment is not proof
            environment = None
        if environment is None:
            return fail("environment_unreadable")
        if environment != record.environment_digest:
            return fail("environment_changed", OffReason.ENVIRONMENT_CHANGED)
        if not refresh_status:
            if not self._status_is_fresh():
                return fail("status_unconfirmed")
            return CheckOutcome(
                code="switch_on", passed=True, step_id=ctx.step.id
            ), None
        status_detail, cloud_off = self._ensure_status(state)
        if cloud_off is not None:
            return fail("cloud_switched_off", cloud_off)
        if status_detail != "ok":
            self._log_status_unconfirmed(state)
            return fail(status_detail)
        if state.status_unconfirmed:
            try:
                self.log.append("status_restored")
            except (SwitchLogError, OSError):
                return fail("switch_log_unwritable")
        return CheckOutcome(code="switch_on", passed=True, step_id=ctx.step.id), None

    def _status_is_fresh(self) -> bool:
        if self._status is None or self._status_fetched_at is None:
            return False
        assert self.record is not None
        age = self._monotonic() - self._status_fetched_at
        return 0 <= age <= self.record.thresholds.status_max_age_s

    def _ensure_status(self, state: SwitchLogState) -> tuple[str, Optional[OffReason]]:
        """Return (detail, cloud_off_reason). ``detail == 'ok'`` means fresh."""

        if self._status_is_fresh():
            return "ok", None
        record = self.record
        assert record is not None
        if self._fetch_status is None:
            return "no_status_source", None
        try:
            request = StatusRequest(
                switch_id=record.switch_id,
                switch_record_sha256=record.digest(),
                runner_id=record.runner_id,
                nonce=self._nonce_factory(),
                runner_event_seq=state.last_seq,
                runner_event_sha256=state.last_sha256,
            )
        except ValueError:
            return "status_request_invalid", None
        # Freshness counts from before the request left, on the monotonic
        # clock, so a slow answer can only shorten the window.
        requested_at = self._monotonic()
        try:
            raw = self._fetch_status(request)
        except Exception:  # noqa: BLE001 - an outage is "unconfirmed", never "on"
            return "status_fetch_failed", None
        try:
            status = verify_status(raw, request=request, pinned_keys=self._pinned_keys)
        except StatusRejected:
            return "status_rejected", None
        if status.state == "off":
            return "ok", status.off_reason or OffReason.TURNED_OFF
        self._status = status
        self._status_fetched_at = requested_at
        return "ok", None

    def _log_status_unconfirmed(self, state: SwitchLogState) -> None:
        if state.status_unconfirmed or self.log is None:
            return
        try:
            self.log.append("status_unconfirmed")
        except (SwitchLogError, OSError):
            pass

    def _log_switch_off(self, reason: OffReason) -> None:
        if self.mode is not ApprovalMode.PROBLEMS_ONLY or self.log is None:
            return
        if self._off_logged is not None:
            return
        try:
            state = self.log.state()
            if not state.is_off:
                self.log.append("switched_off", reason=reason.value)
            self._off_logged = reason
        except (SwitchLogError, OSError):
            # An unwritable log reads as off for the next item anyway.
            pass

    # -- P2 --------------------------------------------------------------------
    def _check_signed_in_user(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(
            detail: str, *, counts: bool, off: Optional[OffReason] = None
        ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="signed_in_user",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="signed_in_user",
                    needs_person=True,
                    counts_toward_problems=counts,
                    switch_off=off,
                ),
            )

        check = self._signed_in_user
        record = self.record
        assert record is not None
        if check is None:
            return fail("not_configured", counts=False)
        if self._dedicated_user_key is None:
            return fail("dedicated_user_unbound", counts=False)
        try:
            reference = check.reference(self._dedicated_user_key)
        except Exception:  # noqa: BLE001
            return fail("dedicated_user_unbound", counts=False)
        if not hmac.compare_digest(reference, record.dedicated_user_ref):
            return fail(
                "dedicated_user_changed",
                counts=False,
                off=OffReason.ENVIRONMENT_CHANGED,
            )
        try:
            frame = ctx.frame()
        except Exception:  # noqa: BLE001
            frame = None
        if not frame:
            return fail("frame_unavailable", counts=True)
        try:
            verdict = check.check(frame)
        except Exception:  # noqa: BLE001
            verdict = "unreadable"
        if verdict == "match":
            detail = "ok" if check.kind == "login_id" else "display_name_by_attestation"
            return (
                CheckOutcome(
                    code="signed_in_user",
                    passed=True,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                None,
            )
        if verdict == "mismatch":
            return fail("other_user_signed_in", counts=True)
        return fail("signed_in_user_unreadable", counts=True)

    # -- P3 --------------------------------------------------------------------
    def _check_right_record(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(detail: str) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="right_record",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="right_record",
                    needs_person=True,
                    counts_toward_problems=True,
                    switch_off=None,
                ),
            )

        checks = [r.identity for r in ctx.prior_results if r.identity is not None]
        current = ctx.result.identity
        if current is not None:
            checks.append(current)
        if any(check.status != "verified" for check in checks):
            return fail("identity_not_affirmative")
        anchored = ctx.step.anchor is not None or bool(ctx.step.identity_armed)
        if anchored and (current is None or current.status != "verified"):
            return fail("save_target_not_verified")
        if not checks:
            return fail("no_right_record_check")
        return CheckOutcome(code="right_record", passed=True, step_id=ctx.step.id), None

    # -- P4 --------------------------------------------------------------------
    def _check_same_screens(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        record = self.record
        assert record is not None
        problem: Optional[str] = None
        problem_step: Optional[str] = ctx.step.id
        if ctx.model_calls:
            problem = "model_call"
        else:
            for result in [*ctx.prior_results, ctx.result]:
                problem = step_screen_problem(result, record)
                if problem is not None:
                    problem_step = result.step_id
                    break
        if problem is None:
            return CheckOutcome(
                code="same_screens", passed=True, step_id=ctx.step.id
            ), None
        return (
            CheckOutcome(
                code="same_screens", passed=False, detail=problem, step_id=problem_step
            ),
            ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="same_screens",
                needs_person=True,
                counts_toward_problems=True,
                switch_off=OffReason.SCREEN_CHANGED,
            ),
        )

    # -- P5 --------------------------------------------------------------------
    def _check_record_check_ready(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        from openadapt_flow.approval_mode.coverage import (
            save_contract_problems,
            save_coverage,
        )
        from openadapt_flow.policy import iter_effect_paths
        from openadapt_flow.runtime.effects.adapter import (
            _probe_adapter_connection,
            verifier_identity,
        )
        from openadapt_flow.verification import VerificationTier, verifier_effect_tier

        def fail(detail: str) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="record_check_ready",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="record_check_ready",
                    needs_person=True,
                    counts_toward_problems=True,
                    switch_off=OffReason.RECORD_CHECK_LOST,
                ),
            )

        record = self.record
        assert record is not None
        verifier = ctx.effect_verifier
        if verifier is None:
            return fail("no_record_check")
        authorization = ctx.governed_authorization
        if authorization is not None and authorization.approves_unverified_write(
            ctx.step
        ):
            return fail("approved_unverified_write")
        coverage = save_coverage(
            ctx.workflow,
            ctx.step,
            is_save=lambda step: self._is_consequential(step, ctx.workflow),
        )
        if not coverage.covered:
            return fail(coverage.problems[0])
        contract = save_contract_problems(ctx.step)
        if contract:
            return fail(contract[0])
        for _path, effects in iter_effect_paths(ctx.step):
            for effect in effects:
                tier = verifier_effect_tier(verifier, effect)
                if tier is VerificationTier.INDEPENDENT_SESSION:
                    # Separate-session UI read-back is a pilot: it is record
                    # evidence while a person approves each entry, and never
                    # lets an entry save without one.
                    return fail(UI_READBACK_PILOT)
                if tier is not VerificationTier.INDEPENDENT_SYSTEM:
                    return fail("read_back_not_system_of_record")
        candidates = getattr(verifier, "_candidates", None) or (verifier,)
        allowed = set(record.verifier_identities)
        for candidate in candidates:
            if verifier_identity(candidate) not in allowed:
                return fail("verifier_identity_changed")
        probe = _probe_adapter_connection(verifier)
        if not probe.ok:
            return fail("record_check_unreachable")
        return (
            CheckOutcome(code="record_check_ready", passed=True, step_id=ctx.step.id),
            None,
        )

    # -- after the run -----------------------------------------------------------
    def finish_item(self, report: "RunReport") -> ApprovalModeItemEvidence:
        """Decide the item, write the switch log, and return the evidence block.

        Safe to call again for the same run (for example after a late
        correction of the report): a changed decision is logged again, an
        unchanged one is not.
        """

        mode = self.mode
        channel = self._item.channel
        record = self.record
        switch_fields: dict[str, Any] = {}
        if record is not None and mode is ApprovalMode.PROBLEMS_ONLY:
            switch_fields = {
                "switch_id": record.switch_id,
                "switch_record_sha256": record.digest(),
            }
        if mode is ApprovalMode.NOT_GATED:
            evidence = ApprovalModeItemEvidence(
                mode=mode,
                channel=channel,
                decision=ItemDecisionKind.NOT_GATED,
                decision_reason="no_per_item_pause",
                needs_person=report.transaction_outcome != "VERIFIED",
            )
            self._finished = evidence
            return evidence
        approvals = self._item_approvals()
        approval_fields: dict[str, Any] = {"approvals": approvals}
        if mode is ApprovalMode.EACH_ITEM:
            decision = self._decision
            if decision is not None:
                evidence = ApprovalModeItemEvidence(
                    mode=mode,
                    channel=channel,
                    item_checks=list(self._item_checks),
                    pre_checks=list(self._pre_checks),
                    gated_step_id=self._gated_step_id,
                    decision=decision.kind,
                    decision_reason=decision.reason,
                    needs_person=decision.kind is not ItemDecisionKind.DECLINED,
                    **approval_fields,
                )
            else:
                kind, reason, needs_person = self._approved_item_decision(
                    report, approvals
                )
                evidence = ApprovalModeItemEvidence(
                    mode=mode,
                    channel=channel,
                    item_checks=list(self._item_checks),
                    pre_checks=list(self._pre_checks),
                    decision=kind,
                    decision_reason=reason,
                    needs_person=needs_person,
                    **approval_fields,
                )
            self._finished = evidence
            return evidence
        if self._declined is not None or (approvals and self._decision is None):
            return self._finish_problems_only_with_person(
                report, approvals, channel=channel
            )

        # problems_only
        if record is None:
            r1 = CheckOutcome(code="verified", passed=False, detail="no_switch_record")
            r2 = CheckOutcome(
                code="whole_run_clean", passed=False, detail="no_switch_record"
            )
        else:
            r1 = check_verified(
                report,
                record,
                saves_without_person=self._saves_without_person,
                gated_results=[result for _, result in self._passed_results.values()],
            )
            r2 = check_whole_run_clean(report, record)
        gate_decision = self._decision
        if gate_decision is None and self._item_fallback is not None:
            gate_decision = self._item_fallback
        decision = decide_problems_only(
            report,
            r1=r1,
            r2=r2,
            gate_decision=gate_decision,
            save_delivered=any(
                result.delivery_attempted for _, result in self._passed_results.values()
            ),
        )
        previous = self._finished
        if (
            previous is not None
            and previous.decision is decision.kind
            and previous.decision_reason == decision.reason
        ):
            recorded = previous.switch_log_recorded
            off = previous.switch_off_reason
        else:
            recorded, off = self._log_finish(decision)
        kind = decision.kind
        reason = decision.reason
        needs_person = decision.needs_person
        counts = decision.counts_toward_problems
        if kind is ItemDecisionKind.NO_PERSON and not recorded:
            # A saved-and-checked item whose result could not be logged still
            # goes to a person. Its open ``item_started`` event also makes the
            # next item find it and switch off as interrupted.
            kind = ItemDecisionKind.PROBLEM
            reason = "switch_log"
            needs_person = True
            counts = False
        evidence = ApprovalModeItemEvidence(
            mode=mode,
            channel=channel,
            **switch_fields,
            item_checks=list(self._item_checks),
            pre_checks=list(self._pre_checks),
            post_checks=[r1, r2],
            saves_without_person=self._saves_without_person,
            gated_step_id=self._gated_step_id,
            decision=kind,
            decision_reason=reason,
            needs_person=needs_person,
            counts_toward_problems=counts,
            switch_off_reason=off,
            switch_log_recorded=recorded,
            approvals=approvals,
        )
        self._finished = evidence
        return evidence

    def _item_approvals(self) -> list[ItemApprovalRef]:
        """Every approval this run used, across resumed legs, then a decline."""

        rows: list[ItemApprovalRef] = []
        store = self.approval_store
        if store is not None and self._run_id is not None:
            try:
                rows = store.consumed_approvals(self._run_id)
            except (OSError, ItemApprovalRefused):
                rows = []
        seen = {row.answer_sha256 for row in rows}
        for row in self._approvals:
            if row.answer_sha256 not in seen:
                rows.append(row)
                seen.add(row.answer_sha256)
        if self._declined is not None and rows[-1:] != [self._declined]:
            rows = [row for row in rows if row != self._declined] + [self._declined]
        return rows

    def _unapproved_delivered_saves(
        self, report: "RunReport", approvals: Sequence[ItemApprovalRef]
    ) -> int:
        """Delivered saves in the report that no person's approval covers."""

        from collections import Counter

        approved_steps = Counter(
            row.step_id for row in approvals if row.decision == "approved"
        )
        workflow = self._workflow
        steps = {}
        if workflow is not None:
            from openadapt_flow.traversal import iter_workflow_steps

            steps = {step.id: step for step in iter_workflow_steps(workflow)}
        delivered: Counter[str] = Counter()
        for result in report.results:
            if not (
                result.delivery_attempted is True
                or result.delivery_uncertainty is not None
            ):
                continue
            step = steps.get(result.step_id)
            if step is not None and workflow is not None:
                from openadapt_flow.run_gate import is_consequential

                consequential = is_consequential(step, workflow)
            else:
                from openadapt_flow.approval_mode.post import _consequential

                consequential = _consequential(result)
            if consequential:
                delivered[result.step_id] += 1
        return sum(
            max(0, count - approved_steps.get(step_id, 0))
            for step_id, count in delivered.items()
        )

    def _approved_item_decision(
        self, report: "RunReport", approvals: Sequence[ItemApprovalRef]
    ) -> tuple[ItemDecisionKind, Optional[str], bool]:
        """(kind, reason, needs_person) for an item no gate check stopped.

        ``approved`` only when a person approved every delivered save and the
        engine's own outcome is ``VERIFIED``. Any other result goes to a
        person; the outcome word is never changed.
        """

        outcome = report.transaction_outcome
        unapproved = self._unapproved_delivered_saves(report, approvals)
        if unapproved:
            return ItemDecisionKind.PROBLEM, "save_not_approved", True
        approved = [row for row in approvals if row.decision == "approved"]
        if approved and outcome == "VERIFIED":
            return ItemDecisionKind.APPROVED, None, False
        return (
            ItemDecisionKind.PROBLEM,
            (outcome or "unknown_outcome").lower(),
            True,
        )

    def _finish_problems_only_with_person(
        self,
        report: "RunReport",
        approvals: list[ItemApprovalRef],
        *,
        channel: Optional[str],
    ) -> ApprovalModeItemEvidence:
        """A ``problems_only`` item a person approved or declined.

        It never counts as saved without a person. A declined item, or an
        approved item that reached ``VERIFIED``, is not a problem; anything
        else is decided as an item that failed after the save.
        """

        record = self.record
        switch_fields: dict[str, Any] = {}
        if record is not None:
            switch_fields = {
                "switch_id": record.switch_id,
                "switch_record_sha256": record.digest(),
            }
        if self._declined is not None:
            decision = ItemDecision(
                kind=ItemDecisionKind.DECLINED,
                reason=self._declined.decline_reason,
                needs_person=False,
                counts_toward_problems=False,
                switch_off=None,
            )
        else:
            kind, reason, needs_person = self._approved_item_decision(report, approvals)
            if kind is ItemDecisionKind.APPROVED:
                decision = ItemDecision(
                    kind=kind,
                    reason=None,
                    needs_person=False,
                    counts_toward_problems=False,
                    switch_off=None,
                )
            else:
                failed = CheckOutcome(code="verified", passed=False, detail="approved")
                approved_steps = {
                    row.step_id for row in approvals if row.decision == "approved"
                }
                decision = decide_problems_only(
                    report,
                    r1=failed,
                    r2=failed.model_copy(update={"code": "whole_run_clean"}),
                    gate_decision=None,
                    save_delivered=any(
                        result.delivery_attempted is True
                        and result.step_id in approved_steps
                        for result in report.results
                    ),
                )
                if reason == "save_not_approved":
                    decision = ItemDecision(
                        kind=ItemDecisionKind.PROBLEM,
                        reason=reason,
                        needs_person=True,
                        counts_toward_problems=True,
                        switch_off=decision.switch_off,
                    )
        recorded, off = self._log_finish(decision)
        evidence = ApprovalModeItemEvidence(
            mode=self.mode,
            channel=channel,
            **switch_fields,
            item_checks=list(self._item_checks),
            pre_checks=list(self._pre_checks),
            gated_step_id=self._gated_step_id,
            decision=decision.kind,
            decision_reason=decision.reason,
            needs_person=decision.needs_person,
            counts_toward_problems=decision.counts_toward_problems,
            switch_off_reason=off,
            switch_log_recorded=recorded,
            approvals=approvals,
        )
        self._finished = evidence
        return evidence

    def _log_finish(self, decision: ItemDecision) -> tuple[bool, Optional[OffReason]]:
        """Append ``item_finished`` and any switch-off. Returns (recorded, off)."""

        off = decision.switch_off
        if self.log is None or self.record is None or self._run_ref is None:
            return False, off
        try:
            self.log.append(
                "item_finished",
                run_ref=self._run_ref,
                decision=decision.kind,
                reason=decision.reason,
                counts_toward_problems=decision.counts_toward_problems,
            )
        except (SwitchLogError, OSError):
            return False, off
        if off is not None:
            self._log_switch_off(off)
        if not decision.counts_toward_problems:
            # Only a counted problem can tip the problem rate.
            return True, off
        try:
            state = self.log.state()
            if not state.is_off and too_many_problems(
                state.finished, self.record.thresholds
            ):
                self.log.append(
                    "switched_off", reason=OffReason.TOO_MANY_PROBLEMS.value
                )
                self._off_logged = OffReason.TOO_MANY_PROBLEMS
                off = off or OffReason.TOO_MANY_PROBLEMS
        except (SwitchLogError, OSError):
            return True, off
        return True, off
