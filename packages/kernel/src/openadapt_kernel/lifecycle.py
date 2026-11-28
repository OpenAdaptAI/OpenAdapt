"""Customer egress ledger audit helper and end-to-end self-healing lifecycle orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from openadapt_kernel.copilot import PortalPack
from openadapt_kernel.dgm_pr import (
    FailureCluster,
    FailureRecord,
    KernelPRProposal,
    cluster_pack_failures,
    run_dgm_pr_coding_agent,
)
from openadapt_kernel.edge_lora import (
    EdgeLoRARuntime,
    LoRATrainingRecord,
    export_lora_training_records,
)
from openadapt_kernel.egress_guard import EgressPolicy, LocalEgressGuard
from openadapt_kernel.extension_bridge import induce_pack_from_extension_recordings
from openadapt_kernel.prepare import PackMutantReport, qualify_and_promote_pack
from openadapt_kernel.stateful_twin import (
    StatefulTwinApp,
    self_heal_multipage_in_twin,
    synthesize_twin_with_coding_agent,
)


def verify_egress_ledger_file(ledger_path: Union[str, Path]) -> Dict[str, Any]:
    """Verify SHA-256 hash-chain integrity of an outbound_egress_ledger.jsonl and return a compliance report."""
    path = Path(ledger_path)
    guard = LocalEgressGuard(path)
    entries = guard.read_ledger()
    integrity_valid = guard.verify_ledger_integrity() if path.exists() else True
    allowed_entries = sum(1 for e in entries if e.get("allowed") is True)
    blocked_entries = sum(1 for e in entries if e.get("allowed") is False)
    phi_bytes_leaked = sum(
        int(e.get("byte_length", 0)) for e in entries if e.get("allowed") is True and e.get("rejected_tokens")
    )
    chain_head = str(entries[-1].get("entry_sha256", "")) if entries else ("0" * 64)
    return {
        "ledger_path": str(path),
        "integrity_valid": integrity_valid,
        "total_entries": len(entries),
        "allowed_entries": allowed_entries,
        "blocked_entries": blocked_entries,
        "phi_bytes_leaked": phi_bytes_leaked,
        "chain_head_sha256": chain_head,
    }


@dataclass
class LifecycleConfig:
    pack_id: str = "lifecycle_pack"
    min_cluster_size: int = 2
    max_heal_turns: int = 3
    repo_root: Optional[Path] = None
    open_draft_pr: bool = False
    run_dgm_checks: bool = False
    egress_ledger_path: Optional[Path] = None
    lora_output_jsonl_path: Optional[Path] = None
    egress_policy: Optional[EgressPolicy] = None


@dataclass
class LifecycleReport:
    stages_executed: List[str] = field(default_factory=list)
    induced_pack: Optional[PortalPack] = None
    twin_app: Optional[StatefulTwinApp] = None
    promotion_report: Optional[PackMutantReport] = None
    run_result: Optional[Any] = None
    edge_lora_fallback_used: bool = False
    edge_lora_output: str = ""
    heal_result: Optional[Dict[str, Any]] = None
    exported_lora_records: List[LoRATrainingRecord] = field(default_factory=list)
    failure_clusters: List[FailureCluster] = field(default_factory=list)
    pr_proposals: List[KernelPRProposal] = field(default_factory=list)
    egress_audit: Dict[str, Any] = field(default_factory=dict)


def run_self_healing_lifecycle(
    config: Optional[LifecycleConfig] = None,
    *,
    extension_sessions: Sequence[tuple[Path, Dict[str, str], Optional[str]]] = (),
    pack: Optional[PortalPack] = None,
    sanitized_trace_summary: Optional[Union[Dict[str, Any], str]] = None,
    coding_agent_fn: Optional[Callable[[str], str]] = None,
    twin_app: Optional[StatefulTwinApp] = None,
    promotion_kwargs: Optional[Mapping[str, Any]] = None,
    run_pack_fn: Optional[Callable[[PortalPack], Any]] = None,
    edge_lora_runtime: Optional[EdgeLoRARuntime] = None,
    edge_lora_prompt: str = "",
    failing_steps: Sequence[str] = (),
    repair_fn: Optional[Callable[..., Any]] = None,
    readback_check: Optional[Callable[..., bool]] = None,
    traces_for_lora: Sequence[Any] = (),
    failure_records: Sequence[FailureRecord] = (),
    dgm_check_commands: Optional[Sequence[Sequence[str]]] = None,
) -> LifecycleReport:
    """Orchestrate the full zero-PHI self-healing lifecycle from recording to LoRA export and DGM-PR."""
    cfg = config or LifecycleConfig()
    report = LifecycleReport()

    active_pack = pack
    if extension_sessions:
        active_pack = induce_pack_from_extension_recordings(cfg.pack_id, extension_sessions)
        report.stages_executed.append("induce_pack")
    report.induced_pack = active_pack

    active_twin = twin_app
    if active_twin is None and sanitized_trace_summary is not None and coding_agent_fn is not None:
        active_twin = synthesize_twin_with_coding_agent(
            sanitized_trace_summary,
            coding_agent_fn=coding_agent_fn,
            egress_policy=cfg.egress_policy,
            ledger_path=cfg.egress_ledger_path,
        )
        report.stages_executed.append("synthesize_twin")
    report.twin_app = active_twin

    if promotion_kwargs:
        report.promotion_report = qualify_and_promote_pack(**dict(promotion_kwargs))
        if report.promotion_report.pack is not None:
            active_pack = report.promotion_report.pack
            report.induced_pack = active_pack
        report.stages_executed.append("qualify_and_promote_pack")

    if run_pack_fn is not None and active_pack is not None:
        report.run_result = run_pack_fn(active_pack)
        report.stages_executed.append("run_portal_pack")
    elif coding_agent_fn is None and edge_lora_runtime is not None and edge_lora_prompt:
        report.edge_lora_output = edge_lora_runtime.infer("pointer_stage", edge_lora_prompt)
        report.edge_lora_fallback_used = True
        report.stages_executed.append("edge_lora_airgapped_fallback")

    if active_twin is not None and failing_steps and repair_fn is not None and readback_check is not None:
        report.heal_result = self_heal_multipage_in_twin(
            active_twin,
            failing_steps,
            repair_fn,
            readback_check=readback_check,
            max_turns=cfg.max_heal_turns,
        )
        report.stages_executed.append("self_heal_in_twin")

    lora_inputs: List[Any] = list(traces_for_lora)
    if active_pack is not None:
        lora_inputs.append(active_pack)
    if lora_inputs:
        report.exported_lora_records = export_lora_training_records(
            lora_inputs,
            output_jsonl_path=cfg.lora_output_jsonl_path,
            egress_policy=cfg.egress_policy,
            ledger_path=cfg.egress_ledger_path,
        )
        report.stages_executed.append("export_lora_dataset")

    if failure_records:
        report.failure_clusters = cluster_pack_failures(
            failure_records, min_cluster_size=cfg.min_cluster_size, policy=cfg.egress_policy
        )
        report.stages_executed.append("cluster_pack_failures")
        if coding_agent_fn is not None and cfg.repo_root is not None:
            for cluster in report.failure_clusters:
                if cluster.occurrence_count >= cfg.min_cluster_size:
                    proposal = run_dgm_pr_coding_agent(
                        cluster,
                        repo_root=cfg.repo_root,
                        coding_agent_fn=coding_agent_fn,
                        open_draft_pr=cfg.open_draft_pr,
                        run_checks=cfg.run_dgm_checks,
                        egress_policy=cfg.egress_policy,
                        ledger_path=cfg.egress_ledger_path,
                        check_commands=dgm_check_commands,
                    )
                    report.pr_proposals.append(proposal)
            if report.pr_proposals:
                report.stages_executed.append("run_dgm_pr_coding_agent")

    if cfg.egress_ledger_path is not None and Path(cfg.egress_ledger_path).exists():
        report.egress_audit = verify_egress_ledger_file(cfg.egress_ledger_path)
        report.stages_executed.append("verify_egress_ledger")

    return report
