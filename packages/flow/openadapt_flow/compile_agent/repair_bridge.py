"""Run a person-approved one-step repair's normal compiled canaries.

This joins the existing RepairDesk/RepairStore, JIT admission and verifier.
It does not propose, approve or sign a new kind of authority, call a model,
or repeat the possibly submitted item that originally halted. Canary inputs
belong to a declared synthetic test record, as in the normal compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class RepairBridgeError(RuntimeError):
    """Fixed refusal; the original held item remains held."""


@dataclass(frozen=True)
class RepairBridgeResult:
    candidate_id: str
    bundle_dir: Path
    run_dir: Path
    receipt: Path
    canary_runs: int


def complete_approved_repair(
    cache: Any,
    prompt: str,
    params: Mapping[str, Any],
    *,
    repair_desk: Any,
    candidate_id: str,
    canary_params: Sequence[Mapping[str, Any]],
) -> RepairBridgeResult:
    """Canary and select one already approved RepairStore candidate.

    No candidate or ACTIVE file is manufactured here. Normal store transitions
    and the existing admission carry, governed Replayer, verify-run receipt,
    canary monitor and cache handoff make every decision. A stopped/uncertain
    canary is never retried by this call or accepted by a later invocation.
    """
    from openadapt_flow.compile_agent.pipeline import _repeats_run1
    from openadapt_flow.compile_agent.repair_selection import _document, _inside, _proof
    from openadapt_flow.ir import RunReport, Workflow
    from openadapt_flow.jit import lifecycle
    from openadapt_flow.repair.candidate import Actor
    from openadapt_flow.repair.lifecycle import RepairStore, observation_from_report
    from openadapt_flow.repair.self_repair import _carry_admission, _repairable
    from openadapt_flow.transaction import steps_requiring_reconciliation

    automation = Actor(kind="automation", id="compiled-repair-canary")
    compiler, key = cache._compiler(prompt, params)
    jit = compiler._jit()
    try:
        # The same closed ID contract used by the existing repair selector.
        if not isinstance(candidate_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", candidate_id):
            raise ValueError("invalid candidate")
        prior_bundle = jit.bundle_dir
        prior = Workflow.load(prior_bundle)
        stop = _document(compiler.dir / "skill-stop.json")
        if stop.get("phase") != "halted" or _document(compiler.dir / "skill-key.json") != key:
            raise ValueError("no matching held task")
        reported_run = Path(stop["replay"]["run_dir"])
        halted_dir = _inside(jit.runs_dir, reported_run.name)
        if reported_run.resolve(strict=True) != halted_dir or reported_run.is_symlink():
            raise ValueError("foreign halted run")
        halted = RunReport.model_validate(_document(halted_dir / "report.json"))
        found = _repairable(halted, prior)
        if not (
            prior.manifest is not None
            and stop["bundle_digest"] == prior.manifest.content_digest
            and halted.bundle_content_digest == prior.manifest.content_digest
            and halted.workflow_name == prior.name
            and halted.execution_outcome == "HALTED"
            and halted.transaction_outcome == "HALTED_BEFORE_EFFECT"
            and not steps_requiring_reconciliation(halted)
            and not isinstance(found, str) and found[1] == "replace"
        ):
            raise ValueError("prior delivery cannot be repaired")
        store = RepairStore(Path(repair_desk.store_for(prior.name)).resolve(strict=True))
        _document(_inside(store.root, f"candidates/{candidate_id}.json"))
        candidate = store.load_candidate(candidate_id)
        approval, provenance, metrics = (
            candidate.approval, candidate.provenance, candidate.canary_metrics
        )
        if not (
            candidate.state in {"approved", "staged"}
            and candidate.candidate_id == candidate_id
            and candidate.campaigns_passed() and not candidate.contract_weakenings
            and candidate.reviewed_by and approval is not None
            and approval.pre_approval_policy_sha256 is None
            and not lifecycle.is_not_a_person_name(approval.approved_by)
            and approval.prior_content_digest == candidate.prior_content_digest == prior.manifest.content_digest
            and approval.proposed_content_digest == candidate.proposed_content_digest
            and candidate.workflow_name == prior.name
            and provenance is not None and provenance.repaired_by == "calling_agent"
            and provenance.halted_run_id == halted_dir.name
            and provenance.halted_step_id == found[0].id
            and len(candidate.taught_steps) == 1
            and metrics.runs_completed == metrics.verified_runs == 0
            and metrics.silent_incorrect == metrics.verification_regressions == 0
            and not metrics.halted and metrics.last_run_at is None
        ):
            raise ValueError("no person-approved one-step candidate")
        rows = [compiler.task.validate(row) for row in canary_params]
        if len(rows) != candidate.canary.max_runs or len({tuple(sorted(row.items())) for row in rows}) != len(rows):
            raise ValueError("canary inputs do not match the existing budget")
        if _repeats_run1(compiler.out_dir / "run1_values.json", compiler.task, rows) is not None:
            raise ValueError("canary inputs need fresh values")
        if any(row == compiler.task.validate(halted.params) for row in rows):
            raise ValueError("the held item is not a canary")
        store._reverify_invariants(candidate)
        if candidate.state == "approved":
            candidate = store.stage(candidate_id, automation)
        staged = _inside(store.root, f"bundles/{candidate.proposed_content_digest}")
        # The original helper is shared with SelfRepair; this is ordinary
        # repaired admission after the store's actual person approval.
        _carry_admission(prior_bundle, staged, candidate_id,
                         approved_by=approval.approved_by, route="person",
                         key_dir=jit.key_dir,
                         approver_root=getattr(jit.teach_authorization, "approver_root", None))
        carried = lifecycle.verify_admission(staged, key_dir=jit.key_dir)
        if carried.get("admission_mode") != "repair" or (carried.get("repair_of") or {}).get("candidate_id") != candidate_id:
            raise ValueError("repair admission unavailable")
        store.start_canary(candidate_id, automation)
    except Exception:
        raise RepairBridgeError("APPROVED_REPAIR_UNAVAILABLE") from None

    last_run = None
    last_receipt = None
    for values in rows:
        try:
            result, _receipt, receipt_path = compiler._governed(
                "repair-canary", values,
                lambda: jit._run_admitted(values, bundle_dir=staged),
            )
            if not isinstance(result.report, RunReport) or result.run_dir is None or receipt_path is None:
                raise RepairBridgeError("REPAIR_CANARY_UNAVAILABLE")
            run_dir = Path(result.run_dir)
            if run_dir.is_symlink() or run_dir.resolve(strict=True) != _inside(jit.runs_dir, run_dir.name):
                raise RepairBridgeError("REPAIR_CANARY_UNAVAILABLE")
            # Reuse the selector's normal report/receipt join for EACH canary,
            # not merely the final run that will be selected after promotion.
            report = _proof(
                jit,
                {"verified_run": run_dir.name,
                 "receipt": str(Path(receipt_path).relative_to(compiler.dir / "receipts"))},
                Workflow.load(staged), halted,
            )
            if report.model_dump() != result.report.model_dump() or compiler.task.validate(report.params) != values:
                raise RepairBridgeError("REPAIR_CANARY_UNAVAILABLE")
            observation = observation_from_report(report)
            if result.status != "VERIFIED" or type(result.model_calls) is not int or result.model_calls != 0:
                observation = observation.model_copy(update={"verified": False, "detail": "compiled canary or independent receipt unavailable"})
            candidate = store.record_canary_run(candidate_id, observation)
            if candidate.canary_metrics.halted:
                raise RepairBridgeError("REPAIR_CANARY_NOT_VERIFIED")
            last_run, last_receipt = result.run_dir, receipt_path
        except BaseException as primary:
            # A possibly submitted canary is not repeated. The task's original
            # stop remains, and normal rollback restores selection only.
            try:
                current = store.load_candidate(candidate_id)
                if current.state == "canary":
                    store.rollback(automation, candidate_id)
            except BaseException:
                pass
            if isinstance(primary, Exception) and not isinstance(primary, RepairBridgeError):
                raise RepairBridgeError("REPAIR_CANARY_UNAVAILABLE") from None
            raise
    if candidate.state != "active" or last_run is None or last_receipt is None:
        raise RepairBridgeError("REPAIR_CANARY_NOT_ACTIVE")
    target = cache.adopt_repair(prompt, rows[-1], repair_desk=repair_desk,
                               candidate_id=candidate_id,
                               verified_run_dir=last_run, receipt=last_receipt)
    return RepairBridgeResult(candidate_id, target, last_run, last_receipt, len(rows))
