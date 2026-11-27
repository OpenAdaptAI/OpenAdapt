"""Unit and MCP server tests for mid-run continuation (continue_halted_run)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import anyio
import jsonschema
import pytest

from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.continuation import (
    CONTINUATION_OUTPUT_SCHEMA,
    HUMAN_REVIEW_SURFACES,
    ContinuationDesk,
    ContinuationError,
    FlowResumeExecutor,
    HaltContinuationRecord,
    redact_parameters,
)
from openadapt_agent.mcp import build_server


def _wire(model: Any) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json")


class RecordingStepExecutor:
    """Deterministic multi-step executor that proves prior steps are not re-run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.executed_steps: list[str] = []

    def resume_halted_run(
        self,
        *,
        record: HaltContinuationRecord,
        supplied_parameters: Mapping[str, Any],
        merged_parameters: Mapping[str, Any],
        retry_transient: bool,
        wait_seconds: int = 60,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "run_id": record.run_id,
                "halt_id": record.halt_id,
                "step_id": record.step_id,
                "checkpoint_id": record.checkpoint_id,
                "completed_steps": list(record.completed_steps),
                "supplied_parameters": dict(supplied_parameters),
                "merged_parameters": dict(merged_parameters),
                "retry_transient": retry_transient,
                "wait_seconds": wait_seconds,
            }
        )
        # Only execute from the halted step onward, never prior completed_steps.
        self.executed_steps.extend([record.step_id, "step_final_verify"])
        return {
            "outcome": "done",
            "reason": "saved_and_checked",
            "run_id": record.run_id,
            "resumed_step": record.step_id,
            "checkpoint_id": record.checkpoint_id,
            "restarted_from_step_1": False,
        }


def _make_desk(tmp_path) -> tuple[ContinuationDesk, RecordingStepExecutor]:
    executor = RecordingStepExecutor()
    desk = ContinuationDesk(tmp_path / "runs", executor=executor)
    return desk, executor


# -- Successful continuations --------------------------------------------------


@pytest.mark.parametrize("halt_class", ["missing_parameter", "missing_input"])
def test_continue_missing_parameter_resumes_from_checkpoint_without_restarting(
    tmp_path, halt_class
):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-missing-001",
        halt_id="halt-0001",
        workflow="submit_referral",
        halt_class=halt_class,
        step_id="step_2_enter_diagnosis",
        checkpoint_id="ckpt-step-1-verified",
        completed_steps=["step_1_select_record"],
        transaction_outcome="HALTED_BEFORE_EFFECT",
        parameter_schema={
            "record_id": {"type": "string"},
            "diagnosis_code": {"type": "string", "pattern": r"^[A-Z][0-9]{2}$"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        missing_parameters=["diagnosis_code"],
        immutable_identity_keys=["record_id"],
        bound_parameters={"record_id": "REC-42"},
    )

    result = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"diagnosis_code": "J06"},
    )

    jsonschema.validate(result, CONTINUATION_OUTPUT_SCHEMA)
    assert result["continued"] is True
    assert result["status"] == "continued"
    assert result["run_id"] == "run-missing-001"
    assert result["halt_id"] == "halt-0001"
    assert result["resumed_from_step"] == "step_2_enter_diagnosis"
    assert result["resumed_from_checkpoint"] == "ckpt-step-1-verified"
    assert result["supplied_parameter_keys"] == ["diagnosis_code"]
    assert result["result"]["outcome"] == "done"
    assert result["result"]["restarted_from_step_1"] is False

    # Verify prior step was NOT re-executed and merged parameters keep bound identity.
    assert len(executor.calls) == 1
    assert executor.executed_steps == ["step_2_enter_diagnosis", "step_final_verify"]
    assert "step_1_select_record" not in executor.executed_steps
    assert executor.calls[0]["merged_parameters"] == {
        "record_id": "REC-42",
        "diagnosis_code": "J06",
    }

    # Verify audit entry links halt_id, hashed resume_token, and outcome.
    audit = desk.read_audit_entry(result["audit_id"])
    assert audit is not None
    assert audit["run_id"] == "run-missing-001"
    assert audit["halt_id"] == "halt-0001"
    assert audit["decision"] == "continued"
    assert audit["resumed_from_step"] == "step_2_enter_diagnosis"
    assert audit["resumed_from_checkpoint"] == "ckpt-step-1-verified"
    assert audit["resume_token_sha256"]
    assert packet["resume_token"] not in json.dumps(audit)
    assert audit["outcome"]["outcome"] == "done"


def test_continue_by_run_id_and_halt_id(tmp_path):
    desk, executor = _make_desk(tmp_path)
    desk.register_halt(
        run_id="run-by-id-001",
        halt_id="halt-by-id-01",
        workflow="update_note",
        halt_class="missing_parameter",
        step_id="step_2_note",
        checkpoint_id="ckpt-1",
        parameter_schema={"note": {"type": "string", "minLength": 2}},
        missing_parameters=["note"],
    )

    result = desk.continue_halted_run(
        run_id="run-by-id-001",
        halt_id="halt-by-id-01",
        parameters={"note": "Follow up next week"},
    )
    assert result["continued"] is True
    assert len(executor.calls) == 1
    assert executor.calls[0]["supplied_parameters"] == {"note": "Follow up next week"}


@pytest.mark.parametrize("halt_class", ["transient_network", "retryable_timeout"])
def test_continue_safe_transient_timeout_before_commit(tmp_path, halt_class):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-net-001",
        halt_id="halt-net-01",
        workflow="fetch_summary",
        halt_class=halt_class,
        step_id="step_2_load_panel",
        checkpoint_id="ckpt-1",
        completed_steps=["step_1_open_chart"],
        transaction_outcome="HALTED_BEFORE_EFFECT",
        write_in_flight=False,
    )

    # Without retry_transient=True, refuses without consuming the token.
    first = desk.continue_halted_run(resume_token=packet["resume_token"])
    assert first["continued"] is False
    assert first["error"] == "retry_transient_required"
    assert len(executor.calls) == 0

    # With retry_transient=True, resumes from step_2_load_panel.
    second = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        retry_transient=True,
    )
    jsonschema.validate(second, CONTINUATION_OUTPUT_SCHEMA)
    assert second["continued"] is True
    assert second["retry_transient"] is True
    assert second["resumed_from_step"] == "step_2_load_panel"
    assert len(executor.calls) == 1
    assert executor.executed_steps == ["step_2_load_panel", "step_final_verify"]


# -- Hard refusals (human_sign_off_required) -----------------------------------


@pytest.mark.parametrize(
    "halt_class",
    [
        "identity_mismatch",
        "record_not_confirmed",
        "target_ambiguity",
        "duplicate_record_collision",
        "policy_violation",
        "permission_denied",
        "approval_needed",
        "reconciliation_required",
        "RECONCILIATION_REQUIRED",
        "lost_reply_after_click",
        "destructive_halt",
        "unknown_halt_class",
        "",
    ],
)
def test_hard_refusal_on_disallowed_halt_classes(tmp_path, halt_class):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-unsafe-001",
        halt_id="halt-unsafe-01",
        workflow="submit_order",
        halt_class=halt_class,
        step_id="step_3_commit",
        transaction_outcome="HALTED_BEFORE_EFFECT",
    )

    result = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"note": "try anyway"},
        retry_transient=True,
    )
    jsonschema.validate(result, CONTINUATION_OUTPUT_SCHEMA)
    assert result["continued"] is False
    assert result["status"] == "refused"
    assert result["error"] == "human_sign_off_required"
    assert result["human_review_surfaces"] == list(HUMAN_REVIEW_SURFACES)
    assert len(executor.calls) == 0

    audit = desk.read_audit_entry(result["audit_id"])
    assert audit is not None
    assert audit["decision"] == "refused"
    assert audit["refusal_code"] == "human_sign_off_required"


@pytest.mark.parametrize(
    "tx_outcome",
    [
        "RECONCILIATION_REQUIRED",
        "COMPLETED_UNVERIFIED",
        "SAVED_AWAITING_CHECK",
        "REJECTED_POLICY",
    ],
)
def test_hard_refusal_on_post_write_reconciliation_outcome_even_if_transient(tmp_path, tx_outcome):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-recon-001",
        halt_id="halt-recon-01",
        workflow="submit_claim",
        halt_class="transient_network",
        step_id="step_4_submit",
        transaction_outcome=tx_outcome,
        write_in_flight=False,
    )

    result = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        retry_transient=True,
    )
    assert result["continued"] is False
    assert result["error"] == "human_sign_off_required"
    assert result["reason"] == "post_write_reconciliation_required"
    assert result["human_review_surfaces"] == list(HUMAN_REVIEW_SURFACES)
    assert len(executor.calls) == 0


def test_hard_refusal_on_transient_timeout_with_write_in_flight(tmp_path):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-inflight-001",
        halt_id="halt-inflight-01",
        workflow="submit_claim",
        halt_class="retryable_timeout",
        step_id="step_4_submit",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        write_in_flight=True,
    )

    result = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        retry_transient=True,
    )
    assert result["continued"] is False
    assert result["error"] == "human_sign_off_required"
    assert result["reason"] == "write_in_flight_uncertainty"
    assert len(executor.calls) == 0


def test_hard_refusal_on_expired_resume_token(tmp_path):
    desk, executor = _make_desk(tmp_path)
    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    packet = desk.register_halt(
        run_id="run-exp-001",
        halt_id="halt-exp-01",
        workflow="update_note",
        halt_class="missing_parameter",
        step_id="step_2_note",
        parameter_schema={"note": {"type": "string"}},
        missing_parameters=["note"],
        ttl_s=60.0,
        now=t0,
    )

    result = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"note": "Late value"},
        now=t0 + timedelta(seconds=61),
    )
    assert result["continued"] is False
    assert result["error"] == "human_sign_off_required"
    assert result["reason"] == "token_expired"
    assert len(executor.calls) == 0


def test_hard_refusal_on_reused_resume_token(tmp_path):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-reuse-001",
        halt_id="halt-reuse-01",
        workflow="update_note",
        halt_class="missing_parameter",
        step_id="step_2_note",
        parameter_schema={"note": {"type": "string"}},
        missing_parameters=["note"],
    )

    first = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"note": "First value"},
    )
    assert first["continued"] is True
    assert len(executor.calls) == 1

    second = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"note": "Second value"},
    )
    assert second["continued"] is False
    assert second["error"] == "human_sign_off_required"
    assert second["reason"] == "token_reused"
    assert len(executor.calls) == 1


def test_hard_refusal_on_invalid_or_mismatched_resume_token(tmp_path):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-valid-001",
        halt_id="halt-valid-01",
        workflow="update_note",
        halt_class="missing_parameter",
        step_id="step_2_note",
        parameter_schema={"note": {"type": "string"}},
        missing_parameters=["note"],
    )

    bad = desk.continue_halted_run(
        resume_token="rtok_nonexistent_token_123456",
        parameters={"note": "x"},
    )
    assert bad["continued"] is False
    assert bad["error"] == "human_sign_off_required"
    assert bad["reason"] == "invalid_resume_token"

    mismatched = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        run_id="run-other-999",
        halt_id="halt-other-99",
        parameters={"note": "x"},
    )
    assert mismatched["continued"] is False
    assert mismatched["error"] == "human_sign_off_required"
    assert mismatched["reason"] == "invalid_resume_token"
    assert len(executor.calls) == 0


def test_hard_refusal_on_immutable_identity_parameter_overwrite(tmp_path):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-ident-001",
        halt_id="halt-ident-01",
        workflow="submit_referral",
        halt_class="missing_parameter",
        step_id="step_2_note",
        parameter_schema={
            "patient_id": {"type": "string"},
            "custom_anchor": {"type": "string"},
            "note": {"type": "string"},
        },
        missing_parameters=["note"],
        immutable_identity_keys=["custom_anchor"],
        bound_parameters={"patient_id": "PAT-100", "custom_anchor": "ANCHOR-1"},
    )

    # Attempt to overwrite default-protected bound identity key (patient_id).
    res1 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"patient_id": "PAT-999", "note": "Updated"},
    )
    assert res1["continued"] is False
    assert res1["error"] == "human_sign_off_required"
    assert res1["reason"] == "immutable_parameter_overwrite"
    assert len(executor.calls) == 0

    # Attempt to overwrite explicitly declared immutable key (custom_anchor).
    res2 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"custom_anchor": "ANCHOR-2", "note": "Updated"},
    )
    assert res2["continued"] is False
    assert res2["error"] == "human_sign_off_required"
    assert res2["reason"] == "immutable_parameter_overwrite"
    assert len(executor.calls) == 0


# -- Parameter schema validation and audit redaction ---------------------------


def test_parameter_validation_rejects_unknown_malformed_and_wrong_types(tmp_path):
    desk, executor = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-schema-001",
        halt_id="halt-schema-01",
        workflow="submit_referral",
        halt_class="missing_parameter",
        step_id="step_2_form",
        parameter_schema={
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "status": {"type": "string", "enum": ["routine", "urgent"]},
        },
        missing_parameters=["count", "status"],
    )

    # Unknown key
    r1 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"count": 2, "status": "routine", "extra_key": "nope"},
    )
    assert r1["continued"] is False
    assert r1["error"] == "invalid_parameters"

    # Malformed key
    r2 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"../evil": "x"},
    )
    assert r2["continued"] is False
    assert r2["error"] == "invalid_parameters"

    # Boolean rejected for integer
    r3 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"count": True, "status": "routine"},
    )
    assert r3["continued"] is False
    assert r3["error"] == "invalid_parameters"

    # Out-of-enum string
    r4 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"count": 2, "status": "stat"},
    )
    assert r4["continued"] is False
    assert r4["error"] == "invalid_parameters"

    # Missing required parameter
    r5 = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"count": 2},
    )
    assert r5["continued"] is False
    assert r5["error"] == "invalid_parameters"
    assert r5["reason"] == "missing_required_parameters"
    assert len(executor.calls) == 0


def test_audit_entry_redacts_secret_and_credential_parameters(tmp_path):
    redacted = redact_parameters(
        {
            "note": "Normal text",
            "api_token": "tok-super-secret",
            "db_password": "correct-horse-battery",
            "nested": {"client_secret": "shh", "safe": "ok"},
            "freeform": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def",
            "stripe_like": "sk_" + "live_" + "1234567890abcdef",
        }
    )
    assert redacted["note"] == "Normal text"
    assert redacted["api_token"] == "[REDACTED]"
    assert redacted["db_password"] == "[REDACTED]"
    assert redacted["nested"]["client_secret"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == "ok"
    assert redacted["freeform"] == "[REDACTED]"
    assert redacted["stripe_like"] == "[REDACTED]"

    desk, _ = _make_desk(tmp_path)
    packet = desk.register_halt(
        run_id="run-redact-001",
        halt_id="halt-redact-01",
        workflow="sync_portal",
        halt_class="missing_parameter",
        step_id="step_2_auth_code",
        parameter_schema={
            "portal_api_key": {"type": "string"},
            "note": {"type": "string"},
        },
        missing_parameters=["portal_api_key", "note"],
    )
    secret_val = "sk_" + "live_secret_value_999"
    res = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"portal_api_key": secret_val, "note": "ok"},
    )
    assert res["continued"] is True
    audit = desk.read_audit_entry(res["audit_id"])
    assert audit is not None
    assert audit["supplied_parameters"]["portal_api_key"] == "[REDACTED]"
    assert audit["supplied_parameters"]["note"] == "ok"
    assert secret_val not in desk.audit_log_path.read_text(encoding="utf-8")


# -- MCP server end-to-end integration tests -----------------------------------


def test_mcp_server_exposes_and_executes_continue_halted_run(
    bundles_root, runner_config, mcp_client
):
    executor = RecordingStepExecutor()
    bridge = AgentBridge(
        bundles_root,
        runner_config,
        allow_run=True,
        continuation_executor=executor,
    )
    missing_pkt = bridge.continuation_desk.register_halt(
        run_id="run-mcp-001",
        halt_id="halt-mcp-01",
        workflow="demo",
        halt_class="missing_parameter",
        step_id="step_2_note",
        checkpoint_id="ckpt-mcp-1",
        completed_steps=["step_1_open"],
        parameter_schema={"note": {"type": "string"}, "patient_id": {"type": "string"}},
        missing_parameters=["note"],
        bound_parameters={"patient_id": "PAT-7"},
    )
    unsafe_pkt = bridge.continuation_desk.register_halt(
        run_id="run-mcp-002",
        halt_id="halt-mcp-02",
        workflow="demo",
        halt_class="identity_mismatch",
        step_id="step_2_verify_target",
        transaction_outcome="HALTED_BEFORE_EFFECT",
    )
    server = build_server(bridge)

    async def probe():
        async with mcp_client(server) as client:
            tools = _wire(await client.list_tools())["tools"]
            ok_res = _wire(
                await client.call_tool(
                    "continue_halted_run",
                    {
                        "resume_token": missing_pkt["resume_token"],
                        "parameters": {"note": "Confirmed over MCP"},
                    },
                )
            )
            refused_res = _wire(
                await client.call_tool(
                    "continue_halted_run",
                    {"resume_token": unsafe_pkt["resume_token"]},
                )
            )
            return tools, ok_res, refused_res

    tools, ok_res, refused_res = anyio.run(probe)
    names = [t["name"] for t in tools]
    assert "continue_halted_run" in names
    cont_tool = next(t for t in tools if t["name"] == "continue_halted_run")
    assert cont_tool["annotations"]["destructiveHint"] is True
    assert cont_tool["annotations"]["idempotentHint"] is False
    assert cont_tool["annotations"]["readOnlyHint"] is False

    assert ok_res["isError"] is False
    ok_payload = json.loads(ok_res["content"][0]["text"])
    assert ok_payload["continued"] is True
    assert ok_payload["resumed_from_step"] == "step_2_note"
    assert len(executor.calls) == 1

    assert refused_res["isError"] is False
    refused_payload = json.loads(refused_res["content"][0]["text"])
    assert refused_payload["continued"] is False
    assert refused_payload["error"] == "human_sign_off_required"
    assert refused_payload["human_review_surfaces"] == list(HUMAN_REVIEW_SURFACES)
    assert len(executor.calls) == 1


def test_continue_halted_run_hidden_and_refused_when_runs_disabled(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=False)
    assert "continue_halted_run" not in {spec.name for spec in bridge.list_tool_specs()}
    with pytest.raises(BridgeError, match="unavailable"):
        bridge.dispatch("continue_halted_run", {"resume_token": "rtok_disabled_0001"})


def test_executor_failure_writes_audit_entry_and_consumes_token_once(tmp_path):
    class FailingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def resume_halted_run(self, **_kwargs) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("backend connection dropped during resume")

    executor = FailingExecutor()
    desk = ContinuationDesk(tmp_path / "runs", executor=executor)
    packet = desk.register_halt(
        run_id="run-fail-001",
        halt_id="halt-fail-01",
        workflow="submit_referral",
        halt_class="missing_parameter",
        step_id="step_2_note",
        checkpoint_id="ckpt-1",
        parameter_schema={"note": {"type": "string"}},
        missing_parameters=["note"],
    )

    with pytest.raises(ContinuationError, match="backend connection dropped during resume"):
        desk.continue_halted_run(
            resume_token=packet["resume_token"],
            parameters={"note": "Attempt 1"},
        )

    assert executor.calls == 1
    entries = desk.list_audit_entries()
    assert len(entries) == 1
    assert entries[0]["decision"] == "executor_error"
    assert entries[0]["refusal_code"] == "executor_error"
    assert entries[0]["run_id"] == "run-fail-001"
    assert entries[0]["halt_id"] == "halt-fail-01"
    assert entries[0]["resumed_from_step"] == "step_2_note"

    # Token is already consumed and linked to the audit entry; second call must refuse.
    persisted = json.loads(
        desk._record_path("run-fail-001", "halt-fail-01").read_text(encoding="utf-8")
    )
    assert persisted["consumed"] is True
    assert persisted["consumed_audit_id"] == entries[0]["audit_id"]

    second = desk.continue_halted_run(
        resume_token=packet["resume_token"],
        parameters={"note": "Attempt 2"},
    )
    assert second["continued"] is False
    assert second["error"] == "human_sign_off_required"
    assert second["reason"] == "token_reused"
    assert executor.calls == 1


def test_flow_resume_executor_refuses_symlink_and_out_of_tree_run_dir(tmp_path):
    class DummyRunner:
        def __init__(self) -> None:
            self.resumed: list[str] = []

        def resume(self, *, run_dir, **_kwargs) -> dict[str, Any]:
            self.resumed.append(str(run_dir))
            return {"outcome": "done"}

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    inside_dir = runs_dir / "run-ok-001"
    inside_dir.mkdir()
    symlink_dir = runs_dir / "run-link-001"
    symlink_dir.symlink_to(outside_dir)

    runner = DummyRunner()
    executor = FlowResumeExecutor(runner, runs_dir=runs_dir)

    def _record_for(path) -> HaltContinuationRecord:
        return HaltContinuationRecord(
            run_id="run-path-001",
            halt_id="halt-path-01",
            workflow="demo",
            halt_class="transient_network",
            step_id="step_1",
            run_dir=str(path),
        )

    with pytest.raises(ContinuationError, match="symlink"):
        executor.resume_halted_run(
            record=_record_for(symlink_dir),
            supplied_parameters={},
            merged_parameters={},
            retry_transient=True,
        )

    with pytest.raises(ContinuationError, match="outside"):
        executor.resume_halted_run(
            record=_record_for(outside_dir),
            supplied_parameters={},
            merged_parameters={},
            retry_transient=True,
        )

    assert runner.resumed == []

    ok = executor.resume_halted_run(
        record=_record_for(inside_dir),
        supplied_parameters={},
        merged_parameters={},
        retry_transient=True,
    )
    assert ok == {"outcome": "done"}
    assert runner.resumed == [str(inside_dir.resolve())]
