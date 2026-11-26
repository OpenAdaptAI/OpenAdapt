"""Tests for Stateful Multi-Page Zero-PHI Digital Twin and DGM-PR Outer-Loop Harness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel.dgm_pr import (
    FailureRecord,
    KernelPRProposal,
    cluster_pack_failures,
    draft_kernel_pr_proposal,
    make_cli_coding_agent,
    run_dgm_pr_coding_agent,
    validate_kernel_pr_safety,
)
from openadapt_kernel.egress_guard import EgressBlockedError, LocalEgressGuard
from openadapt_kernel.stateful_twin import (
    A11yNodeSpec,
    A11yScreenSpec,
    TwinPageSpec,
    TwinRouteSpec,
    project_a11y_tree_to_zero_phi_twin,
    self_heal_multipage_in_twin,
    synthesize_twin_with_coding_agent,
    synthesize_zero_phi_stateful_twin,
)
from openadapt_kernel.zero_phi_repair import (
    DistilledRepairStep,
    execute_zero_phi_coding_repair,
    replay_distilled_zero_phi_repair,
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


def test_project_a11y_tree_desktop_and_citrix_to_zero_phi_twin_e2e(tmp_path: Path) -> None:
    """Project native UIA/Citrix screen-graph into Zero-PHI StatefulTwinApp and self-heal offline."""
    screens = [
        A11yScreenSpec(
            screen_id="claim_entry",
            url_path="/form",
            nodes=(
                A11yNodeSpec("tab_1", "TabItem", "Claim", bbox=(0, 0, 120, 32), platform="uia"),
                A11yNodeSpec(
                    "ed_1",
                    "Edit",
                    "Patient Name",
                    value="Maria Gonzalez",
                    bbox=(10, 40, 260, 32),
                    platform="uia",
                ),
                A11yNodeSpec(
                    "cb_1",
                    "ComboBox",
                    "Plan",
                    bbox=(10, 84, 200, 32),
                    expanded=False,
                    platform="citrix_box",
                    options=("Select", "Primary", "Secondary"),
                ),
                A11yNodeSpec(
                    "chk_1",
                    "CheckBox",
                    "Confirm",
                    checked=False,
                    bbox=(10, 128, 24, 24),
                    platform="macos_ax",
                ),
                A11yNodeSpec(
                    "grid_1",
                    "DataGrid",
                    "History",
                    value="MRN-994821 123-45-6789",
                    bbox=(10, 160, 320, 80),
                    platform="at_spi",
                ),
                A11yNodeSpec("btn_1", "Button", "Save", bbox=(10, 252, 100, 36), platform="uia"),
            ),
            transitions=(
                TwinRouteSpec(
                    method="POST",
                    path_pattern="/submit",
                    mutates_collection="records",
                    status_code=303,
                    redirect_to="/record",
                ),
            ),
        ),
        A11yScreenSpec(
            screen_id="claim_summary",
            url_path="/record",
            nodes=(
                A11yNodeSpec("hdr_1", "Text", "Record Summary", bbox=(0, 0, 200, 28), platform="uia"),
            ),
            store_collection="records",
        ),
    ]

    ledger_path = tmp_path / "a11y_twin_egress.jsonl"
    twin_app = project_a11y_tree_to_zero_phi_twin(screens, ledger_path=ledger_path)

    form_html = twin_app.pages["/form"].sanitized_html
    ledger_text = ledger_path.read_text(encoding="utf-8")
    for secret in ("Maria", "Gonzalez", "MRN-994821", "123-45-6789"):
        assert secret not in form_html
        assert secret not in ledger_text
    assert "$var_1" in form_html
    assert 'role="tab"' in form_html
    assert "<table" in form_html
    assert "width:260px" in form_html
    assert twin_app.egress_audit()["sha256_chain_integrity_verified"] is True

    def repair_desktop_flow(outbound_payload: Dict[str, Any], turn_idx: int) -> str:
        return (
            "def repair(ui, vars):\n"
            '    ui.fill(1, vars["$patient"])\n'
            '    ui.select(2, vars["$plan"])\n'
            "    ui.check(3, True)\n"
            "    ui.click(4)\n"
        )

    def readback_check(page: Any, app: Any) -> bool:
        page.wait_for_load_state("domcontentloaded")
        if "/record" not in page.url:
            return False
        body = page.locator("body").inner_text()
        return "David Chen" in body and "Primary" in body and len(app.store.get("records", [])) == 1

    res = self_heal_multipage_in_twin(
        twin_app,
        failing_steps=["desktop submit form"],
        repair_fn=repair_desktop_flow,
        readback_check=readback_check,
        max_turns=2,
        start_path="/form",
        var_map={"$patient": "David Chen", "$plan": "Primary"},
    )
    assert res["verified"] is True
    assert res["live_network_requests_attempted"] == 0
    assert len(res["store_snapshot"]["records"]) == 1
    assert res["egress_audit"]["sha256_chain_integrity_verified"] is True


def test_control_signature_first_distilled_replay_survives_shifted_modal_ordinal() -> None:
    """ControlSignature-first replay resolves semantic role/label even when modal ordinals shift."""
    base_modal_html = """<!doctype html><html><body>
    <div role="dialog" style="display:block;width:520px;height:320px;">
      <label for="nm">Cookbook Name</label><input id="nm" type="text" />
      <label for="pub">Public</label><input id="pub" type="checkbox" />
      <button id="save" type="submit">Save</button>
    </div>
    <div id="out"></div>
    <script>
      document.getElementById('save').addEventListener('click', () => {
        document.getElementById('out').innerText =
          'SAVED:' + document.getElementById('nm').value + ':' + document.getElementById('pub').checked;
      });
    </script></body></html>"""

    shifted_modal_html = """<!doctype html><html><body>
    <div role="dialog" style="display:block;width:560px;height:420px;">
      <label for="adv">Advanced</label><input id="adv" type="checkbox" />
      <label for="flt">Filter</label><input id="flt" type="text" />
      <button id="clr" type="button">Clear</button>
      <label for="nm2">Cookbook Name</label><input id="nm2" type="text" />
      <label for="pub2">Public</label><input id="pub2" type="checkbox" />
      <button id="save2" type="submit">Save</button>
    </div>
    <div id="out"></div>
    <script>
      document.getElementById('save2').addEventListener('click', () => {
        document.getElementById('out').innerText =
          'SAVED:' + document.getElementById('nm2').value + ':' + document.getElementById('pub2').checked;
      });
    </script></body></html>"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(base_modal_html)
            cold = execute_zero_phi_coding_repair(
                page,
                private_vars={"$title": "Holiday Menu"},
                structural_error="submit disabled",
                coding_agent_fn=lambda twin, vkeys, err: (
                    "def repair(ui, vars):\n"
                    '    ui.fill(0, vars["$title"])\n'
                    "    ui.check(1, True)\n"
                    "    ui.click(2)\n"
                ),
                readback_oracle=lambda p: "SAVED:Holiday Menu:true" in p.locator("#out").inner_text(),
            )
            assert cold.verified is True
            assert len(cold.distilled_steps) == 3
            assert all(s.control_signature is not None for s in cold.distilled_steps)

            # Strip selectors so resolution MUST use ControlSignature (get_by_role / get_by_label),
            # while modal_ordinal (0, 1, 2) now points at the WRONG shifted controls (Advanced, Filter, Clear).
            sig_only_steps = [
                DistilledRepairStep(
                    method=s.method,
                    selector="",
                    modal_ordinal=s.modal_ordinal,
                    var_key=s.var_key,
                    arg=s.arg,
                    is_stripped_target=s.is_stripped_target,
                    ctrl_sig=("", "", "", ""),
                    control_signature=s.control_signature,
                    semantic_role=s.semantic_role,
                    semantic_label=s.semantic_label,
                )
                for s in cold.distilled_steps
            ]

            shifted_page = browser.new_page()
            shifted_page.set_content(shifted_modal_html)
            ok = replay_distilled_zero_phi_repair(
                shifted_page,
                sig_only_steps,
                private_vars={"$title": "Winter Feast"},
                readback_oracle=lambda p: "SAVED:Winter Feast:true" in p.locator("#out").inner_text(),
            )
            assert ok is True
        finally:
            browser.close()


def test_make_cli_coding_agent_egress_blocks_phi_before_spawning_subprocess(tmp_path: Path) -> None:
    """make_cli_coding_agent audits outbound prompt via LocalEgressGuard and parses CLI JSON cleanly."""
    ledger = tmp_path / "cli_egress.jsonl"
    guard = LocalEgressGuard(ledger)

    # Pluggable command_template using python3 to simulate a JSON-emitting coding CLI.
    cli_agent = make_cli_coding_agent(
        command_template=(
            sys.executable,
            "-c",
            'import json, sys; txt = sys.stdin.read(); print(json.dumps({"result": "ok:" + txt.strip()}))',
        ),
        egress_guard=guard,
    )

    assert cli_agent("save form") == "ok:save form"
    with pytest.raises(EgressBlockedError):
        cli_agent("Patient Maria Gonzalez 123-45-6789")
    assert guard.verify_ledger_integrity() is True


def test_synthesize_twin_with_coding_agent_e2e_offline_playwright(tmp_path: Path) -> None:
    """synthesize_twin_with_coding_agent audits trace, parses agent JSON, and runs offline Playwright."""
    ledger = tmp_path / "agent_twin_egress.jsonl"

    # PHI-poisoned trace summary is hard-blocked before coding_agent_fn is ever called.
    called = False

    def fail_if_called(prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    with pytest.raises(EgressBlockedError):
        synthesize_twin_with_coding_agent(
            {"steps": ["fill Maria Gonzalez 123-45-6789"]},
            coding_agent_fn=fail_if_called,
            ledger_path=ledger,
        )
    assert called is False

    # Clean sanitized trace summary synthesizes a runnable multi-page StatefulTwinApp.
    def stub_coding_agent(prompt: str) -> str:
        assert "Maria" not in prompt
        return json.dumps(
            {
                "pages": [
                    {
                        "url_path": "/form",
                        "html": (
                            '<form method="post" action="/submit">'
                            '<label>Title</label><input type="text" name="title" required />'
                            '<button type="submit">Save</button></form>'
                        ),
                    },
                    {
                        "url_path": "/record",
                        "html": '<h1>Record</h1><div data-oa-twin-store="records"></div>',
                    },
                ],
                "api_routes": [
                    {
                        "method": "POST",
                        "path_pattern": "/submit",
                        "mutates_collection": "records",
                        "response_template": {"status": "ok"},
                        "status_code": 303,
                        "redirect_to": "/record",
                    }
                ],
            }
        )

    clean_ledger = tmp_path / "clean_agent_twin_egress.jsonl"
    twin_app = synthesize_twin_with_coding_agent(
        {"workflow": "create record", "pages": ["form", "record"]},
        coding_agent_fn=stub_coding_agent,
        ledger_path=clean_ledger,
    )

    res = self_heal_multipage_in_twin(
        twin_app,
        failing_steps=["submit form"],
        repair_fn=lambda payload, turn: (
            "def repair(ui, vars):\n"
            '    ui.fill(0, vars["$title"])\n'
            "    ui.click(1)\n"
        ),
        readback_check=lambda page, app: (
            page.wait_for_load_state("domcontentloaded") is None
            and "/record" in page.url
            and "Quarterly Review" in page.locator("body").inner_text()
            and len(app.store.get("records", [])) == 1
        ),
        max_turns=2,
        start_path="/form",
        var_map={"$title": "Quarterly Review"},
    )
    assert res["verified"] is True
    assert res["live_network_requests_attempted"] == 0
    assert res["egress_audit"]["sha256_chain_integrity_verified"] is True


def test_run_dgm_pr_coding_agent_isolated_worktree_safe_and_unsafe(tmp_path: Path) -> None:
    """run_dgm_pr_coding_agent runs in an isolated git worktree, approves safe fixes, blocks unsafe diffs."""
    repo = tmp_path / "mini_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "egress_guard.py").write_text(
        "def check(ok: bool) -> bool:\n    assert ok\n    return True\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_basic.py").write_text(
        "from src.egress_guard import check\n\ndef test_ok() -> None:\n    assert check(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    clusters = cluster_pack_failures(
        [
            FailureRecord("p1", "css_allowlist_miss", ("style", "border"), "error"),
            FailureRecord("p2", "css_allowlist_miss", ("style", "border"), "error"),
        ],
        min_cluster_size=2,
    )
    cluster = clusters[0]

    # 1. Safe fix + regression test passes safety validation and pytest inside isolated worktree.
    def safe_agent(_prompt: str) -> str:
        return json.dumps(
            {
                "files": [
                    {
                        "path": "src/helper.py",
                        "content": "def normalize(val: str) -> str:\n    return val.strip().lower()\n",
                    },
                    {
                        "path": "tests/test_helper_regression.py",
                        "content": (
                            "from src.helper import normalize\n\n"
                            "def test_normalize() -> None:\n"
                            "    assert normalize(' Border ') == 'border'\n"
                        ),
                    },
                ],
                "summary": "Add normalize helper and regression test",
            }
        )

    safe_prop = run_dgm_pr_coding_agent(
        cluster,
        repo_root=repo,
        coding_agent_fn=safe_agent,
        run_checks=True,
        check_commands=([sys.executable, "-m", "pytest", "-q"],),
    )
    assert safe_prop.safety_ok is True
    assert safe_prop.requires_human_merge is True
    assert safe_prop.auto_merge_allowed is False
    assert safe_prop.verification_summary["checks_passed"] is True
    assert not (repo / "src" / "helper.py").exists()

    # 2. Unsafe verifier-weakening patch is hard-blocked by validate_kernel_pr_safety.
    def unsafe_agent(_prompt: str) -> str:
        return json.dumps(
            {
                "files": [
                    {
                        "path": "src/egress_guard.py",
                        "content": "def check(ok: bool) -> bool:\n    return True  # bypass\n",
                    },
                    {
                        "path": "tests/test_bypass.py",
                        "content": "def test_dummy() -> None:\n    assert True\n",
                    },
                ],
                "summary": "Remove assert check",
            }
        )

    unsafe_prop = run_dgm_pr_coding_agent(
        cluster,
        repo_root=repo,
        coding_agent_fn=unsafe_agent,
        run_checks=True,
    )
    assert unsafe_prop.safety_ok is False
    assert any("weakened_verifier_or_egress_check" in v for v in unsafe_prop.safety_violations)
    assert unsafe_prop.requires_human_merge is True
