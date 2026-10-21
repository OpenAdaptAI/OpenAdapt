"""An API step under an approval mode (docs/API_TIER.md, docs/APPROVAL_MODES.md).

The person approves the exact request the API step sends: the values, the
record the check reads back and the record check, bound by one digest to the
request bytes. Not a screenshot of a form the API step never fills.

- the run prepares the request (reads only), asks, and sends exactly that
  request once after a person's approval;
- a request that changes while the person looks is not sent, and the GUI
  steps don't run in its place;
- a decline sends nothing;
- a request that can't be prepared asks nobody: the GUI steps run, and their
  save asks the person as before.

The transport is a fake that writes to the MockMed fault server; the values
are made up.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse as up
from typing import Any, Optional

import pytest
import requests

from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.approval_mode.model import ItemDecisionKind
from openadapt_flow.compiler.api_proposal import admit_api_step
from openadapt_flow.ir import WebFormField, Workflow
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.actuators.web_form import WebFormActuator
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests._approver_keys import answer_as_person
from tests.test_api_tier_web_form import TARGET, EnterWritesBackend, _span_workflow
from tests.test_replayer import FakeVision

# Every person in these tests signs with a registered Desktop approver key.
pytestmark = pytest.mark.usefixtures("approver")

APPROVE: dict[str, Any] = dict(
    decision="approve",
    decided_by="human",
    route="desktop_run_it",
    approver_id=None,
    auth_method="none",
)
PARAMS = {"lname": "Marsh"}
FORM = """<html><body><form action='/save.php'>
<input type="hidden" name="csrf" value="{token}">
<input name="lname" value="">
</form></body></html>"""


class SessionTransport:
    """The live session: a form page, a duplicate search and the save."""

    base_url = "http://app"

    def __init__(self, sor_url: str) -> None:
        self.sor_url = sor_url
        self.token = "session-1"
        self.dup = "No matches were found."
        self.posts: list[tuple[str, str]] = []

    def get(self, path: str) -> Any:
        text = FORM.format(token=self.token) if path == "/new.php" else self.dup
        return _Resp(200, text)

    def post_form(self, path: str, body: str, *, referer: str) -> Any:
        self.posts.append((path, body))
        requests.post(
            f"{self.sor_url}/api/encounter",
            json={**TARGET, "note": "api"},
            timeout=5,
        )
        return _Resp(200, "saved")


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status, self.text, self.headers = status, text, {}


class Person:
    """Answers each waiting request; ``on_wait`` runs first, each poll."""

    def __init__(self, decisions: list[Optional[dict[str, Any]]]) -> None:
        self.decisions = list(decisions)
        self.clock = 0.0
        self.store: Optional[ItemApprovalStore] = None
        self.on_wait: Any = None
        self.seen: list[str] = []

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        if self.on_wait is not None:
            self.on_wait()
        assert self.store is not None
        for request in self.store.requests():
            if request.request_id in self.seen:
                continue
            self.seen.append(request.request_id)
            decision = self.decisions.pop(0) if self.decisions else None
            if decision is not None:
                self.store.write_answer(
                    answer_as_person(self.store, request, **decision)
                )


def _workflow() -> Workflow:
    """The span program, its save also sending the session's CSRF value."""

    wf = _span_workflow(admitted=False)
    save = wf.steps[-1]
    binding = save.api_binding
    spec = binding.web_form.model_copy(
        update={
            "fields": [
                WebFormField(name="csrf", source="session"),
                *binding.web_form.fields,
            ]
        }
    )
    binding = binding.model_copy(update={"web_form": spec})
    wf = wf.model_copy(
        update={
            "steps": [*wf.steps[:-1], save.model_copy(update={"api_binding": binding})]
        }
    )
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    return wf


def _run(tmp_path, person: Person, *, transport_setup=None):
    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        run_dir = tmp_path / "run"
        store = ItemApprovalStore(run_dir)
        person.store = store
        transport = SessionTransport(url)
        if transport_setup is not None:
            transport_setup(transport)
        backend = EnterWritesBackend(url)
        gate = ApprovalGate(
            "each_item",
            approval_store=store,
            approval_wait_s=30.0,
            approval_poll_s=1.0,
            sleep=person.sleep,
            monotonic=person.monotonic,
        )
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True, exist_ok=True)
        report = Replayer(
            backend,
            vision=FakeVision(),
            effect_verifier=RestRecordVerifier(url),
            web_form_actuator=WebFormActuator(transport),
            approval_gate=gate,
            poll_interval_s=0.01,
        ).run(_workflow(), bundle_dir=bundle, run_dir=run_dir, params=PARAMS)
        return report, backend, transport, store, db.snapshot()["records"]
    finally:
        stop()


def test_the_person_approves_the_exact_request_and_it_is_sent_once(tmp_path):
    report, backend, transport, store, records = _run(tmp_path, Person([APPROVE]))

    assert report.success is True, [r.error for r in report.results]
    assert backend.actions == []
    ((path, body),) = transport.posts
    assert path == "/save.php" and body == "csrf=session-1&lname=Marsh"
    folded, save = report.results
    assert folded.actuation == "api_folded" and save.actuation == "api"
    assert save.effect_verified is True
    assert [r["note"] for r in records] == ["api"]
    # A person approved the one save that was delivered. (This fixture runs
    # without an execution profile, so the outcome reads "Finished, not
    # checked" and the item still goes to a person; the Regulated profile
    # case is in the compatibility matrix.)
    assert [row.decision for row in report.approval.approvals] == ["approved"]
    assert report.approval.decision_reason != "save_not_approved"

    (request,) = store.requests()
    assert request.form_state_kind == "api_request"
    assert store.status(request) == "used"
    # The approved digest is the digest of the bytes that were sent.
    sent = {
        "method": "POST",
        "path": path,
        "content_type": "application/x-www-form-urlencoded",
        "body": body,
        "referer": "http://app/new.php",
    }
    canonical = json.dumps(sent, sort_keys=True, separators=(",", ":"))
    assert request.api_request.payload_sha256 == (
        "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )
    # The person reads the values, the record and the request, not a form.
    rows = {field.label: field.after for field in request.fields}
    assert rows["lname"] == "Marsh"
    assert rows["Record check: patient_id"] == TARGET["patient_id"]
    assert rows["Request"].startswith("POST /save.php")
    # The session value is in the digest, never in front of the person.
    assert "session-1" not in json.dumps(rows)
    assert (store.run_dir / request.screenshot).read_bytes()[:4] == b"\x89PNG"
    # Nothing about the request reaches the report.
    saved = (tmp_path / "run" / "report.json").read_text()
    assert request.api_request.payload_sha256 not in saved
    assert "Marsh" not in json.dumps(json.loads(saved)["approval"])


def test_a_request_that_changes_while_the_person_looks_is_not_sent(tmp_path):
    person = Person([APPROVE])
    holder: dict[str, SessionTransport] = {}

    def keep(transport: SessionTransport) -> None:
        holder["t"] = transport

    def the_session_value_changes() -> None:
        holder["t"].token = "session-2"

    person.on_wait = the_session_value_changes
    report, backend, transport, store, records = _run(
        tmp_path, person, transport_setup=keep
    )

    assert transport.posts == [] and records == []
    # Not redone through the GUI either: the person approved this request.
    assert backend.actions == []
    assert report.success is False
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    failed = [c for c in report.approval.pre_checks if not c.passed]
    assert failed[-1].detail == "form_changed_after_approval"


def test_a_failed_check_after_approval_is_not_sent(tmp_path):
    person = Person([APPROVE])
    holder: dict[str, SessionTransport] = {}

    def keep(transport: SessionTransport) -> None:
        holder["t"] = transport

    def a_duplicate_appears() -> None:
        holder["t"].dup = "1 match found"

    person.on_wait = a_duplicate_appears
    report, backend, transport, store, records = _run(
        tmp_path, person, transport_setup=keep
    )

    assert transport.posts == [] and records == [] and backend.actions == []
    assert report.approval.decision is ItemDecisionKind.STOP_BEFORE_SAVING
    failed = [c for c in report.approval.pre_checks if not c.passed]
    assert failed[-1].detail == "form_state_unreadable"


def test_a_decline_sends_nothing(tmp_path):
    decline = dict(APPROVE, decision="decline", decline_reason="wrong_values")
    report, backend, transport, store, records = _run(tmp_path, Person([decline]))

    assert transport.posts == [] and records == [] and backend.actions == []
    assert report.approval.decision is ItemDecisionKind.DECLINED


def test_a_request_that_cant_be_prepared_asks_nobody_and_runs_the_gui(tmp_path):
    def duplicate(transport: SessionTransport) -> None:
        transport.dup = "1 match found"

    # Nobody answers: the GUI save then waits for a person and stops.
    report, backend, transport, store, records = _run(
        tmp_path, Person([None]), transport_setup=duplicate
    )

    assert transport.posts == []
    first = report.results[0]
    assert "did not pass" in first.api_fallback
    assert ("press", "Tab") in [(a[0], a[1]) for a in backend.actions]
    # The one request the person saw is the GUI save's, not an API request.
    assert [r.form_state_kind for r in store.requests()] in ([], ["screen_text"])
    assert all(r.api_request is None for r in store.requests())


def test_operation_digest_binds_the_record_check():
    from openadapt_flow.runtime.actuators.web_form import (
        PreparedSave,
        operation_digest,
    )

    prepared = PreparedSave(
        method="POST",
        path="/save.php",
        body=(("lname", "Marsh"),),
        referer="http://app/new.php",
    )
    base = operation_digest(prepared, record_check=["h1"], record=[("id", "p1")])
    assert base != operation_digest(
        prepared, record_check=["h2"], record=[("id", "p1")]
    )
    assert base != operation_digest(
        prepared, record_check=["h1"], record=[("id", "p2")]
    )
    changed = PreparedSave(
        method="POST",
        path="/save.php",
        body=(("lname", "Marsh "),),
        referer="http://app/new.php",
    )
    assert base != operation_digest(changed, record_check=["h1"], record=[("id", "p1")])
    assert up.parse_qs(prepared.encoded()) == {"lname": ["Marsh"]}


def test_the_record_rows_are_listed_once_per_field(tmp_path):
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    wf = _workflow()
    save = wf.steps[-1]
    match = {"patient_id": ValueExpr(param="lname")}
    effects = [
        Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="note",
            value=ValueExpr(literal="Referred"),
        ),
    ]
    binding = save.api_binding.model_copy(update={"effects": effects})
    save = save.model_copy(update={"effects": effects, "api_binding": binding})
    replayer = Replayer(
        EnterWritesBackend("http://127.0.0.1:9"),
        vision=FakeVision(),
        web_form_actuator=WebFormActuator(SessionTransport("http://127.0.0.1:9")),
    )
    view = replayer._web_form_approval_view(save, PARAMS)
    record_rows = [row for row in view.rows if row[0].startswith("Record check:")]
    assert record_rows == [("Record check: patient_id", "Marsh")]
