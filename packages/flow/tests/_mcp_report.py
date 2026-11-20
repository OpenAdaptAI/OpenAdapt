"""Model-valid SYNTHETIC MCP transport fixtures; never qualification evidence.

These empty-result reports test the consumer's transport and identity checks.
Their authorization and digests are synthetic assertions, not signed admission,
observed effects, or proof that any workflow ran. Real model validators remain
active, including the strict serialized RunReport roundtrip.
"""

from __future__ import annotations

from typing import Literal

from openadapt_flow.ir import (
    ExecutionOutcomeEnvelope,
    OutcomeContractCounts,
    RunReport,
)


def synthetic_production_report(
    *,
    profile: Literal["standard", "regulated"] = "standard",
    workflow_name: str = "synthetic-mcp-workflow",
    bundle_content_digest: str = "a" * 64,
    workflow_contract_sha256: str = "b" * 64,
    governed_runtime_inputs_digest: str = "c" * 64,
    execution_target_kind: Literal[
        "web", "windows", "macos", "linux", "rdp", "citrix"
    ] = "web",
    execution_origin: str | None = "https://synthetic.invalid",
    execution_entry_url: str | None = "https://synthetic.invalid/",
    params: dict[str, str | bool | int | float] | None = None,
) -> RunReport:
    """Return a SYNTHETIC positive transport fixture with real typed validation.

    One required authorization contract is asserted as passed, matching the
    existing envelope-model fixture in test_execution_profiles.py. There are no
    identity, postcondition or effect claims because no action was executed.
    Caller-supplied digests only bind the synthetic test case; they confer no
    authority or independent qualification.
    """
    required = OutcomeContractCounts(authorization=1)
    passed = OutcomeContractCounts(authorization=1)
    envelope = ExecutionOutcomeEnvelope(
        outcome="VERIFIED",
        profile=profile,
        production_eligible=True,
        qualification_evidence_only=False,
        execution_completed=True,
        required_contracts=required,
        passed_contracts=passed,
        workflow_contract_sha256=workflow_contract_sha256,
        evidence_classes=["authorization"],
        model_calls=0,
        external_network_calls="none",
    )
    report = RunReport(
        workflow_name=workflow_name,
        started_at="2026-11-20T00:00:00Z",
        execution_profile=profile,
        execution_outcome="VERIFIED",
        production_eligible=True,
        qualification_evidence_only=False,
        execution_completed=True,
        outcome_envelope=envelope,
        transaction_outcome="VERIFIED",
        transaction_billable=True,
        transaction_platform_fault=False,
        success=True,
        execution_target_kind=execution_target_kind,
        execution_origin=execution_origin,
        execution_entry_url=execution_entry_url,
        bundle_content_digest=bundle_content_digest,
        workflow_contract_sha256=workflow_contract_sha256,
        governed_authorization_id="synthetic-mcp-authorization",
        governed_runtime_inputs_digest=governed_runtime_inputs_digest,
        params=dict(params or {}),
        results=[],
        model_calls=0,
        external_network_calls="none",
    )
    return RunReport.model_validate_json(report.model_dump_json(), strict=True)
