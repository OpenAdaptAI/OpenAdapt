"""A lens for an app whose every call is one RPC endpoint (#2390).

The real-app benchmark's held-out ERP sends every read and write as a
JSON-RPC ``POST`` to one path. A GET template can't read a record back, so
every update stopped before the agent acted, and induction found nothing.

Three general pieces, none naming an app:

- ``EndpointTemplate.body_template`` / ``record_path`` / ``error_path``: a
  read or write whose arguments travel in the body, whose record sits at a
  path such as ``result.0``, and whose failure arrives as a 2xx with an
  ``error`` member.
- RPC induction from effects: a read names ids and fields and returns those
  records; a write names one id and values that a later read shows.
- The read-back reads every field the trace read for that model.

The CI part runs on a local server that speaks the same envelope. The live
part runs on a real app when ``OAFLOW_TX_LIVE_RPC_URL`` (the RPC endpoint,
for example ``http://127.0.0.1:9747/tryton/``), ``OAFLOW_TX_LIVE_RPC_USER``
and ``OAFLOW_TX_LIVE_RPC_PASSWORD`` are set, and is skipped otherwise.
Synthetic records only.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

import pytest

from openadapt_flow.tx.induce import InductionRefused, induce_lens_from_har
from openadapt_flow.tx.lens import (
    EntityLensClient,
    TransactionalCommitEngine,
    get_record_path,
    render_body_template,
)
from openadapt_flow.tx.types import TxTask
from openadapt_flow.tx.wire_tap import SessionWireTap, WirePolicy


def test_body_template_rendering() -> None:
    tpl = {"method": "m.read", "params": [["{id:int}"], ["a"], {"u": "{who}"}], "id": 3}
    assert render_body_template(tpl, {"id": "42", "who": "x"}) == {
        "method": "m.read",
        "params": [[42], ["a"], {"u": "x"}],
        "id": 3,
    }
    assert render_body_template(["{payload}"], {}, {"a": 1}) == [{"a": 1}]
    with pytest.raises(ValueError):
        render_body_template(["{id:int}"], {"id": "abc"})
    with pytest.raises(ValueError):
        render_body_template(["{payload}"], {})


def test_record_path() -> None:
    assert get_record_path({"result": [{"id": 1}]}, "result.0") == (True, {"id": 1})
    assert get_record_path({"result": []}, "result.0") == (False, None)


# ---------------------------------------------------------------------------
# A local server that speaks the envelope (CI)
# ---------------------------------------------------------------------------


class _RpcApp:
    def __init__(self) -> None:
        self.rows = {
            1: {"id": 1, "name": "Alpha", "code": "A-1", "stamp": 0},
            2: {"id": 2, "name": "Beta", "code": "B-2", "stamp": 0},
        }
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method, params = body["method"], body["params"]
                out: dict[str, Any] = {"id": body.get("id")}
                try:
                    if method == "model.thing.read":
                        out["result"] = [
                            {"id": i, **{f: app.rows[i][f] for f in params[1]}}
                            for i in params[0]
                        ]
                    elif method == "model.thing.write":
                        for i in params[0]:
                            for k, v in params[1].items():
                                if k not in app.rows[i]:
                                    raise KeyError(k)
                                app.rows[i][k] = v
                            app.rows[i]["stamp"] += 1
                        out["result"] = None
                    elif method == "model.other.search_read":
                        out["result"] = [{"id": 9, "label": "x"}]
                    else:
                        raise KeyError(method)
                except KeyError as exc:
                    out = {"id": body.get("id"), "error": [repr(exc), ""]}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def rpc_app() -> Iterator[_RpcApp]:
    app = _RpcApp()
    try:
        yield app
    finally:
        app.close()


def _rpc(base: str, path: str, method: str, params: list, auth: Optional[str] = None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(
        base + path,
        data=json.dumps({"id": 1, "method": method, "params": params}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _demo(
    base: str,
    path: str,
    model: str,
    rid: int,
    fields: list[str],
    change: dict,
    auth: Optional[str] = None,
    ctx: Any = None,
    noise: Optional[str] = None,
) -> dict:
    tail = [ctx] if ctx is not None else []
    with SessionWireTap(base, WirePolicy(max_mutations=None)) as tap:
        b = tap.base_url
        if noise:
            _rpc(b, path, noise, [[], ["id"], *tail], auth)
        _rpc(b, path, f"{model}.read", [[rid], fields, *tail], auth)
        _rpc(b, path, f"{model}.write", [[rid], change, *tail], auth)
        _rpc(b, path, f"{model}.read", [[rid], fields, *tail], auth)
        return tap.export_har()


def test_rpc_trace_induces_a_body_templated_lens(rpc_app) -> None:
    har = _demo(
        rpc_app.base,
        "/rpc/",
        "model.thing",
        1,
        ["name", "code", "stamp"],
        {"name": "Alpha 2"},
        noise="model.other.search_read",
    )
    lens = induce_lens_from_har(har)
    assert lens.entity_name == "thing"
    assert lens.get_one.method == "POST" and lens.get_one.path_template == "/rpc/"
    assert lens.get_one.body_template["method"] == "model.thing.read"
    assert lens.get_one.body_template["params"][0] == ["{id:int}"]
    assert lens.get_one.body_template["params"][1] == ["name", "code", "stamp"]
    assert lens.get_one.record_path == "result.0"
    assert lens.write_endpoint.body_template["params"][1] == "{payload}"
    # The server bumped ``stamp`` without being asked: volatile, by evidence.
    assert lens.volatile_fields == ("stamp",)


def test_rpc_lens_writes_and_reads_back(rpc_app, tmp_path) -> None:
    har = _demo(
        rpc_app.base,
        "/rpc/",
        "model.thing",
        1,
        ["name", "code", "stamp"],
        {"name": "Alpha 2"},
    )
    lens = induce_lens_from_har(har)
    _, rec, _ = EntityLensClient(lens).get_one("2")
    assert rec is not None and rec["code"] == "B-2"
    engine = TransactionalCommitEngine(lens, tmp_path, allow_unqualified_prototype=True)
    result = engine.execute(
        TxTask(
            task_id="t1",
            operation="update",
            entity_id="2",
            intended_delta={"code": "B-3"},
        )
    )
    assert result.outcome == "UNCERTAIN"
    assert set(result.reasons) == {"unqualified_observation_only"}
    assert rpc_app.rows[2]["code"] == "B-3" and rpc_app.rows[2]["name"] == "Beta"


def test_rpc_error_inside_2xx_is_a_failed_write(rpc_app, tmp_path) -> None:
    har = _demo(
        rpc_app.base, "/rpc/", "model.thing", 1, ["name", "code"], {"name": "A"}
    )
    lens = induce_lens_from_har(har)
    resp = EntityLensClient(lens).write("update", {"nosuch": 1}, entity_id="2")
    assert resp.status_code == 200 and not resp.ok and resp.call_error
    engine = TransactionalCommitEngine(lens, tmp_path, allow_unqualified_prototype=True)
    result = engine.execute(
        TxTask(
            task_id="t2",
            operation="update",
            entity_id="2",
            intended_delta={"nosuch": 1},
        )
    )
    assert result.outcome != "CONFIRMED"
    assert result.outcome in ("REFUTED", "UNCERTAIN", "BLOCKED_PRECOMMIT")


def test_rpc_trace_with_only_reads_is_refused(rpc_app) -> None:
    with SessionWireTap(rpc_app.base, WirePolicy(max_mutations=None)) as tap:
        _rpc(tap.base_url, "/rpc/", "model.thing.read", [[1], ["name"]])
        har = tap.export_har()
    with pytest.raises(InductionRefused, match="RPC trace"):
        induce_lens_from_har(har)


def test_rpc_write_without_a_confirming_read_is_refused(rpc_app) -> None:
    with SessionWireTap(rpc_app.base, WirePolicy(max_mutations=None)) as tap:
        _rpc(tap.base_url, "/rpc/", "model.thing.read", [[1], ["name"]])
        _rpc(tap.base_url, "/rpc/", "model.thing.write", [[1], {"name": "Z"}])
        har = tap.export_har()
    with pytest.raises(InductionRefused):
        induce_lens_from_har(har)


# ---------------------------------------------------------------------------
# Live application (lab only)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("OAFLOW_TX_LIVE_RPC_URL", "").strip()
USER = os.environ.get("OAFLOW_TX_LIVE_RPC_USER", "")
PASSWORD = os.environ.get("OAFLOW_TX_LIVE_RPC_PASSWORD", "")
live = pytest.mark.skipif(
    not (LIVE and USER and PASSWORD), reason="set OAFLOW_TX_LIVE_RPC_*"
)


@pytest.fixture(scope="module")
def live_app() -> dict[str, Any]:
    parts = urlsplit(LIVE)
    base, path = f"{parts.scheme}://{parts.netloc}", parts.path
    login = _rpc(base, path, "common.db.login", [USER, {"password": PASSWORD}, "en"])
    uid, session = login["result"]
    auth = "Session " + base64.b64encode(f"{USER}:{uid}:{session}".encode()).decode()
    tag = secrets.token_hex(3)
    made = _rpc(
        base,
        path,
        "model.party.party.create",
        [[{"name": f"Lens party {tag}", "code": f"LENS-{tag}"}], {}],
        auth,
    )
    return {
        "base": base,
        "path": path,
        "auth": auth,
        "pid": made["result"][0],
        "tag": tag,
    }


FIELDS = ["name", "code", "active", "lang", "write_date"]


@live
def test_live_rpc_lens_induces_writes_and_reads_back(live_app) -> None:
    b, path, auth, pid = (
        live_app["base"],
        live_app["path"],
        live_app["auth"],
        live_app["pid"],
    )
    har = _demo(
        b,
        path,
        "model.party.party",
        pid,
        FIELDS,
        {"name": f"Lens party {live_app['tag']} renamed"},
        auth=auth,
        ctx={},
        noise="model.ir.lang.search_read",
    )
    lens = induce_lens_from_har(har)
    assert lens.entity_name == "party"
    assert lens.get_one.body_template["method"] == "model.party.party.read"
    assert "write_date" in lens.volatile_fields
    engine = TransactionalCommitEngine(
        lens,
        tempfile.mkdtemp(prefix="lens-rpc-"),
        headers={"Authorization": auth},
        allow_unqualified_prototype=True,
    )
    new_code = f"LENS-{live_app['tag']}-2"
    result = engine.execute(
        TxTask(
            task_id="live-" + secrets.token_hex(3),
            operation="update",
            entity_id=str(pid),
            intended_delta={"code": new_code},
        )
    )
    truth = _rpc(b, path, "model.party.party.read", [[pid], ["name", "code"], {}], auth)
    assert truth["result"][0]["code"] == new_code
    assert truth["result"][0]["name"].endswith("renamed")
    assert result.outcome == "UNCERTAIN", result.reasons
    assert set(result.reasons) == {"unqualified_observation_only"}


@live
def test_live_rpc_error_is_not_a_success(live_app) -> None:
    b, path, auth, pid = (
        live_app["base"],
        live_app["path"],
        live_app["auth"],
        live_app["pid"],
    )
    har = _demo(
        b,
        path,
        "model.party.party",
        pid,
        FIELDS,
        {"name": f"Lens party {live_app['tag']} again"},
        auth=auth,
        ctx={},
    )
    lens = induce_lens_from_har(har)
    resp = EntityLensClient(lens, headers={"Authorization": auth}).write(
        "update", {"no_such_field": "x"}, entity_id=str(pid)
    )
    # The app answers 200 with an ``error`` member; the lens reads it as failed.
    assert resp.status_code == 200 and not resp.ok
