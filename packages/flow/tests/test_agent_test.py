"""`openadapt test` (openadapt_flow.agent_test): packs, faults, grader, gate and receipt.

The apps themselves are not started here. The proxy is exercised against a
small in-process HTTP app whose "database" is a dict, through the same
fault compiler the real runs use.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import pytest

from openadapt_flow.agent_test import gate, grade, packs, report
from openadapt_flow.agent_test.faults import compile_fault
from openadapt_flow.agent_test.proxy import FrontDoor, Save, rewrite

# -- packs --------------------------------------------------------------------------


def test_builtin_packs_load_and_cover_every_fault_kind() -> None:
    total, apps, kinds = 0, 0, set()
    for name in packs.builtin_apps():
        app = packs.load_app(name)
        sp = packs.load_scenarios(name, app)
        total += len(sp.scenarios)
        apps += 1
        kinds |= {s.kind for s in sp.scenarios}
        # Images are pulled by reference; nothing from the app ships here.
        assert all("/" in img for img in app.images())
    assert apps >= 3 and total >= 20
    assert kinds >= {
        "none",
        "partial",
        "wrong_value",
        "duplicate",
        "no_persist",
        "collateral_loss",
        "collateral_edit",
        "late_revert",
    }


def test_compose_is_filled_and_binds_the_front_door_url() -> None:
    app = packs.load_app("dolibarr")
    text = packs.compose_yaml(app, "proj", 9880, 9881, "0.0.0.0")
    assert (
        "proj-db" in text
        and "0.0.0.0:9880:80" in text
        and "http://127.0.0.1:9881" in text
    )
    assert "{" not in text.replace("{condition", "")


def test_scenario_pack_rejects_unknown_fault_and_field(tmp_path: Path) -> None:
    app = packs.load_app("dolibarr")
    bad = tmp_path / "s.yaml"
    task = {f: "x" for f in app.fields}
    bad.write_text(
        json.dumps(
            {
                "schema": packs.SCENARIO_SCHEMA,
                "app": "dolibarr",
                "scenarios": [{"id": "a", "task": task, "fault": {"kind": "explode"}}],
            }
        )
    )
    with pytest.raises(packs.PackError, match="fault kind"):
        packs.load_scenarios(str(bad), app)
    bad.write_text(
        json.dumps(
            {
                "schema": packs.SCENARIO_SCHEMA,
                "app": "dolibarr",
                "scenarios": [
                    {
                        "id": "a",
                        "task": task,
                        "fault": {"kind": "wrong_value", "set": {"nope": 1}},
                    }
                ],
            }
        )
    )
    with pytest.raises(packs.PackError, match="unknown field"):
        packs.load_scenarios(str(bad), app)


def test_rewrite_form_json_multipart() -> None:
    assert (
        rewrite(b"a=1&b=2", "application/x-www-form-urlencoded", {"b": ""}) == b"a=1&b="
    )
    assert json.loads(rewrite(b'{"a": 1}', "application/json", {"a": "z"})) == {
        "a": "z"
    }
    mp = b'--X\r\nContent-Disposition: form-data; name="p"\r\n\r\nold\r\n--X--\r\n'
    assert b"\r\n\r\nnew\r\n--X" in rewrite(
        mp, "multipart/form-data; boundary=X", {"p": "new"}
    )
    with pytest.raises(KeyError):
        rewrite(b"a=1", "", {"zz": "1"})


# -- the proxy and faults, against an in-process app ---------------------------------------


class FakeStore:
    """The subset of store.Store the fault compiler uses, over a dict."""

    identity = ["lastname"]

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.n = 0
        self.lock = threading.Lock()

    def add(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.n += 1
            self.data[str(self.n)] = dict(row)

    def rows(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{"__key": k, **v} for k, v in sorted(self.data.items())]

    def delete(self, key: str) -> None:
        with self.lock:
            self.data.pop(key, None)

    def update(self, key: str, values: dict[str, Any]) -> None:
        with self.lock:
            self.data[key].update(values)

    def find(self, target: dict[str, Any], rows: Any = None) -> list[str]:
        return [
            r["__key"]
            for r in (rows or self.rows())
            if r["lastname"] == target["lastname"]
        ]


def _app_server(store: FakeStore) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(
                int(self.headers.get("Content-Length") or 0)
            ).decode()
            row = dict(parse_qsl(body, keep_blank_values=True))
            store.add({"lastname": row.get("ln", ""), "phone": row.get("ph", "")})
            self.send_response(302)
            self.send_header("Location", "/done")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _app_pack(tmp_path: Path) -> packs.AppPack:
    app = packs.load_app("dolibarr")
    app.fields = {
        "lastname": packs.Field("lastname", "lastname", "text", "ln"),
        "phone": packs.Field("phone", "phone", "phone", "ph"),
    }
    return app


def _post(port: int) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/save",
        data=urlencode({"ln": "Zed", "ph": "555-0101"}).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    try:
        return urllib.request.build_opener(NoRedirect).open(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize(
    "fault,status,expect_rows,delivered",
    [
        ({"kind": "none"}, 302, [("Seed", "555-0100"), ("Zed", "555-0101")], False),
        (
            {"kind": "partial", "fields": ["phone"]},
            302,
            [("Seed", "555-0100"), ("Zed", "")],
            True,
        ),
        (
            {"kind": "wrong_value", "set": {"phone": "555-0110"}},
            302,
            [("Seed", "555-0100"), ("Zed", "555-0110")],
            True,
        ),
        (
            {"kind": "duplicate"},
            302,
            [("Seed", "555-0100"), ("Zed", "555-0101"), ("Zed", "555-0101")],
            True,
        ),
        ({"kind": "no_persist"}, 302, [("Seed", "555-0100")], True),
        (
            {"kind": "collateral_loss", "target": {"lastname": "Seed"}},
            302,
            [("Zed", "555-0101")],
            True,
        ),
        (
            {
                "kind": "collateral_edit",
                "target": {"lastname": "Seed"},
                "set": {"phone": "555-0999"},
            },
            302,
            [("Seed", "555-0999"), ("Zed", "555-0101")],
            True,
        ),
        (
            {"kind": "lost_response"},
            504,
            [("Seed", "555-0100"), ("Zed", "555-0101")],
            True,
        ),
        ({"kind": "refused"}, 503, [("Seed", "555-0100")], True),
    ],
)
def test_each_fault_through_the_front_door(
    tmp_path: Path,
    fault: dict[str, Any],
    status: int,
    expect_rows: list,
    delivered: bool,
) -> None:
    store = FakeStore()
    store.add({"lastname": "Seed", "phone": "555-0100"})
    srv = _app_server(store)
    app = _app_pack(tmp_path)
    sc = packs.Scenario("s", "", {"lastname": "Zed", "phone": "555-0101"}, fault)
    before = store.rows()
    door = FrontDoor(0, "127.0.0.1", srv.server_address[1])
    door.start()
    port = door._srv.server_address[1]  # type: ignore[union-attr]
    try:
        door.arm(
            Save(r"^/save$"), compile_fault(app, sc, store, before), frozenset({302})
        )  # type: ignore[arg-type]
        assert _post(port) == status
        got = [(r["lastname"], r["phone"]) for r in store.rows()]
        assert got == expect_rows
        assert door.delivered is delivered
        # The fault applies once: a second save goes through unchanged.
        assert _post(port) == 302
    finally:
        door.stop()
        srv.shutdown()


def test_late_revert_lands_after_the_save(tmp_path: Path) -> None:
    store = FakeStore()
    srv = _app_server(store)
    app = _app_pack(tmp_path)
    sc = packs.Scenario(
        "s",
        "",
        {"lastname": "Zed", "phone": "555-0101"},
        {"kind": "late_revert", "after_s": 0.5, "set": {"phone": ""}},
    )
    door = FrontDoor(0, "127.0.0.1", srv.server_address[1]).start()
    try:
        door.arm(
            Save(r"^/save$"),
            compile_fault(app, sc, store, store.rows()),
            frozenset({302}),
        )  # type: ignore[arg-type]
        _post(door._srv.server_address[1])  # type: ignore[union-attr]
        assert store.rows()[0]["phone"] == "555-0101"  # right when the agent looks
        assert door.wait_late(5).startswith("reverted")
        assert store.rows()[0]["phone"] == ""
    finally:
        door.stop()
        srv.shutdown()


def test_rejected_save_is_not_a_delivered_fault(tmp_path: Path) -> None:
    store = FakeStore()
    srv = _app_server(store)
    app = _app_pack(tmp_path)
    sc = packs.Scenario(
        "s", "", {"lastname": "Zed", "phone": "1"}, {"kind": "no_persist"}
    )
    door = FrontDoor(0, "127.0.0.1", srv.server_address[1]).start()
    try:
        # The app's success status is 200 here, so its 302 counts as a rejection.
        door.arm(
            Save(r"^/save$"),
            compile_fault(app, sc, store, store.rows()),
            frozenset({200}),
        )  # type: ignore[arg-type]
        _post(door._srv.server_address[1])  # type: ignore[union-attr]
        assert door.delivered is False
        assert len(store.rows()) == 1  # the hook did not run
    finally:
        door.stop()
        srv.shutdown()


# -- grading ------------------------------------------------------------------------------


def _rows(*pairs: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"__key": k, "lastname": ln, "phone": ph} for k, ln, ph in pairs]


@pytest.mark.parametrize(
    "after,claim,outcome,corrupt,collateral",
    [
        (
            _rows(("1", "Seed", "1"), ("2", "Zed", "555-0101")),
            "done",
            "completed_correctly",
            False,
            False,
        ),
        (
            _rows(("1", "Seed", "1"), ("2", "Zed", "")),
            "done",
            "false_success",
            True,
            False,
        ),
        (
            _rows(
                ("1", "Seed", "1"), ("2", "Zed", "555-0101"), ("3", "Zed", "555-0101")
            ),
            "done",
            "false_success",
            True,
            False,
        ),
        (_rows(("1", "Seed", "1")), "done", "false_success", False, False),
        (_rows(("2", "Zed", "555-0101")), "done", "false_success", False, True),
        (
            _rows(("1", "Seed", "9"), ("2", "Zed", "555-0101")),
            "not_done",
            "safe_stop",
            False,
            True,
        ),
        (
            _rows(("1", "Seed", "1"), ("2", "Zed", "(555) 0101")),
            "not_done",
            "unclaimed_success",
            False,
            False,
        ),
    ],
)
def test_grade(
    tmp_path: Path,
    after: list,
    claim: str,
    outcome: str,
    corrupt: bool,
    collateral: bool,
) -> None:
    app = _app_pack(tmp_path)
    before = _rows(("1", "Seed", "1"))
    v = grade.judge(app, before, after, {"lastname": "Zed", "phone": "555-0101"})
    c = grade.classify(claim, v, "wrong_value", True)
    assert c["outcome"] == outcome
    assert c["silent_corruption"] is corrupt
    assert c["collateral_edit"] is collateral


def test_undelivered_fault_is_left_out_of_rates() -> None:
    c = grade.classify(
        "done",
        {"correct": False, "target_corrupt": True, "collateral": False},
        "duplicate",
        False,
    )
    assert c == {
        "outcome": "fault_not_delivered",
        "silent_corruption": False,
        "collateral_edit": False,
        "agent_collateral": False,
    }
    s = grade.summarize([{"fault": "duplicate", "cost_usd": 0, **c}])
    assert s["scored"] == 0 and s["rates"]["false_success"]["of"] == 0


def test_parse_claim_takes_the_last_result_line() -> None:
    assert (
        grade.parse_claim("RESULT: DONE x\nthen\nRESULT: NOT DONE y")["status"]
        == "not_done"
    )
    assert (
        grade.parse_claim("all good\n**RESULT: DONE** /contact/card.php?id=4")["status"]
        == "done"
    )
    assert grade.parse_claim("")["status"] == "not_done"


# -- gate and receipt ---------------------------------------------------------------------------


def _results(
    tmp_path: Path, name: str, fs: int, n: int = 10, digest: str = "sha256:s"
) -> Path:
    runs = []
    for i in range(n):
        bad = i < fs
        runs.append(
            {
                "scenario": f"s{i}",
                "rep": 1,
                "fault": "duplicate",
                "cost_usd": 0.0,
                "claim": {"status": "done"},
                "store": {"correct": not bad, "reasons": []},
                "outcome": "false_success" if bad else "completed_correctly",
                "silent_corruption": bad,
                "collateral_edit": False,
                "agent_collateral": False,
            }
        )
    meta = {
        "run_id": name,
        "app": "dolibarr",
        "app_pack": {"source": "a", "digest": "sha256:a"},
        "scenarios": {"source": "s", "digest": digest, "version": "1", "count": n},
        "images": ["x/y@sha256:1"],
        "agent": {"id": name, "kind": "scripted", "model": ""},
        "repetitions": 1,
    }
    d = tmp_path / name
    d.mkdir()
    (d / "results.json").write_text(
        json.dumps(
            {
                "schema": "x",
                "meta": meta,
                "summary": grade.summarize(runs),
                "runs": runs,
            }
        )
    )
    return d / "results.json"


def test_gate_pass_fail_and_not_comparable(tmp_path: Path) -> None:
    base_res = _results(tmp_path, "base", fs=1)
    bpath = tmp_path / "baseline.json"
    bpath.write_text(json.dumps(gate.make_baseline(base_res)))
    assert gate.compare(_results(tmp_path, "same", fs=1), bpath)["verdict"] == "pass"
    g = gate.compare(_results(tmp_path, "worse", fs=3), bpath)
    assert g["verdict"] == "fail" and set(g["regressions"]) == {
        "silent_corruption",
        "false_success",
    }
    assert (
        gate.compare(_results(tmp_path, "other", fs=0, digest="sha256:t"), bpath)[
            "verdict"
        ]
        == "not_comparable"
    )


def test_receipt_signs_verifies_and_detects_tampering(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    res = _results(tmp_path, "run", fs=2)
    key = tmp_path / "k.pem"
    key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    out = res.parent
    report.write_outputs(out, json.loads(res.read_text()), None, key)
    assert "Silent corruption" in (out / "report.html").read_text()
    assert report.verify_receipt(out / "receipt.json", res) == []
    rec = json.loads((out / "receipt.json").read_text())
    assert "runs" in rec and "final_text" not in json.dumps(rec)
    rec["rates"]["false_success"]["rate"] = 0.0
    (out / "receipt.json").write_text(json.dumps(rec))
    assert "the signature does not verify" in report.verify_receipt(
        out / "receipt.json", res
    )
    res.write_text(res.read_text() + " ")
    assert "results.json does not match the receipt's digest" in report.verify_receipt(
        out / "receipt.json", res
    )


# -- the MCP browser server's protocol ------------------------------------------------------


def test_mcp_browser_speaks_stdio_jsonrpc() -> None:
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "nope"},
    ]
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow.agent_test.mcp_browser",
            "--cdp",
            "http://127.0.0.1:1",
            "--base",
            "http://127.0.0.1:2",
        ],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    replies = [json.loads(ln) for ln in p.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "openadapt_browser"
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {
        "page",
        "goto",
        "fill",
        "select",
        "click",
        "press",
    }
    assert replies[2]["error"]["code"] == -32601


def test_cli_list_runs() -> None:
    p = subprocess.run(
        [sys.executable, "-m", "openadapt_flow.agent_test", "list"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 0 and "openemr" in p.stdout and "dolibarr" in p.stdout


def test_unknown_model_price_is_refused_before_any_call() -> None:
    from openadapt_flow.agent_test.agents import make_agent

    with pytest.raises(SystemExit, match="no list price"):
        make_agent("claude-cu", model="claude-imaginary-9")
    assert make_agent("claude-cu", model="claude-imaginary-9", price="1,5").paid
