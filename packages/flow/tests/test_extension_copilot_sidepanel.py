"""End-to-end and contract tests for the OpenAdapt Chrome Side-Panel Co-Pilot.

Covers:
1. ``CopilotViewState`` secret/password key redaction and schema contract.
2. ``CopilotSessionBridge`` atomic state persistence and ``os.O_APPEND`` +
   ``idempotency_key`` decision deduplication and draining.
3. ``openadapt_extension_host.py`` fail-closed validation for
   ``COPILOT_GET_STATE`` and ``COPILOT_SUBMIT_DECISION`` alongside
   ``EXTENSION_EVIDENCE``.
4. Real headless Chromium with the unpacked MV3 extension, installed native
   messaging host, routed ``http://portal.local/form`` tab, and live
   ``chrome-extension://<id>/sidepanel.html`` interaction.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openadapt_flow.copilot_bridge import (
    COPILOT_DECISIONS_FILE,
    COPILOT_STATE_FILE,
    COPILOT_STATE_SCHEMA,
    REDACTED_SECRET,
    CopilotFieldHighlight,
    CopilotSessionBridge,
    CopilotViewState,
)
from openadapt_flow.copilot_protocol import CopilotRunBinding
from openadapt_flow.form_tx.types import (
    FormHaltItem,
    FormHaltKind,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    ObservedOption,
)

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome_extension"
INSTALLER = EXT / "native_host" / "install_host.py"
HOST = EXT / "native_host" / "openadapt_extension_host.py"

LIVE_PORTAL_PASSWORD = "-".join(["synthetic", "portal", "secret", "9941"])

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows registers native hosts in the registry"
)


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_host", INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_host = _load_installer()


def _binding(run_id):
    return CopilotRunBinding(
        run_id=run_id,
        session_nonce="a" * 64,
        generation=1,
        pack_digest="b" * 64,
        dossier_digest="c" * 64,
        operator_id="chrome_sidepanel_operator",
        expires_at_unix=int(time.time()) + 300,
    )


def _claim_all(bridge, binding):
    result = []
    while (item := bridge.claim_bound_decision(binding)) is not None:
        result.append(item.decision)
    return result


def _frame(message: object) -> bytes:
    raw = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _run_host(recording_dir: Path, messages: list[object]) -> list[dict]:
    payload = b"".join(_frame(m) for m in messages)
    proc = subprocess.run(
        [sys.executable, str(HOST), "--recording", str(recording_dir)],
        input=payload,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    out = proc.stdout
    replies: list[dict] = []
    while out:
        (length,) = struct.unpack("<I", out[:4])
        replies.append(json.loads(out[4 : 4 + length].decode("utf-8")))
        out = out[4 + length :]
    return replies


# --- Python bridge & redaction contracts ------------------------------------


def test_copilot_view_state_redacts_secrets_and_password_keys():
    raw_secret_a = "-".join(["live", "pw", "alpha"])
    raw_secret_b = "-".join(["live", "pin", "7744"])
    raw_token = "Bearer " + "-".join(["live", "token", "xyz987"])

    state = CopilotViewState(
        run_id="run-redact-01",
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        status_line=raw_token,
        fields_filled_auto=2,
        total_bound_fields=5,
        halts=(
            FormHaltItem(
                halt_id="halt-secret-field",
                kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                dom_selector="input#account_password",
                field_label="Account Password",
                dossier_key="credentials.password",
                portal_value=raw_secret_a,
                dossier_value=raw_secret_a + "-dossier",
                message="Password field mismatch.",
            ),
            FormHaltItem(
                halt_id="halt-entity-pin",
                kind=FormHaltKind.PREFILL_ENTITY_CONFLICT,
                dom_selector="#row-1",
                field_label="Household Row",
                portal_value={"full_name": "Alex Morgan", "pin": raw_secret_b},
                dossier_value={"full_name": "Alex Morgan", "pin": "0000"},
                message="Entity row conflict.",
            ),
        ),
        field_highlights=(
            CopilotFieldHighlight(
                dom_selector="#row-1",
                badge_state="conflict",
                caption=raw_token,
            ),
        ),
        outputs={
            "reference_number": "REF-100200",
            "portal_password": raw_secret_a,
            "nested": {"api_key": raw_secret_b, "status": "verified"},
        },
    )

    assert state.schema_version == COPILOT_STATE_SCHEMA
    assert state.status_line == REDACTED_SECRET
    assert state.halts[0].portal_value == REDACTED_SECRET
    assert state.halts[0].dossier_value == REDACTED_SECRET
    assert state.halts[1].portal_value == {
        "full_name": "Alex Morgan",
        "pin": REDACTED_SECRET,
    }
    assert state.field_highlights[0].caption == REDACTED_SECRET
    assert state.outputs == {
        "reference_number": "REF-100200",
        "portal_password": REDACTED_SECRET,
        "nested": {"api_key": REDACTED_SECRET, "status": "verified"},
    }

    serialized = state.model_dump_json()
    assert raw_secret_a not in serialized
    assert raw_secret_b not in serialized
    assert "xyz987" not in serialized


def test_copilot_session_bridge_atomic_state_and_idempotent_decisions(tmp_path: Path):
    bridge = CopilotSessionBridge(tmp_path / "session")
    assert bridge.read_state() is None
    assert bridge.drain_new_decisions(set()) == []

    state = CopilotViewState(
        run_id="run-bridge-01",
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        status_line="Paused at 1 ungrounded field.",
        fields_filled_auto=3,
        total_bound_fields=4,
    )
    bridge.publish_state(state)

    assert (bridge.session_dir / COPILOT_STATE_FILE).is_file()
    assert list(bridge.session_dir.glob(".copilot_state.*.tmp")) == []
    loaded = bridge.read_state()
    assert loaded == state

    d1 = HumanResolutionInput(
        halt_id="halt-1",
        action=HumanDecisionAction.PROVIDE_VALUE,
        actor_id="op-1",
        provided_value="standard",
        mapped_dossier_key="plan.tier",
        remember_as_candidate_rule=True,
        idempotency_key="idem-001",
    )
    d2 = HumanResolutionInput(
        halt_id="halt-2",
        action=HumanDecisionAction.DISMISS_PORTAL_ENTITY,
        actor_id="op-1",
        idempotency_key="idem-002",
    )

    assert bridge.append_decision(d1) == (True, 1)
    # Duplicate idempotency_key is ignored without adding another JSONL row.
    assert bridge.append_decision(d1) == (False, 1)
    assert bridge.append_decision(d2) == (True, 2)

    raw_lines = (
        (bridge.session_dir / COPILOT_DECISIONS_FILE)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(raw_lines) == 2

    seen: set[str] = set()
    first_drain = bridge.drain_new_decisions(seen)
    assert [d.idempotency_key for d in first_drain] == ["idem-001", "idem-002"]
    assert seen == {"idem-001", "idem-002"}
    assert bridge.drain_new_decisions(seen) == []

    d3 = HumanResolutionInput(
        halt_id="halt-3",
        action=HumanDecisionAction.CONFIRM_MODAL_ACTION,
        actor_id="op-1",
        idempotency_key="idem-003",
    )
    assert bridge.append_decision(d3) == (True, 3)
    second_drain = bridge.drain_new_decisions(seen)
    assert [d.idempotency_key for d in second_drain] == ["idem-003"]


def test_native_host_copilot_messages_and_fail_closed_validation(tmp_path: Path):
    bridge = CopilotSessionBridge(tmp_path)

    # Initial empty state + unknown type + invalid decision schema fail closed.
    replies = _run_host(
        tmp_path,
        [
            {"type": "COPILOT_GET_STATE"},
            {"type": "UNKNOWN_COMMAND"},
            {
                "type": "COPILOT_SUBMIT_DECISION",
                "decision": {
                    "halt_id": "h1",
                    "action": "not_a_valid_action",
                    "actor_id": "op",
                    "idempotency_key": "k1",
                },
            },
            {
                "type": "COPILOT_SUBMIT_DECISION",
                "decision": {
                    "halt_id": "h1",
                    "action": "provide_value",
                    "actor_id": "op",
                    "idempotency_key": "k1",
                    "unexpected_extra_field": True,
                },
            },
        ],
    )
    assert replies[0] == {"ok": True, "type": "COPILOT_STATE", "state": None}
    assert replies[1]["ok"] is False
    assert replies[2]["ok"] is False
    assert replies[3]["ok"] is False
    assert not (tmp_path / COPILOT_DECISIONS_FILE).exists()

    # Publish state and verify co-existence with EXTENSION_EVIDENCE + decision dedup.
    published = CopilotViewState(
        run_id="run-host-02",
        binding=_binding("run-host-02"),
        halts=(
            FormHaltItem(
                halt_id="halt-tier",
                kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                message="Choose a tier",
            ),
        ),
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        status_line="Awaiting operator resolution",
        fields_filled_auto=5,
        total_bound_fields=7,
    )
    bridge.publish_state(published)

    valid_decision = {
        "halt_id": "halt-tier",
        "action": "provide_value",
        "actor_id": "chrome_sidepanel_operator",
        "provided_value": "plus",
        "remember_as_candidate_rule": True,
        "idempotency_key": "idem-host-1",
    }
    replies_live = _run_host(
        tmp_path,
        [
            {"type": "COPILOT_GET_STATE"},
            {
                "type": "COPILOT_SUBMIT_DECISION",
                "decision": valid_decision,
                "binding": published.binding.model_dump(mode="json"),
            },
            {
                "type": "COPILOT_SUBMIT_DECISION",
                "decision": valid_decision,
                "binding": published.binding.model_dump(mode="json"),
            },
            {
                "type": "EXTENSION_EVIDENCE",
                "evidence": {
                    "kind": "target",
                    "event": "click",
                    "tag": "button",
                    "id": "continue-btn",
                },
            },
        ],
    )
    assert replies_live[0]["ok"] is True
    assert replies_live[0]["type"] == "COPILOT_STATE"
    assert replies_live[0]["state"]["run_id"] == "run-host-02"
    assert replies_live[1] == {
        "ok": True,
        "type": "COPILOT_DECISION_ACK",
        "accepted_new": True,
        "count": 1,
        "halt_id": "halt-tier",
    }
    assert replies_live[2] == {
        "ok": True,
        "type": "COPILOT_DECISION_ACK",
        "accepted_new": False,
        "count": 1,
        "halt_id": "halt-tier",
    }
    assert replies_live[3] == {"ok": True, "count": 1}
    assert (tmp_path / "extension_evidence.jsonl").is_file()


# --- Real Headless Chromium + Unpacked Extension + Installed Native Host ----

_PORTAL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Enrollment Form</title>
  <style>
    body { font-family: sans-serif; margin: 24px; }
    .spacer { height: 1400px; }
    .entity-row { padding: 10px; border: 1px solid #cbd5e1; margin-bottom: 12px; }
    .field-group { margin-bottom: 16px; }
  </style>
</head>
<body>
  <h1>Enrollment Intake</h1>
  <div class="field-group">
    <label for="portal-password">Session Password</label>
    <input id="portal-password" name="password" type="password" value="">
  </div>
  <div class="field-group">
    <label for="applicant-name">Applicant Full Name</label>
    <input id="applicant-name" name="applicant_name" type="text" value="Jordan Rivera">
  </div>
  <div id="entity-row-2" class="entity-row">
    <span class="member-name">Robin Rivera</span>
    <span class="member-role">Former Dependent</span>
  </div>
  <div class="spacer"></div>
  <div class="field-group">
    <label for="coverage-tier">Coverage Tier</label>
    <select id="coverage-tier" name="coverage_tier">
      <option value="">-- Select --</option>
      <option value="standard">Standard Tier</option>
      <option value="plus">Plus Tier</option>
    </select>
  </div>
</body>
</html>
"""


@pytest.fixture
def chrome_copilot(tmp_path: Path):
    """Headless Chromium with unpacked extension, routed portal, and native host."""
    sync = pytest.importorskip("playwright.sync_api")
    profile = tmp_path / "profile"
    session_dir = tmp_path / "session"
    session_dir.mkdir(mode=0o700)

    env = {k: v for k, v in os.environ.items() if k != "OPENADAPT_EXTENSION_RECORDING"}
    with sync.sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                channel="chromium",
                headless=True,
                env=env,
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")

        try:
            workers = context.service_workers
            worker = (
                workers[0]
                if workers
                else context.wait_for_event("serviceworker", timeout=15000)
            )
        except Exception as exc:  # noqa: BLE001
            context.close()
            pytest.fail(f"extension service worker did not start: {exc}")

        extension_id = worker.url.split("/")[2]
        assert install_host.EXTENSION_ID.fullmatch(extension_id), worker.url

        install_host.install(
            install_host.profile_manifest_dir(profile),
            extension_id,
            recording=session_dir,
        )

        context.route(
            "http://portal.local/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=_PORTAL_HTML,
            ),
        )

        yield context, worker, session_dir, extension_id
        context.close()


@posix_only
def test_sidepanel_copilot_end_to_end_native_messaging_and_highlights(chrome_copilot):
    from openadapt_flow.backends._page_quiescence import PageQuiescence

    context, worker, session_dir, extension_id = chrome_copilot
    bridge = CopilotSessionBridge(session_dir)

    # 1. Open routed portal page and populate a live secret in its password field.
    portal_page = context.new_page()
    quiescence = PageQuiescence(portal_page)
    portal_page.goto("http://portal.local/form")
    portal_page.fill("#portal-password", LIVE_PORTAL_PASSWORD)

    # 2. Publish CopilotViewState with PREFILL_ENTITY_CONFLICT + UNGROUNDED_REQUIRED_FIELD.
    initial_state = CopilotViewState(
        run_id="run-e2e-001",
        binding=_binding("run-e2e-001"),
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        status_line="Paused: 1 prefill entity conflict and 1 ungrounded required field.",
        fields_filled_auto=4,
        total_bound_fields=6,
        subroutines_completed=("rate_schedule_lookup",),
        halts=(
            FormHaltItem(
                halt_id="halt-entity-row-2",
                kind=FormHaltKind.PREFILL_ENTITY_CONFLICT,
                screen_id="household_step",
                control_id="ctrl-entity-2",
                dom_selector="#entity-row-2",
                field_label="Household Member Row #2",
                section_text="Household Roster",
                portal_value={
                    "full_name": "Robin Rivera",
                    "relationship": "Former Dependent",
                },
                dossier_value=None,
                message="Portal pre-populated an entity row absent from the grounded dossier.",
                allowed_actions=(
                    HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                    HumanDecisionAction.DISMISS_PORTAL_ENTITY,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
            ),
            FormHaltItem(
                halt_id="halt-coverage-tier",
                kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                screen_id="coverage_step",
                control_id="ctrl-coverage-tier",
                dom_selector="#coverage-tier",
                field_label="Coverage Tier",
                section_text="Benefit Selection",
                dossier_key="enrollment.coverage_tier",
                message="Required field has no grounded fact in the input dossier.",
                allowed_actions=(
                    HumanDecisionAction.PROVIDE_VALUE,
                    HumanDecisionAction.FIXED_ON_SCREEN,
                    HumanDecisionAction.ESCALATE_TO_OWNER,
                    HumanDecisionAction.ABORT_RUN,
                ),
                available_options=(
                    ObservedOption(value="standard", label="Standard Tier"),
                    ObservedOption(value="plus", label="Plus Tier"),
                ),
            ),
        ),
        field_highlights=(
            CopilotFieldHighlight(
                dom_selector="#applicant-name",
                badge_state="filled",
                caption="Auto-filled from intake_document",
            ),
            CopilotFieldHighlight(
                dom_selector="#entity-row-2",
                badge_state="conflict",
                caption="Prefilled row absent from dossier",
            ),
            CopilotFieldHighlight(
                dom_selector="#coverage-tier",
                badge_state="halt",
                caption="Operator input required",
            ),
        ),
    )
    bridge.publish_state(initial_state)

    # 3. Open sidepanel.html and verify real Native Messaging state sync & rendering.
    panel_page = context.new_page()
    panel_page.goto(f"chrome-extension://{extension_id}/sidepanel.html")

    panel_page.locator('#status-pill[data-status="action_needed"]').wait_for(
        timeout=10000
    )
    assert panel_page.locator("#status-pill").inner_text() == "Action Needed"
    assert panel_page.locator("#progress-counter").inner_text() == "4 / 6"
    assert (
        "1 prefill entity conflict" in panel_page.locator("#status-line").inner_text()
    )
    assert worker.evaluate("() => __openadaptExtensionBackground.copilotHaltCount") == 2

    # Verify both halt cards rendered with distinct UI affordances and exact allowed buttons.
    conflict_card = panel_page.locator('.halt-card[data-halt-id="halt-entity-row-2"]')
    ungrounded_card = panel_page.locator(
        '.halt-card[data-halt-id="halt-coverage-tier"]'
    )
    assert conflict_card.is_visible()
    assert ungrounded_card.is_visible()

    assert (
        "Robin Rivera"
        in conflict_card.locator('[data-role="portal-prefilled"]').inner_text()
    )
    assert (
        "(not in dossier)"
        in conflict_card.locator('[data-role="grounded-dossier"]').inner_text()
    )
    conflict_buttons = conflict_card.locator("button.action-btn").all_inner_texts()
    assert conflict_buttons == [
        "Accept Portal Prefill",
        "Remove Portal Entity",
        "Escalate",
        "Abort",
    ]

    ungrounded_buttons = ungrounded_card.locator("button.action-btn").all_inner_texts()
    assert ungrounded_buttons == [
        "Submit Value & Continue",
        "I Fixed It on Screen",
        "Escalate",
        "Abort",
    ]

    # 4. Verify content.js applied data-openadapt-copilot-state highlights on portal DOM
    #    and scrolled attention target into view without disturbing PageQuiescence.
    portal_page.wait_for_function(
        """() => (
            document.querySelector('#applicant-name')?.getAttribute('data-openadapt-copilot-state') === 'filled' &&
            document.querySelector('#entity-row-2')?.getAttribute('data-openadapt-copilot-state') === 'conflict' &&
            document.querySelector('#coverage-tier')?.getAttribute('data-openadapt-copilot-state') === 'halt' &&
            Boolean(document.querySelector('style[data-openadapt-copilot-style="true"]'))
        )""",
        timeout=10000,
    )
    assert (
        portal_page.locator("#entity-row-2").get_attribute(
            "data-openadapt-copilot-caption"
        )
        == "Prefilled row absent from dossier"
    )
    assert (
        portal_page.locator("#coverage-tier").get_attribute(
            "data-openadapt-copilot-caption"
        )
        == "Operator input required"
    )

    # Wait briefly for settle window and verify PageQuiescence treats co-pilot highlights as quiet.
    portal_page.wait_for_timeout(250)
    assert quiescence.probe(0.15) is True, quiescence.last_reason

    # 5. Click resolution controls inside sidepanel.html -> Native Messaging -> JSONL.
    ungrounded_card.locator('select[data-role="operator-input"]').select_option("plus")
    ungrounded_card.locator('input[data-role="remember-as-candidate-rule"]').check()
    ungrounded_card.locator('button[data-action="provide_value"]').click()

    panel_page.wait_for_function(
        """() => {
            const el = document.querySelector('.halt-card[data-halt-id="halt-coverage-tier"]');
            return el && el.dataset.decisionAck === 'true' && el.dataset.acceptedNew === 'true';
        }""",
        timeout=10000,
    )

    # Re-click the exact same submit button to verify end-to-end idempotency deduplication!
    ungrounded_card.locator('button[data-action="provide_value"]').click()
    panel_page.wait_for_function(
        """() => {
            const el = document.querySelector('.halt-card[data-halt-id="halt-coverage-tier"]');
            return el && el.dataset.acceptedNew === 'false';
        }""",
        timeout=10000,
    )

    # Resolve the prefill entity conflict card as well.
    conflict_card.locator('button[data-action="dismiss_portal_entity"]').click()
    panel_page.wait_for_function(
        """() => {
            const el = document.querySelector('.halt-card[data-halt-id="halt-entity-row-2"]');
            return el && el.dataset.decisionAck === 'true' && el.dataset.acceptedNew === 'true';
        }""",
        timeout=10000,
    )

    seen_keys: set[str] = set()
    drained = _claim_all(bridge, initial_state.binding)
    assert len(drained) == 2
    assert drained[0].halt_id == "halt-coverage-tier"
    assert drained[0].action == HumanDecisionAction.PROVIDE_VALUE
    assert drained[0].provided_value == "plus"
    assert drained[0].mapped_dossier_key == "enrollment.coverage_tier"
    assert drained[0].remember_as_candidate_rule is True

    assert drained[1].halt_id == "halt-entity-row-2"
    assert drained[1].action == HumanDecisionAction.DISMISS_PORTAL_ENTITY
    assert drained[1].provided_value is None
    assert drained[1].remember_as_candidate_rule is False

    # Draining again with the updated seen_keys set yields no duplicates.
    assert bridge.drain_new_decisions(seen_keys) == []

    # 6. Publish terminal VERIFIED_DONE state and verify Verified Receipt Card + highlight cleanup.
    done_state = CopilotViewState(
        run_id="run-e2e-001",
        binding=_binding("run-e2e-001"),
        outcome=FormTxOutcome.VERIFIED_DONE,
        status_line="Transaction committed and verified via summary read-back.",
        fields_filled_auto=6,
        total_bound_fields=6,
        subroutines_completed=("rate_schedule_lookup",),
        halts=(),
        field_highlights=(
            CopilotFieldHighlight(
                dom_selector="#applicant-name",
                badge_state="filled",
                caption="Verified on summary screen",
            ),
        ),
        outputs={
            "confirmation_id": "CNF-90210",
            "effective_date": "2026-12-01",
            "session_secret": LIVE_PORTAL_PASSWORD,
        },
        receipt_verified=True,
        receipt_id="rcpt-ed25519-00042",
    )
    bridge.publish_state(done_state)
    panel_page.locator("#refresh-btn").click()

    panel_page.locator('#status-pill[data-status="verified_done"]').wait_for(
        timeout=10000
    )
    assert panel_page.locator("#status-pill").inner_text() == "Verified Done"
    assert panel_page.locator("#progress-counter").inner_text() == "6 / 6"
    assert panel_page.locator(".halt-card").count() == 0

    receipt_card = panel_page.locator("#verified-receipt-card")
    assert receipt_card.is_visible()
    assert (
        receipt_card.locator("#receipt-id-value").inner_text() == "rcpt-ed25519-00042"
    )
    outputs_text = receipt_card.locator('[data-role="receipt-outputs"]').inner_text()
    assert "CNF-90210" in outputs_text
    assert "2026-12-01" in outputs_text
    assert REDACTED_SECRET in outputs_text
    assert LIVE_PORTAL_PASSWORD not in outputs_text

    # Stale conflict/halt highlights on the portal page are cleared on sync.
    portal_page.wait_for_function(
        """() => (
            document.querySelector('#applicant-name')?.getAttribute('data-openadapt-copilot-state') === 'filled' &&
            document.querySelector('#entity-row-2')?.getAttribute('data-openadapt-copilot-state') === null &&
            document.querySelector('#coverage-tier')?.getAttribute('data-openadapt-copilot-state') === null
        )""",
        timeout=10000,
    )

    # Confirm zero live secret exposure across all session files on disk.
    for artifact in session_dir.iterdir():
        if artifact.is_file():
            content = artifact.read_bytes()
            assert LIVE_PORTAL_PASSWORD.encode() not in content, artifact.name


@posix_only
def test_sidepanel_idle_running_text_input_badge_and_scroll(chrome_copilot):
    context, worker, session_dir, extension_id = chrome_copilot
    bridge = CopilotSessionBridge(session_dir)

    portal_page = context.new_page()
    portal_page.goto("http://portal.local/form")
    assert portal_page.evaluate("() => window.scrollY") == 0

    # 1. Before any state is published, sidepanel renders Idle.
    panel_page = context.new_page()
    panel_page.goto(f"chrome-extension://{extension_id}/sidepanel.html")
    panel_page.locator('#status-pill[data-status="idle"]').wait_for(timeout=10000)
    assert panel_page.locator("#status-pill").inner_text() == "Idle"
    assert panel_page.locator("#progress-counter").inner_text() == "0 / 0"

    # 2. Active run with 0 halts renders Running.
    bridge.publish_state(
        CopilotViewState(
            run_id="run-e2e-002",
            binding=_binding("run-e2e-002"),
            outcome=FormTxOutcome.DELIVERY_UNCERTAIN,
            status_line="Reconciling form fields on step 1...",
            fields_filled_auto=2,
            total_bound_fields=5,
            halts=(),
        )
    )
    panel_page.locator("#refresh-btn").click()
    panel_page.locator('#status-pill[data-status="running"]').wait_for(timeout=10000)
    assert panel_page.locator("#status-pill").inner_text() == "Running"
    assert panel_page.locator("#progress-counter").inner_text() == "2 / 5"

    # 3. Publish text-input ungrounded halt below spacer + scalar prefill value conflict.
    bridge.publish_state(
        CopilotViewState(
            run_id="run-e2e-002",
            binding=_binding("run-e2e-002"),
            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
            status_line="Operator confirmation required.",
            fields_filled_auto=3,
            total_bound_fields=5,
            halts=(
                FormHaltItem(
                    halt_id="halt-text-field",
                    kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
                    dom_selector="#coverage-tier",
                    field_label="Group Policy Number",
                    dossier_key="enrollment.group_policy_number",
                    message="Enter policy number from intake packet.",
                    allowed_actions=(
                        HumanDecisionAction.PROVIDE_VALUE,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                    available_options=(),
                ),
                FormHaltItem(
                    halt_id="halt-scalar-conflict",
                    kind=FormHaltKind.PREFILL_VALUE_CONFLICT,
                    dom_selector="#applicant-name",
                    field_label="Applicant Full Name",
                    dossier_key="applicant.full_name",
                    portal_value="J. Rivera",
                    dossier_value="Jordan Rivera",
                    message="Portal prefill differs from grounded dossier.",
                    # The reconciler's value-conflict halts offer these actions.
                    allowed_actions=(
                        HumanDecisionAction.ACCEPT_PORTAL_PREFILL,
                        HumanDecisionAction.PROVIDE_VALUE,
                        HumanDecisionAction.FIXED_ON_SCREEN,
                        HumanDecisionAction.ESCALATE_TO_OWNER,
                        HumanDecisionAction.ABORT_RUN,
                    ),
                ),
            ),
            field_highlights=(
                CopilotFieldHighlight(
                    dom_selector="#coverage-tier",
                    badge_state="halt",
                    caption="Missing policy number",
                ),
                CopilotFieldHighlight(
                    dom_selector="#applicant-name",
                    badge_state="conflict",
                    caption="Value mismatch",
                ),
            ),
        )
    )
    panel_page.locator("#refresh-btn").click()
    panel_page.locator('#status-pill[data-status="action_needed"]').wait_for(
        timeout=10000
    )

    # Verify Chrome action badge shows HALT while not recording, REC takes
    # precedence when recording, and clearing any password-field auth pause
    # restores HALT once recording stops.
    badge_halt = worker.evaluate("() => chrome.action.getBadgeText({})")
    assert badge_halt == "HALT"
    worker.evaluate("() => __openadaptExtensionBackground.setRecording(true)")
    assert worker.evaluate("() => chrome.action.getBadgeText({})") == "REC"
    worker.evaluate("() => __openadaptExtensionBackground.setRecording(false)")
    worker.evaluate("() => __openadaptExtensionBackground.resumeAfterAuth()")
    assert worker.evaluate("() => chrome.action.getBadgeText({})") == "HALT"

    # Verify first attention element (#coverage-tier below 1400px spacer) scrolled into view.
    portal_page.wait_for_function("() => window.scrollY > 200", timeout=10000)

    # Empty text input submission is blocked client-side before sending to native host.
    text_card = panel_page.locator('.halt-card[data-halt-id="halt-text-field"]')
    text_card.locator('button[data-action="provide_value"]').click()
    assert not (session_dir / COPILOT_DECISIONS_FILE).exists()

    # Fill text input and submit, plus accept portal prefill on value conflict.
    text_card.locator('input[data-role="operator-input"]').fill("GRP-44810")
    text_card.locator('button[data-action="provide_value"]').click()
    panel_page.wait_for_function(
        """() => document.querySelector('.halt-card[data-halt-id="halt-text-field"]')?.dataset.decisionAck === 'true'""",
        timeout=10000,
    )

    val_card = panel_page.locator('.halt-card[data-halt-id="halt-scalar-conflict"]')
    # "Submit Value & Continue" on a value conflict needs an input to read from.
    assert val_card.locator('input[data-role="operator-input"]').count() == 1
    val_card.locator('button[data-action="accept_portal_prefill"]').click()
    panel_page.wait_for_function(
        """() => document.querySelector('.halt-card[data-halt-id="halt-scalar-conflict"]')?.dataset.decisionAck === 'true'""",
        timeout=10000,
    )

    decisions = _claim_all(bridge, bridge.read_state().binding)
    assert len(decisions) == 2
    assert decisions[0].action == HumanDecisionAction.PROVIDE_VALUE
    assert decisions[0].provided_value == "GRP-44810"
    assert decisions[1].action == HumanDecisionAction.ACCEPT_PORTAL_PREFILL
