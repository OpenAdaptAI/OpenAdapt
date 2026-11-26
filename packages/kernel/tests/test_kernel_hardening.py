"""Regression tests for the 7 kernel hardening fixes."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    CommitInterceptor,
    NetworkEvent,
    ObligationLedger,
    OnlineACICalibrator,
    ReadBackProof,
    generate_ephemeral_ed25519_keypair,
    handle_jsonrpc_message,
    is_search_or_filter_field,
    mint_receipt,
    qualify_verifier_offline,
    run_portal_pack,
    token_boundary_check,
    verify_cross_tab_dom,
    verify_receipt,
)
from openadapt_kernel.cli import main as cli_main


def _serve_html(
    get_handler: Any,
    post_handler: Any | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            status, body = get_handler(self.path)
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            if post_handler is None:
                self.send_response(404)
                self.end_headers()
                return
            status, body = post_handler(self.path, raw)
            resp = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


# ---- Defect 1: Field-bound, target-origin read-back ----


def test_defect1_source_tab_and_short_token_collision_rejected() -> None:
    # Bare 1-2 char values ("1", "CA") in unrelated free text MUST NOT match.
    cited, missing = token_boundary_check(
        {"state_code": "CA", "quantity": "1"},
        "Step 1: Welcome to CA portal dashboard.",
    )
    assert missing == ["state_code", "quantity"]
    assert cited == []

    # Field-bound values ("State: CA", "quantity=1") MUST match.
    cited_ok, missing_ok = token_boundary_check(
        {"state_code": "CA", "quantity": "1"},
        "State: CA\nquantity=1",
    )
    assert missing_ok == []
    assert cited_ok == ["CA", "1"]

    # Source/reference tab on another origin MUST NEVER satisfy a target tab write.
    src_srv, src_url = _serve_html(
        lambda _p: (
            200,
            "<html><body><dl><dt>State</dt><dd>CA</dd><dt>Quantity</dt><dd>1</dd>"
            "<dt>Policy</dt><dd>POL-901</dd></dl></body></html>",
        )
    )
    tgt_srv, tgt_url = _serve_html(
        lambda _p: (
            200,
            "<html><body><p>Step 1: CA portal empty record.</p></body></html>",
        )
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            source_tab = ctx.new_page()
            source_tab.goto(f"{src_url}/", wait_until="domcontentloaded")
            target_tab = ctx.new_page()
            target_tab.goto(f"{tgt_url}/", wait_until="domcontentloaded")

            verdict = verify_cross_tab_dom(
                target_tab,
                {"state_code": "CA", "quantity": "1", "policy_number": "POL-901"},
                reload_active=True,
                target_origin=tgt_url,
            )
            assert verdict.verified is False
            assert set(verdict.missing_fields) == {"state_code", "quantity", "policy_number"}
            browser.close()
    finally:
        src_srv.shutdown()
        tgt_srv.shutdown()


# ---- Defect 2: Commit detection gates VERIFIED ----


def test_defect2_noop_click_never_returns_verified(tmp_path: Path) -> None:
    # Even when the expected value is already rendered on the page (stale DOM),
    # a no-op button click (zero target-origin mutating HTTP 2xx AND no
    # pre-click-absent -> post-reload-present transition) MUST NOT return VERIFIED.
    srv, base_url = _serve_html(
        lambda _p: (
            200,
            """<!doctype html><html><body>
            <label for="policy_number">Policy Number</label>
            <input id="policy_number" name="policy_number" type="text" />
            <button id="noop-btn" type="button" onclick="return false;">Save</button>
            <div id="stale">POLICY=POL-STALE-1</div>
            </body></html>""",
        )
    )
    try:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "noop-pack",
                    "portal_origin": base_url,
                    "variable_fields": ["policy_number"],
                    "static_defaults": {},
                    "provenance_map": {"policy_number": "policy_number"},
                    "readback_url_pattern": f"{base_url}/",
                }
            ),
            encoding="utf-8",
        )
        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"policy_number": "POL-STALE-1"},
            mode="autopilot",
            url=f"{base_url}/",
            commit_selector="#noop-btn",
            headless=True,
        )
        assert res.outcome != "VERIFIED"
        assert res.commit_detected is False
    finally:
        srv.shutdown()


# ---- Defect 3: Event-driven settle instead of fixed 300ms sleep ----


def test_defect3_event_driven_settle_waits_for_slow_post(tmp_path: Path) -> None:
    saved: List[Dict[str, Any]] = []

    def handle_get(_path: str) -> tuple[int, str]:
        rows = "".join(f"<li>policy_number: {r['policy_number']}</li>" for r in saved)
        return (
            200,
            f"""<!doctype html><html><body>
            <form onsubmit="event.preventDefault(); slowSave();">
              <label for="policy_number">Policy Number</label>
              <input id="policy_number" name="policy_number" type="text" />
              <button id="slow-submit" type="submit">Submit</button>
            </form>
            <ul id="rows">{rows}</ul>
            <script>
              async function slowSave() {{
                await fetch('/api/slow-save', {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{policy_number: document.getElementById('policy_number').value}})
                }});
              }}
            </script>
            </body></html>""",
        )

    def handle_post(_path: str, raw: str) -> tuple[int, str]:
        # Intentionally slower than the old hardcoded 300ms sleep.
        time.sleep(0.55)
        saved.append(json.loads(raw))
        return 201, '{"ok": true}'

    srv, base_url = _serve_html(handle_get, handle_post)
    try:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "slow-pack",
                    "portal_origin": base_url,
                    "variable_fields": ["policy_number"],
                    "static_defaults": {},
                    "provenance_map": {"policy_number": "policy_number"},
                    "readback_url_pattern": f"{base_url}/",
                }
            ),
            encoding="utf-8",
        )
        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"policy_number": "POL-SLOW-550"},
            mode="autopilot",
            url=f"{base_url}/",
            commit_selector="#slow-submit",
            settle_timeout_ms=5000,
            headless=True,
        )
        assert res.outcome == "VERIFIED"
        assert res.commit_detected is True
        assert res.readback_verified is True
        assert len(saved) == 1
    finally:
        srv.shutdown()


# ---- Defect 4: Filters must not fail open ----


def test_defect4_clinical_findings_sort_code_and_collections_post_not_dropped() -> None:
    # Real domain fields MUST be tracked in dirty buffer.
    assert is_search_or_filter_field("clinical_findings", label="Clinical findings") is False
    assert is_search_or_filter_field("sort_code", label="Sort code") is False

    ic = CommitInterceptor()
    assert ic.mark_field_edited("clinical_findings", "Normal sinus rhythm", label="Clinical findings") is True
    assert ic.mark_field_edited("sort_code", "20-00-00", label="Sort code") is True
    assert "clinical_findings" in ic.dirty_fields
    assert "sort_code" in ic.dirty_fields

    # True search/filter controls ARE excluded.
    assert is_search_or_filter_field("q", input_type="search") is True
    assert is_search_or_filter_field("box", aria_role="searchbox") is True
    assert is_search_or_filter_field("table_filter", label="Filter rows") is True

    # POST /api/collections/... MUST NOT be ignored as telemetry.
    ic.begin_step()
    ic.record_network_event(
        NetworkEvent(method="POST", url="https://portal.example/api/collections/42", status=201)
    )
    c_col = ic.classify_step()
    assert c_col.is_commit is True
    assert c_col.kind == "http_mutation"

    # Explicit telemetry hosts/paths ARE ignored.
    for url in (
        "https://portal.example/telemetry/events",
        "https://portal.example/metrics",
        "https://portal.example/beacon",
        "https://www.google-analytics.com/g/collect",
        "https://us.i.posthog.com/e/",
        "https://api.segment.io/v1/t",
    ):
        ic.begin_step()
        ic.record_network_event(NetworkEvent(method="POST", url=url, status=200))
        assert ic.classify_step().is_commit is False, f"Expected telemetry ignored: {url}"


# ---- Defect 5: Hover/focus probe + ACI calibrator wired into run() ----


def test_defect5_hover_focus_probe_and_aci_wired_into_run(tmp_path: Path) -> None:
    saved: List[Dict[str, Any]] = []

    def handle_get(_path: str) -> tuple[int, str]:
        rows = "".join(f"<li>policy_number: {r['policy_number']}</li>" for r in saved)
        return (
            200,
            f"""<!doctype html><html><body>
            <form onsubmit="event.preventDefault(); doSave();">
              <label for="policy_number">Policy Number</label>
              <input id="policy_number" name="policy_number" type="text" />
              <button id="submit-btn" type="submit">Submit</button>
              <div id="nonfocus-div" style="width:80px;height:30px;pointer-events:none;">Fake</div>
            </form>
            <ul>{rows}</ul>
            <script>
              async function doSave() {{
                await fetch('/api/save', {{
                  method: 'POST',
                  headers: {{'Content-Type': 'application/json'}},
                  body: JSON.stringify({{policy_number: document.getElementById('policy_number').value}})
                }});
              }}
            </script>
            </body></html>""",
        )

    def handle_post(_path: str, raw: str) -> tuple[int, str]:
        saved.append(json.loads(raw))
        return 201, '{"ok": true}'

    srv, base_url = _serve_html(handle_get, handle_post)
    try:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "aci-pack",
                    "portal_origin": base_url,
                    "variable_fields": ["policy_number"],
                    "static_defaults": {},
                    "provenance_map": {"policy_number": "policy_number"},
                    "readback_url_pattern": f"{base_url}/",
                }
            ),
            encoding="utf-8",
        )
        aci = OnlineACICalibrator(target_alpha=0.05, gamma=0.1, alpha_t=0.05)
        assert len(aci.history_scores) == 0

        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"policy_number": "POL-ACI-1"},
            mode="autopilot",
            url=f"{base_url}/",
            commit_selector="#submit-btn",
            aci_calibrator=aci,
            headless=True,
        )
        assert res.outcome == "VERIFIED"
        assert len(aci.history_scores) == 1
        assert len(aci.errors) == 1
        assert res.aci_alpha_t == aci.alpha_t

        # Hover/focus probe rejects non-interactive/non-focusable target before click.
        res_bad = run_portal_pack(
            pack_path=pack_path,
            dossier={"policy_number": "POL-ACI-2"},
            mode="autopilot",
            url=f"{base_url}/",
            commit_selector="#nonfocus-div",
            aci_calibrator=aci,
            headless=True,
        )
        assert res_bad.outcome != "VERIFIED"
        assert "commit_control_blocked" in res_bad.reason
    finally:
        srv.shutdown()


# ---- Defect 6: qualify() requires real captured pre-write snapshot ----


def test_defect6_qualify_rejects_missing_or_empty_pre_write_snapshot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pre_write_text"):
        qualify_verifier_offline(
            expected_fields={"policy_number": "POL-1"},
            pre_write_text="   ",
            post_write_text="Confirmed POL-1",
            verifier_fn=lambda f, t: all(v in t for v in f.values()),
        )

    ev_path = tmp_path / "ev.jsonl"
    ev_path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "input", "frame_origin": "https://p.example", "field": "policy_id", "value": "P-10"}),
                json.dumps({"kind": "readback", "url": "https://p.example/done", "text": "Confirmed P-10"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pack_path = tmp_path / "pack.json"
    pack_path.write_text(json.dumps({"pack_id": "p", "variable_fields": ["policy_id"]}), encoding="utf-8")

    # CLI qualify without --pre-text and without pre_write event in JSONL MUST raise ValueError.
    with pytest.raises(ValueError, match="pre-write"):
        cli_main(["qualify", "--pack", str(pack_path), "--evidence", str(ev_path)])

    # MCP kernel_qualify_offline without pre_text MUST return an error.
    mcp_resp = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "kernel_qualify_offline",
                "arguments": {"pack_path": str(pack_path), "evidence_path": str(ev_path)},
            },
        }
    )
    assert mcp_resp is not None
    assert mcp_resp["result"]["isError"] is True


# ---- Defect 7: Zero-PHI receipts, sign all terminal outcomes, UUIDv4 episode IDs ----


def test_defect7_zero_phi_redacted_receipts_all_terminal_outcomes_and_uuid4(tmp_path: Path) -> None:
    priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()

    # Default episode IDs are unique UUIDv4s, never "ep-live-1".
    l1 = ObligationLedger()
    l2 = ObligationLedger()
    assert l1.episode_id != l2.episode_id
    assert l1.episode_id != "ep-live-1"
    uuid.UUID(l1.episode_id.removeprefix("ep-"), version=4)
    uuid.UUID(l2.episode_id.removeprefix("ep-"), version=4)

    # Zero-PHI redacted receipt by default: hashes typed values and strips URL path/query.
    phi_val = "SSN-123-45-6789"
    l1.add_obligation("ob1", "Submit claim", target_system="https://payer.example/claims?patient=alice")
    l1.mark_committed(
        "ob1",
        ReadBackProof(
            channel="fresh_reload",
            url_or_locator="https://payer.example/claims/99?ssn=123-45-6789",
            cited_substrings=[phi_val],
            external_ref="CLM-SECRET-77",
        ),
    )
    assert l1.finalize("done").outcome == "VERIFIED"

    redacted = mint_receipt(l1, "claim-pack", priv_pem)
    assert verify_receipt(redacted, pub_pem) is True
    blob = json.dumps(redacted)
    assert phi_val not in blob
    assert "CLM-SECRET-77" not in blob
    assert "patient=alice" not in blob
    assert "payer.example" in blob
    proof_out = redacted["payload"]["obligations"]["ob1"]["proof"]
    assert proof_out["origin_host"] == "payer.example"
    assert hashlib.sha256(phi_val.encode("utf-8")).hexdigest() in proof_out["cited_value_sha256"]

    # Explicit debug opt-in retains plaintext values.
    debug_receipt = mint_receipt(l1, "claim-pack", priv_pem, include_plaintext_values=True)
    assert verify_receipt(debug_receipt, pub_pem) is True
    assert phi_val in json.dumps(debug_receipt)

    # EVERY terminal outcome (VERIFIED, HALTED, FAILED, PREPARE_REQUIRED) can be signed.
    for outcome in ("VERIFIED", "HALTED", "FAILED", "PREPARE_REQUIRED"):
        lx = ObligationLedger()
        lx.add_obligation("ob1", "Work")
        lx.status = outcome  # type: ignore[assignment]
        rec = mint_receipt(lx, "claim-pack", priv_pem)
        assert verify_receipt(rec, pub_pem) is True
        assert rec["payload"]["outcome"] == outcome
