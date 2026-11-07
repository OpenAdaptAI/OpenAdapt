"""Select an existing promoted repair; this module grants no admission.

The task-local pointer selects only an existing RepairStore ACTIVE bundle.
Every use rechecks its approval, lineage, ordinary admission and retained
normal verification evidence. It never copies, signs or runs a bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.bundle_validation import BundleIntegrityError, BundleValidationError
from openadapt_flow.crypto import CryptoError

SELECTION_FILE = "skill-repair.json"
_FIELDS = {
    "version",
    "store",
    "candidate_id",
    "prior_digest",
    "halted_run",
    "step_id",
    "verified_run",
    "receipt",
}


class RepairSelectionError(RuntimeError):
    """A selected repair no longer has its required ordinary evidence."""


def _document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("missing regular evidence")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("evidence is not an object")
    return value


def _inside(root: Path, relative: str) -> Path:
    part = Path(relative)
    if (
        part.is_absolute()
        or not part.parts
        or any(p in (".", "..") for p in part.parts)
    ):
        raise ValueError("foreign evidence")
    current = root
    for name in part.parts:
        current = current / name
        if current.is_symlink():
            raise ValueError("aliased evidence")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("foreign evidence")
    return resolved


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("unbound time")
    return stamp


def _proof(jit: Any, selected: Mapping[str, Any], workflow: Any, halted: Any) -> Any:
    from openadapt_flow.ir import RunReport
    from openadapt_flow.qualify.result import verify_config_sha256
    from openadapt_flow.runtime.effects.adapter import verifier_identity
    from openadapt_flow.transaction import steps_requiring_reconciliation
    from openadapt_flow.verify_run import (
        _build_verifier,
        evidence_problem,
        load_verify_config,
        receipt_problem,
    )

    run = _inside(jit.runs_dir, selected["verified_run"])
    report = RunReport.model_validate(_document(run / "report.json"))
    if not (
        report.workflow_name == workflow.name
        and report.bundle_content_digest == workflow.manifest.content_digest
        and report.execution_outcome == "VERIFIED"
        and report.transaction_outcome == "VERIFIED"
        and report.halt is None
        and report.model_calls == 0
        and not steps_requiring_reconciliation(report)
        and sorted(report.params) == sorted(jit.task.params)
        and _time(report.started_at) >= _time(halted.started_at)
    ):
        raise ValueError("no verified repaired run")
    path = _inside(jit.task_dir / "receipts", selected["receipt"])
    document = _document(path)
    receipt, evidence = document["receipt"], document["evidence"]
    cfg = load_verify_config(jit.task_dir / "compiled" / "verify.yaml")
    verifier = _build_verifier(cfg, report.params)
    contract = verify_config_sha256(verifier_identity(verifier), list(cfg.effects))
    if not (
        receipt_problem(receipt) is None
        and evidence_problem(evidence, receipt) is None
        and receipt["outcome"] == "verified"
        and receipt["effect_verdict"] == "CONFIRMED"
        and receipt["verification_tier"] == "independent_readback"
        and receipt["workflow_digest"] == contract
        and receipt["executor_kind"] == "flow_replay"
        and receipt["executor_ref_sha256"]
        == "sha256:" + hashlib.sha256(jit.task.key.encode()).hexdigest()
        and evidence["readback"]["baseline_sha256"] is not None
        and _time(evidence["readback"]["baseline_read_at"]) <= _time(report.started_at)
        and _time(report.started_at).replace(microsecond=0)
        <= _time(evidence["readback"]["read_at"])
    ):
        raise ValueError("no independent repaired-run receipt")
    return report


def validate_selection(jit: Any, selected: Mapping[str, Any]) -> Path:
    """Revalidate normal repair evidence, not a caller's approved flag."""
    from openadapt_flow.ir import RunReport, Workflow
    from openadapt_flow.jit import lifecycle
    from openadapt_flow.repair.invariants import enforce_contract_invariants
    from openadapt_flow.repair.lifecycle import RepairStore
    from openadapt_flow.repair.self_repair import _repairable
    from openadapt_flow.transaction import steps_requiring_reconciliation

    try:
        if (
            set(selected) != _FIELDS
            or type(selected["version"]) is not int
            or selected["version"] != 1
        ):
            raise ValueError("unknown selection")
        if not all(isinstance(selected[k], str) for k in _FIELDS - {"version"}):
            raise ValueError("invalid selection")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", selected["candidate_id"]):
            raise ValueError("invalid candidate")
        root = Path(selected["store"])
        if (
            not root.is_absolute()
            or root.is_symlink()
            or root.resolve(strict=True) != root
        ):
            raise ValueError("aliased store")
        store = RepairStore(root)
        _document(store.active_path)
        _document(store.candidate_path(selected["candidate_id"]))
        pointer = store.active_pointer()
        candidate = store.load_candidate(selected["candidate_id"])
        approval, metrics, provenance = (
            candidate.approval,
            candidate.canary_metrics,
            candidate.provenance,
        )
        if not (
            pointer is not None
            and pointer.mode == "active"
            and pointer.candidate_id == candidate.candidate_id
            and candidate.state == "active"
            and candidate.campaigns_passed()
            and not candidate.contract_weakenings
            and approval is not None
            and candidate.reviewed_by
            and approval.prior_content_digest
            == candidate.prior_content_digest
            == selected["prior_digest"]
            and approval.proposed_content_digest
            == candidate.proposed_content_digest
            == pointer.active_digest
            and pointer.prior_digest == candidate.prior_content_digest
            and pointer.active_bundle == f"bundles/{candidate.proposed_content_digest}"
            and pointer.workflow_name == candidate.workflow_name
            and not metrics.halted
            and metrics.silent_incorrect == 0
            and metrics.verification_regressions == 0
            and metrics.verified_runs >= candidate.canary.max_runs
            and metrics.runs_completed == metrics.verified_runs
            and provenance is not None
            and provenance.repaired_by == "calling_agent"
            and provenance.halted_run_id == selected["halted_run"]
            and provenance.halted_step_id == selected["step_id"]
            and len(candidate.taught_steps) == 1
        ):
            raise ValueError("repair is not promoted")
        prior_path = _inside(root, f"bundles/{candidate.prior_content_digest}")
        repaired_path = _inside(root, pointer.active_bundle)
        prior, repaired = Workflow.load(prior_path), Workflow.load(repaired_path)
        before = lifecycle.verify_admission(prior_path, key_dir=jit.key_dir)
        admission = lifecycle.verify_admission(repaired_path, key_dir=jit.key_dir)
        repair_of = admission.get("repair_of") or {}
        if not (
            prior.manifest is not None
            and repaired.manifest is not None
            and prior.manifest.content_digest
            == candidate.prior_content_digest
            == before["digest"]
            and repaired.manifest.content_digest
            == candidate.proposed_content_digest
            == admission["digest"]
            and repaired.name == prior.name == candidate.workflow_name
            and admission.get("admission_mode") == "repair"
            and repair_of.get("candidate_id") == candidate.candidate_id
            and repair_of.get("prior_digest") == candidate.prior_content_digest
            and (admission.get("task") or {}).get("key") == jit.task.key
            and admission.get("policy") == jit.policy
            and sorted(repaired.params) == sorted(jit.task.params)
        ):
            raise ValueError("repair admission does not hold")
        enforce_contract_invariants(prior, repaired)
        run = _inside(jit.runs_dir, selected["halted_run"])
        halted = RunReport.model_validate(_document(run / "report.json"))
        found = _repairable(halted, prior)
        if not (
            halted.workflow_name == prior.name
            and halted.bundle_content_digest == candidate.prior_content_digest
            and halted.execution_outcome == "HALTED"
            and halted.transaction_outcome == "HALTED_BEFORE_EFFECT"
            and not steps_requiring_reconciliation(halted)
            and not isinstance(found, str)
            and found[1] == "replace"
            and found[0].id == selected["step_id"]
        ):
            raise ValueError("prior run cannot be repaired")
        _proof(jit, selected, repaired, halted)
        return repaired_path
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, BundleIntegrityError, BundleValidationError, CryptoError) as exc:
        raise RepairSelectionError(
            "The selected repair is unavailable or no longer verified."
        ) from exc


def selected_bundle(jit: Any) -> Optional[Path]:
    """Only the fixed task-local selection can redirect a compiled bundle."""
    path = jit.task_dir / SELECTION_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return validate_selection(jit, _document(path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RepairSelectionError(
            "The selected repair is unavailable or no longer verified."
        ) from exc
