"""End-to-end tests for unified run_portal_pack pipeline, egress-proxy daemon, and prepare-inbox CLI."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from openadapt_kernel.cli import main as cli_main
from openadapt_kernel.copilot import generate_ephemeral_ed25519_keypair, verify_receipt
from openadapt_kernel.egress_guard import LocalEgressGuard
from openadapt_kernel.prepare import PrepareCard, PrepareResumeResponse
from openadapt_kernel.registry import pull_verified_pack
from openadapt_kernel.runner import run_portal_pack


def _start_local_portal(form_html: str) -> tuple[ThreadingHTTPServer, str, List[Dict[str, str]]]:
    saved_records: List[Dict[str, str]] = []

    class _PortalHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            rendered = form_html.replace(
                "<!--SAVED-->",
                " ".join(f"{k}:{v}" for rec in saved_records for k, v in rec.items()),
            )
            body = rendered.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    saved_records.append({str(k): str(v) for k, v in data.items()})
            except json.JSONDecodeError:
                pass
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PortalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return server, f"http://127.0.0.1:{port}", saved_records


def _post_json(url: str, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_run_portal_pack_step0_prepare_resume_closure_and_ed25519_receipt(tmp_path: Path) -> None:
    """Step-0 PREPARE_ASK -> prepare_handler resume -> commit -> completion closure -> Ed25519 receipt."""
    html = """<!doctype html><html><body>
      <form id="f">
        <label for="member_id">Member ID</label>
        <input id="member_id" name="member_id" type="text" />
        <button id="save_btn" type="button">Save</button>
      </form>
      <div id="saved"><!--SAVED--></div>
      <script>
        document.getElementById('save_btn').addEventListener('click', async () => {
          const val = document.getElementById('member_id').value;
          await fetch('/submit', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({member_id: val})
          });
          document.getElementById('saved').innerText = 'member_id:' + val;
        });
      </script>
    </body></html>"""

    server, base_url, saved = _start_local_portal(html)
    try:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "unified-claim-pack",
                    "portal_origin": base_url,
                    "variable_fields": ["member_id"],
                    "static_defaults": {},
                    "provenance_map": {"member_id": "member_id"},
                    "stage": "VERIFIED_AUTOPILOT",
                }
            ),
            encoding="utf-8",
        )
        priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
        priv_path = tmp_path / "priv.pem"
        priv_path.write_bytes(priv_pem)
        receipt_out = tmp_path / "receipt.json"

        # 1. Step-0 REFUSE blocks with 0 writes.
        refused = run_portal_pack(
            pack_path=pack_path,
            dossier={"member_id": "MEM-7741"},
            mode="autopilot",
            url=base_url,
            commit_selector="#save_btn",
            policy_specs=[
                {"rule_id": "deny_suspended", "conditions": {"status": "suspended"}, "required_disposition": "DENY"}
            ],
            observed_facts={"status": "suspended"},
            task_disposition="APPROVE",
        )
        assert refused.outcome == "REFUSED"
        assert len(saved) == 0

        # 2. Step-0 PREPARE_ASK -> prepare_handler binds missing member_id -> full verified execution + receipt.
        seen_cards: List[PrepareCard] = []

        def operator_prepare_handler(card: PrepareCard) -> PrepareResumeResponse:
            seen_cards.append(card)
            return PrepareResumeResponse(
                card_id=card.card_id,
                operator_id="op_reviewer_1",
                parameter_values={"member_id": "MEM-7741"},
                confirmed_fields={"member_id": "MEM-7741"},
                slot_bindings={0: "member_id"},
                approve_all=False,
            )

        res = run_portal_pack(
            pack_path=pack_path,
            dossier={},
            mode="autopilot",
            url=base_url,
            commit_selector="#save_btn",
            private_key_path=priv_path,
            receipt_out=receipt_out,
            prepare_handler=operator_prepare_handler,
            source_texts={"intake": "Member MEM-7741 confirmed"},
            freetext_notes="Verified member MEM-7741 on intake",
        )
        assert len(seen_cards) == 1
        assert res.outcome == "VERIFIED"
        assert res.readback_verified is True
        assert res.closure_passed is True
        assert res.receipt_path == str(receipt_out)
        receipt = json.loads(receipt_out.read_text(encoding="utf-8"))
        assert verify_receipt(receipt, pub_pem) is True
    finally:
        server.shutdown()
        server.server_close()


def test_run_portal_pack_self_heal_fallback_egress_audit_and_auto_publish(tmp_path: Path) -> None:
    """Warm staging failure -> Zero-PHI digital-twin self-heal -> LocalEgressGuard -> qualify & auto-publish."""
    drifted_html = """<!doctype html><html><body>
      <div role="dialog" style="display:block;width:520px;height:360px;">
        <p>Patient: Maria Gonzalez SSN 123-45-6789</p>
        <label for="ti">Claim Code</label><input id="ti" name="title" type="text" required />
        <label for="mc">Member Code</label><input id="mc" name="note" type="text" required />
        <label for="ok">Confirm</label><input id="ok" type="checkbox" required />
        <button id="save_btn" type="submit" disabled>Save</button>
      </div>
      <div id="saved"><!--SAVED--></div>
      <script>
        const ti = document.getElementById('ti');
        const mc = document.getElementById('mc');
        const ok = document.getElementById('ok');
        const btn = document.getElementById('save_btn');
        const sync = () => { btn.disabled = !(ti.value.trim().length > 0 && mc.value.trim().length > 0 && ok.checked); };
        ti.addEventListener('input', sync);
        mc.addEventListener('input', sync);
        ok.addEventListener('change', sync);
        btn.addEventListener('click', () => {
          document.getElementById('saved').innerText = 'SAVED:' + ti.value + ' ' + mc.value;
        });
      </script>
    </body></html>"""

    server, base_url, _ = _start_local_portal(drifted_html)
    try:
        pack_path = tmp_path / "drifted_pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "drifted-claim-pack",
                    "portal_origin": base_url,
                    "variable_fields": ["claim_code", "member_code"],
                    "static_defaults": {},
                    "provenance_map": {"claim_code": "claim_code", "member_code": "member_code"},
                    "stage": "SHADOW",
                }
            ),
            encoding="utf-8",
        )
        priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
        egress_ledger = tmp_path / "heal_egress_ledger.jsonl"
        registry_dir = tmp_path / "registry"

        def heal_coder(twin_html: str, var_keys: List[str], safe_err: str) -> str:
            assert "Maria" not in twin_html
            assert "123-45-6789" not in twin_html
            return (
                "def repair(ui, vars):\n"
                '    ui.fill(0, vars["$claim_code"])\n'
                '    ui.fill(1, vars["$member_code"])\n'
                "    ui.check(2, True)\n"
                "    ui.click(3)\n"
            )

        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"claim_code": "CLM-8821", "member_code": "MBR-4402"},
            mode="autopilot",
            url=base_url,
            commit_selector="#save_btn",
            coding_agent_fn=heal_coder,
            egress_ledger_path=egress_ledger,
            registry_dir=registry_dir,
            signing_key=priv_pem,
            auto_publish_on_heal=True,
        )

        assert res.outcome == "VERIFIED"
        assert res.healed is True
        assert res.readback_verified is True
        assert len(res.egress_ledger_entry_sha256) == 64
        assert res.published_pack_path is not None
        assert Path(res.published_pack_path).exists()

        pulled = pull_verified_pack(Path(res.published_pack_path), pub_pem)
        assert pulled.pack_id == "drifted-claim-pack"
        assert pulled.stage == "VERIFIED_AUTOPILOT"

        guard = LocalEgressGuard(egress_ledger)
        assert guard.verify_ledger_integrity() is True
        assert "Maria" not in egress_ledger.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()


def test_cli_egress_proxy_and_prepare_inbox(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """egress-proxy serves HTTP 200/403 with SHA-256 ledger; prepare-inbox blocks rubber-stamp and resumes."""
    # 1. Standalone egress-proxy CLI daemon
    ledger_path = tmp_path / "proxy_ledger.jsonl"
    pkg_src = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openadapt_kernel.cli",
            "egress-proxy",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--ledger",
            str(ledger_path),
            "--max-seconds",
            "15",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": pkg_src},
    )
    try:
        assert proc.stdout is not None
        ready_line = proc.stdout.readline().strip()
        ready = json.loads(ready_line)
        assert ready["status"] == "listening"
        proxy_url = ready["url"]

        st_ok, body_ok = _post_json(f"{proxy_url}/check", {"status": "ok", "action": "save"})
        assert st_ok == 200
        assert body_ok["allowed"] is True

        st_blk, body_blk = _post_json(f"{proxy_url}/check", {"text": "Maria Gonzalez 123-45-6789"})
        assert st_blk == 403
        assert body_blk["allowed"] is False
        assert body_blk["reason"] == "non_allowlisted_token"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

    assert LocalEgressGuard(ledger_path).verify_ledger_integrity() is True

    # 2. prepare-inbox CLI subcommand
    card_path = tmp_path / "card.json"
    card_path.write_text(
        json.dumps(
            {
                "card_id": "card-1",
                "portal_origin": "https://portal.example",
                "staged_bindings": {},
                "unresolved_slots": [
                    {
                        "control_idx": 0,
                        "control_label": "member_id",
                        "reason": "missing_required_param",
                        "candidate_vars": ["member_id"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rubber_path = tmp_path / "rubber.json"
    rubber_path.write_text(
        json.dumps({"card_id": "card-1", "operator_id": "op1", "approve_all": True}),
        encoding="utf-8",
    )
    rc_bad = cli_main(["prepare-inbox", "--card", str(card_path), "--response", str(rubber_path)])
    out_bad = json.loads(capsys.readouterr().out)
    assert rc_bad == 1
    assert out_bad["status"] == "HALT"
    assert out_bad["rubber_stamp_blocked"] is True

    good_path = tmp_path / "good.json"
    good_path.write_text(
        json.dumps(
            {
                "card_id": "card-1",
                "operator_id": "op1",
                "parameter_values": {"member_id": "MEM-9001"},
                "confirmed_fields": {"member_id": "MEM-9001"},
                "slot_bindings": {"0": "member_id"},
                "approve_all": False,
            }
        ),
        encoding="utf-8",
    )
    rc_ok = cli_main(["prepare-inbox", "--card", str(card_path), "--response", str(good_path)])
    out_ok = json.loads(capsys.readouterr().out)
    assert rc_ok == 0
    assert out_ok["status"] == "RESUME"
    assert out_ok["updated_dossier"]["member_id"] == "MEM-9001"
