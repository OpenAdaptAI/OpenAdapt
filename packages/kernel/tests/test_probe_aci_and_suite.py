"""Headless Chromium tests for probe_before_click, OnlineACICalibrator, and Multi-Portal Saga crash/resume."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict

from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    CommitInterceptor,
    ObligationLedger,
    OnlineACICalibrator,
    SessionStartNormalizer,
    compute_pixel_delta_ratio,
    generate_ephemeral_ed25519_keypair,
    mint_receipt,
    probe_before_click,
    probe_hover_pixel_delta,
    run_portal_pack,
    stage_form,
    verify_cross_tab_dom,
    verify_receipt,
)


def _start_occluded_and_saga_server() -> tuple[ThreadingHTTPServer, str, Dict[str, object]]:
    state: Dict[str, object] = {
        "portal_a_posts": 0,
        "portal_b_posts": 0,
        "portal_a_ref": "",
        "portal_b_saved": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/occluded":
                html = """<!doctype html><html><body>
                <form id="f">
                  <label for="policy_id">Policy</label>
                  <input id="policy_id" name="policy_id" />
                  <button id="save-btn" type="button" style="position:fixed;left:40px;top:40px;width:140px;height:40px;">Save</button>
                  <div id="glass-overlay" style="position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:9999;background:rgba(0,0,0,0.01);"></div>
                </form>
                </body></html>"""
            elif self.path == "/portal_a":
                ref = state["portal_a_ref"]
                html = f"""<!doctype html><html><body>
                <form id="fa" method="POST" action="/portal_a">
                  <label for="applicant_name">Applicant</label>
                  <input id="applicant_name" name="applicant_name" />
                  <button id="submit-a" type="submit">Issue Reference</button>
                </form>
                <div id="portal-a-status">REF: {ref}</div>
                </body></html>"""
            elif self.path == "/portal_b":
                saved = state["portal_b_saved"]
                html = f"""<!doctype html><html><body>
                <form id="fb" method="POST" action="/portal_b">
                  <label for="portal_a_ref">Portal A Ref</label>
                  <input id="portal_a_ref" name="portal_a_ref" />
                  <label for="coverage_amount">Coverage</label>
                  <input id="coverage_amount" name="coverage_amount" />
                  <button id="submit-b" type="submit">Bind Quote</button>
                </form>
                <div id="portal-b-status">{json.dumps(saved)}</div>
                </body></html>"""
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            from urllib.parse import parse_qs

            parsed = {k: v[0] for k, v in parse_qs(raw).items()}
            if self.path == "/portal_a":
                state["portal_a_posts"] = int(state["portal_a_posts"]) + 1
                state["portal_a_ref"] = f"REF-A{state['portal_a_posts']}"
                self.send_response(303)
                self.send_header("Location", "/portal_a")
                self.end_headers()
            elif self.path == "/portal_b":
                state["portal_b_posts"] = int(state["portal_b_posts"]) + 1
                state["portal_b_saved"] = parsed
                self.send_response(303)
                self.send_header("Location", "/portal_b")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}", state


def test_probe_detects_transparent_overlay_and_runner_refuses_click(tmp_path: Path) -> None:
    srv, base_url, _ = _start_occluded_and_saga_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{base_url}/occluded", wait_until="domcontentloaded")

            blocked = probe_before_click(page, "#save-btn")
            assert not blocked.safe_to_click
            assert blocked.occluded
            assert blocked.top_blocker == "div#glass-overlay"

            page.evaluate("() => document.getElementById('glass-overlay').remove()")
            clear = probe_before_click(page, "#save-btn")
            assert clear.safe_to_click
            assert not clear.occluded
            assert clear.focus_responded
            assert compute_pixel_delta_ratio(b"\x00\x00\x00\x00", b"\x00\x00\x00\x00") == 0.0
            assert compute_pixel_delta_ratio(b"\x00\x00\x00\x00", b"\xff\xff\x00\x00") == 0.5
            assert probe_hover_pixel_delta(page, 80.0, 60.0) >= 0.0
            browser.close()

        pack_path = tmp_path / "pack.json"
        pack_path.write_text(
            json.dumps(
                {
                    "pack_id": "occluded-test",
                    "portal_origin": base_url,
                    "variable_fields": ["policy_id"],
                    "static_defaults": {},
                    "provenance_map": {"policy_id": "policy_id"},
                    "readback_url_pattern": f"{base_url}/occluded",
                }
            ),
            encoding="utf-8",
        )
        res = run_portal_pack(
            pack_path=pack_path,
            dossier={"policy_id": "POL-889"},
            mode="autopilot",
            url=f"{base_url}/occluded",
            commit_selector="#save-btn",
            headless=True,
        )
        assert res.outcome == "ESCALATED"
        assert not res.commit_detected
        assert "commit_control_blocked:occluded_by_overlay(div#glass-overlay)" in res.reason
    finally:
        srv.shutdown()


def test_session_start_normalizer_and_online_aci_under_shift() -> None:
    norm = SessionStartNormalizer(min_frames=3)
    for s in (10.0, 10.5, 9.5):
        norm.observe_benign_nav(s)
    assert norm.ready
    assert abs(norm.z_score(10.0)) < 1e-6
    assert norm.z_score(15.0) > 5.0

    aci = OnlineACICalibrator(target_alpha=0.05, gamma=0.05, alpha_t=0.05)
    # Feed 40 benign scores around 0.2 and occasional shifted anomalies
    for i in range(60):
        score = 0.2 + (0.05 * (i % 5))
        is_anomaly = (i % 15 == 14)
        if is_anomaly:
            score = 3.5
        flagged = aci.should_flag(score)
        realized_error = (flagged != is_anomaly)
        aci.update_from_readback(score, realized_error=realized_error)
    assert 0.0 <= aci.alpha_t <= 1.0
    assert aci.empirical_error_rate < 0.25


def test_multi_portal_saga_crash_and_resume_skips_committed_portal_a(tmp_path: Path) -> None:
    srv, base_url, state = _start_occluded_and_saga_server()
    ledger_path = tmp_path / "saga_ledger.json"
    priv, pub = generate_ephemeral_ed25519_keypair()

    try:
        # Phase 1: Commit Portal A, save ledger, then simulate process crash before Portal B.
        ledger = ObligationLedger(episode_id="saga-ep-1")
        ledger.add_obligation("portal_a", "Create reference on Portal A", target_system=f"{base_url}/portal_a")
        ledger.add_obligation("portal_b", "Bind quote on Portal B", target_system=f"{base_url}/portal_b")
        ledger.record_ssa_literal("applicant_name", "Jordan Lee", source_window="dossier", turn=0)
        ledger.record_ssa_literal("coverage_amount", "450000", source_window="dossier", turn=0)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            ic = CommitInterceptor()
            ic.attach_playwright_page(page)
            page.goto(f"{base_url}/portal_a", wait_until="domcontentloaded")
            staged_a = stage_form(
                page,
                {"applicant_name": "Jordan Lee"},
                ledger=ledger,
                opaque_fields=["applicant_name"],
                interceptor=ic,
            )
            assert staged_a.ok
            page.locator("#submit-a").click()
            page.wait_for_load_state("domcontentloaded")
            verdict_a = verify_cross_tab_dom(page, {"ref": "REF-A1"}, reload_active=True)
            assert verdict_a.verified
            ledger.record_ssa_literal("portal_a_ref", "REF-A1", source_window=f"{base_url}/portal_a", turn=1)
            ledger.mark_committed("portal_a", verdict_a.to_proof())
            ledger.save_saga(ledger_path)
            browser.close()

        assert state["portal_a_posts"] == 1
        assert state["portal_b_posts"] == 0

        # Phase 2: Resume from disk after crash; must skip portal_a and only commit portal_b.
        resumed = ObligationLedger.load_saga(ledger_path)
        pending = resumed.pending_ids()
        assert pending == ["portal_b"]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            ic = CommitInterceptor()
            ic.attach_playwright_page(page)
            page.goto(f"{base_url}/portal_b", wait_until="domcontentloaded")
            staged_b = stage_form(
                page,
                {"portal_a_ref": "REF-A1", "coverage_amount": "450000"},
                ledger=resumed,
                opaque_fields=["portal_a_ref", "coverage_amount"],
                interceptor=ic,
            )
            assert staged_b.ok
            page.locator("#submit-b").click()
            page.wait_for_load_state("domcontentloaded")
            verdict_b = verify_cross_tab_dom(
                page,
                {"portal_a_ref": "REF-A1", "coverage_amount": "450000"},
                reload_active=True,
            )
            assert verdict_b.verified
            resumed.mark_committed("portal_b", verdict_b.to_proof())
            decision = resumed.finalize("done")
            assert decision.outcome == "VERIFIED"
            receipt = mint_receipt(resumed, "saga-pack", priv)
            assert verify_receipt(receipt, pub)
            browser.close()

        # Portal A was NEVER re-submitted after crash recovery.
        assert state["portal_a_posts"] == 1
        assert state["portal_b_posts"] == 1
    finally:
        srv.shutdown()
