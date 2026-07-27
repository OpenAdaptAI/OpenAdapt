"""Model-free, local projections for attended human decisions.

The portable task contains only opaque identifiers, closed enums, counts,
digests, and expiry. Protected screenshots remain separate authenticated local
artifacts. A signed task is presentation integrity, not execution authority;
the runtime's exact pause capability and fresh revalidation remain mandatory.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from openadapt_types import HUMAN_DECISION_TASK_SCHEMA, HumanDecisionTaskV1
from pydantic import ConfigDict, Field

from openadapt_flow.console import data
from openadapt_flow.console.attention import AttentionItem, _last_failed_result
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionRequest,
    AttendedActionStore,
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


class ConsoleAttendedActionRequest(AttendedActionRequest):
    """Browser request bound to both current task and engine capability."""

    model_config = ConfigDict(extra="forbid")

    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")

    def engine_request(self) -> AttendedActionRequest:
        return AttendedActionRequest.model_validate(
            self.model_dump(exclude={"task_digest", "task_signature"})
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


def _task_and_presentation(
    run_dir: Path,
    item: AttentionItem,
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
    presentation = {
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
    if capability is None:
        return None, None, presentation

    task_kind, question_template, _ = question
    if capability.delivery_state == "unknown":
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
    unsigned: dict[str, Any] = {
        "schema_version": HUMAN_DECISION_TASK_SCHEMA,
        "task_id": f"task_{capability.pause_id}",
        "task_revision": 1,
        "tenant_id": None,
        "runner_id": None,
        "run_id": _opaque_id("run", capability.run_id),
        "pause_id": capability.pause_id,
        "capability_digest": capability.digest,
        "bundle_digest": capability.bundle_version,
        "task_kind": task_kind,
        "delivery_state": capability.delivery_state,
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
        "allowed_actions": [
            _ACTION_MAP[action] for action in capability.allowed_actions
        ],
        "required_authn": "local_session",
        "created_at": capability.issued_at,
        "expires_at": capability.expires_at,
        "nonce": capability.pause_id,
        "issuer_key_id": "local_attended_v1",
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
