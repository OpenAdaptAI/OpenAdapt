"""The values a save writes, on the phone. Made-up runs and values.

Invariants under test:

* ``presentation.fields`` carries exactly the values the run recorded, and is
  absent when it recorded none (the phone then shows the screenshot);
* the fields are inside the request digest, so the signed task binds them:
  a value changed after the phone was shown the task makes the answer stale;
* a request without fields keeps the digest it had before fields existed;
* the detail is served ``no-store``;
* the values never reach the signed task, a notification, the delegated
  approver's facts, or the run report row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openadapt_flow.approval_mode.human_attestation import portal_signer
from openadapt_flow.approval_mode.item_approval import (
    MAX_SAVE_FIELDS,
    ItemApprovalRequest,
    ItemApprovalStore,
    SaveField,
    make_answer,
    request_facts,
)
from openadapt_flow.approval_mode.model import DECISION_PORTAL_ROUTE, ApprovalMode
from openadapt_flow.console import save_approvals
from openadapt_flow.console.app import create_app

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"
FORM = ("dom", "sha256:" + "1" * 64)
VALUE = "Made-up Cardiology Ward 7"
FIELDS = [
    SaveField(label="Refer to", after=VALUE),
    SaveField(label="Priority", before="Routine", after="Urgent"),
]


def _open(root: Path, fields=None, run: str = "run-a"):
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
        fields=fields,
    )
    return store, request


def _client(root: Path) -> TestClient:
    bundles = root / "bundles"
    bundles.mkdir(exist_ok=True)
    app = create_app(bundles, root, attend=True)
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )


def test_the_phone_gets_the_recorded_values(tmp_path):
    _, request = _open(tmp_path, FIELDS)
    detail = save_approvals.save_approval_detail(tmp_path, request.request_id)
    assert detail["presentation"]["fields"] == [
        {"label": "Refer to", "before": None, "after": VALUE},
        {"label": "Priority", "before": "Routine", "after": "Urgent"},
    ]


def test_no_recorded_values_means_no_fields_key(tmp_path):
    for fields in (None, []):
        root = tmp_path / ("none" if fields is None else "empty")
        _, request = _open(root, fields)
        detail = save_approvals.save_approval_detail(root, request.request_id)
        assert "fields" not in detail["presentation"]
        assert request.fields is None


def test_the_fields_are_inside_the_request_digest(tmp_path):
    _, with_fields = _open(tmp_path, FIELDS)
    other = with_fields.model_copy(
        update={"fields": (SaveField(label="Refer to", after="Made-up Oncology"),)}
    )
    assert other.digest() != with_fields.digest()
    detail = save_approvals.save_approval_detail(tmp_path, with_fields.request_id)
    # The signed task names the request digest, so it binds the values too.
    assert detail["task"]["capability_digest"] == with_fields.digest()


def test_a_value_changed_after_the_phone_saw_it_makes_the_answer_stale(
    tmp_path, approver
):
    store, request = _open(tmp_path, FIELDS)
    shown = save_approvals.save_approval_detail(tmp_path, request.request_id)
    path = store.dir / f"request-{request.request_id}.json"
    raw = json.loads(path.read_text())
    raw["fields"][0]["after"] = "Made-up Oncology"
    path.chmod(0o600)
    path.write_text(json.dumps(raw))
    payload = save_approvals.SaveApprovalActionRequest(
        action="verify_and_resume",
        task_digest=shown["task_digest"],
        task_signature=shown["task"]["signature"],
    )
    with pytest.raises(save_approvals.SaveApprovalRefused) as err:
        save_approvals.decide_save_approval(tmp_path, request.request_id, payload)
    assert err.value.code == "task_changed"


def test_a_request_without_fields_keeps_its_earlier_digest(tmp_path):
    _, request = _open(tmp_path)
    dumped = request.model_dump(mode="json")
    assert "fields" not in dumped
    # Re-reading the file written before this field existed gives the same digest.
    assert ItemApprovalRequest.model_validate(dumped).digest() == request.digest()


def test_the_detail_is_served_no_store(tmp_path):
    _, request = _open(tmp_path, FIELDS)
    response = _client(tmp_path).get(f"/api/save-approvals/{request.request_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["presentation"]["fields"][0]["after"] == VALUE


def test_the_values_stay_out_of_everything_else(tmp_path, approver):
    store, request = _open(tmp_path, FIELDS)
    client = _client(tmp_path)
    detail = client.get(f"/api/save-approvals/{request.request_id}").json()
    # The signed task carries a digest, never the values.
    assert VALUE not in json.dumps(detail["task"])
    # Notifications are counts.
    assert VALUE not in client.get("/api/attention/notification").text
    assert VALUE not in client.get("/api/save-approvals").text
    assert save_approvals.notification_count(tmp_path) == 1
    # A delegated approver sees closed facts only.
    assert VALUE not in json.dumps(request_facts(request))
    # The run report row is digests and closed codes.
    answer = make_answer(
        request,
        decision="approve",
        decided_by="human",
        route=DECISION_PORTAL_ROUTE,
        approver_id=None,
        auth_method="none",
        decline_reason=None,
        policy=store.context(),
        signer=portal_signer(store.key_directory(), root=store.approver_root),
    )
    assert VALUE not in json.dumps(answer.reference(request).model_dump(mode="json"))
    assert VALUE not in json.dumps(answer.model_dump(mode="json"))


def test_the_field_list_is_closed_and_bounded(tmp_path):
    with pytest.raises(ValidationError):
        SaveField(label="Refer to", after="x", note="free text")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SaveField(label="", after="x")
    with pytest.raises(ValidationError):
        SaveField(label="Reason", after="x" * 513)
    too_many = [SaveField(label=f"F{i}", after="x") for i in range(MAX_SAVE_FIELDS + 1)]
    with pytest.raises(ValidationError):
        _open(tmp_path, too_many)
