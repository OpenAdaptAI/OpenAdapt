"""Execute a ProcessContract by invoking each admitted child through Execute.

The parent is not a workflow-program interpreter and not compose. Each child
must present a live-valid ``QualificationAdmissionEnvelope`` (v1). Window
titles, URLs, and OCR are not evidence. Missing handoff facts HALT.

``execute`` takes real admission objects. Compose's ``capability=None,
admission=None`` bind is not this path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from openadapt_flow.admitted_composition import (
    ProcessContract,
    ProcessContractError,
    live_bundle_content_digest,
    load_child_envelope,
    predecessor_map,
    resolve_pointer,
    topological_order,
)
from openadapt_flow.compiler.compose_authoring import effect_bound_param_names
from openadapt_flow.composition import HandoffBinding
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification_admission import (
    QualificationAdmissionEnvelope,
    QualificationAdmissionError,
    QualificationAdmissionExpected,
    QualificationSignerTrust,
    expected_from_payload,
    verify_qualification_admission,
)
from openadapt_flow.runtime.composition import ChildRunResult

ALWAYS_VERIFIED = frozenset({"VERIFIED"})


@dataclass(frozen=True)
class AdmittedCapability:
    """The independently admitted child Execute receives."""

    name: str
    admission_id: str
    workflow_version_id: str
    bundle_content_digest: str


class AdmittedChildExecutor(Protocol):
    """Bound child runner: ExecuteClient on Cloud, governed ``run`` locally."""

    def __call__(
        self,
        capability: AdmittedCapability,
        admission: QualificationAdmissionEnvelope,
        inputs: Mapping[str, str],
        *,
        workflow: Workflow,
        bundle_dir: Path,
        run_dir: Path,
        child: str,
    ) -> ChildRunResult: ...


@dataclass
class ProcessReport:
    """Parent process result. Not a Workflow RunReport."""

    name: str
    outcome: str
    children: list[ChildRunResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    halted_at: Optional[str] = None
    reason: str = ""
    model_calls: int = 0
    child_admissions: dict[str, str] = field(default_factory=dict)
    child_digests: dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.outcome == "VERIFIED"


def execute(
    capability: AdmittedCapability,
    admission: QualificationAdmissionEnvelope,
    inputs: Mapping[str, str],
    *,
    workflow: Workflow,
    bundle_dir: Path,
    run_dir: Path,
    child: str,
    child_run: AdmittedChildExecutor,
) -> ChildRunResult:
    """Run one independently admitted child.

    ``capability`` and ``admission`` are required. Passing ``None`` is a
    process bug, not a compose compatibility shim.
    """

    if capability is None or admission is None:
        raise ProcessContractError(
            "process execute requires a real AdmittedCapability and "
            "QualificationAdmissionEnvelope; compose's None bind is not "
            "this path"
        )
    return child_run(
        capability,
        admission,
        inputs,
        workflow=workflow,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        child=child,
    )


_EXECUTE_OUTCOMES = {
    "verified": "VERIFIED",
    "halted_before_effect": "HALTED",
    # Delivery uncertainty is not a general halt.  The exact class must reach
    # the parent so it cannot be absorbed or retried as a proved pre-effect stop.
    "reconciliation_required": "RECONCILIATION_REQUIRED",
    "rejected_policy": "HALTED",
    "failed_platform": "FAILED",
    "rolled_back_verified": "ROLLED_BACK",
}


def _attr(value: object, name: str, default: object = None) -> object:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").lower()


def child_run_via_execute_client(
    client: Any,
    *,
    environment_id: str,
    actor_id: str,
    authorization_reference: str | None = None,
    minimum_effect_strength: str = "independent_system_of_record",
) -> AdmittedChildExecutor:
    """Bind process ``execute()`` to Cloud Execute (``POST /v1/executions``).

    Local CLI uses governed ``openadapt-flow run`` instead. This path does not
    poll forever: one ``get_execution`` after accept. A non-terminal state
    HALTs. Receipts do not carry effect-bound param values, so a successor
    handoff still HALTs unless those facts arrive some other confirmed way.
    """

    def child_run(
        capability: AdmittedCapability,
        admission: QualificationAdmissionEnvelope,
        inputs: Mapping[str, str],
        *,
        workflow: Workflow,
        bundle_dir: Path,
        run_dir: Path,
        child: str,
    ) -> ChildRunResult:
        del workflow, bundle_dir
        if capability is None or admission is None:
            raise ProcessContractError(
                "process execute requires a real AdmittedCapability and "
                "QualificationAdmissionEnvelope; compose's None bind is not "
                "this path"
            )
        try:
            from openadapt_types.execute import (
                EffectStrengthV1,
                ExecuteAuthorizationContextV1,
                ExecuteLifecycleStateV1,
                ExecuteRequestV1,
            )
        except ImportError as exc:
            raise ProcessContractError(
                "process Cloud Execute requires openadapt-types "
                "(install openadapt-flow[interop])"
            ) from exc

        request = ExecuteRequestV1(
            qualification_id=capability.admission_id,
            workflow_version=capability.workflow_version_id,
            workflow_digest=f"sha256:{capability.bundle_content_digest}",
            environment_id=environment_id,
            parameters=dict(inputs),
            idempotency_key=f"process-{child}-{capability.admission_id}",
            authorization_context=ExecuteAuthorizationContextV1(
                actor_id=actor_id,
                authorization_reference=authorization_reference or f"process-{child}",
            ),
            minimum_effect_strength=EffectStrengthV1(minimum_effect_strength),
        )
        try:
            accepted = client.create_execution(request)
            execution_id = str(_attr(accepted, "execution_id") or "")
            status = client.get_execution(execution_id)
        except Exception as exc:
            return ChildRunResult(
                child=child,
                outcome="FAILED",
                bound_params={str(k): str(v) for k, v in inputs.items()},
                effect_facts={},
                success=False,
                halt_class=type(exc).__name__,
                report_path=None,
            )

        state_value = _enum_value(_attr(status, "state"))
        payload: dict[str, Any] = {
            "execution_id": execution_id,
            "state": state_value,
            "admission_id": capability.admission_id,
            "workflow_version_id": capability.workflow_version_id,
            "bundle_content_digest": capability.bundle_content_digest,
        }
        report_path = run_dir / "execute-status.json"
        run_dir.mkdir(parents=True, exist_ok=True)

        if state_value != ExecuteLifecycleStateV1.TERMINAL.value:
            payload["reason"] = "Execute did not reach TERMINAL"
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={str(k): str(v) for k, v in inputs.items()},
                effect_facts={},
                success=False,
                halt_class="execute_not_terminal",
                report_path=str(report_path),
            )

        try:
            receipt = client.get_receipt(execution_id)
        except Exception as exc:
            payload["reason"] = str(type(exc).__name__)
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return ChildRunResult(
                child=child,
                outcome="FAILED",
                bound_params={str(k): str(v) for k, v in inputs.items()},
                effect_facts={},
                success=False,
                halt_class=type(exc).__name__,
                report_path=str(report_path),
            )

        receipt_bindings = {
            "execution_id": execution_id,
            "workflow_digest": request.workflow_digest,
            "workflow_version": request.workflow_version,
            "qualification_id": request.qualification_id,
            "environment_id": request.environment_id,
        }
        binding_errors = [
            name
            for name, expected in receipt_bindings.items()
            if str(_attr(receipt, name) or "") != str(expected)
        ]
        status_receipt_id = str(_attr(status, "evidence_receipt_id") or "")
        if status_receipt_id != str(_attr(receipt, "receipt_id") or ""):
            binding_errors.append("evidence_receipt_id")
        status_outcome = _enum_value(_attr(status, "terminal_outcome"))
        receipt_outcome = _enum_value(_attr(receipt, "outcome"))
        if status_outcome != receipt_outcome:
            binding_errors.append("terminal_outcome")
        if binding_errors:
            payload["reason"] = "Execute receipt binding differs"
            payload["binding_errors"] = sorted(binding_errors)
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return ChildRunResult(
                child=child,
                outcome="RECONCILIATION_REQUIRED",
                bound_params={str(k): str(v) for k, v in inputs.items()},
                effect_facts={},
                success=False,
                halt_class="execute_receipt_binding_differs",
                report_path=str(report_path),
            )

        outcome_value = _enum_value(_attr(receipt, "outcome"))
        outcome = _EXECUTE_OUTCOMES.get(outcome_value, "HALTED")
        contracts = _attr(receipt, "contracts")
        model_used = bool(_attr(contracts, "model_used", False))
        payload["outcome"] = outcome_value
        payload["model_used"] = model_used
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return ChildRunResult(
            child=child,
            outcome=outcome,
            bound_params={str(k): str(v) for k, v in inputs.items()},
            effect_facts={},
            model_calls=1 if model_used else 0,
            success=outcome == "VERIFIED",
            report_path=str(report_path),
        )

    child_run.execute_via = "execute_client"  # type: ignore[attr-defined]
    return child_run


def _handoffs_into(contract: ProcessContract, child_name: str) -> list[HandoffBinding]:
    return [item for item in contract.handoffs if item.to_child == child_name]


def _halt(
    contract: ProcessContract,
    *,
    results: list[ChildRunResult],
    applied: dict[str, str],
    halted_at: str,
    reason: str,
    model_calls: int,
    child_admissions: dict[str, str],
    child_digests: dict[str, str],
    root_run: Path,
    outcome: str = "HALTED",
) -> ProcessReport:
    report = ProcessReport(
        name=contract.name,
        outcome=outcome,
        children=results,
        handoffs=applied,
        halted_at=halted_at,
        reason=reason,
        model_calls=model_calls,
        child_admissions=dict(child_admissions),
        child_digests=dict(child_digests),
    )
    _write_parent_report(root_run, report, contract)
    return report


def execute_process_contract(
    contract: ProcessContract,
    *,
    parent_dir: Path | str,
    run_dir: Path | str,
    inputs: Optional[Mapping[str, str]] = None,
    child_run: AdmittedChildExecutor,
    trusted_signers: Mapping[str, QualificationSignerTrust],
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
    expected_by_child: Optional[Mapping[str, QualificationAdmissionExpected]] = None,
) -> ProcessReport:
    """Sequence admitted children. Unverified admissions never reach Execute."""

    parent = Path(parent_dir)
    root_run = Path(run_dir)
    root_run.mkdir(parents=True, exist_ok=True)
    parent_inputs = dict(inputs or {})
    order = topological_order(contract)
    preds = predecessor_map(contract)
    results: list[ChildRunResult] = []
    by_name: dict[str, ChildRunResult] = {}
    applied: dict[str, str] = {}
    total_model = 0
    child_admissions: dict[str, str] = {}
    child_digests: dict[str, str] = {}
    clock = now or datetime.now(timezone.utc)

    for child_name in order:
        spec = contract.child(child_name)
        try:
            envelope = load_child_envelope(parent, spec)
        except ProcessContractError as exc:
            return _halt(
                contract,
                results=results,
                applied=applied,
                halted_at=child_name,
                reason=str(exc),
                model_calls=total_model,
                child_admissions=child_admissions,
                child_digests=child_digests,
                root_run=root_run,
            )
        payload = envelope.payload
        child_admissions[child_name] = payload.admission_id
        child_digests[child_name] = payload.bundle_content_digest
        if (
            payload.admission_id != spec.admission_id
            or payload.workflow_version_id != spec.workflow_version_id
            or payload.bundle_content_digest != spec.bundle_content_digest
        ):
            return _halt(
                contract,
                results=results,
                applied=applied,
                halted_at=child_name,
                reason=(
                    f"child {child_name!r} parent pointer does not match "
                    "its QualificationAdmissionEnvelope"
                ),
                model_calls=total_model,
                child_admissions=child_admissions,
                child_digests=child_digests,
                root_run=root_run,
            )

        bundle = resolve_pointer(parent, spec.bundle)
        try:
            workflow = Workflow.load(bundle)
        except Exception as exc:
            return _halt(
                contract,
                results=results,
                applied=applied,
                halted_at=child_name,
                reason=(
                    f"child {child_name!r} bundle could not be loaded "
                    f"({type(exc).__name__})"
                ),
                model_calls=total_model,
                child_admissions=child_admissions,
                child_digests=child_digests,
                root_run=root_run,
                outcome="FAILED",
            )
        live_digest = live_bundle_content_digest(workflow, bundle)
        if live_digest != payload.bundle_content_digest:
            return _halt(
                contract,
                results=results,
                applied=applied,
                halted_at=child_name,
                reason=(
                    f"child {child_name!r} live bundle digest does not match "
                    "its admission envelope"
                ),
                model_calls=total_model,
                child_admissions=child_admissions,
                child_digests=child_digests,
                root_run=root_run,
            )

        expected = (expected_by_child or {}).get(child_name)
        if expected is None:
            expected = expected_from_payload(payload)
        try:
            verify_qualification_admission(
                envelope,
                trusted_signers=trusted_signers,
                expected=expected,
                revoked_admission_ids=revoked_admission_ids,
                now=clock,
            )
        except QualificationAdmissionError as exc:
            return _halt(
                contract,
                results=results,
                applied=applied,
                halted_at=child_name,
                reason=(
                    f"child {child_name!r} admission refused before Execute: {exc}"
                ),
                model_calls=total_model,
                child_admissions=child_admissions,
                child_digests=child_digests,
                root_run=root_run,
            )

        for pred in preds[child_name]:
            pred_result = by_name.get(pred)
            if pred_result is None:
                return _halt(
                    contract,
                    results=results,
                    applied=applied,
                    halted_at=child_name,
                    reason=(
                        f"child {child_name!r} cannot start: predecessor "
                        f"{pred!r} never ran"
                    ),
                    model_calls=total_model,
                    child_admissions=child_admissions,
                    child_digests=child_digests,
                    root_run=root_run,
                )
            if pred_result.outcome != "VERIFIED":
                absorbed = set(contract.allow_halt.get(pred, ()))
                if pred_result.outcome not in absorbed:
                    return _halt(
                        contract,
                        results=results,
                        applied=applied,
                        halted_at=pred,
                        reason=(
                            f"child {pred!r} ended {pred_result.outcome}; "
                            "successors HALT because that class is not in "
                            f"allow_halt ({sorted(absorbed) or 'none'})"
                        ),
                        model_calls=total_model,
                        child_admissions=child_admissions,
                        child_digests=child_digests,
                        root_run=root_run,
                        outcome=(
                            "RECONCILIATION_REQUIRED"
                            if pred_result.outcome == "RECONCILIATION_REQUIRED"
                            else "HALTED"
                        ),
                    )

        child_inputs = dict(parent_inputs)
        for handoff in _handoffs_into(contract, child_name):
            pred_result = by_name.get(handoff.from_child)
            if pred_result is None:
                return _halt(
                    contract,
                    results=results,
                    applied=applied,
                    halted_at=child_name,
                    reason=(
                        f"missing handoff evidence {handoff.from_child}."
                        f"{handoff.source} -> {child_name}.{handoff.target}; "
                        f"predecessor {handoff.from_child!r} has not run"
                    ),
                    model_calls=total_model,
                    child_admissions=child_admissions,
                    child_digests=child_digests,
                    root_run=root_run,
                )
            fact = pred_result.effect_facts.get(handoff.source)
            if fact is None or fact == "":
                return _halt(
                    contract,
                    results=results,
                    applied=applied,
                    halted_at=child_name,
                    reason=(
                        f"missing handoff evidence {handoff.from_child}."
                        f"{handoff.source} -> {child_name}.{handoff.target}; "
                        "the predecessor did not confirm that effect-bound "
                        "parameter"
                    ),
                    model_calls=total_model,
                    child_admissions=child_admissions,
                    child_digests=child_digests,
                    root_run=root_run,
                )
            child_inputs[handoff.target] = fact
            applied_key = (
                f"{handoff.from_child}.{handoff.source}->{child_name}.{handoff.target}"
            )
            applied[applied_key] = fact

        capability = AdmittedCapability(
            name=child_name,
            admission_id=payload.admission_id,
            workflow_version_id=payload.workflow_version_id,
            bundle_content_digest=payload.bundle_content_digest,
        )
        child_run_dir = root_run / "children" / child_name
        child_run_dir.mkdir(parents=True, exist_ok=True)
        result = execute(
            capability,
            envelope,
            child_inputs,
            workflow=workflow,
            bundle_dir=bundle,
            run_dir=child_run_dir,
            child=child_name,
            child_run=child_run,
        )
        bound_names = effect_bound_param_names(workflow)
        verified_facts = {
            key: value
            for key, value in {**result.bound_params, **result.effect_facts}.items()
            if key in bound_names and value != ""
        }
        if result.outcome == "VERIFIED":
            result.effect_facts = verified_facts
        else:
            result.effect_facts = {}
        results.append(result)
        by_name[child_name] = result
        total_model += result.model_calls
        if result.outcome != "VERIFIED":
            absorbed = set(contract.allow_halt.get(child_name, ()))
            if result.outcome not in absorbed:
                return _halt(
                    contract,
                    results=results,
                    applied=applied,
                    halted_at=child_name,
                    reason=(
                        f"child {child_name!r} ended {result.outcome}; "
                        "successors HALT because that class is not in "
                        f"allow_halt ({sorted(absorbed) or 'none'})"
                    ),
                    model_calls=total_model,
                    child_admissions=child_admissions,
                    child_digests=child_digests,
                    root_run=root_run,
                    outcome=(
                        "RECONCILIATION_REQUIRED"
                        if result.outcome == "RECONCILIATION_REQUIRED"
                        else "HALTED"
                    ),
                )

    last = results[-1] if results else None
    if last is None:
        outcome = "FAILED"
        reason = "process has no runnable children"
    elif all(item.outcome == "VERIFIED" for item in results) and total_model == 0:
        outcome = "VERIFIED"
        reason = ""
    elif all(item.success or item.outcome == "VERIFIED" for item in results):
        outcome = "COMPLETED_UNVERIFIED"
        reason = "every child finished but the parent is not VERIFIED"
    else:
        outcome = (
            last.outcome
            if last.outcome
            in {
                "HALTED",
                "FAILED",
                "ROLLED_BACK",
                "COMPLETED_UNVERIFIED",
                "RECONCILIATION_REQUIRED",
            }
            else "HALTED"
        )
        reason = f"last child {last.child} ended {last.outcome}"

    report = ProcessReport(
        name=contract.name,
        outcome=outcome,
        children=results,
        handoffs=applied,
        reason=reason,
        model_calls=total_model,
        child_admissions=child_admissions,
        child_digests=child_digests,
    )
    _write_parent_report(root_run, report, contract)
    return report


def _write_parent_report(
    run_dir: Path, report: ProcessReport, contract: ProcessContract
) -> None:
    children_payload = []
    for item in report.children:
        spec = contract.child(item.child)
        children_payload.append(
            {
                "child": item.child,
                "admission_id": report.child_admissions.get(
                    item.child, spec.admission_id
                ),
                "workflow_version_id": spec.workflow_version_id,
                "bundle_content_digest": report.child_digests.get(
                    item.child, spec.bundle_content_digest
                ),
                "outcome": item.outcome,
                "success": item.success,
                "model_calls": item.model_calls,
                "halt_class": item.halt_class,
                "report_path": item.report_path,
                "effect_fact_names": sorted(item.effect_facts),
            }
        )
    payload = {
        "schema_version": contract.schema_version,
        "name": report.name,
        "outcome": report.outcome,
        "success": report.success,
        "halted_at": report.halted_at,
        "reason": report.reason,
        "model_calls": report.model_calls,
        "handoffs": {key: "<bound>" for key in report.handoffs},
        "children": children_payload,
    }
    (run_dir / "process-report.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
