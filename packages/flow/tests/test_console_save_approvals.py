"""Save approvals on the decision portal. Made-up runs and images.

Invariants under test:

* the portal shows a waiting save as a signed, PHI-free decision task, plus
  the exact screenshot the request was opened with;
* an answer binds the task the phone was shown: a changed or forged task is
  refused, and the answer is single-use (a second tap is refused);
* only a person answers here (``decided_by = human`` on the
  ``decision_portal`` route), and a decline needs a closed reason;
* answering is offered only by the attended console the portal relays to.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openadapt_flow.approval_mode.item_approval import (
    ItemApprovalStore,
    decided_by_summary,
)
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.console import save_approvals
from openadapt_flow.console.app import create_app
from openadapt_flow.contracts.human_decision import HumanDecisionTaskV1

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
FORM = ("dom", "sha256:" + "1" * 64)


def _open(root: Path, run: str = "run-a", **kwargs):
    store = ItemApprovalStore(root / run)
    store.write_initiator(None)
    request = store.open_request(
        run_id=run,
        workflow_name="made-up-intake",
        step_id="made-up-save-step",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest=None,
        frame_png=FRAME,
        form_state=FORM,
        **kwargs,
    )
    return store, request


def _answer(root: Path, request_id: str, action="verify_and_resume", **extra):
    detail = save_approvals.save_approval_detail(root, request_id)
    payload = save_approvals.SaveApprovalActionRequest(
        action=action,
        task_digest=detail["task_digest"],
        task_signature=detail["task"]["signature"],
        **extra,
    )
    return save_approvals.decide_save_approval(root, request_id, payload)


def test_a_waiting_save_is_a_signed_phi_free_task(tmp_path):
    store, request = _open(tmp_path)
    [row] = save_approvals.list_save_approvals(tmp_path)
    assert row.request_id == request.request_id
    detail = save_approvals.save_approval_detail(tmp_path, request.request_id)
    task = HumanDecisionTaskV1.model_validate(detail["task"])
    assert task.task_kind.value == "save_approval"
    assert task.question.template.value == "approve_save"
    assert task.capability_digest == request.digest()
    assert [a.value for a in task.allowed_actions] == ["verify_and_resume", "reject"]
    assert task.required_authn.value == "local_session"
    assert task.evidence.sensitive_evidence_local_only is True
    # Nothing a person typed, and no run or workflow label, is in the task.
    wire = json.dumps(detail["task"])
    for text in ("made-up-intake", "Click", "run-a", "made-up-save-step"):
        assert text not in wire
    # The same request always yields the same signed task.
    again = save_approvals.save_approval_detail(tmp_path, request.request_id)
    assert again["task"] == detail["task"]
    # The screenshot is exactly the one the request was opened with.
    frame = save_approvals.save_approval_frame(tmp_path, request.request_id)
    assert hashlib.sha256(frame).hexdigest() == request.screenshot_sha256
    assert detail["presentation"]["step_intent"] == "Click 'Save'"


def test_an_approval_is_recorded_once_as_a_person_on_the_portal(tmp_path):
    store, request = _open(tmp_path)
    receipt = _answer(tmp_path, request.request_id)
    assert receipt.state.value == "accepted_pending_runner"
    assert receipt.report_success is False
    assert store.status(request) == "approved"
    answer = store.read_answer(request.request_id)
    assert answer.decided_by == "human" and answer.route == "decision_portal"
    assert answer.approver_kind == "unauthenticated"
    assert decided_by_summary(store, request)["route"] == "decision_portal"
    # Single use: the save is no longer waiting, so a second tap is refused.
    assert save_approvals.list_save_approvals(tmp_path) == []
    with pytest.raises(save_approvals.SaveApprovalRefused) as err:
        _answer(tmp_path, request.request_id)
    assert err.value.code == "request_not_waiting"


def test_a_decline_needs_one_closed_reason(tmp_path):
    store, request = _open(tmp_path)
    with pytest.raises(save_approvals.SaveApprovalRefused) as err:
        _answer(tmp_path, request.request_id, action="reject")
    assert err.value.code == "decline_reason_required"
    with pytest.raises(save_approvals.SaveApprovalRefused):
        _answer(tmp_path, request.request_id, decline_reason="wrong_values")
    receipt = _answer(
        tmp_path, request.request_id, action="reject", decline_reason="wrong_record"
    )
    assert receipt.state.value == "rejected"
    assert store.read_answer(request.request_id).decline_reason == "wrong_record"


def test_an_answer_for_another_task_is_refused(tmp_path):
    _, first = _open(tmp_path, "run-a")
    _, second = _open(tmp_path, "run-b")
    detail = save_approvals.save_approval_detail(tmp_path, first.request_id)
    payload = save_approvals.SaveApprovalActionRequest(
        action="verify_and_resume",
        task_digest=detail["task_digest"],
        task_signature=detail["task"]["signature"],
    )
    with pytest.raises(save_approvals.SaveApprovalRefused) as err:
        save_approvals.decide_save_approval(tmp_path, second.request_id, payload)
    assert err.value.code == "task_changed"
    forged = payload.model_copy(update={"task_signature": "hmac-sha256:" + "0" * 64})
    with pytest.raises(save_approvals.SaveApprovalRefused):
        save_approvals.decide_save_approval(tmp_path, first.request_id, forged)


def test_an_expired_save_is_not_offered(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    _open(tmp_path, now=old, ttl_s=60)
    assert save_approvals.list_save_approvals(tmp_path) == []


def test_a_swapped_screenshot_is_refused(tmp_path):
    store, request = _open(tmp_path)
    (store.run_dir / request.screenshot).chmod(0o600)
    (store.run_dir / request.screenshot).write_bytes(b"\x89PNG other")
    with pytest.raises(save_approvals.SaveApprovalRefused) as err:
        save_approvals.save_approval_frame(tmp_path, request.request_id)
    assert err.value.code == "frame_digest_mismatch"


def _client(root: Path, *, attend: bool) -> TestClient:
    bundles = root / "bundles"
    bundles.mkdir(exist_ok=True)
    app = create_app(bundles, root, attend=attend)
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )


def test_console_routes_relay_the_task_and_take_one_answer(tmp_path):
    _, request = _open(tmp_path)
    client = _client(tmp_path, attend=True)
    [row] = client.get("/api/save-approvals").json()
    detail = client.get(f"/api/save-approvals/{row['request_id']}").json()
    frame = client.get(detail["presentation"]["screenshot_route"])
    assert frame.status_code == 200 and frame.content == FRAME
    assert frame.headers["cache-control"] == "no-store"
    body = {
        "action": "verify_and_resume",
        "task_digest": detail["task_digest"],
        "task_signature": detail["task"]["signature"],
    }
    url = f"/api/save-approvals/{request.request_id}/actions/verify_and_resume"
    assert client.post(url, json={**body, "action": "reject"}).status_code == 400
    first = client.post(url, json=body)
    assert first.status_code == 200
    assert first.json()["state"] == "accepted_pending_runner"
    assert client.post(url, json=body).status_code == 409
    assert client.get(f"/api/save-approvals/{request.request_id}").status_code == 404


def test_only_the_attended_console_takes_answers(tmp_path):
    _, request = _open(tmp_path)
    client = _client(tmp_path, attend=False)
    detail = client.get(f"/api/save-approvals/{request.request_id}").json()
    response = client.post(
        f"/api/save-approvals/{request.request_id}/actions/verify_and_resume",
        json={
            "action": "verify_and_resume",
            "task_digest": detail["task_digest"],
            "task_signature": detail["task"]["signature"],
        },
    )
    assert response.status_code == 404
    assert client.get("/api/save-approvals/not-an-id").status_code == 404
