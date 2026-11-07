from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch, request):
    for name in ("SEAL_HOME", "RECEIPT_HOME", "LEDGER_DIR", "VERIFIER_STATE_DIR"):
        monkeypatch.setenv("OPENADAPT_" + name, str(tmp_path / name.lower()))
    for name in ("KEY", "KEY_FILE", "KEY_PASSPHRASE", "PKCS11_MODULE", "PKCS11_PIN"):
        monkeypatch.delenv("OPENADAPT_SEAL_" + name, raising=False)
    # Mirror the canonical verifier fixture: no ambient host secret or key
    # selector may be reached, including by the CLI subprocesses below.
    frame_secret = secrets.token_hex(32)
    monkeypatch.setenv("OPENADAPT_FRAME_SECRET", frame_secret)
    monkeypatch.delenv("OPENADAPT_FRAME_SECRET_FILE", raising=False)
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST_HOME", str(tmp_path / "at-rest"))
    monkeypatch.setenv(
        "OPENADAPT_DURABLE_AUTHORITY_DB", str(tmp_path / "authority.sqlite")
    )
    for name in (
        "OPENADAPT_FLOW_AT_REST",
        "OPENADAPT_BUNDLE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def configured():
        # Closed failure text: never put an environment value in diagnostics.
        if (
            os.environ.get("OPENADAPT_FRAME_SECRET") != frame_secret
            or "OPENADAPT_FRAME_SECRET_FILE" in os.environ
        ):
            raise AssertionError(
                "explicit synthetic frame-secret configuration required"
            )
        if os.environ.get("OPENADAPT_FLOW_AT_REST_HOME") != str(tmp_path / "at-rest"):
            raise AssertionError("owned synthetic at-rest home required")
        if os.environ.get("OPENADAPT_DURABLE_AUTHORITY_DB") != str(
            tmp_path / "authority.sqlite"
        ):
            raise AssertionError("owned synthetic authority path required")

    configured()  # before any test capture or inherited CLI environment
    from openadapt_flow.runtime.effects import db_snapshot

    original = db_snapshot._host_secret
    calls = []

    def synthetic_host_secret():
        configured()  # fail before the canonical code could take a file fallback
        calls.append(True)
        return original()

    monkeypatch.setattr(db_snapshot, "_host_secret", synthetic_host_secret)
    request.node.user_properties.append(
        (
            "credential_isolation",
            "explicit synthetic frame secret; file fallback disabled; owned homes",
        )
    )
    monkeypatch.setenv("OPENADAPT_LOCAL_SEAL", "on")
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    yield
    request.node.user_properties.append(("guarded_frame_secret_calls", str(len(calls))))


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.mode = "correct"
        self.posts = 0
        self.unreadable = False
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE item (id INTEGER PRIMARY KEY, code TEXT, quantity TEXT)"
            )

    def records(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM item ORDER BY id")]

    def contract(self, kind="sql"):
        verifier = (
            {
                "kind": "sql",
                "sqlite_database": str(self.path),
                "sql_query": "SELECT * FROM item",
            }
            if kind == "sql"
            else {
                "kind": "rest",
                "base_url": self.base,
                "records_path": "/records",
                "records_key": "records",
            }
        )
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": verifier,
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "code": {"param": "code"},
                        "quantity": {"param": "quantity"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 0.2,
                }
            ],
            "frame": {"identity_field": "id"},
        }

    def save(self, values):
        reply = httpx.post(self.base + "/items", json=values)
        reply.raise_for_status()
        return reply.text


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "store.sqlite")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if store.unreadable:
                self.reply(503, {"error": "unavailable"})
            else:
                self.reply(200, {"records": store.records()})

        def do_POST(self):
            values = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            store.posts += 1
            if store.mode != "skip":
                quantity = "wrong" if store.mode == "wrong" else values["quantity"]
                with sqlite3.connect(store.path) as db:
                    db.execute(
                        "INSERT INTO item(code, quantity) VALUES (?, ?)",
                        (values["code"], quantity),
                    )
                    if store.mode == "duplicate":
                        db.execute(
                            "INSERT INTO item(code, quantity) VALUES (?, ?)",
                            (values["code"], quantity),
                        )
                    if store.mode == "collateral":
                        db.execute(
                            "INSERT INTO item(code, quantity) VALUES ('unexpected', '9')"
                        )
            # A successful writer response is deliberately independent of
            # whether the requested values were actually persisted.
            self.reply(200, {"claimed": "saved"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    store.base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
