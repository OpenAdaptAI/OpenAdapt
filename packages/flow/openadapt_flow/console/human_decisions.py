"""Model-free, local projections for attended human decisions.

The portable task contains only opaque identifiers, closed enums, counts,
digests, and expiry. Protected screenshots remain separate authenticated local
artifacts. A signed task is presentation integrity, not execution authority;
the runtime's exact pause capability and fresh revalidation remain mandatory.

Two projections come out of one pause, and they are not the same thing:

* ``task`` -- the signed, Cloud-safe :class:`HumanDecisionTaskV1`. It is what
  :func:`portable_remote_decision_task` relays to an authenticated remote
  surface, so nothing may be added to it that could carry protected content.
* ``presentation`` -- **local only**. It is returned by :func:`decision_detail`
  to the loopback console and, through the customer-controlled runner-local
  portal, to a paired phone on the customer's own network. This is the boundary
  that already serves protected screenshot crops, and it is where
  :mod:`openadapt_flow.console.halt_detail` puts the closed-vocabulary "what
  broke / what gets re-checked" detail an operator needs to answer at all.
  :func:`portable_remote_decision_task` discards all of it except one closed
  re-projection: :func:`openadapt_flow.console.decision_context.remote_halt_context`
  rebuilds ``presentation["halt"]`` **without** its single string field, so a
  remote surface can say what broke in values that cannot represent protected
  content. Which tier a projection carries is
  :mod:`openadapt_flow.decision_delivery`'s decision, recorded on the wire, and
  never inferred.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, Optional

from openadapt_types import (
    HUMAN_DECISION_TASK_SCHEMA,
    HumanDecisionReceiptV1,
    HumanDecisionTaskV1,
)
from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.console import data
from openadapt_flow.console import halt_detail as halt_detail_mod
from openadapt_flow.console.attention import AttentionItem, _last_failed_result
from openadapt_flow.console.decision_context import (
    RemoteHaltContextV1,
    remote_halt_context,
)
from openadapt_flow.decision_delivery import (
    DecisionDeliveryTier,
    effective_remote_tier,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.execution_profiles import execution_profile_contract
from openadapt_flow.runtime.durable.attended import (
    AttendedActionExecutor,
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
    AttendedDecision,
    AttendedRelayBinding,
    attended_decision_payload,
    execute_attended_action,
)

_TASK_COPY: dict[str, tuple[str, str, str]] = {
    "identity": (
        "identity",
        "confirm_identity",
        "Does the live application show the intended record?",
    ),
    "disambiguation": (
        "ambiguity",
        "resolve_ambiguity",
        "Can you prepare one unambiguous target in the live application?",
    ),
    "resolution": (
        "ambiguity",
        "resolve_ambiguity",
        "Can you prepare one unambiguous target in the live application?",
    ),
    "human_required": (
        "human_step",
        "complete_human_step",
        "Have you completed the required human step in the live application?",
    ),
    "effect_refuted": (
        "effect",
        "confirm_persisted_effect",
        "Is the destination record ready for OpenAdapt to check again?",
    ),
    "effect_indeterminate": (
        "effect",
        "confirm_persisted_effect",
        "Is the independent verifier ready for OpenAdapt to check again?",
    ),
    "effect_escalated": (
        "effect",
        "confirm_persisted_effect",
        "Is the destination record ready for OpenAdapt to check again?",
    ),
    "effect_unverifiable": (
        "effect",
        "confirm_persisted_effect",
        "Is a qualified verifier now available for OpenAdapt to check?",
    ),
    "placeholder_effect": (
        "effect",
        "confirm_persisted_effect",
        "Has the required effect verifier been configured and qualified?",
    ),
}
_DEFAULT_TASK_COPY = (
    "halt",
    "review_halt",
    "Is the live application ready for OpenAdapt to verify and continue?",
)
_ACTION_MAP = {
    "continue": "verify_and_resume",
    "skip": "skip",
    "reject": "reject",
    "teach": "teach",
    "escalate": "escalate",
}
_SUBSTRATE_MAP = {
    "web": "browser",
    "windows": "windows",
    "macos": "macos",
    "linux": "linux",
    "rdp": "rdp",
    "citrix": "citrix",
}

#: Engine decision status -> (portable receipt state, closed reason code).
#: ``prepared``/``delivery_started`` are journal states that were reached
#: without a terminal receipt, so both project as "may have been sent".
_RECEIPT_STATE: dict[str, tuple[str, str]] = {
    "prepared": ("accepted_pending_runner", "pending_runner"),
    "delivery_started": ("delivery_uncertain", "delivery_uncertain"),
    "delivery_uncertain": ("delivery_uncertain", "delivery_uncertain"),
    "completed": ("completed", "verified_and_resumed"),
    "refused": ("refused", "revalidation_refused"),
    "halted": ("halted", "continuation_halted"),
    "needs_demonstration": ("demonstration_requested", "demonstration_requested"),
    "escalated": ("escalated", "escalation_recorded"),
    #: Terminal and distinct from ``escalated``: that one leaves the run
    #: resumable, this one ends it. A consumer reads the pair to decide whether
    #: to tell the operator someone will pick this up, and the two answers are
    #: opposite.
    "rejected": ("rejected", "rejected_by_operator"),
}


def decision_receipt(decision: AttendedDecision) -> HumanDecisionReceiptV1:
    """Project one engine decision into the closed, PHI-free shared receipt.

    ``AttendedDecision`` is the durable audit record: it carries a free-text
    message and the operator principal on purpose. Neither may leave the
    runner, so this projection *rebuilds* a closed value instead of redacting
    the audit record field by field.

    The shape is the shared ``openadapt-types`` contract rather than a
    Flow-local model, so protected content stays structurally unrepresentable
    on both sides of the wire: every field is an opaque id, a digest, a closed
    enum, or a pattern-checked RFC 3339 timestamp. The shared type also pins
    the permitted ``state``/``reason_code`` pairs and forbids
    ``report_success`` outside ``completed``, which a Flow-local model could
    not enforce for a remote consumer.

    ``action`` uses the portable vocabulary (``verify_and_resume``), never the
    engine's internal ``continue``, so a consumer compares it directly against
    the task's ``allowed_actions``. The receipt is unsigned: the console
    returns it over loopback, and an unsigned receipt never verifies, so a
    remote consumer that requires a signature is not weakened by its absence.
    """
    state, reason_code = _RECEIPT_STATE[decision.status]
    if decision.status == "completed" and decision.action == "skip":
        reason_code = "skipped_and_resumed"
    return HumanDecisionReceiptV1(
        task_id=f"task_{decision.pause_id}",
        pause_id=decision.pause_id,
        capability_digest=decision.capability_digest,
        request_digest=decision.request_digest,
        decision_digest=_sha256(attended_decision_payload(decision)),
        transition_receipt_digest=decision.transition_receipt_digest,
        action=_ACTION_MAP[decision.action],  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        reason_code=reason_code,  # type: ignore[arg-type]
        report_success=decision.report_success,
        decided_at=decision.created_at,
    )


class ConsoleAttendedActionRequest(AttendedActionRequest):
    """Browser request bound to both current task and engine capability."""

    model_config = ConfigDict(extra="forbid")

    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")

    def engine_request(self) -> AttendedActionRequest:
        return AttendedActionRequest.model_validate(
            self.model_dump(exclude={"task_digest", "task_signature"})
        )


class RemoteDecisionPrincipal(BaseModel):
    """Identity asserted by an already-authenticated delivery transport.

    Flow deliberately does not implement Cloud auth.  The proprietary control
    plane verifies AAL2 and its authenticated runner channel before
    constructing this closed, payload-free principal at the public engine
    boundary.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,199}$")
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    runner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    assurance: Literal["aal2"] = "aal2"


class RemoteDecisionProjection(BaseModel):
    """PHI-free binding a remote AAL2 surface may relay verbatim."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.remote-decision-projection/v1"] = (
        "openadapt.remote-decision-projection/v1"
    )
    task: HumanDecisionTaskV1
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    phase: Literal["paused"] = "paused"
    event_sequence: int = Field(ge=1)
    expected_transition_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    #: Which rung of :class:`~openadapt_flow.decision_delivery.DecisionDeliveryTier`
    #: this projection carries. Recorded rather than implied, so a consumer that
    #: renders a decision on degraded context can say so, and so an audit can
    #: tell a context-free answer from an informed one.
    delivery_tier: Literal["remote_closed_context", "remote_identifiers"] = (
        "remote_identifiers"
    )
    #: What broke, in closed enums, bounded integers and booleans only. Present
    #: exactly when ``delivery_tier`` is ``remote_closed_context``; ``None``
    #: otherwise, and ``None`` also when the halt carried no category this
    #: engine version recognises. It is never a partial or best-effort object.
    halt_context: Optional[RemoteHaltContextV1] = None
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RemoteAttendedActionRequest(ConsoleAttendedActionRequest):
    """A remote response echoed against its exact projected pause binding."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    runner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    phase: Literal["paused"] = "paused"
    event_sequence: int = Field(ge=1)
    idempotency_scope_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def engine_request(self) -> AttendedActionRequest:
        return AttendedActionRequest.model_validate(
            self.model_dump(
                exclude={
                    "task_digest",
                    "task_signature",
                    "tenant_id",
                    "runner_id",
                    "phase",
                    "event_sequence",
                    "idempotency_scope_digest",
                    "binding_digest",
                }
            )
        )


def _opaque_id(prefix: str, value: str) -> str:
    """Keep caller-supplied run labels out of the portable task."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _coverage(
    failed: Any,
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    identity_required: Optional[int] = None
    identity_confirmed: Optional[int] = None
    effect_required: Optional[int] = None
    effect_confirmed: Optional[int] = None

    if failed is not None and failed.identity is not None:
        identity = failed.identity
        identity_required = identity.quorum_required
        if identity_required is None:
            identity_required = len(identity.signal_evidence) or 1
        identity_confirmed = identity.quorum_verified
        if identity_confirmed is None:
            identity_confirmed = sum(
                signal.verdict == "verified" for signal in identity.signal_evidence
            )
            if not identity.signal_evidence:
                identity_confirmed = 1 if identity.status == "verified" else 0

    if failed is not None:
        if failed.effect_evidence:
            effect_required = len(failed.effect_evidence)
            effect_confirmed = sum(
                evidence.final_verdict == "confirmed"
                for evidence in failed.effect_evidence
            )
        elif failed.effect_contract_hashes:
            effect_required = len(failed.effect_contract_hashes)
            effect_confirmed = effect_required if failed.effect_verified else 0

    return (
        identity_required,
        identity_confirmed,
        effect_required,
        effect_confirmed,
    )


def _risk_class(failed: Any, effect_required: Optional[int]) -> str:
    if failed is None:
        return "unknown"
    if failed.risk == "irreversible":
        return "irreversible"
    if effect_required:
        return "consequential"
    return "unknown"


def _remote_scope(
    deployment: Optional[DeploymentConfig],
) -> tuple[bool, Optional[str], Optional[str]]:
    if deployment is None or not deployment.human_decisions.remote.enabled:
        return False, None, None
    remote = deployment.human_decisions.remote
    # DeploymentConfig validation guarantees these values. Keep the assertion
    # local so a future schema regression cannot silently weaken issuance.
    if remote.tenant_id is None or remote.runner_id is None:
        raise AttendedActionRefused(
            "remote decision issuance lacks an exact tenant or runner binding"
        )
    return True, remote.tenant_id, remote.runner_id


def _remote_delivery_tier(
    deployment: DeploymentConfig, report: Any = None
) -> DecisionDeliveryTier:
    """How much context this pause's remote projection may carry.

    Three independent ceilings apply and the weakest wins:

    * the deployment's own ``human_decisions.remote.context_tier``;
    * the profile named in ``deployment.runtime.profile``; and
    * the profile the RUN was actually executed under, recorded on its report.

    The third matters because a governed dispatch can carry an execution
    profile the local deployment file does not name — a hosted runner is given
    its authorization at dispatch time. Reading only the deployment would let a
    run executed as ``regulated`` be projected under a ``demo`` ceiling. An
    unprofiled deployment and a report with no profile both take ``regulated``,
    matching
    :func:`~openadapt_flow.execution_profiles.resolve_execution_profile`'s
    default — the strictest posture, never the most permissive.
    """
    profiles = [
        getattr(report, "execution_profile", None) or "regulated",
        deployment.runtime.profile or "regulated",
    ]
    ceiling = max(
        execution_profile_contract(profile).max_remote_decision_tier
        for profile in profiles
    )
    try:
        return effective_remote_tier(
            deployment.human_decisions.remote.context_tier, ceiling
        )
    except ValueError as exc:
        raise AttendedActionRefused(str(exc)) from exc


def _task_and_presentation(
    run_dir: Path,
    item: AttentionItem,
    *,
    deployment: Optional[DeploymentConfig] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], dict[str, Any]]:
    report, _ = data._load_report(run_dir)
    failed, _ = _last_failed_result(report)
    question = _TASK_COPY.get(item.category, _DEFAULT_TASK_COPY)
    capability = None
    try:
        capability = AttendedActionStore(run_dir).read()
    except AttendedActionRefused:
        pass

    (
        identity_required,
        identity_confirmed,
        effect_required,
        effect_confirmed,
    ) = _coverage(failed)
    observed_tiers = [
        evidence.verification_tier
        for evidence in (failed.effect_evidence if failed is not None else [])
        if evidence.verification_tier is not None
    ]
    presentation: dict[str, Any] = {
        "question": question[2],
        "explanation": item.headline,
        "next_action": item.next_action,
        "assurance": (
            "Your answer does not mark the run verified. OpenAdapt re-checks "
            "the live state and required effects before it can continue."
        ),
        "before_artifact_id": item.before_artifact_id,
        "after_artifact_id": item.after_artifact_id,
    }
    # A decision task is a projection of an OPEN pause. The signed capability
    # file survives a completed resume, so a run that already continued would
    # otherwise keep offering an answerable question the engine must refuse.
    if capability is None or not item.durably_paused:
        return None, None, presentation

    task_kind, question_template, _ = question
    # "May have been sent" must never collapse into "Not sent" or "Sent". The
    # capability records what the engine knew when it paused; a later attended
    # request for this same pause can have crossed the delivery boundary
    # without returning a terminal receipt. The pause-wide journal is the
    # authority on that, so re-read it every time the task is projected.
    delivery_state = capability.delivery_state
    if (
        AttendedActionStore(run_dir).unresolved_delivery(capability.pause_id)
        is not None
    ):
        delivery_state = "unknown"
    if delivery_state == "unknown":
        task_kind = "delivery_uncertain"
        question_template = "review_uncertain_delivery"
        presentation["question"] = (
            "Have you completed the required human step so OpenAdapt can "
            "reconcile the live state before any retry?"
            if item.human_required
            else "Is the live destination ready for OpenAdapt to reconcile "
            "the uncertain action before any retry?"
        )
    surface = report.execution_target_kind if report is not None else None
    substrate = _SUBSTRATE_MAP.get(surface, "unknown") if surface else "unknown"
    # LOCAL-ONLY. `presentation` never crosses the Cloud lane: the remote
    # projection below returns a `RemoteDecisionProjection` built from `task`
    # alone and discards this block. Keeping the enrichment here rather than in
    # the signed task is what lets the phone say what broke without widening a
    # contract whose `safe_slots` are null by design.
    presentation["halt"] = halt_detail_mod.halt_detail(
        run_dir,
        category=item.category,
        report=report,
        failed=failed,
        substrate=substrate,
        delivery_state=delivery_state,
        identity_required=identity_required,
    )
    remote, tenant_id, runner_id = _remote_scope(deployment)
    unsigned: dict[str, Any] = {
        "schema_version": HUMAN_DECISION_TASK_SCHEMA,
        "task_id": f"task_{capability.pause_id}",
        "task_revision": 1,
        "tenant_id": tenant_id,
        "runner_id": runner_id,
        "run_id": _opaque_id("run", capability.run_id),
        "pause_id": capability.pause_id,
        "capability_digest": capability.digest,
        "bundle_digest": capability.bundle_version,
        "task_kind": task_kind,
        "delivery_state": delivery_state,
        "risk_class": _risk_class(failed, effect_required),
        "substrate": substrate,
        "question": {
            "template": question_template,
            "safe_slots": {
                "candidate_count": None,
                "required_signal_count": (
                    identity_required
                    if task_kind == "identity"
                    else effect_required
                    if task_kind == "effect"
                    else None
                ),
                "confirmed_signal_count": (
                    identity_confirmed
                    if task_kind == "identity"
                    else effect_confirmed
                    if task_kind == "effect"
                    else None
                ),
            },
        },
        "evidence": {
            "identity_required_count": identity_required,
            "identity_confirmed_count": identity_confirmed,
            "effect_required_count": effect_required,
            "effect_confirmed_count": effect_confirmed,
            "minimum_effect_tier": (
                report.governed_minimum_effect_tier if report is not None else None
            ),
            "observed_effect_tier": min(observed_tiers) if observed_tiers else None,
            "frame_available_locally": bool(
                item.before_artifact_id or item.after_artifact_id
            ),
            "sensitive_evidence_local_only": True,
        },
        # Relayed verbatim from the sealed capability, including `reject`, even
        # when the journal above has since made delivery uncertain. Filtering
        # it here was tried and reverted: `allowed_actions` is inside the
        # signed payload, so withdrawing one action mid-flight changes the task
        # digest, and every request the phone already holds then fails as "the
        # task changed" instead of with the specific refusal that tells the
        # operator a write may have landed. `execute_attended_action` owns that
        # refusal, exactly as it already does for continue and skip, and its
        # message names the correct next step.
        "allowed_actions": [
            _ACTION_MAP[action] for action in capability.allowed_actions
        ],
        "required_authn": "aal2" if remote else "local_session",
        "created_at": capability.issued_at,
        "expires_at": capability.expires_at,
        "nonce": capability.pause_id,
        "issuer_key_id": (
            "customer_runner_attended_v1" if remote else "local_attended_v1"
        ),
        "signature_algorithm": "hmac-sha256",
    }
    task = AttendedActionStore(run_dir).seal_human_decision_task(unsigned)
    task_digest = HumanDecisionTaskV1.model_validate(task).digest
    return task, task_digest, presentation


def decision_detail(run_dir: Path, item: AttentionItem) -> dict[str, Any]:
    task, task_digest, presentation = _task_and_presentation(run_dir, item)
    return {
        "item": item.model_dump(mode="json"),
        "task": task,
        "task_digest": task_digest,
        "presentation": presentation,
    }


def _sha256(payload: dict[str, Any]) -> str:
    """Digest one payload under the normative cross-language canonical form.

    ``ensure_ascii=True`` matches ``openadapt-types``' canonicalization. The
    remote-projection digests this also feeds are computed over
    pattern-constrained ASCII values only, so aligning cannot move them (see
    ``test_remote_binding_digests_are_ascii_canonicalization_invariant``); the
    receipt's ``decision_digest`` covers the engine's free-text message, which
    can be non-ASCII, so only that digest is made reproducible by the change.
    """
    import json

    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def portable_remote_decision_task(
    run_dir: Path,
    item: AttentionItem,
    *,
    deployment: DeploymentConfig,
) -> RemoteDecisionProjection:
    """Project one exact pause for an authenticated remote AAL2 surface.

    No screenshot, OCR text, free text, workflow label, parameter, or path is
    returned. Identifiers alone never activate this path: the deployment must
    explicitly enable remote issuance and bind both tenant and runner.

    At ``remote_closed_context`` the projection additionally carries
    :class:`~openadapt_flow.console.decision_context.RemoteHaltContextV1`. That
    object has no string-valued field, so this docstring's first sentence stays
    literally true: the context widens what a remote operator KNOWS without
    widening what the envelope can REPRESENT. The signed
    :class:`~openadapt_types.HumanDecisionTaskV1` is unchanged, byte for byte,
    at every tier.
    """
    remote, tenant_id, runner_id = _remote_scope(deployment)
    if not remote or tenant_id is None or runner_id is None:
        raise AttendedActionRefused(
            "remote decision issuance is not explicitly enabled for this deployment"
        )
    task_raw, task_digest, presentation = _task_and_presentation(
        run_dir, item, deployment=deployment
    )
    if task_raw is None or task_digest is None:
        raise AttendedActionRefused(
            "the run has no current signed human decision task or pause capability"
        )
    task = HumanDecisionTaskV1.model_validate(task_raw)
    # The rest of `presentation` -- the screenshot artifact ids, the composed
    # question, the gated control label inside `halt` -- is still discarded.
    # Only the closed-vocabulary re-projection of `halt` may cross, and only at
    # the tier that permits it.
    report, _ = data._load_report(run_dir)
    tier = _remote_delivery_tier(deployment, report)
    halt_context = (
        remote_halt_context(presentation.get("halt"))
        if tier is DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT
        else None
    )
    # A tier that promised context and produced none is reported as the tier it
    # actually delivered. The projection never claims a fidelity it did not
    # reach; that is the same rule the effect ladder applies to evidence.
    delivered_tier = (
        DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT
        if halt_context is not None
        else DecisionDeliveryTier.REMOTE_IDENTIFIERS
    )
    capability = AttendedActionStore(run_dir).read()
    idempotency_scope = _sha256(
        {
            "schema_version": "openadapt.remote-decision-idempotency/v1",
            "tenant_id": tenant_id,
            "runner_id": runner_id,
            "task_id": task.task_id,
            "task_revision": task.task_revision,
            "pause_id": task.pause_id,
            "capability_digest": task.capability_digest,
        }
    )
    binding = {
        "schema_version": "openadapt.remote-decision-projection/v1",
        "task_digest": task_digest,
        "tenant_id": tenant_id,
        "runner_id": runner_id,
        "phase": "paused",
        "event_sequence": capability.event_sequence,
        "capability_digest": task.capability_digest,
        "allowed_actions": [str(action.value) for action in task.allowed_actions],
        "expires_at": task.expires_at,
        "expected_transition_digest": capability.expected_transition_digest,
        "idempotency_scope_digest": idempotency_scope,
    }
    # `delivery_tier` and `halt_context` are deliberately NOT in the binding.
    #
    # The binding is execution AUTHORITY, and `admit_remote_action` re-derives
    # it on every response, including a replayed one after the pause already
    # resumed. It must therefore be a deterministic function of the signed
    # capability. The halt context is not: it is read from the run's live
    # checkpoint, which legitimately empties once the pause closes, so binding
    # it would make a correct idempotent replay refuse.
    #
    # Nothing is lost. The context is PRESENTATION -- a hosted surface renders
    # it and is trusted to render it faithfully, exactly as it is trusted to
    # render the task it already receives. What protects the customer is
    # downstream and unchanged: any returned decision is re-bound to the exact
    # current pause capability, and the engine re-reads the live application and
    # re-proves every contract in `will_recheck` before anything continues.
    return RemoteDecisionProjection(
        task=task,
        task_digest=task_digest,
        event_sequence=capability.event_sequence,
        expected_transition_digest=capability.expected_transition_digest,
        idempotency_scope_digest=idempotency_scope,
        delivery_tier=delivered_tier.name.lower(),  # type: ignore[arg-type]
        halt_context=halt_context,
        binding_digest=_sha256(binding),
    )


def admit_remote_action(
    run_dir: Path,
    item: AttentionItem,
    request: RemoteAttendedActionRequest,
    *,
    deployment: DeploymentConfig,
    principal: RemoteDecisionPrincipal,
) -> AttendedActionRequest:
    """Rebind one AAL2 response to the current engine-owned pause capability."""
    projection = portable_remote_decision_task(run_dir, item, deployment=deployment)
    task = projection.task
    if (
        principal.assurance != "aal2"
        or request.tenant_id != principal.tenant_id
        or request.runner_id != principal.runner_id
        or task.tenant_id != principal.tenant_id
        or task.runner_id != principal.runner_id
    ):
        raise AttendedActionRefused(
            "the authenticated remote principal does not match the issued task scope"
        )
    if (
        request.task_digest != projection.task_digest
        or request.task_signature != task.signature
        or request.capability_digest != task.capability_digest
        or request.phase != projection.phase
        or request.event_sequence != projection.event_sequence
        or request.idempotency_scope_digest != projection.idempotency_scope_digest
        or request.binding_digest != projection.binding_digest
    ):
        raise AttendedActionRefused(
            "the remote decision does not match the exact current pause projection"
        )
    if not AttendedActionStore(run_dir).verify_human_decision_task(
        task.model_dump(mode="json")
    ):
        raise AttendedActionRefused(
            "the remote human decision task signature could not be verified"
        )
    portable_action = _ACTION_MAP.get(request.action)
    allowed = {
        action.value if hasattr(action, "value") else str(action)
        for action in task.allowed_actions
    }
    if portable_action not in allowed:
        raise AttendedActionRefused(
            "the requested remote action is not allowed for the current pause"
        )
    return request.engine_request()


def execute_remote_attended_action(
    run_dir: Path,
    item: AttentionItem,
    request: RemoteAttendedActionRequest,
    *,
    deployment: DeploymentConfig,
    principal: RemoteDecisionPrincipal,
    executor: Optional[AttendedActionExecutor] = None,
    relay_binding: Optional[AttendedRelayBinding] = None,
    key: Optional[str] = None,
) -> AttendedDecision:
    """Provider-neutral AAL2 admission followed by normal governed execution.

    The returned projection and authenticated principal are presentation and
    authentication evidence. They never replace the engine pause capability.
    Continue/Skip therefore traverse the same exact-capability, single-flight,
    fresh-live-revalidation, and deterministic-resume path as a local decision.
    """
    engine_request = admit_remote_action(
        run_dir,
        item,
        request,
        deployment=deployment,
        principal=principal,
    )
    # The AAL2 interactive route attributes a human decider. This is route
    # provenance, not biometric or physical-presence evidence.
    return execute_attended_action(
        run_dir,
        engine_request,
        operator=principal.subject,
        decided_by="human",
        executor=executor,
        relay_binding=relay_binding,
        key=key,
    )


def admit_console_action(
    run_dir: Path,
    item: AttentionItem,
    request: ConsoleAttendedActionRequest,
) -> AttendedActionRequest:
    """Bind a browser decision to the exact current signed task projection."""
    task, task_digest, _ = _task_and_presentation(run_dir, item)
    if task is None or task_digest is None:
        raise AttendedActionRefused(
            "the run has no current signed human decision task or pause capability"
        )
    if (
        request.task_digest != task_digest
        or request.task_signature != task["signature"]
    ):
        raise AttendedActionRefused(
            "the human decision task changed; reload the current pause"
        )
    if not AttendedActionStore(run_dir).verify_human_decision_task(task):
        raise AttendedActionRefused(
            "the human decision task signature could not be verified"
        )
    if request.capability_digest != task["capability_digest"]:
        raise AttendedActionRefused(
            "the human decision task does not match the pause capability"
        )
    portable_action = _ACTION_MAP.get(request.action)
    if portable_action not in task["allowed_actions"]:
        raise AttendedActionRefused(
            "the requested action is not allowed for the current pause"
        )
    return request.engine_request()
