"""Actual native-message framing and live browser/reconciler operator handoff."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs

import pytest

from openadapt_flow.copilot_bridge import CopilotSessionBridge, CopilotViewState
from openadapt_flow.copilot_protocol import CopilotBoundDecision, CopilotRunBinding
from openadapt_flow.copilot_runtime import CopilotFormRuntime
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.reconciler import FormReconciler
from openadapt_flow.form_tx.types import (
    FieldAnchorSpec,
    FieldBindingSpec,
    FormHaltItem,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    InputDossier,
    ProvenanceKind,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

HOST = (
    Path(__file__).resolve().parents[1]
    / "chrome_extension/native_host/openadapt_extension_host.py"
)


def _binding(run_id="run-bound", generation=1):
    return CopilotRunBinding(
        run_id=run_id,
        session_nonce="a" * 64,
        generation=generation,
        pack_digest="b" * 64,
        dossier_digest="c" * 64,
        operator_id="local-operator",
        expires_at_unix=int(time.time()) + 60,
    )


def _halt(**changes):
    return FormHaltItem(
        halt_id="halt-name",
        kind=FormHaltKind.UNGROUNDED_REQUIRED_FIELD,
        field_label="Display name",
        dossier_key="person.name",
        dom_selector="#name",
        message="Grounded value required",
        **changes,
    )


def _state(binding, halt=None):
    return CopilotViewState(
        binding=binding,
        run_id=binding.run_id,
        outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
        status_line="Operator needed",
        fields_filled_auto=0,
        total_bound_fields=1,
        halts=(halt or _halt(),),
    )


def _decision(binding, **changes):
    values = dict(
        halt_id="halt-name",
        action=HumanDecisionAction.PROVIDE_VALUE,
        actor_id="local-operator",
        provided_value="Synthetic Name",
        mapped_dossier_key="person.name",
        idempotency_key="decision-1",
    )
    values.update(changes)
    return CopilotBoundDecision(
        binding=binding, decision=HumanResolutionInput(**values)
    )


def _native(directory, messages):
    frames = []
    for message in messages:
        body = json.dumps(message).encode()
        frames.append(struct.pack("<I", len(body)) + body)
    result = subprocess.run(
        [sys.executable, str(HOST), "--recording", str(directory)],
        input=b"".join(frames),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    replies = []
    offset = 0
    while offset < len(result.stdout):
        (length,) = struct.unpack("<I", result.stdout[offset : offset + 4])
        replies.append(json.loads(result.stdout[offset + 4 : offset + 4 + length]))
        offset += 4 + length
    return replies


def _envelope(item):
    return {"type": "COPILOT_SUBMIT_DECISION", **item.model_dump(mode="json")}


def test_native_decisions_are_bound_deduplicated_and_claimed_before_effect(tmp_path):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    bridge.publish_state(_state(binding))
    item = _decision(binding)
    replies = _native(tmp_path, [_envelope(item), _envelope(item)])
    assert [reply["accepted_new"] for reply in replies] == [True, False]
    assert bridge.claim_bound_decision(binding) == item
    # A different process/restarted owner cannot reclaim an interrupted action.
    assert CopilotSessionBridge(tmp_path).claim_bound_decision(binding) is None
    changed = item.model_copy(
        update={
            "decision": item.decision.model_copy(update={"provided_value": "Different"})
        }
    )
    assert _native(tmp_path, [_envelope(changed)])[0]["ok"] is False
    if os.name == "posix":
        assert (
            tmp_path / "copilot_bound_decisions.sqlite3"
        ).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "other-run"},
        {"session_nonce": "d" * 64},
        {"generation": 2},
        {"pack_digest": "d" * 64},
        {"dossier_digest": "d" * 64},
        {"operator_id": "someone-else"},
        {"expires_at_unix": 1},
    ],
)
def test_native_rejects_wrong_or_expired_scope(tmp_path, change):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    bridge.publish_state(_state(binding))
    item = _decision(binding.model_copy(update=change))
    assert _native(tmp_path, [_envelope(item)])[0]["ok"] is False
    assert bridge.claim_bound_decision(binding) is None


@pytest.mark.parametrize(
    "change",
    [
        {"halt_id": "old-halt"},
        {"actor_id": "other-operator"},
        {"mapped_dossier_key": "person.bank_account"},
        {"action": HumanDecisionAction.CONFIRM_MODAL_ACTION},
        {"provided_value": {"password": "synthetic-secret"}},
        {"note": "unstructured input is not stored"},
    ],
)
def test_native_rejects_unallowed_or_secret_decisions(tmp_path, change):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    bridge.publish_state(_state(binding))
    assert (
        _native(tmp_path, [_envelope(_decision(binding, **change))])[0]["ok"] is False
    )
    assert bridge.claim_bound_decision(binding) is None


def test_secret_halt_can_only_be_fixed_on_screen_or_stopped(tmp_path):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    halt = _halt().model_copy(
        update={"field_label": "Password", "dom_selector": "input[type=password]"}
    )
    bridge.publish_state(_state(binding, halt))
    assert _native(tmp_path, [_envelope(_decision(binding))])[0]["ok"] is False
    manual = _decision(
        binding, action=HumanDecisionAction.FIXED_ON_SCREEN, provided_value=None
    )
    assert _native(tmp_path, [_envelope(manual)])[0]["accepted_new"] is True
    assert "Synthetic Name" not in (
        tmp_path / "copilot_bound_decisions.sqlite3"
    ).read_bytes().decode(errors="ignore")


def test_unbound_mailbox_has_no_operator_authority(tmp_path):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    bridge.publish_state(_state(binding).model_copy(update={"binding": None}))
    assert _native(tmp_path, [_envelope(_decision(binding))])[0]["ok"] is False
    with pytest.raises(ValueError, match="ownership or generation"):
        bridge.claim_bound_decision(binding)


def test_session_publication_refuses_another_active_owner(tmp_path):
    bridge = CopilotSessionBridge(tmp_path)
    binding = _binding()
    bridge.publish_owned_state(_state(binding), expected_binding=None)
    other = binding.model_copy(update={"session_nonce": "d" * 64})
    with pytest.raises(ValueError, match="active owner"):
        CopilotSessionBridge(tmp_path).publish_owned_state(
            _state(other), expected_binding=None
        )
    with pytest.raises(ValueError, match="ownership changed"):
        bridge.publish_owned_state(_state(other), expected_binding=other)
    assert bridge.read_state().binding == binding


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_world_readable_session_directory_is_refused(tmp_path):
    directory = tmp_path / "unsafe"
    directory.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        CopilotSessionBridge(directory).publish_state(_state(_binding()))


def test_live_reconciler_native_host_handoff_and_saved_readback(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    saved = {}

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/summary":
                body = (
                    '<div id="summary"><input id="saved-name" aria-label="Display name" value="'
                    + saved.get("name", "")
                    + '"></div>'
                )
            else:
                body = '<main id="entry"><form action="/save" method="post"><label for="name">Display name</label><input id="name" name="name" required><button id="save">Save draft</button></form></main>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"])).decode()
            saved["name"] = parse_qs(data)["name"][0]
            self.send_response(303)
            self.send_header("Location", "/summary")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    entry = FieldBindingSpec(
        binding_id="name",
        dossier_key="person.name",
        anchor=FieldAnchorSpec(css_selector="#name"),
        required_in_pack=True,
    )
    summary = entry.model_copy(
        update={
            "binding_id": "saved-name",
            "anchor": FieldAnchorSpec(css_selector="#saved-name"),
        }
    )
    pack = FormPack(
        pack_id="local-entry",
        domain_schema="synthetic-contact",
        ui_profile_id="standard",
        bindings=(entry,),
        screens=(
            WizardScreenSpec(
                screen_id="entry",
                landmark_selector="#entry",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(css_selector="#save"),
            ),
        ),
        summary_check=SummaryCheckSpec(
            summary_url_path="/summary",
            summary_landmark_selector="#summary",
            field_readbacks=(summary,),
        ),
        commit_url_patterns=("/save",),
    )
    dossier = InputDossier(
        dossier_id="synthetic-dossier", request_id="synthetic-request"
    )
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel="chromium")
            page = browser.new_page()
            page.goto(origin)
            surface = PlaywrightFormSurface(
                page, UIProfileSpec(profile_id="standard"), base_origin=origin
            )
            reconciler = FormReconciler(surface)
            session = reconciler.create_session(pack, dossier)
            runtime = CopilotFormRuntime(
                reconciler, session, tmp_path / "session", operator_id="local-operator"
            )
            result = runtime.start()
            assert result.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
            state = runtime.bridge.read_state()
            assert state is not None and state.binding is not None
            assert saved == {}
            item = _decision(state.binding, halt_id=result.halts[0].halt_id)
            assert (
                _native(runtime.bridge.session_dir, [_envelope(item)])[0][
                    "accepted_new"
                ]
                is True
            )
            result = runtime.resume_pending()
            assert saved == {"name": "Synthetic Name"}
            assert result.human_resolutions[0].actor_id == "local-operator"
            assert (
                session.effective_facts["person.name"].provenance
                == ProvenanceKind.HUMAN_OPERATOR
            )
            assert result.outcome == FormTxOutcome.VERIFIED_DONE
            assert runtime.bridge.read_state().receipt_verified is True
            # The old generation cannot authorize another write, even with a new key.
            stale = item.model_copy(
                update={
                    "decision": item.decision.model_copy(
                        update={"idempotency_key": "stale-retry"}
                    )
                }
            )
            assert (
                _native(runtime.bridge.session_dir, [_envelope(stale)])[0]["ok"]
                is False
            )
            assert runtime.resume_pending() == result
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
