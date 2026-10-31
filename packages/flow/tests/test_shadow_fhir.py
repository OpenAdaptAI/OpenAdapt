"""Shadow mode's FHIR path: grouping writes into episodes, what each check is
told, and how the verifier's outcome becomes a verdict.

The verifier (``ensure_record``) is stubbed here: its own tests cover it,
and the live Medplum run (``benchmark/fhir_medplum/public/shadow_run.py``)
covers the two together. Records are made up.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import ensure_record
from openadapt_flow.ensure_record import Baseline, EnsureResult, load_contract
from openadapt_flow.shadow import Shadow
from openadapt_flow.shadow_report import render_html

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "benchmark/fhir_medplum/public/contracts/allergy.yaml"
)


class Fhir:
    """Answers each write with ``status`` and a new id; records what came in."""

    def __init__(self) -> None:
        self.status = 201
        self.received: list[tuple[str, str, bytes]] = []
        self.on_write: Any = None
        fx = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _do(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fx.received.append((self.command, self.path, body))
                if fx.on_write:
                    fx.on_write()
                rid = f"a{len(fx.received)}"
                out = json.dumps({"resourceType": "AllergyIntolerance", "id": rid})
                self.send_response(fx.status)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out.encode())

            do_POST = do_PUT = do_DELETE = do_GET = _do

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def _allergy(patient: str, code: str = "91936005") -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "patient": {"reference": patient},
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]},
        "criticality": "high",
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setenv("BENCH_FHIR_URL", "http://127.0.0.1:9/fhir/R4")
    monkeypatch.setenv("BENCH_AGENT_REF", "Practitioner/bot")
    monkeypatch.setenv("BENCH_RECORD_TOKEN", "t")
    calls: dict[str, Any] = {"verify": [], "outcome": "created_verified", "ev": {}}

    def baseline(contract: Any, params: Any, reader: Any = None) -> Baseline:
        return Baseline(started=datetime.now(timezone.utc), matches={})

    def verify(contract: Any, params: Any, base: Any, **kw: Any) -> EnsureResult:
        calls["verify"].append({"params": dict(params), **kw})
        if calls.get("during"):
            calls.pop("during")()
        return EnsureResult(calls["outcome"], "a1", ["stub"], dict(calls["ev"]))

    monkeypatch.setattr(ensure_record, "capture_baseline", baseline)
    monkeypatch.setattr(ensure_record, "verify_ensure", verify)
    monkeypatch.setattr(
        ensure_record.FhirReader,
        "fetch",
        lambda self, url, params=None: ({}, {"resourceType": "AllergyIntolerance"}),
    )
    monkeypatch.setattr(
        ensure_record.FhirReader, "server_time", lambda self: datetime.now(timezone.utc)
    )
    fhir = Fhir()
    sh = Shadow(
        upstream=fhir.url,
        mode="fhir",
        contracts=[load_contract(CONTRACT)],
        out=tmp_path / "out",
        quiet_s=3600,
    ).start()
    yield {"sh": sh, "fhir": fhir, "calls": calls}
    sh.stop()


def _send(sh: Shadow, method: str, path: str, body: Any = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", sh.server.server_address[1])
    raw = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=raw, headers={"Content-Type": "application/json"})
    status = conn.getresponse().status
    conn.close()
    return status


def test_a_retry_and_a_correction_are_one_check(env: dict[str, Any]) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    fixed = {**_allergy("Patient/p1"), "criticality": "low", "id": "a1"}
    _send(sh, "PUT", "/fhir/R4/AllergyIntolerance/a1", fixed)
    sh.flush()
    (ep,) = sh.episodes
    assert len(ep.writes) == 3 and ep.verdict == "CONFIRMED"
    # The check is told the agent's latest values: its correction.
    assert env["calls"]["verify"][0]["params"]["criticality"] == "low"


def test_other_unchecked_episodes_are_declared_and_nothing_else(
    env: dict[str, Any],
) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p2"))
    sh.flush()
    first, second = env["calls"]["verify"]
    assert first["declared"] == ["AllergyIntolerance/a2"]
    # By the second check the first is checked: its record counts again.
    assert second["declared"] == []


def test_a_refused_write_with_nothing_landed_is_uncertain(env: dict[str, Any]) -> None:
    env["fhir"].status = 422
    env["calls"]["outcome"] = "needs_review"
    env["calls"]["ev"] = {"agent_writes": 0}
    _send(env["sh"], "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    env["sh"].flush()
    assert env["sh"].episodes[0].verdict == "UNCERTAIN"


def test_a_write_during_a_check_makes_it_uncertain(env: dict[str, Any]) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    env["calls"]["during"] = lambda: _send(
        sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p9")
    )
    sh.flush()
    assert sh.episodes[0].verdict == "UNCERTAIN"
    assert "while this was checked" in sh.episodes[0].reasons[0]


def test_an_update_of_a_record_it_didnt_see_created_is_uncertain(
    env: dict[str, Any],
) -> None:
    sh = env["sh"]
    _send(sh, "PUT", "/fhir/R4/AllergyIntolerance/x", _allergy("Patient/p1"))
    _send(sh, "DELETE", "/fhir/R4/AllergyIntolerance/y")
    _send(sh, "POST", "/fhir/R4/Observation", {"resourceType": "Observation"})
    sh.flush()
    assert [e.verdict for e in sh.episodes] == ["UNCERTAIN"] * 3
    assert "AllergyIntolerance/y" in sh.episodes[1].refs
    assert env["calls"]["verify"] == []


def test_a_path_outside_ascii_still_goes_through(env: dict[str, Any]) -> None:
    sh = env["sh"]
    conn = http.client.HTTPConnection("127.0.0.1", sh.server.server_address[1])
    conn.putrequest("POST", "/fhir/R4/AllergyIntolerance?n=x")
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    conn.getresponse().read()
    # A raw byte in the target: the shadow percent-encodes it and sends it on.
    raw = "POST /fhir/R4/Observation?n=\xe9 HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}"
    import socket

    with socket.create_connection(sh.server.server_address[:2]) as s:
        s.sendall(raw.encode("latin-1"))
        assert b" 201 " in s.recv(100).split(b"\r\n", 1)[0]
    sh.flush()
    paths = [p for _, p, _ in env["fhir"].received]
    assert "/fhir/R4/Observation?n=%E9" in paths
    assert all(e.verdict for e in sh.episodes) and not sh.errors


def test_a_confirmed_write_is_checked_again_and_a_late_change_refutes_it(
    env: dict[str, Any],
) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    (ep,) = sh.episodes
    assert ep.verdict == "CONFIRMED" and ep.recheck_at > 0
    assert sh.results()["rechecks_pending"] == 1
    # Later, a side write by the agent's credential lands outside the proxy.
    env["calls"]["outcome"] = "needs_review"
    env["calls"]["ev"] = {"agent_writes": 2}
    sh.run_rechecks(now=True)
    doc = sh.results()
    assert ep.late_verdict == "REFUTED" and doc["late_refuted"] == 1
    assert "Landed wrong after the check (1)" in render_html(doc)
    # The recheck is told every other write the agent sent, checked or not.
    assert env["calls"]["verify"][-1]["declared"] == []
    assert ep.late_reasons[0].startswith("since the first check: ")


def _stored(patient: str) -> dict[str, Any]:
    return {
        **_allergy(patient),
        "id": "a1",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
    }


def test_the_recheck_refutes_a_confirmed_record_changed_to_another_patient(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sh = env["sh"]
    stored = {"now": _stored("Patient/p1")}
    monkeypatch.setattr(
        ensure_record.FhirReader,
        "fetch",
        lambda self, url, params=None: ({}, stored["now"]),
    )
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    (ep,) = sh.episodes
    # Nothing moved: the recheck confirms.
    env["calls"]["outcome"] = "found_existing"
    sh.run_rechecks(now=True)
    assert ep.late_verdict == "CONFIRMED", ep.late_reasons
    # Moved to another patient: refuted, though no write of the agent's shows.
    ep.late_verdict = ""
    stored["now"] = _stored("Patient/p2")
    sh.run_rechecks(now=True)
    assert ep.late_verdict == "REFUTED"
    assert "no longer the person" in ep.late_reasons[0]
    assert sh.results()["counts"]["REFUTED"] == 1  # counted by the final verdict


def test_a_later_put_to_the_confirmed_record_is_not_excused(
    env: dict[str, Any],
) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    _send(sh, "PUT", "/fhir/R4/AllergyIntolerance/a1", _allergy("Patient/p2"))
    sh.flush()
    sh.run_rechecks(now=True)
    assert "AllergyIntolerance/a1" not in env["calls"]["verify"][-1]["declared"]


def test_a_write_during_a_recheck_makes_it_uncertain(env: dict[str, Any]) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    env["calls"]["outcome"] = "needs_review"
    env["calls"]["ev"] = {"agent_writes": 1}
    env["calls"]["during"] = lambda: _send(
        sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p9")
    )
    sh.run_rechecks(now=True)
    assert sh.episodes[0].late_verdict == "UNCERTAIN"
    assert "while this was rechecked" in sh.episodes[0].late_reasons[0]


def test_a_later_write_with_no_known_record_makes_a_late_refutation_uncertain(
    env: dict[str, Any],
) -> None:
    sh = env["sh"]
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    sh.flush()
    env["fhir"].status = 504  # a later create whose reply is lost
    _send(sh, "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p2"))
    env["calls"]["outcome"] = "needs_review"
    env["calls"]["ev"] = {"agent_writes": 1}
    sh.flush()
    sh.run_rechecks(now=True)
    assert sh.episodes[0].late_verdict == "UNCERTAIN"
    assert "can't tell apart" in sh.episodes[0].late_reasons[0]


def test_a_recheck_that_cant_be_scheduled_says_so(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_clock(self: Any) -> Any:
        raise ensure_record.Unreadable("no Date header")

    monkeypatch.setattr(ensure_record.FhirReader, "server_time", no_clock)
    _send(env["sh"], "POST", "/fhir/R4/AllergyIntolerance", _allergy("Patient/p1"))
    env["sh"].flush()
    ep = env["sh"].episodes[0]
    assert ep.verdict == "CONFIRMED" and ep.late_verdict == "UNCERTAIN"
    assert env["sh"].results()["counts"]["UNCERTAIN"] == 1


def test_kinds_name_how_a_write_landed_wrong() -> None:
    from openadapt_flow.shadow_report import kind_of

    created_none = ["the agent created 0 Patient records, not one"]
    assert kind_of(created_none + ["wrote 1 other resource(s)"]) == (
        "another record changed"
    )
    assert kind_of(["the agent created 2 Patient records, not one"]) == (
        "saved more than once"
    )
    both = ["wrong or missing: phone", "someone else changed the created record"]
    assert kind_of(both) == "changed by someone else"
