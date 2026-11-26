"""Real-boundary tests for SQLite RO, PDF stream filtering, multi-window popups, shadow-sync, and MCP server."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openadapt_kernel import (
    extract_pdf_visible_text,
    handle_jsonrpc_message,
    run_portal_pack,
    serve_stdio,
    verify_pdf_readback,
    verify_sqlite_ro,
)
from openadapt_kernel.cli import main


def test_sqlite_ro_oracle_verifies_and_refutes(tmp_path: Path) -> None:
    db_path = tmp_path / "clinic.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE claims (ref TEXT, member_id TEXT, code TEXT)")
    conn.execute("INSERT INTO claims VALUES ('CLM-901', 'MEM-442', 'D2740')")
    conn.commit()
    conn.close()

    ok = verify_sqlite_ro(
        db_path,
        "SELECT ref, member_id, code FROM claims WHERE member_id = ?",
        ("MEM-442",),
        {"member_id": "MEM-442", "code": "D2740"},
        ref_column="ref",
    )
    assert ok.verified is True
    assert ok.external_ref == "CLM-901"
    assert ok.to_proof().channel == "sql_read"

    missing = verify_sqlite_ro(
        db_path,
        "SELECT ref, member_id, code FROM claims WHERE member_id = ?",
        ("MEM-442",),
        {"member_id": "MEM-442", "code": "D9999"},
    )
    assert missing.verified is False
    assert missing.missing_fields == ["code"]


def test_pdf_stream_oracle_reads_flate_and_blocks_invisible_3tr_spoof(tmp_path: Path) -> None:
    # Real FlateDecode PDF stream containing visible text AND invisible 3 Tr spoofed text
    content_stream = (
        b"BT /F1 12 Tf 0 Tr (Confirmation REF=PA-7741 Member MEM-442 Code D2740) Tj "
        b"3 Tr (SPOOFED_CODE D9999) Tj 0 Tr (Approved) Tj ET"
    )
    compressed = zlib.compress(content_stream)
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream\nendobj\n%%EOF"
    pdf_path = tmp_path / "confirm.pdf"
    pdf_path.write_bytes(pdf_bytes)

    visible = extract_pdf_visible_text(pdf_bytes)
    assert "PA-7741" in visible
    assert "D2740" in visible
    assert "D9999" not in visible  # 3 Tr invisible text must be filtered out

    v_ok = verify_pdf_readback(pdf_path, {"member": "MEM-442", "code": "D2740"}, ref_pattern=r"REF=(PA-\d+)")
    assert v_ok.verified is True
    assert v_ok.external_ref == "PA-7741"

    v_spoof = verify_pdf_readback(pdf_path, {"code": "D9999"})
    assert v_spoof.verified is False


def test_runner_multi_window_child_popup_and_sqlite_cross_rail(tmp_path: Path) -> None:
    db_path = tmp_path / "quotes.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE quotes (ref TEXT, policy_number TEXT, replacement_cost TEXT)")
    conn.commit()
    conn.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/estimator":
                html = """<!doctype html>
<html><body>
  <label for="sqft">Square Feet</label>
  <input id="sqft" name="sqft" type="text" />
  <button id="apply-est" type="button" onclick="window.opener.postMessage({cost: '420000'}, '*'); window.close();">Apply</button>
</body></html>"""
            else:
                html = """<!doctype html>
<html><body>
  <form onsubmit="event.preventDefault(); saveAll();">
    <label for="policy_number">Policy Number</label>
    <input id="policy_number" name="policy_number" type="text" />
    <input id="replacement_cost" name="replacement_cost" type="text" value="" />
    <button id="open-est" type="button" onclick="window.open('/estimator', 'est', 'width=400,height=300')">Estimate</button>
    <button id="submit-all" type="submit">Submit</button>
  </form>
  <script>
    window.addEventListener('message', (e) => {
      if (e.data && e.data.cost) document.getElementById('replacement_cost').value = e.data.cost;
    });
    async function saveAll() {
      await fetch('/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          policy_number: document.getElementById('policy_number').value,
          replacement_cost: document.getElementById('replacement_cost').value,
        }),
      });
    }
  </script>
</body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
            data = json.loads(raw)
            c = sqlite3.connect(db_path)
            c.execute("INSERT INTO quotes VALUES (?, ?, ?)", ("QT-55", data["policy_number"], data["replacement_cost"]))
            c.commit()
            c.close()
            resp = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "popup-quote-v1",
                    "portal_origin": f"http://127.0.0.1:{port}",
                    "variable_fields": ["policy_number"],
                    "static_defaults": {},
                    "provenance_map": {"policy_number": "src_policy"},
                    "readback_url_pattern": f"http://127.0.0.1:{port}/",
                }
            ),
            encoding="utf-8",
        )
        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"src_policy": "POL-555"},
            mode="autopilot",
            url=f"http://127.0.0.1:{port}/",
            commit_selector="#submit-all",
            popup_trigger_selector="#open-est",
            popup_bindings={"sqft": "2400"},
            popup_submit_selector="#apply-est",
            sqlite_db_path=db_path,
            sqlite_query="SELECT ref, policy_number, replacement_cost FROM quotes",
            episode_id="ep-popup-1",
            headless=True,
        )
        assert res.outcome == "VERIFIED"
        assert res.staged_fields["policy_number"] == "POL-555"
        assert res.staged_fields["sqft"] == "2400"
        assert res.readback_verified is True
    finally:
        srv.shutdown()


def test_shadow_sync_and_stdio_mcp_server_roundtrip(tmp_path: Path) -> None:
    rec_root = tmp_path / "recordings"
    for idx, pol in enumerate(("POL-1", "POL-2"), start=1):
        d = rec_root / f"run_{idx}"
        d.mkdir(parents=True)
        (d / "dossier.json").write_text(json.dumps({"src_pol": pol}), encoding="utf-8")
        (d / "extension_evidence.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"kind": "input", "frame_origin": "https://p.example", "field": "policy", "value": pol}),
                    json.dumps({"kind": "input", "frame_origin": "https://p.example", "field": "state", "value": "ON"}),
                    json.dumps({"kind": "readback", "url": "https://p.example/ok", "text": f"Saved {pol} ON"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    pack_path = tmp_path / "synced_pack.json"
    assert main(["shadow-sync", "--dir", str(rec_root), "--pack-id", "synced-v1", "--out", str(pack_path)]) == 0
    assert json.loads(pack_path.read_text(encoding="utf-8"))["provenance_map"] == {"policy": "src_pol"}

    # Test stdio JSON-RPC 2.0 MCP server loop
    init_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp is not None and init_resp["result"]["serverInfo"]["name"] == "openadapt-kernel"

    req_stream = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "kernel_qualify_offline",
                    "arguments": {
                        "pack_path": str(pack_path),
                        "evidence_path": str(rec_root / "run_1" / "extension_evidence.jsonl"),
                        "pre_text": "Quote form before submission",
                    },
                },
            }
        )
        + "\n"
    )
    out_stream = io.StringIO()
    serve_stdio(stdin=req_stream, stdout=out_stream)
    lines = [json.loads(ln) for ln in out_stream.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert len(lines[0]["result"]["tools"]) == 4
    qual_payload = json.loads(lines[1]["result"]["content"][0]["text"])
    assert qual_payload["ok"] is True
    assert qual_payload["report"]["qualified"] is True
