"""The commit guard decides each request of an agent it doesn't control.

The page here is a fake: when the guard lets a save go on, the fake sends it
to MockMed's fault server the way the agent's browser would, with the patient
that is on its screen. The guard reads the save back from ``GET /api/db``.
No browser, no model calls, nothing beyond localhost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.agent_guard.cdp import CdpGuardPage
from openadapt_flow.agent_guard.commit_guard import BLOCK_STATUS, CommitGuard
from openadapt_flow.agent_guard.config import ConfigError, parse_config
from openadapt_flow.agent_guard.requests import (
    HeldRequest,
    NotASave,
    classify,
    origin_of,
)
from openadapt_flow.guard import GuardedSave, GuardTask
from openadapt_flow.guard_demo import NOTE, FakeAgent, person_approves, verify_yaml
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify_run import parse_verify_config

APP = "http://127.0.0.1:8765"


def _req(
    method: str = "POST",
    path: str = "/api/encounter",
    *,
    body: Optional[Any] = None,
    content_type: str = "application/json",
    origin: str = APP,
    resource_type: str = "Fetch",
    rid: str = "r1",
) -> HeldRequest:
    return HeldRequest(
        request_id=rid,
        method=method,
        url=origin + path,
        resource_type=resource_type,
        post_data=json.dumps(body) if body is not None else None,
        content_type=content_type,
        network_id="n-" + rid,
    )


# -- which request is a save ---------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "kind"),
    [
        (_req("GET", "/api/db"), "read"),
        (_req("HEAD", "/"), "read"),
        (_req("OPTIONS", "/api/encounter"), "read"),
        (_req("POST", body={"patient_id": "p1", "note": "x"}), "save"),
        (_req("PUT"), "save"),
        (_req("PATCH"), "save"),
        (_req("DELETE", "/api/encounter/4"), "save"),
        (_req("PROPFIND"), "save"),
        (_req(body={"query": "query Q { patient(id: 1) { name } }"}), "read"),
        (_req(body={"query": "{ patient(id: 1) { name } }"}), "read"),
        (
            _req(body={"query": "# a comment with mutation\nquery Q { a }"}),
            "read",
        ),
        (_req(body={"query": 'mutation M { addNote(text: "x") { id } }'}), "save"),
        (
            _req(
                body=[
                    {"query": "query A { a }"},
                    {"query": "mutation B { b }", "variables": {}},
                ]
            ),
            "save",
        ),
        (
            _req(body={"extensions": {"persistedQuery": {"sha256Hash": "ab"}}}),
            "save",
        ),
        # A save that happens to have a "query" field is not GraphQL.
        (_req(body={"query": "query about knee pain", "note": "x"}), "save"),
        (_req("GET", "/graphql?query=mutation%20M%20%7B%20a%20%7D"), "save"),
        (_req("GET", "/graphql?query=query%20Q%20%7B%20a%20%7D"), "read"),
        (_req("POST", origin="http://127.0.0.1:9999"), "other_origin"),
    ],
)
def test_classify(request_: HeldRequest, kind: str) -> None:
    assert classify(request_, app_origin=APP) == kind


def test_not_a_save_rules_match_method_path_and_origin() -> None:
    rules = (
        NotASave("POST", "/chart/signin"),
        NotASave("*", "/collect*", origin="https://stats.example"),
    )
    assert classify(_req(path="/chart/signin"), app_origin=APP, not_saves=rules) == (
        "not_a_save"
    )
    assert classify(_req("PUT", "/chart/signin"), app_origin=APP, not_saves=rules) == (
        "save"
    )
    other = _req(path="/collect/v2", origin="https://stats.example")
    assert classify(other, app_origin=APP, not_saves=rules) == "not_a_save"
    assert origin_of("HTTPS://Stats.Example:443/x") == "https://stats.example"


def test_held_request_from_cdp_event() -> None:
    held = HeldRequest.from_cdp(
        {
            "requestId": "interception-1",
            "networkId": "1000.7",
            "resourceType": "XHR",
            "request": {
                "method": "post",
                "url": APP + "/api/encounter?fault=ok",
                "postData": "{}",
                "headers": {"Content-Type": "application/json"},
            },
        }
    )
    assert (held.method, held.path, held.content_type) == (
        "POST",
        "/api/encounter",
        "application/json",
    )
    assert held.network_id == "1000.7"


# -- the guard against a real record service ----------------------------------------


class FakePage:
    """The agent's tab: sends what the guard lets through to MockMed."""

    def __init__(self, agent: FakeAgent, patient: str, *, fault: str = "") -> None:
        self.agent = agent
        self.patient = patient
        self.fault = fault
        self.sent: list[str] = []
        self.blocked: list[dict[str, Any]] = []
        self.notices: list[tuple[str, str]] = []
        self.pending: dict[str, HeldRequest] = {}
        self.guard: Optional[CommitGuard] = None

    def screen_text(self) -> Optional[str]:
        return self.agent.open_chart(self.patient)

    def screenshot(self) -> Optional[bytes]:
        from openadapt_flow.guard_demo import tiny_png

        return tiny_png()

    def continue_request(self, request_id: str) -> None:
        self.sent.append(request_id)
        held = self.pending.pop(request_id, None)
        if held is not None and held.method == "POST":
            self.agent.click_save(self.patient, NOTE, fault=self.fault)
            assert self.guard is not None
            self.guard.on_request_done(held.network_id)

    def block_request(self, request_id: str, **response: Any) -> None:
        self.pending.pop(request_id, None)
        self.blocked.append({"id": request_id, **response})

    def notify(self, message: str, *, level: str) -> None:
        self.notices.append((level, message))

    def save(self, rid: str = "r1", **kw: Any) -> HeldRequest:
        held = _req(origin=self.agent.base, rid=rid, body={"note": NOTE}, **kw)
        self.pending[rid] = held
        assert self.guard is not None
        self.guard.on_request(held)
        return held


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _task() -> GuardTask:
    return GuardTask(
        task_id="task-guard-0001",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
        authorization_ref="ticket-0001",
    )


def _guard(
    sor, tmp_path: Path, page: FakePage, *, approval: str = "not_gated", **kw: Any
) -> CommitGuard:
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(sor[0])), tmp_path / "state", approval=approval
    )
    guard = CommitGuard(
        guarded,
        _task(),
        page,
        app_origin=sor[0],
        events_path=tmp_path / "events.jsonl",
        **kw,
    )
    page.guard = guard
    return guard


def _rows(db) -> list[tuple[str, str]]:
    return [(r["patient_id"], r["note"]) for r in db.snapshot()["records"]]


def test_right_record_save_goes_through_and_reads_back_verified(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.finished
    assert guard.summary.verdict == "VERIFIED"
    assert _rows(sor[1]) == [("p1", NOTE)]
    assert page.sent == ["r1"] and not page.blocked
    assert guard.summary.saves_held == 1 and len(guard.summary.hold_ms) == 1
    assert page.notices[-1][0] == "ok"
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    # No record values in the log: methods, digests, decisions.
    assert NOTE not in json.dumps(events) and "p1" not in json.dumps(events)
    assert events[-1] == {
        "event": "read_back",
        "t": events[-1]["t"],
        "verdict": "VERIFIED",
    }


def test_wrong_record_on_screen_is_blocked_and_the_agent_is_told(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p2")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert _rows(sor[1]) == []
    (blocked,) = page.blocked
    assert blocked["status"] == BLOCK_STATUS
    body = json.loads(blocked["body"])["openadapt_guard"]
    assert body["code"] == "identity_mismatch"
    assert "patient_id" in body["message"]
    assert blocked["headers"]["X-OpenAdapt-Guard"].startswith("blocked")
    assert page.notices[-1][0] == "error"
    assert "blocked this save" in page.notices[-1][1]
    assert not guard.finished
    # The agent opens the right record and saves again: that one goes through.
    page.patient = "p1"
    page.save("r2")
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    assert _rows(sor[1]) == [("p1", NOTE)]


def test_second_save_of_the_task_is_blocked(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    # A double-submit: two saves before the first is read back.
    guard.on_request(_req(origin=sor[0], rid="a", body={"note": NOTE}))
    page.pending["b"] = _req(origin=sor[0], rid="b", body={"note": NOTE})
    guard.on_request(page.pending["b"])
    assert [b["id"] for b in page.blocked] == ["b"]
    assert json.loads(page.blocked[0]["body"])["openadapt_guard"]["code"] == (
        "needs_reconciliation"
    )


def test_a_save_that_does_not_land_is_not_verified(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1", fault="no_persist")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "RECONCILIATION_REQUIRED"
    assert page.notices[-1][0] == "error"


def test_save_lands_on_another_record_is_not_verified(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1", fault="wrong_record")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict != "VERIFIED"


def test_observe_only_never_blocks_and_still_reads_back(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p2")
    guard = _guard(sor, tmp_path, page, observe_only=True)
    page.save()
    guard.poll()
    assert page.blocked == []
    assert _rows(sor[1]) == [("p2", NOTE)]
    assert guard.summary.would_block[0]["code"] == "identity_mismatch"
    assert guard.summary.verdict != "VERIFIED"
    # A second save is let through too, and logged.
    page.save("r2")
    assert page.blocked == []
    assert guard.summary.would_block[-1]["code"] == "already_committed"


def test_each_item_holds_the_save_until_a_person_approves(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, approval="each_item")
    page.save()
    assert page.sent == [] and guard.busy
    assert page.notices[-1][0] == "info"
    guard.poll()
    assert page.sent == []
    # Another save while one waits for a person is blocked.
    page.save("r2")
    assert [b["id"] for b in page.blocked] == ["r2"]
    waiting = guard._waiting
    assert waiting is not None
    g = waiting[0]
    request = g._request
    person_approves(str(g.dir), request.request_id, request.digest())
    guard.poll()
    assert page.sent == ["r1"]
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"


def test_write_to_another_origin_is_blocked(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    guard.on_request(_req(origin="http://127.0.0.1:1", rid="x"))
    assert page.blocked[0]["id"] == "x"
    assert json.loads(page.blocked[0]["body"])["openadapt_guard"]["code"] == (
        "other_origin"
    )


def test_blocked_form_post_gets_a_page_that_says_why(sor, tmp_path):
    page = FakePage(FakeAgent(sor[0]), "p2")
    guard = _guard(sor, tmp_path, page)
    held = _req(
        origin=sor[0],
        path="/encounter/save",
        resource_type="Document",
        content_type="application/x-www-form-urlencoded",
    )
    guard.on_request(held)
    (blocked,) = page.blocked
    assert blocked["content_type"].startswith("text/html")
    assert "OpenAdapt blocked this save" in blocked["body"]


def test_no_read_back_waits_past_response_wait(sor, tmp_path):
    clock = [0.0]
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, response_wait_s=5.0, clock=lambda: clock[0])
    held = _req(origin=sor[0], body={"note": NOTE})
    guard.on_request(held)  # sent; the fake never reports it done
    guard.poll()
    assert not guard.finished
    clock[0] = 6.0
    guard.poll()
    # Nothing landed (the fake page didn't send it): not verified.
    assert guard.summary.verdict == "RECONCILIATION_REQUIRED"


# -- the CDP adapter fails closed ---------------------------------------------------


class FakeCDP:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.handlers: dict[str, Any] = {}
        self.detached = False

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        self.sent.append((method, params or {}))
        return {}

    def detach(self) -> None:
        self.detached = True


class FakeTab:
    def __init__(self) -> None:
        self.cdp = FakeCDP()
        self.context = self

    def new_cdp_session(self, _page: Any) -> FakeCDP:
        return self.cdp


def _paused(rid: str, method: str = "POST") -> dict:
    return {
        "requestId": rid,
        "networkId": "n" + rid,
        "resourceType": "Fetch",
        "request": {"method": method, "url": APP + "/api/x", "headers": {}},
    }


def test_cdp_page_pauses_writes_and_blocks_on_a_guard_error() -> None:
    tab = FakeTab()
    page = CdpGuardPage(tab)
    enable = dict(tab.cdp.sent)["Fetch.enable"]
    kinds = {p["resourceType"] for p in enable["patterns"]}
    assert {"Document", "XHR", "Fetch", "Ping"} <= kinds
    assert "Image" not in kinds

    class Broken:
        def on_request(self, _held: Any) -> None:
            raise RuntimeError("boom")

    tab.cdp.handlers["Fetch.requestPaused"](_paused("1"))
    with pytest.raises(RuntimeError):
        page.drain(Broken())
    method, params = tab.cdp.sent[-1]
    assert method == "Fetch.fulfillRequest" and params["requestId"] == "1"
    assert params["responseCode"] == BLOCK_STATUS


def test_cdp_page_blocks_what_is_still_paused_before_it_detaches() -> None:
    tab = FakeTab()
    page = CdpGuardPage(tab)

    class Holds:
        def on_request(self, _held: Any) -> None:
            pass  # e.g. waiting for a person

        def on_request_done(self, _nid: Any) -> None:
            pass

        def poll(self) -> None:
            pass

    tab.cdp.handlers["Fetch.requestPaused"](_paused("7"))
    page.drain(Holds())
    tab.cdp.handlers["Fetch.requestPaused"](_paused("8"))  # never drained
    page.release()
    methods = [m for m, _ in tab.cdp.sent]
    fulfilled = [p["requestId"] for m, p in tab.cdp.sent if m == "Fetch.fulfillRequest"]
    assert fulfilled == ["7", "8"]
    assert methods.index("Fetch.disable") > methods.index("Fetch.fulfillRequest")
    assert tab.cdp.detached


# -- guard.yaml ------------------------------------------------------------------------


def test_config_parses_and_refuses(tmp_path):
    verify = tmp_path / "verify.yaml"
    verify.write_text("verifier: {kind: rest}\n")
    raw = {
        "schema_version": "openadapt.agent-guard/v1",
        "app_url": "http://127.0.0.1:8765/#tasks",
        "verify": "verify.yaml",
        "not_saves": [{"method": "POST", "path": "/chart/signin"}],
        "teach": {"dir": "teach", "inputs": ["note"], "db": "app.sqlite"},
    }
    cfg = parse_config(raw, base=tmp_path)
    assert cfg.app_origin == "http://127.0.0.1:8765"
    assert cfg.approval == "not_gated" and not cfg.observe_only
    assert cfg.teach is not None and cfg.teach.inputs == ("note",)
    assert cfg.teach.dir == (tmp_path / "teach").resolve()
    for bad in (
        {**raw, "schema_version": "x"},
        {**raw, "surprise": 1},
        {**raw, "approval": "auto"},
        {**raw, "app_url": "file:///etc"},
        {**raw, "not_saves": [{"method": "POST"}]},
        {**raw, "teach": {"name": "x"}},
        {**raw, "verify": "missing.yaml"},
    ):
        with pytest.raises(ConfigError):
            parse_config(bad, base=tmp_path)
