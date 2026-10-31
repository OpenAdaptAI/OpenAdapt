"""Shadow mode: a pass-through proxy that checks each write and never blocks.

An HTTP app writes to SQLite behind the shadow; the app can break a write
the way a tool does. The real verifier (whole-database frame) judges it.
Records are made up.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import shadow as shadow_mod
from openadapt_flow.shadow import Route, Shadow
from openadapt_flow.shadow_report import render_html, write_report
from openadapt_flow.verifier import Contract


@pytest.fixture(autouse=True)
def _homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


class App:
    """A small app: POST /api/patients saves a row; ``fault`` breaks it."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.fault = ""
        self.received: list[bytes] = []
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._reply(200, b'{"ok": true}')

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                app.received.append(body)
                doc = json.loads(body)
                con = sqlite3.connect(app.db)
                fname = "Eda" if app.fault == "wrong_value" else doc["first"]
                row = (fname, doc["last"], doc["email"])
                con.execute(
                    "INSERT INTO patient (fname, lname, email) VALUES (?,?,?)", row
                )
                if app.fault == "duplicate":
                    con.execute(
                        "INSERT INTO patient (fname, lname, email) VALUES (?,?,?)", row
                    )
                if app.fault == "side_write":
                    con.execute("UPDATE patient SET phone='0' WHERE id=1")
                con.commit()
                con.close()
                self._reply(201, b'{"saved": true}', extra=("X-App", "yes"))

            def _reply(self, code: int, body: bytes, extra: tuple = ()) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if extra:
                    self.send_header(*extra)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT, "
        "email TEXT, phone TEXT);"
        "INSERT INTO patient VALUES (1, 'Wendell', 'Prior', 'w@example.test', '1');"
    )
    con.commit()
    con.close()
    return db


def _contract(db: Path) -> Contract:
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM patient",
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "fields": {"fname": "fname", "lname": "lname", "email": "email"},
            },
            "frame": {
                "identity_field": "id",
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "views": {"patient": ""},
                    "new_row_fields": {"patient": ["id", "phone"]},
                },
            },
        }
    )


BODY = {"first": "Ada", "last": "Quill", "email": "ada@example.test"}


def _shadow(tmp_path: Path, app: App, db: Path, **kw: Any) -> Shadow:
    route = Route(
        __import__("re").compile(r"POST /api/patients"),
        _contract(db),
        {"fname": "first", "lname": "last", "email": "email"},
        "patient",
    )
    return Shadow(
        upstream=app.url,
        mode="sql",
        routes=[route],
        out=tmp_path / "out",
        quiet_s=60,
        **kw,
    ).start()


def _post(url: str, body: Any, path: str = "/api/patients") -> tuple[int, dict, bytes]:
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    out = resp.status, dict(resp.getheaders()), resp.read()
    conn.close()
    return out


def _run(tmp_path: Path, fault: str = "") -> tuple[dict[str, Any], App, Shadow]:
    db = _db(tmp_path)
    app = App(db)
    app.fault = fault
    sh = _shadow(tmp_path, app, db)
    status, headers, body = _post(sh.url, BODY)
    assert status == 201 and body == b'{"saved": true}' and headers["X-App"] == "yes"
    sh.stop()
    return sh.results(), app, sh


def test_a_correct_write_is_confirmed_and_passes_through_unchanged(
    tmp_path: Path,
) -> None:
    doc, app, _ = _run(tmp_path)
    assert doc["counts"]["CONFIRMED"] == 1 and doc["writes_seen"] == 1
    assert app.received == [json.dumps(BODY).encode()]
    (item,) = doc["items"]
    assert item["sent"] == {
        "fname": "Ada",
        "lname": "Quill",
        "email": "ada@example.test",
    }


@pytest.mark.parametrize("fault", ["wrong_value", "duplicate", "side_write"])
def test_a_broken_write_is_refuted_and_still_goes_through(
    tmp_path: Path, fault: str
) -> None:
    doc, app, _ = _run(tmp_path, fault)
    assert doc["counts"]["REFUTED"] == 1, doc["items"][0]["reasons"]
    assert len(app.received) == 1  # the shadow added no write
    page = render_html(doc)
    assert "Landed wrong (1)" in page


def test_an_unreadable_store_is_uncertain_and_the_write_goes_on(tmp_path: Path) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db)
    sh.routes[0].contract.config.verifier.sqlite_database = str(tmp_path / "gone" / "x")
    sh.routes[0].contract.config.frame.database.read["sqlite_database"] = str(
        tmp_path / "gone" / "x"
    )
    assert _post(sh.url, BODY)[0] == 201
    sh.stop()
    doc = sh.results()
    assert doc["counts"] == {"REFUTED": 0, "UNCERTAIN": 1, "CONFIRMED": 0, "PENDING": 0}
    assert len(app.received) == 1


def test_a_slow_baseline_holds_the_write_no_longer_than_hold_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db, hold_ms=200)
    real = sh._sql_baseline

    def slow(*a: Any) -> Any:
        time.sleep(1.5)
        return real(*a)

    monkeypatch.setattr(sh, "_sql_baseline", slow)
    t0 = time.perf_counter()
    assert _post(sh.url, BODY)[0] == 201
    assert time.perf_counter() - t0 < 1.0
    sh.stop()
    doc = sh.results()
    assert doc["counts"]["UNCERTAIN"] == 1
    assert "wasn't read within 200 ms" in doc["items"][0]["reasons"][0]


def test_a_write_with_no_route_is_uncertain_not_hidden(tmp_path: Path) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db)
    assert _post(sh.url, BODY, path="/api/other")[0] == 201
    sh.stop()
    doc = sh.results()
    assert doc["counts"]["UNCERTAIN"] == 1
    assert doc["items"][0]["reasons"] == ["no route matches this write"]


def test_a_shadow_failure_never_stops_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db)

    def boom(*_: Any) -> Any:
        raise RuntimeError("shadow bug")

    monkeypatch.setattr(sh, "_observe", boom)
    assert _post(sh.url, BODY)[0] == 201
    sh.stop()
    assert len(app.received) == 1 and sh.errors


def test_reads_pass_through_and_the_report_is_written(tmp_path: Path) -> None:
    doc, app, sh = _run(tmp_path)
    page = write_report(doc, tmp_path / "rep", setup={"minutes": 3, "decisions": ["a"]})
    assert json.loads((tmp_path / "rep" / "report.json").read_text())["counts"]
    assert "1 decisions" in page.read_text() or "decisions" in page.read_text()


def test_the_shadow_listens_on_loopback_only(tmp_path: Path) -> None:
    with pytest.raises(shadow_mod.ShadowConfigError):
        Shadow(
            upstream="http://x",
            mode="sql",
            routes=[object()],
            out=tmp_path,
            listen=("0.0.0.0", 0),
        )


def test_a_write_the_route_doesnt_fit_is_uncertain_not_refuted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db)
    sh.routes[0].params["lname"] = "surname"  # a key this write doesn't send
    assert _post(sh.url, BODY)[0] == 201
    sh.stop()
    (item,) = sh.results()["items"]
    assert item["verdict"] == "UNCERTAIN" and len(app.received) == 1
    assert "doesn't give lname (at surname)" in item["reasons"][0]


def test_a_late_write_after_a_sql_confirmation_is_refuted_on_the_recheck(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    app = App(db)
    sh = _shadow(tmp_path, app, db)
    sh.routes[0].contract.config.frame.database.recheck_after_s = 1
    assert _post(sh.url, BODY)[0] == 201
    sh.flush()
    (ep,) = sh.episodes
    assert ep.verdict == "CONFIRMED" and ep.recheck_at
    con = sqlite3.connect(db)  # a background job, a second later
    con.execute("UPDATE patient SET phone='9' WHERE id=1")
    con.commit()
    con.close()
    sh.run_rechecks(now=True)
    sh.stop()
    assert ep.late_verdict == "REFUTED" and sh.results()["late_refuted"] == 1
