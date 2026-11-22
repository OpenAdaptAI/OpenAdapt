"""Integration tests for the Pillar 8 closed-loop Watch -> Compile -> Verify -> Self-Heal demo."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from openadapt_flow.actor_commit import CommitStatus, InboxActionType

_DEMO_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "closed_loop_watch_compile_verify.py"
)


def _load_demo_module():
    spec = importlib.util.spec_from_file_location(
        "closed_loop_watch_compile_verify", _DEMO_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_closed_loop_watch_compile_verify_self_heal_end_to_end(tmp_path: Path) -> None:
    """All four Pillar 8 stages run deterministically in <2s with zero network calls."""
    demo = _load_demo_module()
    result = demo.run_closed_loop_demo(tmp_path / "closed-loop")

    # Fast and deterministic.
    assert result.elapsed_s < 2.0

    # Stage 1: Watch (read-only SQLite CDC monitor).
    assert result.watch_events_initial == 1
    assert result.recorded_steps == 6
    assert result.cdc_jsonl_path.exists()

    # Stage 2: Compile (AST-validated zero-token Python module).
    assert result.compiled_v1_path.exists()
    assert len(result.compiled_v1_sha256) == 64

    # Stage 3: Verify (independent read-back + Ed25519 DSSE receipt -> VERIFIED).
    assert result.healthy_receipt.status is CommitStatus.VERIFIED
    assert result.healthy_receipt.readback_checked is True
    assert all(d.matched for d in result.healthy_receipt.readback_diffs)
    assert result.healthy_dsse.verified is True
    assert result.healthy_dsse.path.exists()
    assert result.healthy_cdc_events == 1

    # Stage 4: Fault & Self-Heal (silent UI drift -> VETO -> Inbox -> Human override -> v2).
    assert result.fault_receipt.status is CommitStatus.NEEDS_HUMAN
    mismatched = {d.field for d in result.fault_receipt.readback_diffs if not d.matched}
    assert mismatched == {"amount_cents", "approved_code"}
    assert result.inbox_card.status is CommitStatus.NEEDS_HUMAN
    assert "oa-inbox-card" in result.inbox_card.html
    assert any(a.action is InboxActionType.OVERRIDE for a in result.inbox_card.actions)

    # Human resolution emitted a re-compile event and a distinct v2 compiled workflow.
    assert result.human_resolution.action is InboxActionType.OVERRIDE
    assert result.healed_cdc_events == 1
    assert result.compiled_v2_path.exists()
    assert result.compiled_v2_sha256 != result.compiled_v1_sha256

    recompile_lines = [
        json.loads(line)
        for line in result.recompile_event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(recompile_lines) == 1
    event = recompile_lines[0]
    assert event["event_type"] == "workflow.recompile_requested"
    assert event["previous_bundle_sha256"] == result.compiled_v1_sha256
    assert event["updated_bundle_sha256"] == result.compiled_v2_sha256
    assert set(event["mismatched_fields"]) == {"amount_cents", "approved_code"}

    # Event-sourced audit trail persisted to memory_log.md and cdc_events.jsonl.
    memory_log = result.memory_log_path.read_text(encoding="utf-8")
    assert "VERIFIED" in memory_log
    assert "NEEDS_HUMAN" in memory_log
    cdc_events = [
        json.loads(line)
        for line in result.cdc_jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = [e["event_type"] for e in cdc_events]
    assert event_types == [
        "record.created",  # CLM-1001 baseline
        "record.created",  # CLM-1002 healthy
        "record.created",  # CLM-1003 drifted
        "record.updated",  # CLM-1003 healed after human override
    ]


def test_closed_loop_demo_cli_entrypoint(tmp_path: Path, capsys) -> None:
    """CLI entrypoint exits 0 and emits valid JSON summary."""
    demo = _load_demo_module()
    out_dir = tmp_path / "cli-run"
    rc = demo.main(["--out", str(out_dir)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage_3_verify"]["status"] == "VERIFIED"
    assert payload["stage_3_verify"]["dsse_verified"] is True
    assert payload["stage_4_fault_and_self_heal"]["fault_status"] == "NEEDS_HUMAN"
    assert (
        payload["stage_2_compile"]["compiled_v1_sha256"]
        != payload["stage_4_fault_and_self_heal"]["compiled_v2_sha256"]
    )
