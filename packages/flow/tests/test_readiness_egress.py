"""Readiness runs every candidate under the flight recorder's egress (J7).

The sandbox parts run for real on macOS (``sandbox-exec``). Model calls go to
a loopback stand-in for the provider API (``FakeProvider``); nothing leaves
this machine. The join test reads ``fixtures/readiness/p3_egress.json``, an
``egress.json`` written by the flight recorder's own ``ScrubbedEgress`` (one
scrubbed call to the stand-in, usage.json agreeing).
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.readiness.compare import compare_case
from openadapt_flow.readiness.egress import (
    EgressRefused,
    EgressSession,
    metered_cost,
    preflight,
)
from openadapt_flow.readiness.launcher import (
    EgressOptions,
    LaunchJob,
    SubprocessLauncher,
)
from openadapt_flow.readiness.scorecard import build_scorecard
from tests.fixtures.flight.fake_provider import FakeProvider

FIXTURE = Path(__file__).parent / "fixtures" / "readiness" / "p3_egress.json"
J7_KEYS = {"mode", "enforced", "model_calls", "cost_usd", "cost_source", "refused_requests"}
CLEAN_GATE = {"extra_targets": {}, "ungated_targets": {}, "gate_lost": False, "errors": []}


def _block_or_skip() -> None:
    from openadapt_flow.flight.egress import egress_block_available

    ok, why = egress_block_available()
    if not ok:
        pytest.skip(why)


class FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("Zyxwv Qtest", "<PERSON>")

    def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
        return image


@pytest.fixture
def provider():
    fake = FakeProvider().start()
    yield fake
    fake.stop()


@pytest.fixture
def cdp_port():
    """A loopback listener standing in for the browser's DevTools port."""

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


#: The candidate: one model call through ANTHROPIC_BASE_URL, then a connect
#: to a public (TEST-NET) address, the DevTools port and another loopback
#: port; then it writes the call count it claims to usage.json.
AGENT = """
import json, os, socket, sys, urllib.request
calls, claim, cdp, other = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
out = []
if calls == "1":
    req = urllib.request.Request(
        os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
        data=json.dumps({"model": "claude-opus-5-5", "max_tokens": 8,
                         "messages": [{"role": "user", "content": "hi Zyxwv Qtest"}]}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    try:
        out.append(str(urllib.request.build_opener(urllib.request.ProxyHandler({}))
                       .open(req, timeout=30).status))
    except Exception as exc:
        out.append("call failed " + type(exc).__name__)
for host, port in (("192.0.2.1", 443), ("127.0.0.1", cdp), ("127.0.0.1", other)):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); out.append("connected")
    except PermissionError:
        out.append("EPERM")
    except OSError as exc:
        out.append("error " + type(exc).__name__)
print(" ".join(out))
with open(os.environ["OPENADAPT_USAGE_PATH"], "w") as fh:
    json.dump({"schema": "openadapt.agent-usage/v1", "calls": claim}, fh)
"""


def _run_agent(tmp_path: Path, options: EgressOptions, cdp_port: int, calls: str,
               claim: int, other: int) -> tuple[str, Any]:
    run_dir = tmp_path / "task-0001"
    run_dir.mkdir(parents=True, exist_ok=True)
    job = LaunchJob(task_id="task-0001", instruction="x", inputs={},
                    cdp_url=f"http://127.0.0.1:{cdp_port}", page_url="http://127.0.0.1/",
                    run_dir=run_dir)
    launcher = SubprocessLauncher(
        [sys.executable, "-c", AGENT, calls, str(claim), str(cdp_port), str(other)],
        egress=options,
    )
    agent = launcher.start(job)
    deadline = time.monotonic() + 60
    while agent.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    agent.stop()
    result = agent.result()
    return (run_dir / "agent.log").read_text().strip().splitlines()[-1], result


def test_scrubbed_calls_are_metered_and_the_agent_reaches_only_its_ports(
    tmp_path: Path, provider: FakeProvider, cdp_port: int
) -> None:
    _block_or_skip()
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        other = spare.getsockname()[1]
    options = EgressOptions(mode="scrubbed", spend_cap_usd=1.0,
                            upstreams={"anthropic": provider.url}, scrubber=FakeScrubber())
    out, result = _run_agent(tmp_path, options, cdp_port, "1", 1, other)
    assert out == "200 EPERM connected EPERM"
    assert b"Zyxwv Qtest" not in provider.received_bytes()
    egress = result.egress
    assert egress["mode"] == "scrubbed" and egress["enforced"] is True
    assert egress["model_calls"] == 1 and egress["refused"] is False
    assert metered_cost(egress) == (egress["cost_usd"], "metered") and egress["cost_usd"] > 0
    on_disk = json.loads((tmp_path / "task-0001" / "egress.json").read_text())
    assert set(on_disk) == J7_KEYS


def test_an_agent_that_claims_calls_the_proxy_didnt_see_is_refused(
    tmp_path: Path, provider: FakeProvider, cdp_port: int
) -> None:
    _block_or_skip()
    options = EgressOptions(mode="scrubbed", upstreams={"anthropic": provider.url},
                            scrubber=FakeScrubber())
    _, result = _run_agent(tmp_path, options, cdp_port, "1", 3, cdp_port)
    assert result.egress["refused"] is True
    assert "reports 3 model calls" in result.egress["reason"]


def test_blocked_mode_allows_no_model_call(tmp_path: Path, cdp_port: int) -> None:
    _block_or_skip()
    out, result = _run_agent(tmp_path, EgressOptions(mode="blocked"), cdp_port, "1", 0,
                             cdp_port + 1 if cdp_port < 65535 else cdp_port - 1)
    words = out.split()
    assert words[0] == "call" and words[1] == "failed"  # no model reachable
    assert words[-3:] == ["EPERM", "connected", "EPERM"]
    egress = result.egress
    assert egress == {"mode": "blocked", "enforced": True, "model_calls": 0, "cost_usd": 0.0,
                      "cost_source": "metered", "refused_requests": 0, "refused": False,
                      "reason": None}
    assert set(json.loads((tmp_path / "task-0001" / "egress.json").read_text())) == J7_KEYS


def test_blocked_mode_refuses_a_claimed_call(tmp_path: Path, cdp_port: int) -> None:
    _block_or_skip()
    _, result = _run_agent(tmp_path, EgressOptions(mode="blocked"), cdp_port, "0", 2, cdp_port)
    assert result.egress["refused"] is True
    assert "reports 2 model calls" in result.egress["reason"]


def test_unscrubbed_is_recorded(tmp_path: Path, provider: FakeProvider, cdp_port: int) -> None:
    _block_or_skip()
    options = EgressOptions(mode="unscrubbed", upstreams={"anthropic": provider.url})
    _, result = _run_agent(tmp_path, options, cdp_port, "1", 1, cdp_port)
    assert result.egress["mode"] == "unscrubbed" and result.egress["model_calls"] == 1
    assert b"Zyxwv Qtest" in provider.received_bytes()  # the opt-in is real


def test_no_block_refuses_before_anything_runs(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    from openadapt_flow.flight import egress as flight

    monkeypatch.setattr(flight, "egress_block_available", lambda: (False, "test: no block"))
    for mode in ("scrubbed", "blocked", "unscrubbed"):
        with pytest.raises(EgressRefused, match="no block"):
            preflight(mode, scrubber=FakeScrubber())
        with pytest.raises(EgressRefused):
            EgressSession(mode, tmp_path, cdp_port=9222, scrubber=FakeScrubber()).start()
    with pytest.raises(EgressRefused):
        preflight("open")


def test_a_missing_scrubber_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from openadapt_flow.flight import egress as flight
    from openadapt_flow.flight import scrub

    monkeypatch.setattr(flight, "egress_block_available", lambda: (True, ""))

    def missing(**_kw: Any) -> Any:
        raise scrub.ScrubberUnavailable("the tesseract binary is not on PATH")

    monkeypatch.setattr(scrub, "require_scrubber", missing)
    with pytest.raises(EgressRefused, match="tesseract"):
        preflight("scrubbed")


# -- the J7 join, on the flight recorder's own egress.json ------------------------------


def test_the_flight_recorders_egress_json_is_read_as_metered_cost() -> None:
    summary = json.loads(FIXTURE.read_text())
    assert set(summary) == J7_KEYS
    cost, source = metered_cost(summary)
    assert source == "metered" and cost == summary["cost_usd"]
    assert metered_cost({**summary, "cost_source": "unknown", "cost_usd": None}) == (None, None)


def _ref() -> dict[str, Any]:
    return {"task_id": "case-0001", "expected_record": {"patient_id": "p1"},
            "human_fields": [{"name": "type", "value": "Triage"}], "human_duration_s": 60.0}


def _proposed() -> dict[str, Any]:
    return {"request_index": 0, "identity": {"ok": True, "code": "verified"},
            "fields": [{"name": "type", "value": "Triage", "parse": "json"}]}


def test_scorecard_cost_and_egress_come_from_the_proxy() -> None:
    summary = json.loads(FIXTURE.read_text())
    cost, source = metered_cost(summary)
    run = {"status": "HELD", "agent_wall_s": 20.0, "landed_writes": 0,
           "target_gate": CLEAN_GATE, "model_cost_usd": cost, "model_cost_source": source,
           "egress": {**summary, "refused": False, "reason": None},
           "usage": {"calls": 1, "cost_usd": 99.0}}  # the agent's own claim is ignored
    case = compare_case(_ref(), run, _proposed(), before_screen="p1",
                        held=[{"request_index": 0}])
    assert case["status"] == "MATCHED"
    card = build_scorecard({"cases": [case], "egress": {"mode": "scrubbed", "enforced": True}})
    assert card["model_cost_usd"]["total"] == summary["cost_usd"]
    assert card["model_cost_usd"]["source"] == "metered"
    assert card["egress"] == {"mode": "scrubbed", "enforced": True, "model_calls": 1}


def test_a_refused_egress_fails_the_case_closed() -> None:
    run = {"status": "EGRESS_REFUSED", "agent_wall_s": 20.0, "landed_writes": 0,
           "target_gate": CLEAN_GATE,
           "egress": {"mode": "scrubbed", "refused": True, "reason": "reports 3 calls"}}
    case = compare_case(_ref(), run, _proposed(), before_screen="p1",
                        held=[{"request_index": 0}])
    assert (case["status"], case["reason"]) == ("NOT_CHECKED", "egress_refused")
    card = build_scorecard({"cases": [case], "egress": {"mode": "scrubbed", "enforced": True}})
    assert card["failed_scope"] == 1 and card["scope_ok"] is False
