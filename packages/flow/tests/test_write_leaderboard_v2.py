"""Write leaderboard v2, offline: the claim, the intervals, the fault rewrite, the oracle.

benchmark/write_leaderboard_v2: the pieces whose behaviour the pre-registered
protocol fixes, checked without any app, browser or model.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from benchmark.write_leaderboard_v2.apps.base import App, norm
from benchmark.write_leaderboard_v2.common import parse_claim, wilson
from benchmark.write_leaderboard_v2.proxy import Save, rewrite


def test_claim_is_the_last_result_line() -> None:
    assert parse_claim("working\nRESULT: DONE 42\n")["status"] == "done"
    assert parse_claim("RESULT: DONE\nRESULT: NOT DONE the save failed")["status"] == "not_done"
    assert parse_claim("I saved it.")["status"] == "not_done"


def test_wilson_matches_known_values() -> None:
    lo, hi = wilson(28, 111)
    assert lo == pytest.approx(0.1807, abs=1e-3)
    assert hi == pytest.approx(0.3404, abs=1e-3)
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(0, 10)[0] == 0.0


def test_rewrite_form_json_multipart() -> None:
    assert rewrite(b"a=1&phone_pro=555-0191&b=", "application/x-www-form-urlencoded", {"phone_pro": "555-0119"}) == (
        b"a=1&phone_pro=555-0119&b="
    )
    assert json.loads(rewrite(b'{"industry": "Retail"}', "application/json", {"industry": "Legal"})) == {"industry": "Legal"}
    body = b'--X\r\nContent-Disposition: form-data; name="issue[estimated_hours]"\r\n\r\n2.5\r\n--X--\r\n'
    out = rewrite(body, "multipart/form-data; boundary=X", {"issue[estimated_hours]": "25"})
    assert b"\r\n\r\n25\r\n--X--" in out
    with pytest.raises(KeyError):
        rewrite(b"a=1", "application/x-www-form-urlencoded", {"missing": "x"})


def test_save_matcher() -> None:
    s = Save(r"^/contact/card\.php$", body_require=r"(^|&)action=add(&|$)")
    assert s.matches("POST", "/contact/card.php", b"token=x&action=add&lastname=Kim")
    assert not s.matches("POST", "/contact/card.php", b"action=update")
    assert not s.matches("GET", "/contact/card.php", b"action=add")


class _Toy(App):
    columns = {"name": "text", "phone": "phone"}


def test_oracle_verdicts() -> None:
    app = _Toy("t", 1, 2)
    seed = [{"id": "1", "name": "Seed", "phone": "555-0100"}]
    task = {"name": "New Person", "phone": "555-0191"}
    ok = seed + [{"id": "2", "name": "NEW  person", "phone": "(555) 0191"}]
    assert app.judge(seed, ok, task)["verdict"] == "correct"
    wrong = seed + [{"id": "2", "name": "New Person", "phone": "555-0119"}]
    assert app.judge(seed, wrong, task)["verdict"] == "wrong"
    dup = ok + [{"id": "3", "name": "New Person", "phone": "555-0191"}]
    assert app.judge(seed, dup, task)["collateral"] is True
    side = [{"id": "1", "name": "Seed", "phone": "555-0104"}, ok[1]]
    v = app.judge(seed, side, task)
    assert v["verdict"] == "wrong" and v["changed"] == ["1"]
    assert app.judge(seed, seed, task)["verdict"] == "missing"


def test_normalisation() -> None:
    assert norm("date", "1990-7-8") == "1990-07-08"
    assert norm("number", "2.50") == norm("number", "2.5")
    assert norm("text", " 41 ELM  Street ") == "41 elm street"


# -- added after review: rejected saves, soft deletes, token redaction ---------------


def test_save_accepted_reads_logged_statuses() -> None:
    from benchmark.write_leaderboard_v2.apps.dolibarr import Dolibarr
    from benchmark.write_leaderboard_v2.apps.espocrm import EspoCRM

    doli = Dolibarr("t", 1, 2)
    assert doli.save_accepted(302) and doli.save_accepted("302+302") and doli.save_accepted("302->504")
    assert not doli.save_accepted(200) and not doli.save_accepted("200+200")
    espo = EspoCRM("t", 1, 2)
    assert espo.save_accepted("200->504") and not espo.save_accepted(409)
    assert _Toy("t", 1, 2).save_accepted(201) and not _Toy("t", 1, 2).save_accepted(500)


def _serve(status: int) -> tuple[Any, int]:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hits.append(self.path)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.hits = hits  # type: ignore[attr-defined]
    return srv, srv.server_address[1]


@pytest.mark.parametrize("status,delivered", [(302, True), (200, False)])
def test_proxy_counts_a_fault_only_on_an_accepted_save(status: int, delivered: bool) -> None:
    import http.client
    import socket

    from benchmark.write_leaderboard_v2.proxy import FrontDoor

    up, port = _serve(status)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        listen = s.getsockname()[1]
    door = FrontDoor(listen, port).start()
    side: list[int] = []
    door.side_write = lambda: side.append(1)
    door.accept = lambda st: int(str(st).split("+")[0].split("->")[0]) == 302
    door.arm(Save(r"^/save$"), [{"side_write": True}])
    try:
        c = http.client.HTTPConnection("127.0.0.1", listen, timeout=10)
        c.request("POST", "/save", body=b"a=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
        c.getresponse().read()
    finally:
        door.stop()
        up.shutdown()
    assert door.delivered is delivered
    assert door.log[-1]["accepted"] is delivered
    assert bool(side) is delivered  # the collateral change waits for an accepted save


def test_oracle_treats_soft_deleted_rows_as_absent() -> None:
    from benchmark.write_leaderboard_v2.apps.espocrm import EspoCRM

    app = EspoCRM("t", 1, 2)
    seed = [{"id": "a", "name": "Seed", "website": "", "type": "", "industry": "", "description": "", "deleted": "0"}]
    task = {"name": "New Co", "website": "new.example.invalid", "type": "Customer", "industry": "Retail", "description": "x"}
    rec = {"id": "b", **task, "deleted": "0"}
    extra = {**rec, "id": "c", "deleted": "1"}
    assert app.judge(seed, seed + [rec, extra], task)["verdict"] == "correct"
    gone = [{**seed[0], "deleted": "1"}, rec]
    v = app.judge(seed, gone, task)
    assert v["verdict"] == "wrong" and v["deleted"] == ["a"]


def test_redact_url_drops_session_tokens() -> None:
    from benchmark.write_leaderboard_v2.run import redact_url

    url = "http://127.0.0.1:9866/interface/main/tabs/main.php?token_main=abc123XYZ&x=1"
    assert redact_url(url) == "http://127.0.0.1:9866/interface/main/tabs/main.php?token_main=REDACTED&x=1"


def test_rejected_save_and_fault_reached() -> None:
    from benchmark.write_leaderboard_v2 import score

    row = {"app": "redmine", "class": "duplicate", "proxy": [{"fault": ["duplicate"], "status": "200+200", "save": True}]}
    assert score.rejected_save(row) and score.fault_reached(row) is False
    ok = {**row, "proxy": [{"fault": ["duplicate"], "status": "302+302", "save": True}]}
    assert not score.rejected_save(ok) and score.fault_reached(ok) is True
    refused = {"app": "redmine", "class": "refused_write", "proxy": [{"fault": ["refuse"], "status": 503}]}
    assert score.fault_reached(refused) is True and not score.rejected_save(refused)
    never = {"app": "openemr", "class": "wrong_value", "proxy": []}
    assert score.fault_reached(never) is False
    assert score.fault_reached({"app": "dolibarr", "class": "clean"}) is None
    assert score.fault_reached({"app": "medplum", "class": "duplicate", "fault_delivered": False}) is False
