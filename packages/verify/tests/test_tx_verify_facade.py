"""End-to-end integration tests for openadapt_verify.tx, CLI subcommands, and network.jsonl bridge.

Self-contained so ``packages/verify`` clean-install CI runs without ``packages/flow/benchmark``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from openadapt_flow.tx import (
    EndpointTemplate,
    EntityLensSpec,
    load_or_create_signing_key,
    verify_tx_receipt,
)
from openadapt_flow.tx.induce import certify_lens_on_sandbox
from openadapt_flow.tx.types import CertifiedLensPack

from openadapt_verify.cli import main as verify_cli_main
from openadapt_verify.tx import tx_verified


@contextmanager
def _replace_task_server() -> Iterator[tuple[str, sqlite3.Connection]]:
    """Minimal multi-threaded SQLite server whose PUT replaces omitted fields (Vikunja class)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lock = threading.Lock()
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            description TEXT NOT NULL,
            assignee TEXT NOT NULL,
            budget_cents INTEGER NOT NULL,
            last_author TEXT NOT NULL,
            version INTEGER NOT NULL
        )
        """
    )
    for i in range(25):
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"TSK-{i:04d}",
                f"Task {i}",
                "open",
                f"Desc {i}",
                "ops-1",
                1000 + i,
                "seed",
                1,
            ),
        )
    conn.commit()
    counter = [100]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, code: int, body: Any, etag: str | None = None) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/tasks/"):
                tid = self.path.rsplit("/", 1)[-1]
                with lock:
                    row = conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (tid,)
                    ).fetchone()
                if row is None:
                    self._send(404, {"error": "not_found"})
                    return
                data = dict(row)
                self._send(200, data, etag=f'"{tid}:v{data["version"]}"')
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            actor = self.headers.get("X-Actor-Id") or "anonymous"
            with lock:
                counter[0] += 1
                tid = f"TSK-{counter[0]:04d}"
                conn.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        str(payload.get("title", "")),
                        str(payload.get("status", "open")),
                        str(payload.get("description", "")),
                        str(payload.get("assignee", "")),
                        int(payload.get("budget_cents", 0)),
                        actor,
                        1,
                    ),
                )
                conn.commit()
                row = dict(
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
                )
            self._send(201, row, etag=f'"{tid}:v1"')

        def do_PUT(self) -> None:  # noqa: N802
            tid = self.path.rsplit("/", 1)[-1]
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            actor = self.headers.get("X-Actor-Id") or "anonymous"
            with lock:
                cur = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (tid,)
                ).fetchone()
                if cur is None:
                    self._send(404, {"error": "not_found"})
                    return
                next_v = int(cur["version"]) + 1
                # Replace-style PUT: omitted fields get wiped to "" / 0!
                conn.execute(
                    """
                    UPDATE tasks
                    SET title=?, status=?, description=?, assignee=?, budget_cents=?, last_author=?, version=?
                    WHERE id=?
                    """,
                    (
                        str(payload.get("title", "")),
                        str(payload.get("status", "")),
                        str(payload.get("description", "")),
                        str(payload.get("assignee", "")),
                        int(payload.get("budget_cents", 0)),
                        actor,
                        next_v,
                        tid,
                    ),
                )
                conn.commit()
                row = dict(
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
                )
            self._send(200, row, etag=f'"{tid}:v{next_v}"')

        def do_DELETE(self) -> None:  # noqa: N802
            tid = self.path.rsplit("/", 1)[-1]
            with lock:
                conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
                conn.commit()
            self._send(200, {"deleted": tid})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", conn
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()


def test_end_to_end_network_capture_to_certified_pack_and_cli_tx_run(
    tmp_path: Path,
) -> None:
    """Full pipeline: network.jsonl -> tx-induce (sandbox certify) -> tx-run -> COSE receipt verify."""
    with _replace_task_server() as (base_url, conn):
        rec_dir = tmp_path / "rec"
        rec_dir.mkdir()
        target_id = "TSK-0001"
        initial = dict(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (target_id,)).fetchone()
        )

        network_lines = [
            json.dumps({"schema": "openadapt.network-capture/1", "exchanges": 3}),
            json.dumps(
                {
                    "seq": 0,
                    "t": 0.1,
                    "method": "POST",
                    "url": f"{base_url}/api/tasks",
                    "resource_type": "fetch",
                    "status": 201,
                    "request_type": "application/json",
                    "body": json.dumps(
                        {
                            "title": "Audit intake",
                            "status": "open",
                            "description": "Keep me intact",
                            "assignee": "ops-1",
                            "budget_cents": 5000,
                        }
                    ),
                    "response_type": "application/json",
                    "response_text": json.dumps(
                        {
                            "id": "TSK-0099",
                            "title": "Audit intake",
                            "status": "open",
                            "description": "Keep me intact",
                            "assignee": "ops-1",
                            "budget_cents": 5000,
                            "last_author": "user",
                            "version": 1,
                        }
                    ),
                }
            ),
            json.dumps(
                {
                    "seq": 1,
                    "t": 0.5,
                    "method": "GET",
                    "url": f"{base_url}/api/tasks/{target_id}",
                    "resource_type": "fetch",
                    "status": 200,
                    "response_type": "application/json",
                    "response_text": json.dumps(initial),
                }
            ),
            json.dumps(
                {
                    "seq": 2,
                    "t": 1.0,
                    "method": "PUT",
                    "url": f"{base_url}/api/tasks/{target_id}",
                    "resource_type": "fetch",
                    "status": 200,
                    "request_type": "application/json",
                    "body": json.dumps({**initial, "status": "in_progress"}),
                    "response_type": "application/json",
                    "response_text": json.dumps(
                        {**initial, "status": "in_progress", "version": 2}
                    ),
                }
            ),
        ]
        (rec_dir / "network.jsonl").write_text("\n".join(network_lines) + "\n")

        sample_rec_path = tmp_path / "sample_rec.json"
        sample_upd_path = tmp_path / "sample_upd.json"
        pack_path = tmp_path / "certified_pack.json"

        sample_rec_path.write_text(
            json.dumps(
                {
                    "title": "Probe Sample",
                    "status": "open",
                    "description": "Untouched body",
                    "assignee": "ops-2",
                    "budget_cents": 2500,
                }
            )
        )
        sample_upd_path.write_text(json.dumps({"status": "closed"}))

        rc = verify_cli_main(
            [
                "tx-induce",
                "--recording",
                str(rec_dir),
                "--sandbox-url",
                base_url,
                "--sample-record",
                str(sample_rec_path),
                "--sample-update",
                str(sample_upd_path),
                "--out",
                str(pack_path),
            ]
        )
        assert rc == 0
        pack_data = json.loads(pack_path.read_text())
        assert pack_data["certified"] is False
        assert pack_data["probes_passed"] is True
        assert pack_data["sensitivity"] == 1.0
        assert pack_data["specificity"] == 1.0
        assert pack_data["lens"]["write_mode"] == "replace"

        # The fixture's author field is declared metadata, not a guessed exception
        # inferred from an unrelated observed difference. Requalify that exact lens.
        original_pack = CertifiedLensPack.model_validate(pack_data)
        declared_lens = original_pack.lens.model_copy(
            update={
                "get_one": original_pack.lens.get_one.model_copy(
                    update={"author_field": "last_author"}
                ),
                "write_endpoint": original_pack.lens.write_endpoint.model_copy(
                    update={"author_field": "last_author"}
                ),
            }
        )
        reviewed_pack = certify_lens_on_sandbox(
            declared_lens,
            sample_record=json.loads(sample_rec_path.read_text()),
            sample_update=json.loads(sample_upd_path.read_text()),
        )
        assert not reviewed_pack.certified and reviewed_pack.probes_passed
        assert not reviewed_pack.probe_integrity_errors()
        assert reviewed_pack.admission_errors() == ("unqualified_probe_report",)
        pack_path.write_text(json.dumps(reviewed_pack.model_dump()))

        clean_id = "TSK-0005"
        before_row = dict(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (clean_id,)).fetchone()
        )
        task_path = tmp_path / "task.json"
        state_dir = tmp_path / "tx_state"
        task_path.write_text(
            json.dumps(
                {
                    "task_id": "cli-update-001",
                    "operation": "update",
                    "entity_id": clean_id,
                    "expected_identity": {"title": before_row["title"]},
                    "intended_delta": {"status": "closed"},
                }
            )
        )

        rc_run = verify_cli_main(
            [
                "tx-run",
                "--prototype-unqualified",
                "--lens",
                str(pack_path),
                "--task",
                str(task_path),
                "--state",
                str(state_dir),
            ]
        )
        assert rc_run == 3
        after_row = dict(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (clean_id,)).fetchone()
        )
        assert after_row["status"] == "closed"
        assert after_row["description"] == before_row["description"]
        assert after_row["assignee"] == before_row["assignee"]
        assert after_row["budget_cents"] == before_row["budget_cents"]

        cose_files = list(state_dir.rglob("tx-receipt.cose"))
        assert len(cose_files) == 1
        priv_key = load_or_create_signing_key(state_dir)
        verified_payload = verify_tx_receipt(
            cose_files[0],
            priv_key.public_key(),
            log_dir=state_dir / "log",
        )
        assert verified_payload["outcome"] == "UNCERTAIN"
        assert verified_payload["qualification_state"] == "unqualified"
        assert verified_payload["rows_read"] == 2

        # Rogue external agent attempts a collateral second write through OPENADAPT_TX_PROXY_URL
        rogue_target = "TSK-0010"
        rogue_collateral = "TSK-0011"
        collateral_before = dict(
            conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (rogue_collateral,)
            ).fetchone()
        )
        rogue_task_path = tmp_path / "rogue_task.json"
        rogue_task_path.write_text(
            json.dumps(
                {
                    "task_id": "cli-rogue-002",
                    "operation": "update",
                    "entity_id": rogue_target,
                    "intended_delta": {"status": "in_progress"},
                }
            )
        )
        rogue_script = tmp_path / "rogue_agent.py"
        rogue_script.write_text(
            "import json, os, urllib.request\n"
            "base = os.environ['OPENADAPT_TX_PROXY_URL']\n"
            f"r0 = json.loads(urllib.request.urlopen(base + '/api/tasks/{rogue_target}').read())\n"
            "r0['status'] = 'in_progress'\n"
            f"req1 = urllib.request.Request(base + '/api/tasks/{rogue_target}', data=json.dumps(r0).encode(), headers={{'Content-Type':'application/json'}}, method='PUT')\n"
            "urllib.request.urlopen(req1)\n"
            f"req2 = urllib.request.Request(base + '/api/tasks/{rogue_collateral}', data=b'{{\"status\":\"hacked\"}}', headers={{'Content-Type':'application/json'}}, method='PUT')\n"
            "try:\n"
            "    urllib.request.urlopen(req2)\n"
            "except Exception:\n"
            "    pass\n"
        )
        rc_rogue = verify_cli_main(
            [
                "tx-run",
                "--prototype-unqualified",
                "--lens",
                str(pack_path),
                "--task",
                str(rogue_task_path),
                "--state",
                str(state_dir),
                "--",
                sys.executable,
                str(rogue_script),
            ]
        )
        assert rc_rogue == 4
        collateral_after = dict(
            conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (rogue_collateral,)
            ).fetchone()
        )
        assert collateral_after == collateral_before


def test_tx_verified_decorator(tmp_path: Path) -> None:
    """Verify @tx_verified decorator wraps Python write callables cleanly."""
    with _replace_task_server() as (base_url, conn):
        lens = EntityLensSpec(
            entity_name="task",
            base_url=base_url,
            id_field="id",
            write_mode="replace",
            get_one=EndpointTemplate(method="GET", path_template="/api/tasks/{id}"),
            write_endpoint=EndpointTemplate(
                method="PUT",
                path_template="/api/tasks/{id}",
                author_field="last_author",
                version_field="version",
            ),
            volatile_fields=("version", "last_author"),
            expected_author="tx-agent",
        )
        tid = "TSK-0002"

        @tx_verified(
            lens,
            actor_id="tx-agent",
            workdir=tmp_path / "dec_state",
            allow_unqualified_prototype=True,
        )
        def update_task_status(entity_id: str, status: str) -> None:
            row = dict(
                conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (entity_id,)
                ).fetchone()
            )
            body = json.dumps({**row, "status": status}).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/tasks/{entity_id}",
                data=body,
                headers={"Content-Type": "application/json", "X-Actor-Id": "tx-agent"},
                method="PUT",
            )
            with urllib.request.urlopen(req):
                pass

        res = update_task_status(entity_id=tid, status="closed")
        assert res.outcome == "UNCERTAIN"
        assert "action_error:wire_evidence_missing" in res.reasons
        assert res.rows_read == 2
        assert os.path.isfile(res.receipt_path or "")


def test_facade_exports_enforcing_backend_and_unqualified_report_defaults():
    from openadapt_flow.tx import LensProbeReport as FlowReport
    from openadapt_flow.tx import SQLiteAtomicRowAuthority as FlowAuthority

    from openadapt_verify.tx import (
        LensProbeReport,
        SQLiteAtomicRowAuthority,
        load_lens_or_pack,
    )

    assert LensProbeReport is FlowReport and SQLiteAtomicRowAuthority is FlowAuthority
    lens = EntityLensSpec(
        entity_name="row",
        base_url="http://127.0.0.1:1",
        get_one=EndpointTemplate(method="GET", path_template="/rows/{id}"),
        write_endpoint=EndpointTemplate(method="PATCH", path_template="/rows/{id}"),
    )
    report = load_lens_or_pack(
        {"lens": lens.model_dump(), "sensitivity": 0, "specificity": 0}
    )
    assert isinstance(report, LensProbeReport) and report.certified is False
