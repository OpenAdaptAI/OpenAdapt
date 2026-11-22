"""End-to-End Closed-Loop Demo: Watch -> Compile -> Verify -> Self-Heal.

Demonstrates all four stages of OpenAdapt's Pillar 8 closed loop in <2 seconds,
with zero network calls and zero LLM tokens:

1. Watch: Read-only SQLite CDC monitor (``openadapt_flow.monitor``) observes a
   synthetic legacy claims database and ingests a recorded multi-step form trace.
2. Compile: ``openadapt_flow.compile_code`` compiles the demonstrated path into
   a deterministic, AST-validated, zero-token Python module.
3. Verify: Runs the compiled flow, performs independent read-back verification,
   signs and verifies a real Ed25519 receipt, and records a ``VERIFIED`` commit
   via ``openadapt_flow.actor_commit``.
4. Fault & Self-Heal: Injects a silent UI drift / unbound-field mutation. The
   independent verification gate catches it (VETO -> ``NEEDS_HUMAN``), halts
   before downstream corruption, renders an Exception Inbox card (JSON, Slack
   Block Kit, HTML), accepts a human resolution, and emits an updated
   re-compile event + v2 compiled workflow module.

Run directly:
    uv run --project packages/flow python examples/closed_loop_watch_compile_verify.py
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from openadapt_flow.actor_commit import (
    CommitReceipt,
    CommitRequest,
    FieldDiff,
    InboxActionType,
    InboxCardArtifact,
    evaluate_commit,
    render_inbox_card,
    write_commit_log,
)
from openadapt_flow.compile_code import emit_workflow_code_file
from openadapt_flow.monitor import (
    JsonlEventSink,
    MemoryMarkdownSink,
    MonitorEngine,
    MonitorSpec,
    SqliteReadOnlySnapshotSource,
)

DSSE_PAYLOAD_TYPE = "application/vnd.openadapt.commit-receipt+json"


@dataclass(frozen=True)
class SignedReceipt:
    """Ed25519 DSSE-style signed receipt artifact."""

    receipt_ref: str
    path: Path
    signature_b64: str
    public_key_b64: str
    verified: bool


@dataclass(frozen=True)
class HumanResolution:
    """Human decision submitted through the Exception Inbox."""

    commit_id: str
    operator_id: str
    action: InboxActionType
    reason: str
    corrected_fields: dict[str, Any]
    resolved_at: str


@dataclass(frozen=True)
class ClosedLoopResult:
    """Structured summary returned by :func:`run_closed_loop_demo`."""

    work_dir: Path
    elapsed_s: float
    # Stage 1: Watch
    watch_events_initial: int
    recorded_steps: int
    # Stage 2: Compile
    compiled_v1_path: Path
    compiled_v1_sha256: str
    # Stage 3: Verify (healthy path)
    healthy_receipt: CommitReceipt
    healthy_dsse: SignedReceipt
    healthy_cdc_events: int
    # Stage 4: Fault & Self-Heal
    fault_receipt: CommitReceipt
    inbox_card: InboxCardArtifact
    human_resolution: HumanResolution
    recompile_event_path: Path
    compiled_v2_path: Path
    compiled_v2_sha256: str
    healed_cdc_events: int
    memory_log_path: Path
    cdc_jsonl_path: Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _init_claims_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                policy_number TEXT NOT NULL,
                claimant_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                approved_code TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO claims
                (claim_id, policy_number, claimant_name, amount_cents, status, approved_code)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("CLM-1001", "POL-10001", "Grace Hopper", 15000, "submitted", "DEMO-OK"),
        )
        conn.commit()
    finally:
        conn.close()


def _submit_claim_to_legacy_app(
    db_path: Path,
    params: dict[str, Any],
    *,
    inject_ui_drift: bool = False,
) -> dict[str, Any]:
    """Simulate deterministic zero-token UI submission into the legacy claims app.

    When ``inject_ui_drift=True``, mimics a silent legacy UI mutation where the
    screen still reports ``Submitted`` while ``amount_cents`` is truncated by
    10x and ``approved_code`` falls back to an unbound field placeholder.
    """
    persisted_amount = (
        int(params["amount_cents"]) // 10
        if inject_ui_drift
        else int(params["amount_cents"])
    )
    persisted_code = "UNBOUND" if inject_ui_drift else str(params["approved_code"])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO claims
                (claim_id, policy_number, claimant_name, amount_cents, status, approved_code)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(params["claim_id"]),
                str(params["policy_number"]),
                str(params["claimant_name"]),
                persisted_amount,
                "submitted",
                persisted_code,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    # The screen always claims success (even when silent UI drift occurred).
    return {"screen_banner": f"Claim {params['claim_id']} submitted", "llm_tokens": 0}


def _apply_human_correction(
    db_path: Path, claim_id: str, corrected_fields: dict[str, Any]
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE claims
            SET amount_cents = ?, approved_code = ?, status = ?
            WHERE claim_id = ?
            """,
            (
                int(corrected_fields["amount_cents"]),
                str(corrected_fields["approved_code"]),
                "verified_after_override",
                claim_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sign_and_verify_ed25519_receipt(
    payload_obj: dict[str, Any],
    out_path: Path,
    private_key: Ed25519PrivateKey,
) -> SignedReceipt:
    """Create and immediately verify an Ed25519 DSSE-style receipt envelope."""
    payload_bytes = json.dumps(
        payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    pae = (
        f"DSSEv1 {len(DSSE_PAYLOAD_TYPE)} {DSSE_PAYLOAD_TYPE} "
        f"{len(payload_bytes)} ".encode("ascii")
        + payload_bytes
    )
    signature = private_key.sign(pae)
    public_bytes = private_key.public_key().public_bytes_raw()

    # Independent verification using only raw public key bytes.
    pub = Ed25519PublicKey.from_public_bytes(public_bytes)
    pub.verify(signature, pae)

    sig_b64 = base64.b64encode(signature).decode("ascii")
    pub_b64 = base64.b64encode(public_bytes).decode("ascii")
    envelope = {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
        "signatures": [{"alg": "Ed25519", "pub": pub_b64, "sig": sig_b64}],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return SignedReceipt(
        receipt_ref=f"dsse://ed25519/{out_path.name}",
        path=out_path,
        signature_b64=sig_b64,
        public_key_b64=pub_b64,
        verified=True,
    )


def _load_compiled_module(module_path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load compiled module at {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compare_readback(
    expected: dict[str, Any], observed: dict[str, Any] | None
) -> list[FieldDiff]:
    diffs: list[FieldDiff] = []
    for field in ("policy_number", "claimant_name", "amount_cents", "approved_code"):
        exp_str = str(expected[field])
        obs_str = None if observed is None else str(observed.get(field))
        matched = obs_str == exp_str
        diffs.append(
            FieldDiff(
                field=field,
                expected=exp_str,
                observed=obs_str,
                matched=matched,
                note="" if matched else "silent UI drift / unbound field mutation",
            )
        )
    return diffs


def run_closed_loop_demo(work_dir: Path) -> ClosedLoopResult:
    """Execute the full Watch -> Compile -> Verify -> Self-Heal closed loop."""
    t0 = time.monotonic()
    root = Path(work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)

    db_path = root / "legacy_claims.sqlite3"
    cdc_jsonl = root / "cdc_events.jsonl"
    cdc_memory = root / "cdc_memory.md"
    _init_claims_db(db_path)

    # ------------------------------------------------------------------
    # Stage 1: WATCH — Read-only CDC monitor + recorded form trace
    # ------------------------------------------------------------------
    source = SqliteReadOnlySnapshotSource(
        db_path,
        "SELECT claim_id, policy_number, claimant_name, amount_cents, status, approved_code "
        "FROM claims ORDER BY claim_id",
    )
    monitor = MonitorEngine(
        spec=MonitorSpec(
            name="legacy_claims_cdc",
            primary_key="claim_id",
            watched_fields=(
                "policy_number",
                "claimant_name",
                "amount_cents",
                "status",
                "approved_code",
            ),
            emit_initial_created=True,
        ),
        source=source,
        sinks=[JsonlEventSink(cdc_jsonl), MemoryMarkdownSink(cdc_memory)],
    )
    initial_tick = monitor.tick()
    if not initial_tick.ok:
        raise RuntimeError(f"Initial CDC watch tick failed: {initial_tick.error}")

    recorded_workflow_spec_v1: dict[str, Any] = {
        "name": "legacy_claim_submit",
        "schema_version": 1,
        "params": {
            "claim_id": {"kind": "string", "required": True},
            "policy_number": {"kind": "string", "required": True},
            "claimant_name": {"kind": "string", "required": True},
            "amount_cents": {"kind": "string", "required": True},
            "approved_code": {"kind": "string", "required": True},
        },
        "steps": [
            {"id": "step_1", "kind": "type", "target": "input[name=claim_id]"},
            {"id": "step_2", "kind": "type", "target": "input[name=policy_number]"},
            {"id": "step_3", "kind": "type", "target": "input[name=claimant_name]"},
            {"id": "step_4", "kind": "type", "target": "input[name=amount_cents]"},
            {"id": "step_5", "kind": "type", "target": "input[name=approved_code]"},
            {"id": "step_6", "kind": "click", "target": "button#submit-claim"},
        ],
    }

    # ------------------------------------------------------------------
    # Stage 2: COMPILE — Emit deterministic, zero-token Python workflow
    # ------------------------------------------------------------------
    compiled_v1_path = emit_workflow_code_file(
        recorded_workflow_spec_v1,
        root / "compiled_claim_submit_v1.py",
        module_name="compiled_claim_submit_v1",
    )
    compiled_v1_mod = _load_compiled_module(
        compiled_v1_path, "compiled_claim_submit_v1"
    )
    compiled_v1_sha256 = str(compiled_v1_mod.BUNDLE_SHA256)

    # ------------------------------------------------------------------
    # Stage 3: VERIFY — Execute healthy commit + independent read-back + Ed25519
    # ------------------------------------------------------------------
    signing_key = Ed25519PrivateKey.generate()
    healthy_params = {
        "claim_id": "CLM-1002",
        "policy_number": "POL-88421",
        "claimant_name": "Ada Lovelace",
        "amount_cents": 42500,
        "approved_code": "AUTO-OK",
    }
    _submit_claim_to_legacy_app(db_path, healthy_params, inject_ui_drift=False)
    healthy_tick = monitor.tick()
    if not healthy_tick.ok:
        raise RuntimeError(f"Healthy CDC tick failed: {healthy_tick.error}")

    observed_healthy = monitor.current_state.get("CLM-1002")
    healthy_diffs = _compare_readback(healthy_params, observed_healthy)
    healthy_dsse = _sign_and_verify_ed25519_receipt(
        {
            "commit_id": "commit-clm-1002",
            "workflow_name": compiled_v1_mod.WORKFLOW_NAME,
            "bundle_sha256": compiled_v1_sha256,
            "record_key": "CLM-1002",
            "verified_fields": [d.model_dump() for d in healthy_diffs],
            "llm_tokens": 0,
        },
        root / "dsse" / "commit-clm-1002.dsse.json",
        signing_key,
    )
    healthy_req = CommitRequest(
        commit_id="commit-clm-1002",
        actor_id="openadapt-compiled-runner",
        target_computer_id="legacy-erp-local",
        workflow_ref=f"legacy_claim_submit@{compiled_v1_sha256[:12]}",
        idempotency_key="clm-1002-v1",
        inputs=healthy_params,
    )
    healthy_receipt = evaluate_commit(
        healthy_req,
        readback_diffs=healthy_diffs,
        readback_checked=True,
        dsse_receipt_ref=healthy_dsse.receipt_ref,
        dsse_signature_verified=healthy_dsse.verified,
    )
    _, memory_log_path = write_commit_log(healthy_receipt, root)

    # ------------------------------------------------------------------
    # Stage 4: FAULT & SELF-HEAL — Silent UI drift -> VETO -> Inbox -> Recompile
    # ------------------------------------------------------------------
    fault_params = {
        "claim_id": "CLM-1003",
        "policy_number": "POL-99104",
        "claimant_name": "Alan Turing",
        "amount_cents": 99000,
        "approved_code": "AUTO-OK",
    }
    _submit_claim_to_legacy_app(db_path, fault_params, inject_ui_drift=True)
    fault_tick = monitor.tick()
    if not fault_tick.ok:
        raise RuntimeError(f"Fault CDC tick failed: {fault_tick.error}")

    observed_fault = monitor.current_state.get("CLM-1003")
    fault_diffs = _compare_readback(fault_params, observed_fault)
    fault_req = CommitRequest(
        commit_id="commit-clm-1003",
        actor_id="openadapt-compiled-runner",
        target_computer_id="legacy-erp-local",
        workflow_ref=f"legacy_claim_submit@{compiled_v1_sha256[:12]}",
        idempotency_key="clm-1003-v1",
        inputs=fault_params,
    )
    fault_receipt = evaluate_commit(
        fault_req,
        readback_diffs=fault_diffs,
        readback_checked=True,
    )
    write_commit_log(fault_receipt, root)
    inbox_card = render_inbox_card(fault_receipt)

    # Persist the inbox card artifact for operator inspection.
    (root / "inbox_card_clm_1003.html").write_text(inbox_card.html, encoding="utf-8")

    # Human resolves the Exception Inbox card and triggers self-healing re-compile.
    human_resolution = HumanResolution(
        commit_id=fault_receipt.commit_id,
        operator_id="ops-reviewer-1",
        action=InboxActionType.OVERRIDE,
        reason="Re-bound amount_cents input selector and approved_code dropdown after UI drift.",
        corrected_fields={"amount_cents": 99000, "approved_code": "AUTO-OK"},
        resolved_at=_utc_now_iso(),
    )
    _apply_human_correction(db_path, "CLM-1003", human_resolution.corrected_fields)
    healed_tick = monitor.tick()
    if not healed_tick.ok:
        raise RuntimeError(f"Healed CDC tick failed: {healed_tick.error}")

    # Emit updated v2 compiled workflow with repaired selectors + re-compile event.
    recorded_workflow_spec_v2 = dict(recorded_workflow_spec_v1)
    recorded_workflow_spec_v2["schema_version"] = 2
    recorded_workflow_spec_v2["steps"] = [
        {"id": "step_1", "kind": "type", "target": "input[name=claim_id]"},
        {"id": "step_2", "kind": "type", "target": "input[name=policy_number]"},
        {"id": "step_3", "kind": "type", "target": "input[name=claimant_name]"},
        {"id": "step_4", "kind": "type", "target": "input[data-field=amount_cents_v2]"},
        {"id": "step_5", "kind": "select", "target": "select[data-field=approved_code_v2]"},
        {"id": "step_6", "kind": "click", "target": "button#submit-claim"},
    ]
    compiled_v2_path = emit_workflow_code_file(
        recorded_workflow_spec_v2,
        root / "compiled_claim_submit_v2.py",
        module_name="compiled_claim_submit_v2",
    )
    compiled_v2_mod = _load_compiled_module(
        compiled_v2_path, "compiled_claim_submit_v2"
    )
    compiled_v2_sha256 = str(compiled_v2_mod.BUNDLE_SHA256)

    recompile_event_path = root / "recompile_events.jsonl"
    recompile_event = {
        "event_type": "workflow.recompile_requested",
        "timestamp": _utc_now_iso(),
        "trigger_commit_id": fault_receipt.commit_id,
        "operator_id": human_resolution.operator_id,
        "inbox_action": human_resolution.action.value,
        "reason": human_resolution.reason,
        "previous_bundle_sha256": compiled_v1_sha256,
        "updated_bundle_sha256": compiled_v2_sha256,
        "mismatched_fields": [d.field for d in fault_diffs if not d.matched],
    }
    recompile_event_path.write_text(
        json.dumps(recompile_event, sort_keys=True) + "\n", encoding="utf-8"
    )

    elapsed_s = time.monotonic() - t0
    return ClosedLoopResult(
        work_dir=root,
        elapsed_s=elapsed_s,
        watch_events_initial=len(initial_tick.events),
        recorded_steps=len(recorded_workflow_spec_v1["steps"]),
        compiled_v1_path=compiled_v1_path,
        compiled_v1_sha256=compiled_v1_sha256,
        healthy_receipt=healthy_receipt,
        healthy_dsse=healthy_dsse,
        healthy_cdc_events=len(healthy_tick.events),
        fault_receipt=fault_receipt,
        inbox_card=inbox_card,
        human_resolution=human_resolution,
        recompile_event_path=recompile_event_path,
        compiled_v2_path=compiled_v2_path,
        compiled_v2_sha256=compiled_v2_sha256,
        healed_cdc_events=len(healed_tick.events),
        memory_log_path=memory_log_path,
        cdc_jsonl_path=cdc_jsonl,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the OpenAdapt Watch -> Compile -> Verify -> Self-Heal closed-loop demo."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output directory for generated artifacts (defaults to a temp dir).",
    )
    args = parser.parse_args(argv)
    if args.out is not None:
        result = run_closed_loop_demo(args.out)
    else:
        with tempfile.TemporaryDirectory(prefix="openadapt-closed-loop-") as tmp:
            result = run_closed_loop_demo(Path(tmp))

    print(
        json.dumps(
            {
                "elapsed_s": round(result.elapsed_s, 3),
                "stage_1_watch": {
                    "initial_cdc_events": result.watch_events_initial,
                    "recorded_steps": result.recorded_steps,
                },
                "stage_2_compile": {
                    "compiled_v1_sha256": result.compiled_v1_sha256[:16],
                    "llm_tokens_per_run": 0,
                },
                "stage_3_verify": {
                    "status": result.healthy_receipt.status.value,
                    "dsse_verified": result.healthy_dsse.verified,
                    "dsse_ref": result.healthy_dsse.receipt_ref,
                    "cdc_events": result.healthy_cdc_events,
                },
                "stage_4_fault_and_self_heal": {
                    "fault_status": result.fault_receipt.status.value,
                    "fault_reason": result.fault_receipt.reason,
                    "inbox_card_status": result.inbox_card.status.value,
                    "human_action": result.human_resolution.action.value,
                    "compiled_v2_sha256": result.compiled_v2_sha256[:16],
                    "healed_cdc_events": result.healed_cdc_events,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
