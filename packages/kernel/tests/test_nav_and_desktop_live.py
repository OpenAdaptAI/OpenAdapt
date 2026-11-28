"""Tests for Zero-PHI AppNavigationGraph, StuckLoopDetector, ReconciliationWatcher, and Live Desktop Drivers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel.desktop_driver import (
    CitrixBoxGraphDriver,
    DesktopControlAction,
    MacOSAXDriver,
    WindowsUIADriver,
    get_desktop_driver,
)
from openadapt_kernel.macro import ControlSignature
from openadapt_kernel.nav_graph import (
    AppNavigationGraph,
    ReconciliationWatcher,
    StuckLoopDetector,
    sanitize_url_to_view_pattern,
)
from openadapt_kernel.prepare import PendingReconciliationRecord
from openadapt_kernel.providers import OptionalDependencyError
from openadapt_kernel.stateful_twin import (
    A11yNodeSpec,
    A11yScreenSpec,
    TwinRouteSpec,
    project_a11y_tree_to_zero_phi_twin,
    self_heal_multipage_in_twin,
)


def test_app_navigation_graph_zero_phi_bfs_and_json_roundtrip(tmp_path: Path) -> None:
    """AppNavigationGraph strips PHI from URLs/signatures, finds BFS shortest paths, and round-trips JSON."""
    assert (
        sanitize_url_to_view_pattern(
            "https://portal.example/patients/MRN-994821/claims/123456/edit?ssn=123-45-6789"
        )
        == "/patients/{id}/claims/{id}/edit"
    )

    graph = AppNavigationGraph()
    sig_patients = ControlSignature(role="button", label_tokens=("patients", "Maria_Gonzalez"))
    sig_claim = ControlSignature(role="button", label_tokens=("claim", "new"))
    sig_settings = ControlSignature(role="button", label_tokens=("settings",))
    sig_back = ControlSignature(role="button", label_tokens=("search",))

    # Direct 2-hop path: /home -> /patients/{id} -> /claims/new
    graph.record_transition(
        "https://portal.example/home",
        "https://portal.example/patients/MRN-994821?name=Maria",
        sig_patients,
        src_landmarks=("main", "Maria Gonzalez"),
        dst_landmarks=("patient", "detail"),
    )
    graph.record_transition(
        "https://portal.example/patients/MRN-994821",
        "https://portal.example/claims/new",
        sig_claim,
    )
    # Longer detour path: /home -> /settings -> /search -> /claims/new
    graph.record_transition("/home", "/settings", sig_settings)
    graph.record_transition("/settings", "/search", sig_back)
    graph.record_transition("/search", "/claims/new", sig_claim)

    # Recording same edge increments success_count
    e_repeat = graph.record_transition(
        "https://portal.example/home",
        "https://portal.example/patients/MRN-881122",
        sig_patients,
    )
    assert e_repeat.success_count == 2

    serialized = json.dumps(graph.to_dict())
    for secret in ("MRN-994821", "MRN-881122", "Maria", "Gonzalez", "123-45-6789"):
        assert secret not in serialized

    path = graph.shortest_path("https://portal.example/home", "/claims/new")
    assert len(path) == 2
    assert path[0].dst_view_id == "/patients/{id}"
    assert path[1].dst_view_id == "/claims/new"

    save_path = tmp_path / "nav_graph.json"
    graph.save_json(save_path)
    loaded = AppNavigationGraph.load_json(save_path)
    loaded_path = loaded.shortest_path("/home", "/claims/new")
    assert len(loaded_path) == 2
    assert loaded_path[0].success_count == 2


def test_stuck_loop_detector_signals_twin_fallback() -> None:
    """StuckLoopDetector hashes normalized URL+DOM and triggers fallback after max_unchanged_steps."""
    det = StuckLoopDetector(max_unchanged_steps=2)
    s0 = det.observe("https://portal.example/patients/123", "<button disabled>Save</button>")
    assert s0.unchanged_count == 0
    assert s0.should_fallback_to_twin is False

    s1 = det.observe("https://portal.example/patients/456", "  <button disabled>Save</button>  ")
    assert s1.unchanged_count == 1
    assert s1.should_fallback_to_twin is False

    s2 = det.observe("https://portal.example/patients/789", "<button disabled>Save</button>")
    assert s2.unchanged_count == 2
    assert s2.should_fallback_to_twin is True

    s3 = det.observe("https://portal.example/patients/789", "<button>Save</button>")
    assert s3.unchanged_count == 0
    assert s3.should_fallback_to_twin is False


def test_reconciliation_watcher_reconciled_and_escalated() -> None:
    """ReconciliationWatcher transitions async submissions to RECONCILED_COMPLETE or RECONCILIATION_ESCALATED."""
    watcher = ReconciliationWatcher()
    rec_ok = PendingReconciliationRecord(
        obligation_id="ob-reconcile-1",
        card_id="card-1",
        portal_origin="https://clearinghouse.example",
        receipt_ref="CLM-1001",
        expected_by_readback_key={"claim_id": ("CLM-1001", "casefold_ws", 0)},
    )
    rec_timeout = PendingReconciliationRecord(
        obligation_id="ob-timeout-2",
        card_id="card-2",
        portal_origin="https://clearinghouse.example",
        receipt_ref="CLM-1002",
        expected_by_readback_key={"claim_id": ("CLM-1002", "casefold_ws", 0)},
    )
    watcher.enqueue(rec_ok, max_attempts=3)
    watcher.enqueue(rec_timeout, max_attempts=3)

    calls: Dict[str, int] = {}

    def probe(rec: PendingReconciliationRecord) -> Tuple[bool, str]:
        calls[rec.obligation_id] = calls.get(rec.obligation_id, 0) + 1
        if rec.obligation_id == "ob-reconcile-1" and calls[rec.obligation_id] >= 2:
            return True, "clearinghouse_ack_verified"
        return False, "still_processing"

    settled = watcher.poll_until_settled(probe, interval_s=0.0, max_rounds=5)
    assert settled["ob-reconcile-1"].status == "RECONCILED_COMPLETE"
    assert settled["ob-reconcile-1"].attempts == 2
    assert settled["ob-timeout-2"].status == "RECONCILIATION_ESCALATED"
    assert settled["ob-timeout-2"].attempts == 3
    assert watcher.summary() == {"pending": 0, "reconciled": 1, "escalated": 1}


def test_live_macos_ax_driver_to_zero_phi_twin_e2e(tmp_path: Path) -> None:
    """Live macOS AX probe -> project_a11y_tree_to_zero_phi_twin -> offline Playwright (0 PHI, 0 network)."""
    if sys.platform != "darwin":
        pytest.skip("Live macOS AX test runs only on macOS (darwin)")

    # Always verify MacOSAXDriver -> Zero-PHI twin projection with explicit app_name (no System Events consent prompt).
    explicit_driver = get_desktop_driver("macos_ax", app_name="Window")
    assert isinstance(explicit_driver, MacOSAXDriver)
    explicit_screen = explicit_driver.capture_a11y_tree(screen_id="macos_window", url_path="/main")
    assert explicit_screen.nodes and explicit_screen.nodes[0].platform == "macos_ax"

    ledger_path = tmp_path / "live_macos_ax_egress.jsonl"
    twin_app = project_a11y_tree_to_zero_phi_twin([explicit_screen], ledger_path=ledger_path)
    assert "/main" in twin_app.pages
    assert twin_app.egress_audit()["sha256_chain_integrity_verified"] is True

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            twin_app.install_on_page(page)
            page.goto(twin_app.base_url + "/main", wait_until="domcontentloaded")
            assert twin_app.live_network_requests_attempted == 0
        finally:
            browser.close()

    # Probe live frontmost window via osascript; gracefully skip if Accessibility/Automation consent is not granted.
    live_driver = get_desktop_driver("macos_ax")
    assert isinstance(live_driver, MacOSAXDriver)
    try:
        live_screen = live_driver.capture_a11y_tree(screen_id="live_macos_screen", url_path="/main")
    except (subprocess.TimeoutExpired, PermissionError, OSError) as exc:
        pytest.skip(f"macOS System Events Accessibility/Automation permission not granted to headless shell: {exc}")
    assert live_screen.nodes and live_screen.nodes[0].platform == "macos_ax"


def test_uia_and_citrix_driver_integrated_with_nav_graph_and_reconciliation(tmp_path: Path) -> None:
    """WindowsUIADriver + CitrixBoxGraphDriver integrated with twin projection, AppNavigationGraph, and ReconciliationWatcher."""
    uia = get_desktop_driver("uia")
    assert isinstance(uia, WindowsUIADriver)
    if sys.platform != "win32":
        with pytest.raises(OptionalDependencyError):
            uia.capture_a11y_tree()

    # End-to-end CitrixBoxGraphDriver + UIA-projected multi-screen twin + AppNavigationGraph + ReconciliationWatcher
    citrix = get_desktop_driver(
        "citrix_box",
        nodes=[
            A11yNodeSpec("ed_title", "Edit", "Title", value="Maria Gonzalez", bbox=(10, 20, 220, 30), platform="citrix_box"),
            A11yNodeSpec("btn_save", "Button", "Save", bbox=(10, 60, 100, 32), platform="citrix_box"),
        ],
        transitions=(
            TwinRouteSpec(
                method="POST",
                path_pattern="/submit",
                mutates_collection="records",
                status_code=303,
                redirect_to="/record",
            ),
        ),
    )
    assert isinstance(citrix, CitrixBoxGraphDriver)
    inv = citrix.invoke_control(DesktopControlAction("fill", target_node_id="ed_title", value="Draft Value"))
    assert inv.ok is True
    assert citrix.read_back("ed_title") == "Draft Value"

    entry_screen = citrix.capture_a11y_tree(screen_id="citrix_entry", url_path="/form")
    record_screen = A11yScreenSpec(
        screen_id="uia_record",
        url_path="/record",
        nodes=(A11yNodeSpec("hdr_1", "Text", "Record Summary", platform="uia"),),
        store_collection="records",
    )
    ledger_path = tmp_path / "citrix_uia_egress.jsonl"
    twin_app = project_a11y_tree_to_zero_phi_twin([entry_screen, record_screen], ledger_path=ledger_path)
    assert "Maria" not in twin_app.pages["/form"].sanitized_html

    heal = self_heal_multipage_in_twin(
        twin_app,
        failing_steps=["citrix submit form"],
        repair_fn=lambda payload, turn: (
            "def repair(ui, vars):\n"
            '    ui.fill(0, vars["$title"])\n'
            "    ui.click(1)\n"
        ),
        readback_check=lambda page, app: (
            page.wait_for_load_state("domcontentloaded") is None
            and "/record" in page.url
            and "Claim 900" in page.locator("body").inner_text()
            and len(app.store.get("records", [])) == 1
        ),
        max_turns=2,
        start_path="/form",
        var_map={"$title": "Claim 900"},
    )
    assert heal["verified"] is True
    assert heal["live_network_requests_attempted"] == 0

    nav = AppNavigationGraph()
    nav.record_transition("/form", "/record", ControlSignature(role="button", label_tokens=("save",)))
    assert len(nav.shortest_path("/form", "/record")) == 1

    watcher = ReconciliationWatcher()
    watcher.enqueue(
        PendingReconciliationRecord(
            obligation_id="ob-citrix-1",
            card_id="card-citrix",
            portal_origin=twin_app.base_url,
            receipt_ref=heal["store_snapshot"]["records"][0]["id"],
            expected_by_readback_key={"title": ("Claim 900", "casefold_ws", 0)},
        )
    )
    settled = watcher.poll_until_settled(
        lambda r: (any(rec["id"] == r.receipt_ref for rec in heal["store_snapshot"]["records"]), "persisted_in_store")
    )
    assert settled["ob-citrix-1"].status == "RECONCILED_COMPLETE"
