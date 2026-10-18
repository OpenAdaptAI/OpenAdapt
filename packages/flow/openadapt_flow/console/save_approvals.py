"""Save approvals on the decision portal: the phone half of ``each_item``.

Under an approval mode a run stops before a save and writes an
:class:`~openadapt_flow.approval_mode.item_approval.ItemApprovalRequest`. This
module offers that request to the person through the same signed
:class:`~openadapt_flow.contracts.human_decision.HumanDecisionTaskV1` contract
the decision portal already renders for paused runs, so a paired phone can
approve or decline the save with one tap:

* :func:`list_save_approvals` finds every waiting request under the console's
  run root.
* :func:`save_approval_detail` returns the signed, PHI-free task plus a
  local-only ``presentation`` block (the step's own wording and the screenshot
  route). The screenshot is the exact image the request was opened with; the
  person sees what will be saved.
* :func:`decide_save_approval` admits one answer. The phone must echo the
  digest and signature of the task it was shown, so an answer binds the exact
  request (run, step, screenshot digest and form-state digest). The answer
  file is created exclusively, so a second tap, a replay, or a later answer
  from Desktop is refused. The waiting run re-reads the form before it saves
  and stops if anything changed; nothing here saves anything.

What this module never does
---------------------------

* It never sends anything off the runner. The task is the portal's local
  task (``required_authn = local_session``); the hosted lane does not carry
  save approvals, because Cloud never receives the screenshot and a person
  must see exactly what will be saved before approving it.
* It never lets an agent, a rule or a model answer: every answer here is
  ``decided_by = human`` on the ``decision_portal`` route, and the run checks
  that again itself.
* It never names a person it cannot prove. A paired phone proves a device,
  not who held it, so the answer is recorded as an unauthenticated person on
  the decision portal, exactly like a signed-out Desktop answer.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.approval_mode.approver import StepClass
from openadapt_flow.approval_mode.durable_carry import carry_answer_to_durable_pause
from openadapt_flow.approval_mode.item_approval import (
    APPROVAL_DIRNAME,
    ItemApprovalRefused,
    ItemApprovalRequest,
    ItemApprovalStore,
    make_answer,
)
from openadapt_flow.approval_mode.model import DECISION_PORTAL_ROUTE, DeclineReason
from openadapt_flow.console import data
from openadapt_flow.contracts.human_decision import (
    HUMAN_DECISION_TASK_SCHEMA,
    HumanDecisionAction,
    HumanDecisionReceiptReason,
    HumanDecisionReceiptState,
    HumanDecisionReceiptV1,
    HumanDecisionTaskV1,
    sign_human_decision_task_hmac,
)

#: The run-local HMAC key that signs this run's save-approval tasks. Created
#: once, 0600, inside the run's private ``item-approval`` folder.
TASK_KEY_FILENAME = "portal-task-key"
_TASK_KEY_BYTES = 32
_ISSUER_KEY_ID = "local_save_approval_v1"
_REQUEST_ID_LENGTH = 32

_RISK_CLASS: dict[StepClass, str] = {
    StepClass.READ_ONLY: "read_only",
    StepClass.REVERSIBLE: "state_changing",
    StepClass.CONSEQUENTIAL: "consequential",
    StepClass.IRREVERSIBLE: "irreversible",
    StepClass.CLINICAL_WRITE: "irreversible",
}


class SaveApprovalRefused(ValueError):
    """An answer the portal will not record. ``code`` is closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SaveApprovalActionRequest(BaseModel):
    """What the phone sends back: the task it saw and one decision."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["verify_and_resume", "reject"]
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    #: Required to decline, refused to approve. Same closed list as Desktop.
    decline_reason: Optional[DeclineReason] = None


class SaveApprovalSummary(BaseModel):
    """One waiting save in the portal's list. PHI-free."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    step_class: str
    created_at: str
    expires_at: str


# -- finding requests ----------------------------------------------------------


def _has_requests(path: Path) -> bool:
    folder = path / APPROVAL_DIRNAME
    return (
        not path.is_symlink()
        and path.is_dir()
        and folder.is_dir()
        and not folder.is_symlink()
    )


def _waiting(root: Path) -> list[tuple[Path, ItemApprovalRequest]]:
    found: list[tuple[Path, ItemApprovalRequest]] = []
    for run_dir in data._scan(Path(root), _has_requests):
        store = ItemApprovalStore(run_dir)
        for request in store.requests():
            if store.status(request) == "waiting":
                found.append((run_dir, request))
    found.sort(key=lambda pair: (pair[1].created_at, pair[1].request_id))
    return found


def list_save_approvals(root: Path) -> list[SaveApprovalSummary]:
    """Every save waiting for a person under ``root``, oldest first."""

    return [
        SaveApprovalSummary(
            request_id=request.request_id,
            step_class=request.step_class.value,
            created_at=request.created_at,
            expires_at=request.expires_at,
        )
        for _, request in _waiting(root)
    ]


def find_waiting(root: Path, request_id: str) -> tuple[Path, ItemApprovalRequest]:
    """The waiting request with this id. Raises :class:`SaveApprovalRefused`."""

    if (
        not isinstance(request_id, str)
        or len(request_id) != _REQUEST_ID_LENGTH
        or any(char not in "0123456789abcdef" for char in request_id)
    ):
        raise SaveApprovalRefused("request_unknown", "no such save approval")
    for run_dir, request in _waiting(root):
        if request.request_id == request_id:
            return run_dir, request
    raise SaveApprovalRefused(
        "request_not_waiting",
        "this save is no longer waiting; it was answered, used or expired",
    )


# -- the signed task -----------------------------------------------------------


def _task_key(store: ItemApprovalStore) -> bytes:
    """This run's portal task key, created once and never rewritten."""

    path = store.dir / TASK_KEY_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(_TASK_KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, read_flags)
    except OSError as exc:
        raise SaveApprovalRefused(
            "task_key_unreadable", "the portal task key is unreadable"
        ) from exc
    with os.fdopen(descriptor, "rb") as handle:
        key = handle.read(_TASK_KEY_BYTES + 1)
    if len(key) != _TASK_KEY_BYTES:
        raise SaveApprovalRefused(
            "task_key_unreadable", "the portal task key is malformed"
        )
    return key


def _sha256_prefixed(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_approval_task(
    run_dir: Path, request: ItemApprovalRequest
) -> HumanDecisionTaskV1:
    """The signed, PHI-free decision task for one waiting save.

    Every field is a closed code or an opaque digest. ``capability_digest`` is
    the request's own digest, so the task names exactly the run, step,
    screenshot and form state the person is asked about.
    """

    store = ItemApprovalStore(run_dir)
    bundle = request.bundle_content_digest
    fields: dict[str, Any] = {
        "schema_version": HUMAN_DECISION_TASK_SCHEMA,
        "task_id": f"task_save_{request.request_id}",
        "task_revision": 1,
        "tenant_id": None,
        "runner_id": None,
        "run_id": _sha256_prefixed(request.run_id).replace("sha256:", "run_"),
        "pause_id": f"save_{request.request_id}",
        "capability_digest": request.digest(),
        "bundle_digest": (
            f"sha256:{bundle}"
            if bundle is not None
            else _sha256_prefixed(f"unbound:{request.workflow_name}")
        ),
        "task_kind": "save_approval",
        "delivery_state": "not_delivered",
        "risk_class": _RISK_CLASS.get(request.step_class, "unknown"),
        "substrate": "unknown",
        "question": {"template": "approve_save", "safe_slots": {}},
        "evidence": {
            "frame_available_locally": True,
            "sensitive_evidence_local_only": True,
        },
        "allowed_actions": ["verify_and_resume", "reject"],
        "required_authn": "local_session",
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "nonce": f"save_nonce_{request.request_id}",
        "issuer_key_id": _ISSUER_KEY_ID,
        "signature_algorithm": "hmac-sha256",
    }
    return sign_human_decision_task_hmac(key=_task_key(store), fields=fields)


def save_approval_detail(root: Path, request_id: str) -> dict[str, Any]:
    """The portal's view of one waiting save: signed task + local presentation.

    ``presentation`` stays on this runner. It holds the step's own wording and
    the route of the exact screenshot the request was opened with.
    """

    run_dir, request = find_waiting(root, request_id)
    task = save_approval_task(run_dir, request)
    return {
        "task": task.model_dump(mode="json"),
        "task_digest": task.digest,
        "presentation": {
            "question": "Save this?",
            "step_intent": request.step_intent,
            "step_index": request.step_index,
            "step_class": request.step_class.value,
            "explanation": (
                "This is exactly what OpenAdapt will save. Before it saves, "
                "it reads the form again; if anything changed, it stops and "
                "asks again."
            ),
            "screenshot_route": f"/api/save-approvals/{request.request_id}/frame",
            "screenshot_sha256": request.screenshot_sha256,
            "decline_reasons": ["wrong_values", "wrong_record", "screen_problem"],
            "expires_at": request.expires_at,
        },
    }


def save_approval_frame(root: Path, request_id: str) -> bytes:
    """The exact screenshot of the waiting save, checked against its digest."""

    run_dir, request = find_waiting(root, request_id)
    try:
        return ItemApprovalStore(run_dir).read_frame(request)
    except ItemApprovalRefused as exc:
        raise SaveApprovalRefused(exc.code, str(exc)) from exc


# -- answering -----------------------------------------------------------------


def decide_save_approval(
    root: Path,
    request_id: str,
    payload: SaveApprovalActionRequest,
) -> HumanDecisionReceiptV1:
    """Record one person's answer from the portal. Single use.

    Raises :class:`SaveApprovalRefused` when the save is no longer waiting,
    the task the phone echoes isn't the current one, or the answer itself is
    refused by the run's own checks.
    """

    run_dir, request = find_waiting(root, request_id)
    task = save_approval_task(run_dir, request)
    if not (
        hmac.compare_digest(payload.task_digest, task.digest)
        and hmac.compare_digest(payload.task_signature, task.signature)
    ):
        raise SaveApprovalRefused(
            "task_changed", "this save changed since it was shown; reload it"
        )
    approve = payload.action == "verify_and_resume"
    if approve == (payload.decline_reason is not None):
        raise SaveApprovalRefused(
            "decline_reason_required" if not approve else "decline_reason_not_allowed",
            "a decline needs exactly one reason; an approval takes none",
        )
    store = ItemApprovalStore(run_dir)
    try:
        answer = make_answer(
            request,
            decision="approve" if approve else "decline",
            decided_by="human",
            route=DECISION_PORTAL_ROUTE,
            approver_id=None,
            auth_method="none",
            decline_reason=payload.decline_reason,
            policy=store.context(),
        )
        store.write_answer(answer)
    except ItemApprovalRefused as exc:
        raise SaveApprovalRefused(exc.code, str(exc)) from exc
    # A run that stopped waiting left a durable pause; the answer has to reach
    # it, or an approval waits for a resume that never sees it and a decline
    # leaves the pause open.
    carry_answer_to_durable_pause(
        run_dir, request, answer, approver=f"{DECISION_PORTAL_ROUTE}:person"
    )
    request_digest = (
        "sha256:"
        + hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    )
    return HumanDecisionReceiptV1(
        task_id=task.task_id,
        pause_id=task.pause_id,
        capability_digest=task.capability_digest,
        request_digest=request_digest,
        decision_digest=answer.digest(),
        action=HumanDecisionAction(payload.action),
        # An approval is not a save: the waiting run re-reads the form and
        # saves only if nothing changed. A decline stops this save.
        state=(
            HumanDecisionReceiptState.ACCEPTED_PENDING_RUNNER
            if approve
            else HumanDecisionReceiptState.REJECTED
        ),
        reason_code=(
            HumanDecisionReceiptReason.PENDING_RUNNER
            if approve
            else HumanDecisionReceiptReason.REJECTED_BY_OPERATOR
        ),
        report_success=False,
        decided_at=answer.answered_at,
    )


def notification_count(root: Path) -> int:
    """How many saves are waiting, for the PHI-free OS notification."""

    return len(_waiting(root))


__all__ = [
    "TASK_KEY_FILENAME",
    "SaveApprovalActionRequest",
    "SaveApprovalRefused",
    "SaveApprovalSummary",
    "decide_save_approval",
    "find_waiting",
    "list_save_approvals",
    "notification_count",
    "save_approval_detail",
    "save_approval_frame",
    "save_approval_task",
]
