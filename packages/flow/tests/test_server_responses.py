"""The server's own answer to a save, kept as evidence and labelled honestly.

When the browser sees the server answer a save request, the answer is kept on
the step and counted on the receipt at check level ``same_screen``. It comes
from the same transaction as the save, so on its own it never confirms an
effect and never makes a run VERIFIED. No body, URL or value is kept.
"""

from __future__ import annotations

import hashlib
import http.server
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.backends._server_responses import ServerResponseLog
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    ServerResponseEvidence,
    Step,
    Workflow,
)
from openadapt_flow.receipt import build_receipt
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match

BODY = b'{"id": "enc-7", "status": "saved"}'


# -- the log, on fakes -------------------------------------------------------------


class _Request:
    def __init__(self, method: str) -> None:
        self.method = method


class _Response:
    def __init__(self, method: str, status: int, url: str, body: bytes) -> None:
        self.request = _Request(method)
        self.status = status
        self.url = url
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self._body = body

    def body(self) -> bytes:
        return self._body


class _Page:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


def test_only_answers_to_write_requests_are_kept_without_values() -> None:
    page = _Page()
    log = ServerResponseLog(page)
    emit = page.handlers["response"]
    emit(_Response("GET", 200, "https://emr.test/patients/MRN-1", b"x"))
    mark = log.mark()
    emit(_Response("POST", 201, "https://emr.test/api/encounters?patient=MRN-1", BODY))
    emit(_Response("GET", 200, "https://emr.test/api/encounters", b"[]"))
    [answer] = log.since(mark)
    assert answer.method == "POST" and answer.status == 201 and answer.ok
    assert answer.body_sha256 == hashlib.sha256(BODY).hexdigest()
    assert answer.content_type == "application/json"
    assert answer.check_level == "same_screen"
    dumped = answer.model_dump_json()
    for leak in ("MRN-1", "emr.test", "enc-7", "patient="):
        assert leak not in dumped
    # The query string is not part of the endpoint digest.
    other = _Response("POST", 201, "https://emr.test/api/encounters", BODY)
    emit(other)
    assert log.since(mark)[1].endpoint_sha256 == answer.endpoint_sha256


def test_an_answer_whose_body_is_gone_keeps_its_status() -> None:
    page = _Page()
    log = ServerResponseLog(page)

    class Gone(_Response):
        def body(self) -> bytes:
            raise RuntimeError("navigated")

    page.handlers["response"](Gone("PUT", 500, "https://emr.test/api/x", b""))
    [answer] = log.since(0)
    assert answer.status == 500 and not answer.ok and answer.body_sha256 is None


# -- the replayer --------------------------------------------------------------------


class AnsweringBackend(FakeBackend):
    """A fake backend whose save gets a server answer, as a browser would see."""

    def __init__(self, status: int = 201) -> None:
        super().__init__()
        self.status = status
        self.answers: list[ServerResponseEvidence] = []

    def server_response_mark(self) -> int:
        return len(self.answers)

    def server_responses_since(self, mark: int) -> list[ServerResponseEvidence]:
        return self.answers[mark:]

    def press(self, key: str) -> None:
        super().press(key)
        self.answers.append(
            ServerResponseEvidence(
                method="POST",
                status=self.status,
                endpoint_sha256="e" * 64,
                body_sha256=hashlib.sha256(BODY).hexdigest(),
                body_bytes=len(BODY),
                content_type="application/json",
            )
        )


class _Verifier:
    """A verifier that can't read the record: the save is unconfirmed."""

    substrate = "rest"
    verification_tier = 1

    def capture_pre_state(self, context: Any = None) -> EffectState:
        return EffectState(substrate="rest", reachable=True, records=[])

    def verify(self, effect: Effect, before: EffectState, context: Any = None):
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=effect.kind,
            substrate="rest",
            reason="record store unreachable",
            unavailable=True,
        )


def _workflow() -> Workflow:
    return Workflow(
        name="save",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": "p1"},
                        expected_count=1,
                        timeout_s=0.0,
                    )
                ],
            )
        ],
    )


def test_a_201_from_the_server_never_makes_the_run_verified(tmp_path: Path) -> None:
    vision = FakeVision()
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    backend = AnsweringBackend(201)
    report = Replayer(
        backend, vision=vision, effect_verifier=_Verifier(), poll_interval_s=0.01
    ).run(_workflow(), bundle_dir=bundle, run_dir=tmp_path / "run")
    [result] = report.results
    assert [a.status for a in result.server_responses] == [201]
    assert result.server_responses[0].check_level == "same_screen"
    # The server said "created"; the record read couldn't confirm it.
    assert report.execution_outcome != "VERIFIED"
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.transaction_billable is False
    saved = (tmp_path / "run" / "report.json").read_text()
    assert '"server_responses"' in saved


def test_steps_without_answers_serialize_as_before() -> None:
    from openadapt_flow.ir import StepResult

    assert (
        "server_responses"
        not in StepResult(step_id="a", intent="b", ok=True).model_dump_json()
    )


def test_the_receipt_counts_answers_as_supporting_evidence() -> None:
    from tests.test_run_receipt import _report

    report = _report()
    plain = build_receipt(report)
    assert "server_responses" not in plain.model_dump()
    report.results[-1].server_responses = [
        ServerResponseEvidence(method="POST", status=201, endpoint_sha256="e" * 64),
        ServerResponseEvidence(method="POST", status=500, endpoint_sha256="e" * 64),
    ]
    receipt = build_receipt(report)
    assert receipt.server_responses is not None
    assert (receipt.server_responses.observed, receipt.server_responses.ok) == (2, 1)
    assert receipt.server_responses.check_level == "same_screen"
    # Evidence strength still comes from the read-back alone.
    assert receipt.effect_tier_reached == plain.effect_tier_reached


# -- a real browser -------------------------------------------------------------------


@pytest.fixture()
def save_server() -> Iterator[str]:
    page = (
        b"<html><body><button id=save onclick=\"fetch('/api/save?patient=MRN-9',"
        b"{method:'POST',body:'note'}).then(r=>r.text())"
        b".then(()=>document.body.append('Saved'))\">Save</button></body></html>"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_the_browser_backend_keeps_the_answer_to_a_real_save(save_server: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - env specific
            pytest.skip(f"no chromium: {exc}")
        try:
            page = browser.new_page()
            backend = PlaywrightBackend(page)
            page.goto(save_server)
            mark = backend.server_response_mark()
            page.click("#save")
            page.wait_for_function("document.body.innerText.includes('Saved')")
            [answer] = backend.server_responses_since(mark)
        finally:
            browser.close()
    assert answer.method == "POST" and answer.status == 201
    assert answer.body_sha256 == hashlib.sha256(BODY).hexdigest()
    assert "MRN-9" not in answer.model_dump_json()
