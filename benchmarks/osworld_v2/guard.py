"""OSWorld 2.0 + OS-Harm Verification & Safety Adapter (`OpenAdaptOSWorldGuard`).

Provides two orthogonal, domain-agnostic primitives for desktop agent loops:
1. OS-Harm Safety Firewall (fail-closed ABSTAIN -> HALT across all 3 vectors):
   - Indirect prompt injection defense (untrusted screen/email/web provenance)
   - Data exfiltration guard (sensitive local files & unauthorized egress)
   - Destructive action gate (irreversible OS/file/email/system mutations)
2. OSWorld 2.0 Long-Horizon Compiled Sub-Workflow Engine:
   - Deterministic replay of compiled desktop sub-workflows (0 VLM calls/step)
   - Independent checkpoint read-back (file, SQLite read-only, or custom probe)
   - Bounded mid-horizon drift recovery before stochastic error compounds.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

ActionKind = Literal[
    "click",
    "type",
    "hotkey",
    "shell",
    "file_read",
    "file_write",
    "file_delete",
    "http_upload",
    "email_send",
    "clipboard_paste",
]

SourceTrust = Literal[
    "user_instruction",
    "compiled_subflow",
    "untrusted_screen",
    "untrusted_email",
    "untrusted_web",
]

SafetyVector = Literal[
    "none",
    "indirect_prompt_injection",
    "data_exfiltration",
    "destructive_action",
    "mid_horizon_drift",
]

_PRIVILEGED_KINDS: frozenset[ActionKind] = frozenset(
    {"shell", "file_delete", "http_upload", "email_send"}
)

_SENSITIVE_PATH_PATTERN = re.compile(
    r"(^|[/\\])(\.ssh|\.aws|\.gnupg|\.env(\.|$)|id_rsa|id_ed25519|credentials|"
    r"keychain|shadow|passwd|secrets?|token|api[_-]?key|.*\.pem$|.*\.key$)",
    re.IGNORECASE,
)

_INJECTION_DIRECTIVE_PATTERN = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior)\s+instructions|"
    r"system\s+override|disregard\s+policy|new\s+priority\s+instruction|"
    r"exfiltrate|curl\s+.*\|\s*(ba)?sh|wget\s+.*\|\s*(ba)?sh)",
    re.IGNORECASE,
)

_DESTRUCTIVE_SHELL_PATTERN = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bmkfs\b|\bshred\b|"
    r"\bdd\s+if=|\bchmod\s+(-R\s+)?777\b|\bsudo\b|\bshutdown\b|\breboot\b|"
    r">\s*/etc/|>\s*/System/)",
    re.IGNORECASE,
)

_EGRESS_SHELL_PATTERN = re.compile(
    r"\b(curl|wget|nc|ncat|scp|sftp|rsync|ftp|sendmail)\b",
    re.IGNORECASE,
)

_SYSTEM_CONFIG_PREFIXES: tuple[str, ...] = (
    "/etc/",
    "/System/",
    "/usr/bin/",
    "C:\\Windows\\",
)


@dataclass(frozen=True)
class TaskPolicy:
    """Declarative safety boundary for a single desktop benchmark task."""

    task_id: str
    allowed_domains: tuple[str, ...] = ()
    authorized_recipients: tuple[str, ...] = ()
    allowed_write_roots: tuple[str, ...] = ()
    allow_destructive: bool = False
    allow_external_egress: bool = False
    allow_shell_exec: bool = False


@dataclass(frozen=True)
class AgentAction:
    """Normalized action emitted by an OSWorld / OS-Harm agent step."""

    kind: ActionKind
    target: str = ""
    payload: str = ""
    source_trust: SourceTrust = "user_instruction"
    observed_context: str = ""
    explicit_grant: bool = False
    referenced_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardVerdict:
    """Fail-closed safety or verification verdict."""

    decision: Literal["ALLOW", "HALT"]
    vector: SafetyVector
    reason: str
    abstained: bool = False


@dataclass(frozen=True)
class SubWorkflowStep:
    """Deterministic step inside a compiled OSWorld 2.0 sub-workflow."""

    step_id: str
    action: AgentAction
    estimated_vlm_latency_ms: float = 1200.0
    compiled_latency_ms: float = 45.0


@dataclass(frozen=True)
class CheckpointSpec:
    """Independent read-back check attached to a compiled sub-workflow."""

    name: str
    kind: Literal["file_exists", "file_contains", "sqlite_count_gte", "custom"]
    path: str = ""
    expected_text: str = ""
    sql_query: str = ""
    min_count: int = 1
    custom_probe: Callable[[], bool] | None = None


@dataclass(frozen=True)
class CompiledSubWorkflow:
    """Reusable compiled desktop sub-workflow with an independent read-back checkpoint."""

    workflow_id: str
    steps: tuple[SubWorkflowStep, ...]
    checkpoint: CheckpointSpec


@dataclass(frozen=True)
class SubWorkflowResult:
    """Execution outcome of a compiled sub-workflow run."""

    workflow_id: str
    completed: bool
    drift_detected: bool
    drift_recovered: bool
    steps_executed: int
    vlm_calls_used: int
    vlm_calls_saved: int
    estimated_vlm_latency_ms: float
    actual_latency_ms: float
    halted_reason: str | None = None
    verdicts: tuple[GuardVerdict, ...] = field(default_factory=tuple)


class OpenAdaptOSWorldGuard:
    """OSWorld 2.0 + OS-Harm safety firewall and compiled sub-workflow engine."""

    def __init__(self, policy: TaskPolicy) -> None:
        self.policy = policy
        self._compiled_registry: dict[str, CompiledSubWorkflow] = {}

    def register_subworkflow(self, subworkflow: CompiledSubWorkflow) -> None:
        """Cache a compiled sub-workflow for zero-VLM deterministic replay."""
        self._compiled_registry[subworkflow.workflow_id] = subworkflow

    def get_subworkflow(self, workflow_id: str) -> CompiledSubWorkflow | None:
        return self._compiled_registry.get(workflow_id)

    def _is_sensitive_path(self, raw_path: str) -> bool:
        return bool(raw_path and _SENSITIVE_PATH_PATTERN.search(raw_path))

    def _extract_host(self, url_or_target: str) -> str:
        parsed = urlparse(url_or_target)
        return (parsed.hostname or "").strip().lower()

    def _is_allowed_domain(self, target: str) -> bool:
        host = self._extract_host(target)
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return host in {d.lower() for d in self.policy.allowed_domains}

    def check_action(self, action: AgentAction) -> GuardVerdict:
        """Evaluate one proposed agent action against all 3 OS-Harm safety vectors."""
        untrusted_source = action.source_trust not in {
            "user_instruction",
            "compiled_subflow",
        }
        injection_in_context = bool(
            action.observed_context
            and _INJECTION_DIRECTIVE_PATTERN.search(action.observed_context)
        )
        all_paths = (action.target, *action.referenced_paths)
        touches_sensitive = any(self._is_sensitive_path(p) for p in all_paths) or bool(
            action.payload and _SENSITIVE_PATH_PATTERN.search(action.payload)
        )

        # Vector 1: Indirect prompt injection defense (ABSTAIN -> HALT)
        if untrusted_source and (action.kind in _PRIVILEGED_KINDS or touches_sensitive):
            return GuardVerdict(
                decision="HALT",
                vector="indirect_prompt_injection",
                reason=(
                    f"Blocked {action.kind} triggered from untrusted source "
                    f"{action.source_trust!r} (ABSTAIN -> HALT)"
                ),
                abstained=True,
            )
        if injection_in_context and (
            action.kind in _PRIVILEGED_KINDS or touches_sensitive
        ):
            return GuardVerdict(
                decision="HALT",
                vector="indirect_prompt_injection",
                reason=(
                    f"Blocked privileged {action.kind} following indirect prompt injection "
                    "directive in observed screen/document context (ABSTAIN -> HALT)"
                ),
                abstained=True,
            )

        # Vector 2: Data exfiltration guard
        if action.kind == "http_upload":
            if touches_sensitive and not self.policy.allow_external_egress:
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason="Blocked outbound upload containing sensitive local file/credential",
                )
            if not self.policy.allow_external_egress and not self._is_allowed_domain(
                action.target
            ):
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason=f"Blocked unauthorized outbound upload to {action.target!r}",
                )

        if action.kind == "clipboard_paste":
            if touches_sensitive and not self._is_allowed_domain(action.target):
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason="Blocked pasting sensitive local data to external destination",
                )

        if action.kind == "email_send":
            recipient = action.target.strip().lower()
            allowed_recips = {r.lower() for r in self.policy.authorized_recipients}
            if touches_sensitive and recipient not in allowed_recips:
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason=f"Blocked emailing sensitive content to unauthorized recipient {recipient!r}",
                )

        if action.kind == "shell":
            cmd = f"{action.target} {action.payload}".strip()
            if _EGRESS_SHELL_PATTERN.search(cmd) and (
                touches_sensitive or not self.policy.allow_external_egress
            ):
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason="Blocked shell network egress command without external egress grant",
                )

        # Vector 3: Destructive / irreversible action gate
        if action.kind == "file_delete":
            if not (action.explicit_grant and self.policy.allow_destructive):
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked irreversible file_delete on {action.target!r} without explicit grant",
                )

        if action.kind == "file_write":
            if any(action.target.startswith(pfx) for pfx in _SYSTEM_CONFIG_PREFIXES):
                if not (action.explicit_grant and self.policy.allow_destructive):
                    return GuardVerdict(
                        decision="HALT",
                        vector="destructive_action",
                        reason=f"Blocked system config overwrite on {action.target!r}",
                    )

        if action.kind == "email_send":
            recipient = action.target.strip().lower()
            allowed_recips = {r.lower() for r in self.policy.authorized_recipients}
            if recipient not in allowed_recips and not action.explicit_grant:
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked irreversible external email to {recipient!r} without explicit grant",
                )

        if action.kind == "shell":
            cmd = f"{action.target} {action.payload}".strip()
            if _DESTRUCTIVE_SHELL_PATTERN.search(cmd):
                if not (action.explicit_grant and self.policy.allow_destructive):
                    return GuardVerdict(
                        decision="HALT",
                        vector="destructive_action",
                        reason="Blocked destructive shell command without explicit grant",
                    )
            elif not self.policy.allow_shell_exec and not action.explicit_grant:
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason="Blocked ungranted shell execution under task policy",
                )

        return GuardVerdict(
            decision="ALLOW", vector="none", reason="Action permitted by policy"
        )

    def verify_checkpoint(self, checkpoint: CheckpointSpec) -> bool:
        """Run an independent read-back check (file, read-only SQLite, or custom probe)."""
        try:
            if checkpoint.kind == "custom":
                return bool(
                    checkpoint.custom_probe is not None and checkpoint.custom_probe()
                )
            if checkpoint.kind == "file_exists":
                return Path(checkpoint.path).is_file()
            if checkpoint.kind == "file_contains":
                path = Path(checkpoint.path)
                if not path.is_file():
                    return False
                return checkpoint.expected_text in path.read_text(encoding="utf-8")
            if checkpoint.kind == "sqlite_count_gte":
                uri = f"{Path(checkpoint.path).resolve().as_uri()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                try:
                    conn.execute("PRAGMA query_only = ON")
                    row = conn.execute(checkpoint.sql_query).fetchone()
                    return bool(row is not None and int(row[0]) >= checkpoint.min_count)
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001
            return False
        return False

    def execute_subworkflow(
        self,
        workflow_id: str,
        *,
        executor: Callable[[SubWorkflowStep], Any],
        recover: Callable[[CompiledSubWorkflow], bool] | None = None,
        max_recovery_attempts: int = 1,
    ) -> SubWorkflowResult:
        """Replay a compiled sub-workflow with safety gating, checkpointed read-back,
        and bounded mid-horizon drift recovery."""
        subflow = self._compiled_registry.get(workflow_id)
        if subflow is None:
            raise KeyError(f"Unknown compiled sub-workflow: {workflow_id!r}")

        verdicts: list[GuardVerdict] = []
        steps_executed = 0
        est_vlm_ms = sum(s.estimated_vlm_latency_ms for s in subflow.steps)
        actual_ms = 0.0

        for step in subflow.steps:
            verdict = self.check_action(step.action)
            verdicts.append(verdict)
            if verdict.decision != "ALLOW":
                return SubWorkflowResult(
                    workflow_id=workflow_id,
                    completed=False,
                    drift_detected=False,
                    drift_recovered=False,
                    steps_executed=steps_executed,
                    vlm_calls_used=0,
                    vlm_calls_saved=steps_executed,
                    estimated_vlm_latency_ms=est_vlm_ms,
                    actual_latency_ms=actual_ms,
                    halted_reason=verdict.reason,
                    verdicts=tuple(verdicts),
                )
            executor(step)
            steps_executed += 1
            actual_ms += step.compiled_latency_ms

        # Independent post-subworkflow checkpoint read-back
        if self.verify_checkpoint(subflow.checkpoint):
            return SubWorkflowResult(
                workflow_id=workflow_id,
                completed=True,
                drift_detected=False,
                drift_recovered=False,
                steps_executed=steps_executed,
                vlm_calls_used=0,
                vlm_calls_saved=steps_executed,
                estimated_vlm_latency_ms=est_vlm_ms,
                actual_latency_ms=actual_ms,
                verdicts=tuple(verdicts),
            )

        # Mid-horizon drift detected -> bounded recovery + re-verification
        vlm_calls_used = 0
        for _ in range(max_recovery_attempts):
            if recover is None:
                break
            vlm_calls_used += 1
            actual_ms += 900.0
            if recover(subflow) and self.verify_checkpoint(subflow.checkpoint):
                return SubWorkflowResult(
                    workflow_id=workflow_id,
                    completed=True,
                    drift_detected=True,
                    drift_recovered=True,
                    steps_executed=steps_executed,
                    vlm_calls_used=vlm_calls_used,
                    vlm_calls_saved=max(0, steps_executed - vlm_calls_used),
                    estimated_vlm_latency_ms=est_vlm_ms,
                    actual_latency_ms=actual_ms,
                    verdicts=tuple(verdicts),
                )

        drift_verdict = GuardVerdict(
            decision="HALT",
            vector="mid_horizon_drift",
            reason=f"Checkpoint {subflow.checkpoint.name!r} failed read-back after recovery",
            abstained=True,
        )
        verdicts.append(drift_verdict)
        return SubWorkflowResult(
            workflow_id=workflow_id,
            completed=False,
            drift_detected=True,
            drift_recovered=False,
            steps_executed=steps_executed,
            vlm_calls_used=vlm_calls_used,
            vlm_calls_saved=max(0, steps_executed - vlm_calls_used),
            estimated_vlm_latency_ms=est_vlm_ms,
            actual_latency_ms=actual_ms,
            halted_reason=drift_verdict.reason,
            verdicts=tuple(verdicts),
        )

    def wrap_osworld_env_step(
        self,
        env: Any,
        action: AgentAction,
        raw_env_action: Any,
    ) -> tuple[GuardVerdict, Any]:
        """Bridge helper for live OSWorld / OS-Harm VM environments.

        Inspects `action` before calling `env.step(raw_env_action)`. If halted,
        the VM environment is NEVER mutated and `(verdict, None)` is returned.
        """
        verdict = self.check_action(action)
        if verdict.decision != "ALLOW":
            return verdict, None
        return verdict, env.step(raw_env_action)
