"""Tests for Stateful Multi-Page Zero-PHI Digital Twin and DGM-PR Outer-Loop Harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel.dgm_pr import (
    FailureRecord,
    KernelPRProposal,
    cluster_pack_failures,
    draft_kernel_pr_proposal,
    validate_kernel_pr_safety,
)
from openadapt_kernel.stateful_twin import (
    TwinPageSpec,
    TwinRouteSpec,
    self_heal_multipage_in_twin,
    synthesize_zero_phi_stateful_twin,
)

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def _build_multipage_specs() -> tuple[List[TwinPageSpec], List[TwinRouteSpec]]:
    login_html = f"""<!doctype html><html><body>
      <h1>Login</h1>
      <img src="data:image/png;base64,{TINY_PNG_B64}" alt="Maria Gonzalez Photo" />
      <p>Member: Maria Gonzalez SSN 123-45-6789 DOB 1981-04-12</p>
      <form method="post" action="/login">
        <label>Username</label><input type="text" name="username" value="Maria Gonzalez" required />
        <button type="submit">Continue</button>
      </form>
      <script>window.__SECRET__ = "MRN-994821";</script>
    </body></html>"""

    search_html = """<!doctype html><html><body>
      <h1>Create Record</h1>
      <p>Patient: Maria Gonzalez MRN-994821</p>
      <form method="post" action="/submit">
        <label>Title</label><input type="text" name="title" placeholder="Maria Gonzalez Claim" required />
        <label>Note</label><input type="text" name="note" required />
        <button type="submit">Save</button>
      </form>
      <a href="/record">View Record</a>
    </body></html>"""

    record_html = """<!doctype html><html><body>
      <h1>Record Summary</h1>
      <div data-oa-twin-store="records"></div>
      <div data-oa-twin-store="notes"></div>
      <form method="post" action="/note">
        <label>Note</label><input type="text" name="memo" required />
        <button type="submit">Add</button>
      </form>
    </body></html>"""

    pages = [
        TwinPageSpec(url_path="/login", sanitized_html=login_html),
        TwinPageSpec(url_path="/search", sanitized_html=search_html),
        TwinPageSpec(url_path="/record", sanitized_html=record_html),
    ]
    routes = [
        TwinRouteSpec(
            method="POST",
            path_pattern="/login",
            mutates_collection="session",
            status_code=303,
            redirect_to="/search",
        ),
        TwinRouteSpec(
            method="POST",
            path_pattern="/submit",
            mutates_collection="records",
            status_code=303,
            redirect_to="/record",
        ),
        TwinRouteSpec(
            method="POST",
            path_pattern="/note",
            mutates_collection="notes",
            status_code=303,
            redirect_to="/record",
        ),
        TwinRouteSpec(
            method="GET",
            path_pattern="/api/records",
            response_template={"status": "ok", "items": "{{collection}}", "count": "{{count}}"},
            mutates_collection="records",
            status_code=200,
        ),
    ]
    return pages, routes


def test_stateful_twin_strips_phi_and_persists_multipage_post_to_get_readback(
    tmp_path: Path,
) -> None:
    """Multi-page offline Playwright execution: Page 1 POST -> store -> Page 2 GET readback, 0 PHI, 0 network."""
    pages, routes = _build_multipage_specs()
    ledger_path = tmp_path / "stateful_twin_egress.jsonl"
    twin_app = synthesize_zero_phi_stateful_twin(pages, api_routes=routes, ledger_path=ledger_path)

    phi_tokens = ("Maria", "Gonzalez", "123-45-6789", "1981-04-12", "MRN-994821", "data:image", "iVBOR", "<script")
    ledger_text = ledger_path.read_text(encoding="utf-8")
    for spec in twin_app.pages.values():
        for secret in phi_tokens:
            assert secret not in spec.sanitized_html, f"PHI leaked in {spec.url_path}: {secret!r}"
    for secret in phi_tokens:
        assert secret not in ledger_text, f"PHI leaked in egress ledger: {secret!r}"

    audit = twin_app.egress_audit()
    assert audit["sha256_chain_integrity_verified"] is True
    assert audit["blocked_count"] == 0
    assert audit["allowed_count"] >= len(pages) + len(routes)

    def mock_repair_fn(outbound_payload: Dict[str, Any], turn_idx: int) -> str:
        # Turn 1 tries an unsafe import to verify AST firewall retry.
        if turn_idx == 1:
            return "import os\ndef repair(ui, vars):\n    ui.click(2)\n"
        return (
            "def repair(ui, vars):\n"
            '    ui.fill(0, vars["$title"])\n'
            '    ui.fill(1, vars["$note"])\n'
            "    ui.click(2)\n"
        )

    def readback_check(page: Any, app: Any) -> bool:
        page.wait_for_load_state("domcontentloaded")
        if "/record" not in page.url:
            return False
        body = page.locator("body").inner_text()
        if "Followup Plan" not in body or "Approved" not in body:
            return False
        api_json = page.evaluate("async () => (await fetch('/api/records')).json()")
        return api_json.get("status") == "ok" and api_json.get("count") == 1 and len(app.store.get("records", [])) == 1

    result = self_heal_multipage_in_twin(
        twin_app,
        failing_steps=["submit form on search page for Maria Gonzalez"],
        repair_fn=mock_repair_fn,
        readback_check=readback_check,
        max_turns=3,
        start_path="/search",
        var_map={"$title": "Followup Plan", "$note": "Approved"},
    )

    assert result["verified"] is True
    assert result["turns_used"] == 2
    assert result["live_network_requests_attempted"] == 0
    assert len(result["distilled_steps"]) == 3
    assert [s.method for s in result["distilled_steps"]] == ["fill_var", "fill_var", "click"]
    assert len(result["egress_ledger_entry_sha256"]) == 64
    assert result["egress_audit"]["sha256_chain_integrity_verified"] is True
    assert len(result["store_snapshot"]["records"]) == 1

    # Confirm external network requests are aborted and never leave the offline twin.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            twin_app.install_on_page(page)
            page.goto(twin_app.base_url + "/login", wait_until="domcontentloaded")
            failed_ext = page.evaluate(
                "async () => { try { await fetch('https://external.example.invalid/leak'); return false; } catch (e) { return true; } }"
            )
            assert failed_ext is True
            assert twin_app.live_network_requests_attempted == 1
        finally:
            browser.close()


def test_cluster_pack_failures_strips_phi_and_tenant_ids() -> None:
    """cluster_pack_failures groups recurring failure signatures while stripping tenant and PHI strings."""
    records = [
        FailureRecord(
            pack_id="clinic-alpha-pack-1",
            failure_category="css_allowlist_miss",
            signature_tokens=("style", "border", "Maria_Gonzalez"),
            error_message="non_allowlisted_token on Maria Gonzalez MRN-994821",
            tenant_id="tenant_hospital_north",
        ),
        FailureRecord(
            pack_id="clinic-beta-pack-2",
            failure_category="css_allowlist_miss",
            signature_tokens=("style", "border", "David_Chen"),
            error_message="non_allowlisted_token on David Chen 123-45-6789",
            tenant_id="tenant_hospital_south",
        ),
        FailureRecord(
            pack_id="clinic-gamma-pack-3",
            failure_category="exploratory_prefix_noise",
            signature_tokens=("modal", "button", "disabled"),
            error_message="submit disabled before required option",
            tenant_id="tenant_clinic_west",
        ),
        FailureRecord(
            pack_id="clinic-delta-pack-4",
            failure_category="exploratory_prefix_noise",
            signature_tokens=("modal", "button", "disabled"),
            error_message="submit disabled before required option",
            tenant_id="tenant_clinic_east",
        ),
        FailureRecord(
            pack_id="clinic-oneoff-pack-5",
            failure_category="rare_timeout",
            signature_tokens=("wait",),
            error_message="single timeout",
            tenant_id="tenant_oneoff",
        ),
    ]

    clusters = cluster_pack_failures(records, min_cluster_size=2)
    assert len(clusters) == 2
    serialized = json.dumps([c.__dict__ for c in clusters])
    for secret in ("Maria", "Gonzalez", "David", "Chen", "994821", "123-45-6789", "tenant_hospital", "clinic-alpha"):
        assert secret not in serialized
    assert all(c.occurrence_count == 2 for c in clusters)
    assert all(len(c.pack_ids) == 2 for c in clusters)


def test_validate_kernel_pr_safety_and_draft_proposal() -> None:
    """validate_kernel_pr_safety blocks verifier weakening, missing tests, app names, and legacy imports."""
    weak_diff = (
        "--- a/packages/kernel/src/openadapt_kernel/egress_guard.py\n"
        "+++ b/packages/kernel/src/openadapt_kernel/egress_guard.py\n"
        "@@ -10,3 +10,2 @@\n"
        "-    raise EgressBlockedError(verdict)\n"
        "+    return True  # skip\n"
    )
    ok_weak, violations_weak = validate_kernel_pr_safety(
        weak_diff,
        changed_files=[
            "packages/kernel/src/openadapt_kernel/egress_guard.py",
            "packages/kernel/tests/test_egress_regression.py",
        ],
    )
    assert ok_weak is False
    assert any("weakened_verifier_or_egress_check" in v for v in violations_weak)

    no_test_diff = (
        "--- a/packages/kernel/src/openadapt_kernel/macro.py\n"
        "+++ b/packages/kernel/src/openadapt_kernel/macro.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+# improve prefix pruning\n"
    )
    ok_no_test, violations_no_test = validate_kernel_pr_safety(
        no_test_diff,
        changed_files=["packages/kernel/src/openadapt_kernel/macro.py"],
    )
    assert ok_no_test is False
    assert "missing_regression_test" in violations_no_test

    app_and_legacy_diff = (
        "--- a/packages/kernel/src/openadapt_kernel/macro.py\n"
        "+++ b/packages/kernel/src/openadapt_kernel/macro.py\n"
        "@@ -1,2 +1,4 @@\n"
        "+from openadapt_flow.runner import run\n"
        "+# special case for dentrix portal\n"
    )
    ok_bad, violations_bad = validate_kernel_pr_safety(
        app_and_legacy_diff,
        changed_files=[
            "packages/kernel/src/openadapt_kernel/macro.py",
            "packages/kernel/tests/test_macro_fix.py",
        ],
    )
    assert ok_bad is False
    assert "forbidden_legacy_import:openadapt_flow" in violations_bad
    assert "forbidden_app_name:dentrix" in violations_bad

    safe_diff = (
        "--- a/packages/kernel/src/openadapt_kernel/macro.py\n"
        "+++ b/packages/kernel/src/openadapt_kernel/macro.py\n"
        "@@ -10,2 +10,3 @@\n"
        "+# prune redundant exploratory prefix steps before verified repair\n"
    )
    safe_files = [
        "packages/kernel/src/openadapt_kernel/macro.py",
        "packages/kernel/tests/test_prefix_pruning_regression.py",
    ]
    ok_safe, violations_safe = validate_kernel_pr_safety(safe_diff, changed_files=safe_files)
    assert ok_safe is True
    assert violations_safe == []

    clusters = cluster_pack_failures(
        [
            FailureRecord("p1", "exploratory_prefix_noise", ("modal", "button"), "disabled"),
            FailureRecord("p2", "exploratory_prefix_noise", ("modal", "button"), "disabled"),
        ],
        min_cluster_size=2,
    )
    proposal = draft_kernel_pr_proposal(
        clusters[0],
        patch_diff=safe_diff,
        changed_files=safe_files,
        verification_summary={"pytest_passed": True, "ruff_ok": True, "mypy_ok": True},
    )
    assert proposal.safety_ok is True
    assert proposal.safety_violations == ()
    assert proposal.requires_human_merge is True
    assert proposal.auto_merge_allowed is False
    assert proposal.proposal_id.startswith("dgm_pr_")

    with pytest.raises(ValueError):
        KernelPRProposal(
            proposal_id="dgm_pr_bad",
            cluster_id=clusters[0].cluster_id,
            title="bad",
            summary="bad",
            patch_diff=safe_diff,
            changed_files=tuple(safe_files),
            verification_summary={},
            safety_ok=True,
            requires_human_merge=False,
        )
