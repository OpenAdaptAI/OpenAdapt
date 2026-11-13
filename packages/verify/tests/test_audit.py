"""``openadapt-verify audit``: the fault proxy, and audits of the demo scripts."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from openadapt_verify import audit, audit_demo, quickstart


class _Upstream:
    """Records every request; answers writes 201 with a counter."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _serve(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append((self.command, self.path, body))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.base + "/api/done")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                reply = json.dumps({"n": len(outer.seen)}).encode()
                self.send_response(201 if self.command == "POST" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def writes(self):
        return [(p, b) for m, p, b in self.seen if m == "POST"]


@pytest.fixture
def upstream():
    server = _Upstream()
    yield server
    server.close()


def _send(proxy, method, path, body=b"", content_type="application/json"):
    conn = http.client.HTTPConnection(*proxy.httpd.server_address[:2], timeout=5)
    try:
        conn.request(method, path, body=body, headers={"Content-Type": content_type})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        conn.close()


VALUES = {"code": "item-7", "quantity": "3"}
BODY = json.dumps({"code": "item-7", "quantity": 3, "meta": {"note": "x"}}).encode()


@pytest.mark.parametrize(
    "kind, forwarded, status",
    [
        ("no_persist", [], 201),
        ("rejected", [], 409),
        ("partial", [{"code": "", "quantity": 3, "meta": {"note": "x"}}], 201),
        (
            "wrong_value",
            [{"code": "item-7-changed", "quantity": 3, "meta": {"note": "x"}}],
            201,
        ),
        ("duplicate", [json.loads(BODY)] * 2, 201),
        (
            "extra_write",
            [
                json.loads(BODY),
                {"code": "item-7-changed", "quantity": 3, "meta": {"note": "x"}},
            ],
            201,
        ),
    ],
)
def test_each_fault_changes_only_the_first_write(upstream, kind, forwarded, status):
    proxy = audit.FaultProxy(upstream.base)
    try:
        proxy.arm(kind, VALUES)
        got, _headers, _body = _send(proxy, "POST", "/api/items", BODY)
        # A retry or second write in the same run goes through unchanged.
        _send(proxy, "POST", "/api/items", BODY)
        injection = proxy.disarm()
    finally:
        proxy.close()
    assert got == status
    assert injection.applied and injection.request == "POST /api/items"
    sent = [json.loads(b) for _p, b in upstream.writes()]
    assert sent == forwarded + [json.loads(BODY)]


def test_no_persist_replays_the_last_real_success_reply(upstream):
    proxy = audit.FaultProxy(upstream.base)
    try:
        proxy.arm(None, VALUES)
        _, _, real = _send(proxy, "POST", "/api/items", BODY)
        proxy.disarm()
        proxy.arm("no_persist", VALUES)
        status, _, faked = _send(proxy, "POST", "/api/items", BODY)
        proxy.disarm()
    finally:
        proxy.close()
    assert (status, faked) == (201, real)
    assert len(upstream.writes()) == 1


def test_lost_response_lands_the_write_and_drops_the_reply(upstream):
    proxy = audit.FaultProxy(upstream.base)
    try:
        proxy.arm("lost_response", VALUES)
        with pytest.raises((http.client.HTTPException, ConnectionError)):
            _send(proxy, "POST", "/api/items", BODY)
        assert proxy.disarm().applied
    finally:
        proxy.close()
    assert len(upstream.writes()) == 1


def test_form_bodies_wrong_record_and_write_path(upstream):
    proxy = audit.FaultProxy(
        upstream.base, write_path=r"^/api/", wrong_record=("patient", "other")
    )
    form = b"patient=p1&note=item-7"
    try:
        proxy.arm("partial", VALUES)
        _send(proxy, "POST", "/login", b"user=a")  # not the write: unchanged
        _send(proxy, "POST", "/api/save", form, "application/x-www-form-urlencoded")
        proxy.disarm()
        proxy.arm("wrong_record", VALUES)
        _send(proxy, "POST", "/api/save", form, "application/x-www-form-urlencoded")
        proxy.disarm()
    finally:
        proxy.close()
    assert [b for _p, b in upstream.writes()] == [
        b"user=a",
        b"patient=p1&note=",
        b"patient=other&note=item-7",
    ]


def test_a_body_without_declared_values_is_not_measured(upstream):
    proxy = audit.FaultProxy(upstream.base)
    try:
        proxy.arm("wrong_value", VALUES)
        _send(proxy, "POST", "/api/items", b'{"other": "z"}')
        injection = proxy.disarm()
    finally:
        proxy.close()
    assert not injection.applied
    assert "no declared value" in injection.detail
    assert upstream.writes() == [("/api/items", b'{"other": "z"}')]


def test_reads_and_redirects_pass_through(upstream):
    proxy = audit.FaultProxy(upstream.base)
    try:
        proxy.arm("no_persist", VALUES)
        status, headers, _ = _send(proxy, "GET", "/redirect")
        assert not proxy.disarm().applied
    finally:
        proxy.close()
    assert status == 302
    assert headers["Location"] == proxy.base + "/api/done"


# -- audits through the real verifier ------------------------------------------


def _summary(report):
    return {
        k: (v["bad_saves"], v["missed"], v["false_alarms"])
        for k, v in report["summary"].items()
    }


def _demo(tmp_path, script):
    # A CLI subprocess: the audit isolates verifier state per run, which the
    # test fixture's pinned frame secret would refuse in-process.
    out = tmp_path / "report.json"
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_verify",
            "audit",
            "--demo",
            script,
            "--runs",
            "8",
            "--report",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 1, run.stderr
    return run, json.loads(out.read_text())


def test_cli_demo_shows_the_naive_scripts_misses(tmp_path):
    run, report = _demo(tmp_path, "naive")
    assert "reported success on 5 of 6 bad saves" in run.stdout
    assert report["format"] == audit.FORMAT
    assert _summary(report) == {
        "clean": (0, 0, 0),
        "no_persist": (1, 1, 0),
        "rejected": (1, 0, 0),
        "partial": (1, 1, 0),
        "wrong_value": (1, 1, 0),
        "duplicate": (1, 1, 0),
        "extra_write": (1, 1, 0),
        "lost_response": (0, 0, 1),
    }
    assert set(report["not_run"]) == {"stale", "collateral_unaudited", "wrong_record"}


def test_a_read_back_check_still_misses_the_extra_writes(tmp_path):
    _run, report = _demo(tmp_path, "readback")
    assert _summary(report) == {
        "clean": (0, 0, 0),
        "no_persist": (1, 0, 0),
        "rejected": (1, 0, 0),
        "partial": (1, 0, 0),
        "wrong_value": (1, 0, 0),
        "duplicate": (1, 1, 0),
        "extra_write": (1, 1, 0),
        "lost_response": (0, 0, 0),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cli_with_a_contract_success_regex_and_wrong_record(tmp_path):
    store = quickstart.RestReadPath(tmp_path)
    try:
        contract = tmp_path / "verify.yaml"
        contract.write_text(yaml.safe_dump(store.contract()))
        params = tmp_path / "values.json"
        params.write_text(json.dumps(audit_demo.PARAMS))
        port = _free_port()
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "openadapt_verify",
                "audit",
                "--upstream",
                store.server.base,
                "--listen",
                f"127.0.0.1:{port}",
                "--contract",
                str(contract),
                "--params",
                str(params),
                "--fault",
                "no_persist",
                "--wrong-record",
                "code=someone-else",
                "--runs",
                "3",
                "--success-regex",
                "^saved",
                "--",
                sys.executable,
                "-m",
                "openadapt_verify.audit_demo",
                "naive",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        store.close()
    assert run.returncode == 1, run.stderr
    assert "output matches '^saved'" in run.stdout
    assert "reported success on 2 of 2 bad saves" in run.stdout


def test_a_contract_that_reads_through_the_proxy_is_refused(tmp_path):
    port = _free_port()
    contract = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": f"http://127.0.0.1:{port}",
            "records_path": "/items",
            "records_key": "items",
            "records_complete": True,
        },
        "effects": quickstart.ReadPath.effects({"code": "code"}),
        "frame": {"identity_field": "id"},
    }
    with pytest.raises(ValueError, match="reads through the audit proxy"):
        audit.audit(
            contract,
            {"code": "x"},
            ["true"],
            upstream="http://127.0.0.1:1",
            listen=("127.0.0.1", port),
        )


def test_schedule_starts_each_round_clean():
    assert audit.schedule(["a", "b"], 5) == ["clean", "a", "b", "clean", "a"]
