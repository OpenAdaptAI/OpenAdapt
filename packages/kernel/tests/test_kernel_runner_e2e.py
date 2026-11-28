"""End-to-end tests for openadapt-kernel run against a real local HTTP persistence server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

from openadapt_kernel.cli import main


def _start_portal_server(drop_writes: bool = False) -> tuple[ThreadingHTTPServer, str, List[Dict[str, Any]]]:
    saved_records: List[Dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            rows = "".join(
                f"<li>REF={r['ref']} POLICY={r['policy_number']} ZIP={r['postal_code']}</li>"
                for r in saved_records
            )
            html = f"""<!doctype html>
<html><body>
  <form id="qform" onsubmit="event.preventDefault(); doSave();">
    <label for="policy_number">Policy Number</label>
    <input id="policy_number" name="policy_number" type="text" />
    <label for="postal_code">Postal Code</label>
    <input id="postal_code" name="postal_code" type="text" />
    <label for="state_code">State</label>
    <input id="state_code" name="state_code" type="text" />
    <button type="submit" data-testid="submit-btn">Submit</button>
  </form>
  <div id="banner"></div>
  <ul id="persisted-list">{rows}</ul>
  <script>
    async function doSave() {{
      const payload = {{
        policy_number: document.getElementById('policy_number').value,
        postal_code: document.getElementById('postal_code').value,
        state_code: document.getElementById('state_code').value,
      }};
      await fetch('/api/submit', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload),
      }});
      document.getElementById('banner').innerText = 'Saved successfully!';
    }}
  </script>
</body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw) if raw else {}
            if not drop_writes:
                data["ref"] = f"QT-{len(saved_records) + 1}"
                saved_records.append(data)
            resp = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/", saved_records


def test_cli_run_draft_autopilot_and_dropped_write_refusal(tmp_path: Path) -> None:
    pack_path = tmp_path / "pack.json"
    pack_path.write_text(
        json.dumps(
            {
                "pack_id": "carrier-quote-v1",
                "portal_origin": "http://127.0.0.1",
                "variable_fields": ["policy_number", "postal_code"],
                "static_defaults": {"state_code": "OK"},
                "provenance_map": {"policy_number": "src_policy", "postal_code": "src_zip"},
                "readback_url_pattern": "http://127.0.0.1/",
                "stage": "VERIFIED_AUTOPILOT",
                "shadow_passes": 10,
                "edge_branches": {},
            }
        ),
        encoding="utf-8",
    )
    dossier_path = tmp_path / "dossier.json"
    dossier_path.write_text(json.dumps({"src_policy": "POL-777", "src_zip": "74103"}), encoding="utf-8")

    priv_path = tmp_path / "priv.pem"
    pub_path = tmp_path / "pub.pem"
    assert main(["keygen", "--priv", str(priv_path), "--pub", str(pub_path)]) == 0

    # 1. Healthy server: Draft-to-Commit stages form without POST, Autopilot commits + verifies + signs
    srv_ok, url_ok, records_ok = _start_portal_server(drop_writes=False)
    try:
        rc_draft = main(["run", "--pack", str(pack_path), "--dossier", str(dossier_path), "--mode", "draft", "--url", url_ok])
        assert rc_draft == 0
        assert len(records_ok) == 0  # Draft mode must NEVER submit

        receipt_path = tmp_path / "receipt.json"
        rc_auto = main(
            [
                "run",
                "--pack",
                str(pack_path),
                "--dossier",
                str(dossier_path),
                "--mode",
                "autopilot",
                "--url",
                url_ok,
                "--commit-selector",
                '[data-testid="submit-btn"]',
                "--priv",
                str(priv_path),
                "--receipt-out",
                str(receipt_path),
                "--episode-id",
                "ep-http-ok",
            ]
        )
        assert rc_auto == 0
        assert len(records_ok) == 1
        assert receipt_path.exists()
        assert main(["verify-receipt", "--receipt", str(receipt_path), "--pub", str(pub_path)]) == 0
    finally:
        srv_ok.shutdown()

    # 2. Dropped-write server (shows 'Saved successfully!' banner while dropping DB write):
    # Fresh reload refutes persistence -> MUST return exit 1 (ESCALATED) and sign non-VERIFIED receipt.
    srv_bad, url_bad, records_bad = _start_portal_server(drop_writes=True)
    try:
        bad_receipt = tmp_path / "bad_receipt.json"
        rc_bad = main(
            [
                "run",
                "--pack",
                str(pack_path),
                "--dossier",
                str(dossier_path),
                "--mode",
                "autopilot",
                "--url",
                url_bad,
                "--commit-selector",
                '[data-testid="submit-btn"]',
                "--priv",
                str(priv_path),
                "--receipt-out",
                str(bad_receipt),
                "--episode-id",
                "ep-http-bad",
            ]
        )
        assert rc_bad == 1
        assert len(records_bad) == 0
        assert bad_receipt.exists()
        assert main(["verify-receipt", "--receipt", str(bad_receipt), "--pub", str(pub_path)]) == 0
        bad_data = json.loads(bad_receipt.read_text(encoding="utf-8"))
        assert bad_data["payload"]["outcome"] == "ESCALATED"
    finally:
        srv_bad.shutdown()
