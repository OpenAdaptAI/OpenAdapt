"""End-to-end tests for cross-frame indexing, critic veto preservation, raw demo induction, and runner wiring."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    ClosureVerdict,
    PortalPack,
    RawDemoTrace,
    calibrate_critic_decision,
    induce_cartridge_from_raw_demos,
    observe_schema,
    qualify_app_cartridge_offline,
    run_portal_pack,
    stage_form,
)


def _start_local_server(routes: Dict[str, str]) -> Tuple[ThreadingHTTPServer, str]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = routes.get(self.path, "<html><body>Not Found</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw_host = server.server_address[0]
    host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else str(raw_host)
    port = int(server.server_address[1])
    return server, f"http://{host}:{port}"


def test_cross_frame_control_index_unique_and_stage_form_both_frames() -> None:
    iframe_html = (
        "<!doctype html><html><body>"
        '<label for="sub_in">Sub Field</label>'
        '<input id="sub_in" name="sub_field" type="text" />'
        "</body></html>"
    )
    main_html = (
        "<!doctype html><html><body>"
        '<label for="main_in">Main Field</label>'
        '<input id="main_in" name="main_field" type="text" />'
        '<iframe id="nested_frame" src="/iframe"></iframe>'
        "</body></html>"
    )
    server, base_url = _start_local_server({"/": main_html, "/iframe": iframe_html})
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/", wait_until="networkidle")
                controls = observe_schema(page, include_frames=True)
                indices = [c.control_index for c in controls]
                assert len(indices) >= 2
                assert len(indices) == len(set(indices)), f"Duplicate cross-frame control_index: {indices}"

                main_ctrl = next(c for c in controls if c.element_id == "main_in")
                sub_ctrl = next(c for c in controls if c.element_id == "sub_in")
                assert main_ctrl.frame_index == -1
                assert sub_ctrl.frame_index >= 0
                assert main_ctrl.control_index != sub_ctrl.control_index

                res = stage_form(page, {main_ctrl.control_index: "Alpha", sub_ctrl.control_index: "Beta"})
                assert res.ok
                assert page.locator("#main_in").input_value() == "Alpha"
                assert page.frame_locator("iframe#nested_frame").locator("#sub_in").input_value() == "Beta"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_calibrate_critic_decision_preserves_veto_on_untemplated_or_ood() -> None:
    passed_closure = ClosureVerdict("VERIFIED", True, "all_checks_passed", decision="COMPLETE")

    v_untemplated = calibrate_critic_decision(
        passed_closure,
        critic_vetoed=True,
        critic_reason="missing domain nuance",
        untemplated_freetext=True,
    )
    assert v_untemplated.action == "ESCALATE"
    assert not v_untemplated.overrode_false_escalation
    assert v_untemplated.reason == "preserved_critic_veto_on_untemplated_or_ood_prose"

    v_ood = calibrate_critic_decision(
        passed_closure,
        critic_vetoed=True,
        critic_reason="unseen edge case",
        ood_case=True,
    )
    assert v_ood.action == "ESCALATE"
    assert not v_ood.overrode_false_escalation
    assert v_ood.reason == "preserved_critic_veto_on_untemplated_or_ood_prose"

    v_default = calibrate_critic_decision(
        passed_closure,
        critic_vetoed=True,
        critic_reason="conservative style nit",
    )
    assert v_default.action == "ALLOW_COMPLETE"
    assert v_default.overrode_false_escalation


def test_induce_cartridge_from_raw_demos_deterministic_and_zero_phi_agent(tmp_path: Path) -> None:
    demos = [
        RawDemoTrace(
            demo_id="d1",
            goal_text="Process high value case",
            visited_views=("summary_tab", "audit_tab"),
            dom_observations={
                "invoice_amount": "$1500",
                "tier": "enterprise",
                "reference_code": "REF-9912",
            },
            committed_fields={
                "disposition": "ESCALATE_REVIEW",
                "notes": "Escalated under reference REF-9912 for enterprise amount $1500.",
            },
            post_commit_actions=("clear_queue_flag", "notify_supervisor"),
            post_commit_url="/cases/101",
        ),
        RawDemoTrace(
            demo_id="d2",
            goal_text="Process high value case 2",
            visited_views=("summary_tab", "audit_tab", "history_tab"),
            dom_observations={
                "invoice_amount": "$2400",
                "tier": "enterprise",
                "reference_code": "REF-9944",
            },
            committed_fields={
                "disposition": "ESCALATE_REVIEW",
                "notes": "Escalated under reference REF-9944 for enterprise amount $2400.",
            },
            post_commit_actions=("clear_queue_flag", "notify_supervisor"),
            post_commit_url="/cases/102",
        ),
        RawDemoTrace(
            demo_id="d3",
            goal_text="Process routine case",
            visited_views=("summary_tab",),
            dom_observations={
                "invoice_amount": "$200",
                "tier": "standard",
                "reference_code": "REF-1001",
            },
            committed_fields={
                "disposition": "AUTO_APPROVE",
                "notes": "Approved standard case REF-1001.",
            },
            post_commit_actions=("archive_item",),
            post_commit_url="/cases/103",
        ),
    ]

    cart = induce_cartridge_from_raw_demos(
        demos,
        cartridge_id="raw_demo_cart",
        portal_origin="https://portal.example.test",
        ledger_path=tmp_path / "det_egress.jsonl",
    )
    qual = qualify_app_cartridge_offline(cart)
    assert qual.qualified, f"Qualification failed: {qual}"

    by_disp = {b.disposition: b for b in cart.sop_spec.branches}
    assert set(by_disp.keys()) == {"ESCALATE_REVIEW", "AUTO_APPROVE"}
    assert by_disp["ESCALATE_REVIEW"].required_read_artifacts == ("summary_tab", "audit_tab")
    assert by_disp["AUTO_APPROVE"].required_read_artifacts == ("summary_tab",)
    assert "reference_code" in by_disp["ESCALATE_REVIEW"].required_note_slots
    assert "invoice_amount" in by_disp["ESCALATE_REVIEW"].when
    assert str(by_disp["ESCALATE_REVIEW"].when["invoice_amount"]).startswith(">=")
    assert cart.sop_spec.post_commit_obligations["ESCALATE_REVIEW"] == ("clear_queue_flag", "notify_supervisor")
    assert cart.sop_spec.post_commit_obligations["AUTO_APPROVE"] == ("archive_item",)

    # Also verify zero-PHI bijective sanitization when coding_agent_fn is supplied
    seen_prompts: List[str] = []

    def _mock_agent(prompt: str) -> str:
        seen_prompts.append(prompt)
        assert "REF-9912" not in prompt
        assert "1500" not in prompt
        return json.dumps({"branches": []})

    cart2 = induce_cartridge_from_raw_demos(
        demos,
        cartridge_id="raw_demo_cart_agent",
        portal_origin="https://portal.example.test",
        coding_agent_fn=_mock_agent,
        ledger_path=tmp_path / "agent_egress.jsonl",
    )
    assert len(seen_prompts) == 1
    assert cart2.egress_audit["allowed_count"] == 1
    assert cart2.egress_audit["blocked_count"] == 0


def test_runner_bijective_symbols_ood_halt_and_canvas_servo(tmp_path: Path) -> None:
    pack = PortalPack(
        pack_id="intl_pack",
        portal_origin="http://127.0.0.1",
        readback_url_pattern="/intl",
        variable_fields=["antrag_code"],
        static_defaults={},
        provenance_map={},
        stage="VERIFIED_AUTOPILOT",
    )
    pack_path = tmp_path / "intl_pack.json"
    pack_path.write_text(json.dumps(asdict(pack)), encoding="utf-8")

    # 1. Cartridge OOD halt before staging/committing
    raw_cart = induce_cartridge_from_raw_demos(
        [
            RawDemoTrace(
                demo_id="d1",
                goal_text="Approve tier A",
                visited_views=("main",),
                dom_observations={"tier": "gold"},
                committed_fields={"disposition": "APPROVE"},
            )
        ],
        cartridge_id="ood_cart",
        portal_origin="http://127.0.0.1",
        ledger_path=tmp_path / "ood_egress.jsonl",
    )
    ood_res = run_portal_pack(
        pack_path=pack_path,
        dossier={"antrag_code": "DE-42", "tier": "platinum_unseen"},
        mode="autopilot",
        cartridge=raw_cart,
    )
    assert ood_res.outcome == "PREPARE_REQUIRED"
    assert ood_res.reason == "ood_unseen_feature_value"

    # 2. German/Japanese form self-healing via BijectiveSymbolCodec (zero raw German/Japanese leaked)
    intl_html = (
        "<!doctype html><html><body>"
        "<form onsubmit=\"event.preventDefault(); document.getElementById('out').textContent = "
        "document.getElementById('f_de').value;\">"
        '<label for="f_de">Antragsnummer 申請番号</label>'
        '<input id="f_de" type="text" required />'
        '<button id="go" type="submit">Absenden 送信</button>'
        "</form>"
        '<div id="out"></div>'
        "</body></html>"
    )
    canvas_html = (
        "<!doctype html><html><body>"
        '<canvas id="opaque_app" width="640" height="400" style="width:640px;height:400px;"></canvas>'
        "</body></html>"
    )
    server, base_url = _start_local_server({"/intl": intl_html, "/canvas": canvas_html})
    try:
        seen_html: List[str] = []

        def _intl_coding_agent(twin_html: str, var_keys: List[str], _err: str, _fb: Dict[str, Any]) -> str:
            seen_html.append(twin_html)
            assert "Antragsnummer" not in twin_html
            assert "申請番号" not in twin_html
            assert "Absenden" not in twin_html
            assert "SYM_" in twin_html
            return (
                "def repair(ui, vars):\n"
                "    ui.fill(0, vars['$antrag_code'])\n"
                "    ui.click(1)\n"
            )

        heal_res = run_portal_pack(
            pack_path=pack_path,
            dossier={"antrag_code": "DE-2026-88"},
            mode="autopilot",
            url=f"{base_url}/intl",
            commit_selector="#go",
            coding_agent_fn=_intl_coding_agent,
            egress_ledger_path=tmp_path / "intl_egress.jsonl",
            use_bijective_twin_symbols=True,
        )
        assert heal_res.healed is True
        assert heal_res.outcome == "VERIFIED"
        assert heal_res.egress_ledger_entry_sha256
        assert len(seen_html) == 1

        # 3a. Opaque <canvas> without visual_servo_click_fn -> ESCALATED
        esc_res = run_portal_pack(
            pack_path=pack_path,
            dossier={"antrag_code": "DE-1"},
            mode="autopilot",
            url=f"{base_url}/canvas",
        )
        assert esc_res.outcome == "ESCALATED"
        assert esc_res.reason == "opaque_visual_surface_requires_servo"

        # 3b. Opaque <canvas> with visual_servo_click_fn -> invokes callback cleanly
        servo_calls: List[Dict[str, Any]] = []

        def _servo_cb(_page: Any, surface: Dict[str, Any]) -> None:
            servo_calls.append(dict(surface))

        servo_res = run_portal_pack(
            pack_path=pack_path,
            dossier={"antrag_code": "DE-1"},
            mode="autopilot",
            url=f"{base_url}/canvas",
            visual_servo_click_fn=_servo_cb,
        )
        assert len(servo_calls) == 1
        assert servo_calls[0]["tag"] == "canvas"
        assert servo_res.outcome == "VISUAL_SERVO_EXECUTED"
    finally:
        server.shutdown()
        server.server_close()
