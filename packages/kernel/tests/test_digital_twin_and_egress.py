"""Tests for Zero-PHI Digital Twin Self-Healing Compiler and Customer-Verifiable Egress Guard."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest
from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    EgressBlockedError,
    EgressPolicy,
    LocalEgressGuard,
    capture_zero_phi_digital_twin,
    inspect_outbound_payload,
    replay_distilled_zero_phi_repair,
    self_heal_in_digital_twin,
    summarize_egress_ledger,
)

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def _post_json(url: str, payload: Any, content_type: str = "application/json") -> tuple[int, Dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else str(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_capture_zero_phi_digital_twin_strips_all_phi_and_images_and_passes_egress(
    tmp_path: Path,
) -> None:
    """Test 1: Synthetic regulated form with fake PHI and base64 PNG yields 0 PHI bytes and passes egress."""
    regulated_html = f"""<!doctype html><html><body>
    <div role="dialog" style="display:block;width:540px;height:420px;padding:12px;">
      <h2>Patient Claim Review</h2>
      <img src="data:image/png;base64,{TINY_PNG_B64}" alt="Maria Gonzalez ID Scan" />
      <canvas width="100" height="40"></canvas>
      <svg width="20" height="20"><circle cx="10" cy="10" r="8"/></svg>
      <p>Patient Name: Maria Gonzalez</p>
      <p>Date of Birth: 1981-04-12</p>
      <p>SSN: 123-45-6789 | MRN: MRN-994821 | Amount: $2,450.00</p>
      <label for="pt_name">Patient Name</label>
      <input id="pt_name" name="Maria_Gonzalez" type="text" value="Maria Gonzalez"
             placeholder="Enter Maria Gonzalez" aria-label="Patient Name Maria Gonzalez" required />
      <label for="dob_in">DOB</label>
      <input id="dob_in" type="date" value="1981-04-12" required />
      <label for="tier_sel">Plan Tier</label>
      <select id="tier_sel" required>
        <option value="">Select</option>
        <option value="Primary">Primary</option>
        <option value="MRN-994821">Maria Gonzalez Special Plan</option>
      </select>
      <button id="save_btn" type="submit" disabled>Save</button>
    </div>
    <script>window.__PHI__ = "Maria Gonzalez 123-45-6789 MRN-994821 $2,450.00";</script>
    </body></html>"""

    phi_strings = [
        "Maria",
        "Gonzalez",
        "1981-04-12",
        "1981",
        "123-45-6789",
        "6789",
        "MRN-994821",
        "994821",
        "$2,450.00",
        "2,450",
        "2450",
        "data:image",
        "base64",
        "iVBOR",
        "<img",
        "<canvas",
        "<svg",
        "<script",
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(regulated_html)
            twin = capture_zero_phi_digital_twin(page)
        finally:
            browser.close()

    serialized_controls = json.dumps(twin.controls)
    for secret in phi_strings:
        assert secret not in twin.sanitized_html, f"PHI/binary leaked into twin HTML: {secret!r}"
        assert secret not in serialized_controls, f"PHI/binary leaked into twin controls: {secret!r}"

    assert "CANARY_TEXT_" in twin.sanitized_html
    assert "CANARY_DATE_" in twin.sanitized_html
    assert "CANARY_NUM_" in twin.sanitized_html
    assert 'role="dialog"' in twin.sanitized_html
    assert "data-oa-idx=" in twin.sanitized_html
    assert "disabled" in twin.sanitized_html
    assert "required" in twin.sanitized_html
    assert twin.egress_verdict.allowed is True

    ledger_path = tmp_path / "outbound_egress_ledger.jsonl"
    guard = LocalEgressGuard(ledger_path)
    verdict = guard.check_and_record(twin.sanitized_html, destination="digital_twin_export")
    assert verdict.allowed is True
    assert len(verdict.entry_sha256) == 64
    assert len(verdict.payload_sha256) == 64
    assert guard.verify_ledger_integrity() is True

    with guard.serve_http() as base_url:
        status, body = _post_json(f"{base_url}/twin", {"sanitized_html": twin.sanitized_html})
        assert status == 200
        assert body["allowed"] is True
        assert len(body["entry_sha256"]) == 64


def test_active_red_team_phi_poisoning_hard_blocked_3_of_3_via_http_403_and_ledger(
    tmp_path: Path,
) -> None:
    """Test 2: Active red-team poisoning attempts (name, SSN, base64 PNG) are hard-blocked 3/3."""
    ledger_path = tmp_path / "outbound_egress_ledger.jsonl"
    guard = LocalEgressGuard(ledger_path)

    poison_cases = [
        ("raw_patient_name", {"text": "Patient Name: Maria Gonzalez"}, "non_allowlisted_token"),
        ("raw_ssn", {"text": "SSN 123-45-6789"}, "non_allowlisted_token"),
        (
            "base64_png_screenshot",
            {"screenshot": f"data:image/png;base64,{TINY_PNG_B64}"},
            "binary_or_image_blocked",
        ),
    ]

    with guard.serve_http() as base_url:
        for case_name, payload, expected_reason in poison_cases:
            status, body = _post_json(f"{base_url}/{case_name}", payload)
            assert status == 403, f"Expected HTTP 403 for {case_name}, got {status}: {body}"
            assert body["allowed"] is False
            assert body["reason"] == expected_reason
            assert body["rejected_tokens"], f"Expected rejected_tokens for {case_name}"

            with pytest.raises(EgressBlockedError) as exc_info:
                guard.call_guarded(lambda p: p, payload, destination=f"direct:{case_name}")
            assert exc_info.value.verdict.allowed is False
            assert exc_info.value.verdict.reason == expected_reason

    # Raw bytes / image MIME also hard-blocked.
    raw_verdict = inspect_outbound_payload(b"\x89PNG\r\n\x1a\nfake_png_bytes", EgressPolicy())
    assert raw_verdict.allowed is False
    assert raw_verdict.reason == "binary_or_image_blocked"

    entries = guard.read_ledger()
    summary = summarize_egress_ledger(entries)
    assert summary["blocked_count"] == 6
    assert summary["allowed_count"] == 0
    assert summary["total_allowed_bytes"] == 0
    assert guard.verify_ledger_integrity() is True

    # Tampering with even one character in the ledger breaks cryptographic integrity.
    tampered = ledger_path.read_text(encoding="utf-8").replace("Maria Gonzalez", "Maria_Redacted")
    ledger_path.write_text(tampered, encoding="utf-8")
    assert guard.verify_ledger_integrity() is False


def test_end_to_end_offline_twin_tdd_self_heal_and_replay_with_zero_phi_and_zero_network(
    tmp_path: Path,
) -> None:
    """Test 3: Offline twin TDD self-heal repairs disabled-submit modal with 0 network and 0 PHI exported."""
    portal_html = f"""<!doctype html><html><body>
    <div role="dialog" style="display:block;width:520px;height:340px;padding:16px;">
      <h2>Claim Submission</h2>
      <img src="data:image/png;base64,{TINY_PNG_B64}" alt="Chart Scan" />
      <p>Member: Maria Gonzalez (1981-04-12) MRN-994821</p>
      <label for="nm">Patient Name</label>
      <input id="nm" type="text" required />
      <label for="plan">Plan Option</label>
      <select id="plan" required>
        <option value="">Select</option>
        <option value="Primary">Primary</option>
        <option value="Secondary">Secondary</option>
      </select>
      <button id="save_btn" type="submit" disabled>Save</button>
    </div>
    <div id="result"></div>
    <script>
      const nm = document.getElementById('nm');
      const plan = document.getElementById('plan');
      const save = document.getElementById('save_btn');
      const sync = () => {{ save.disabled = !(nm.value.trim().length > 0 && plan.value.length > 0); }};
      nm.addEventListener('input', sync);
      plan.addEventListener('change', sync);
      save.addEventListener('click', () => {{
        document.getElementById('result').innerText = 'SUBMITTED:' + nm.value + ':' + plan.value;
      }});
    </script>
    </body></html>"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            live_page = browser.new_page()
            live_page.set_content(portal_html)
            twin = capture_zero_phi_digital_twin(live_page)
        finally:
            browser.close()

    ledger_path = tmp_path / "outbound_egress_ledger.jsonl"
    guard = LocalEgressGuard(ledger_path)
    seen_prompts: List[str] = []

    def mock_coding_agent(
        twin_html: str,
        var_keys: List[str],
        safe_error: str,
        feedback: Dict[str, Any],
    ) -> str:
        seen_prompts.append(twin_html)
        # Turn 1 intentionally tries a disallowed import to exercise AST firewall retry.
        if feedback["turn"] == 1:
            return "import os\ndef repair(ui, vars):\n    ui.click(2)\n"
        return (
            "def repair(ui, vars):\n"
            '    ui.fill(0, vars["$patient_name"])\n'
            '    ui.select(1, vars["$plan_option"])\n'
            "    ui.click(2)\n"
        )

    heal_res = self_heal_in_digital_twin(
        twin,
        var_map={"$patient_name": "Maria Gonzalez", "$plan_option": "Primary"},
        coding_agent_fn=mock_coding_agent,
        egress_guard=guard,
        max_turns=3,
        structural_error="Save button disabled for Maria Gonzalez MRN-994821",
    )

    assert heal_res.verified is True
    assert heal_res.turns_used == 2
    assert heal_res.live_network_requests_attempted == 0
    assert len(heal_res.egress_ledger_entry_sha256) == 64
    assert len(heal_res.distilled_steps) == 3
    assert [s.method for s in heal_res.distilled_steps] == ["fill_var", "select", "click"]

    # Verify zero PHI ever reached coding_agent_fn or the compliance ledger.
    ledger_text = ledger_path.read_text(encoding="utf-8")
    for secret in ("Maria", "Gonzalez", "1981-04-12", "MRN-994821", "iVBOR", "data:image"):
        assert secret not in ledger_text
        for prompt in seen_prompts:
            assert secret not in prompt
    assert guard.verify_ledger_integrity() is True

    # Replay the distilled steps locally on a fresh live portal page with DIFFERENT private data (0 cloud calls).
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            fresh_page = browser.new_page()
            fresh_page.set_content(portal_html)
            replayed = replay_distilled_zero_phi_repair(
                fresh_page,
                heal_res.distilled_steps,
                private_vars={"$patient_name": "David Chen", "$plan_option": "Primary"},
                readback_oracle=lambda p: "SUBMITTED:David Chen:Primary" in p.locator("#result").inner_text(),
            )
            assert replayed is True
        finally:
            browser.close()
