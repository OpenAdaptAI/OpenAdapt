"""OSWorld 2.0 + OS-Harm Verification & Safety Adapter (`OpenAdaptOSWorldGuard`).

Standalone adapter kept separate from `openadapt_flow`'s governed runtime so it
can wrap external OSWorld / OS-Harm VM step loops without requiring heavy VM or
engine dependencies during offline contract tests.

Fail-closed invariants:
1. Deny by default on unknown `AgentAction.kind` (strict runtime allowlist).
2. Missing provenance (`source_trust`) defaults to `"untrusted"` (never trusted).
3. Actions cannot vouch for themselves: grants come ONLY from `TaskPolicy` or
   an explicit operator `consent_callback`, never from a field on `AgentAction`.
4. `TaskPolicy.allowed_write_roots` is enforced via `os.path.realpath` on every
   `file_write` and `file_delete` (blocking path traversal and out-of-root writes).
5. Checkpointed sub-workflow replay requires a real independent read-back probe
   (`file_exists`, `file_contains`, read-only SQLite, or custom probe); missing
   probes fail closed (`False`).
"""

from __future__ import annotations

import os
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

ALLOWED_ACTION_KINDS: frozenset[str] = frozenset(
    {
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
    }
)

SourceTrust = Literal[
    "untrusted",
    "user_instruction",
    "compiled_subflow",
    "untrusted_screen",
    "untrusted_email",
    "untrusted_web",
]

_TRUSTED_SOURCES: frozenset[str] = frozenset({"user_instruction", "compiled_subflow"})

SafetyVector = Literal[
    "none",
    "unknown_action",
    "indirect_prompt_injection",
    "data_exfiltration",
    "destructive_action",
    "mid_horizon_drift",
]

_PRIVILEGED_KINDS: frozenset[str] = frozenset(
    {"shell", "file_write", "file_delete", "http_upload", "email_send"}
)

_SENSITIVE_PATH_PATTERN = re.compile(
    r"(^|[/\\])(\.ssh|\.aws|\.gnupg|\.env(\.|$)|\.bashrc|\.zshrc|\.profile|"
    r"id_rsa|id_ed25519|credentials|keychain|shadow|passwd|secrets?|token|"
    r"api[_-]?key|.*\.pem$|.*\.key$)",
    re.IGNORECASE,
)

_INJECTION_DIRECTIVE_PATTERN = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior)\s+instructions|"
    r"system\s+override|disregard\s+policy|new\s+priority\s+instruction|"
    r"exfiltrate|curl\s+.*\|\s*(ba)?sh|wget\s+.*\|\s*(ba)?sh)",
    re.IGNORECASE,
)

_DESTRUCTIVE_SHELL_PATTERN = re.compile(
    r"(\brm\s+-[a-z]*[rf]|\bmkfs\b|\bshred\b|\bdd\s+if=|"
    r"\bchmod\s+(-R\s+)?777\b|\bsudo\b|\bshutdown\b|\breboot\b|"
    r"\bfind\b.*\s-delete\b|\bshutil\.rmtree\b|\bos\.(remove|unlink)\b|"
    r">\s*/etc/|>\s*/System/)",
    re.IGNORECASE,
)

_EGRESS_SHELL_PATTERN = re.compile(
    r"\b(curl|wget|nc|ncat|scp|sftp|rsync|ftp|sendmail)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskPolicy:
    """Declarative safety boundary for a single desktop task (operator-controlled)."""

    task_id: str
    allowed_domains: tuple[str, ...] = ()
    authorized_recipients: tuple[str, ...] = ()
    allowed_write_roots: tuple[str, ...] = ()
    allow_destructive: bool = False
    allow_external_egress: bool = False
    allow_shell_exec: bool = False


@dataclass(frozen=True)
class AgentAction:
    """Normalized action emitted by an agent step.

    Note: `source_trust` defaults to `"untrusted"` (fail-closed) so unlabeled
    actions from live environments never bypass provenance checks. Actions carry
    no self-grant fields; permissions come strictly from `TaskPolicy` or an
    operator consent callback.
    """

    kind: str
    target: str = ""
    payload: str = ""
    source_trust: SourceTrust = "untrusted"
    observed_context: str = ""
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
    """Deterministic step inside a compiled sub-workflow."""

    step_id: str
    action: AgentAction


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
    recovery_calls_used: int
    halted_reason: str | None = None
    verdicts: tuple[GuardVerdict, ...] = field(default_factory=tuple)


def _realpath_norm(raw_path: str) -> str:
    return os.path.realpath(os.path.expanduser(raw_path))


class OpenAdaptOSWorldGuard:
    """Fail-closed safety firewall and checkpointed sub-workflow engine."""

    def __init__(
        self,
        policy: TaskPolicy,
        *,
        consent_callback: Callable[[AgentAction, str], bool] | None = None,
    ) -> None:
        self.policy = policy
        self._consent_callback = consent_callback
        self._compiled_registry: dict[str, CompiledSubWorkflow] = {}

    def register_subworkflow(self, subworkflow: CompiledSubWorkflow) -> None:
        """Cache a compiled sub-workflow for deterministic replay."""
        self._compiled_registry[subworkflow.workflow_id] = subworkflow

    def get_subworkflow(self, workflow_id: str) -> CompiledSubWorkflow | None:
        return self._compiled_registry.get(workflow_id)

    def _is_sensitive_path(self, raw_path: str) -> bool:
        return bool(raw_path and _SENSITIVE_PATH_PATTERN.search(raw_path))

    def _is_within_allowed_write_roots(self, target_path: str) -> bool:
        if not target_path.strip() or not self.policy.allowed_write_roots:
            return False
        resolved_target = _realpath_norm(target_path)
        for root in self.policy.allowed_write_roots:
            resolved_root = _realpath_norm(root)
            try:
                if (
                    os.path.commonpath([resolved_target, resolved_root])
                    == resolved_root
                ):
                    return True
            except ValueError:
                continue
        return False

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

    def _operator_consents(self, action: AgentAction, reason: str) -> bool:
        if self._consent_callback is None:
            return False
        return bool(self._consent_callback(action, reason))

    def check_action(self, action: AgentAction) -> GuardVerdict:
        """Evaluate one proposed agent action fail-closed."""
        # 0. Strict runtime allowlist on ActionKind (deny by default)
        if action.kind not in ALLOWED_ACTION_KINDS:
            return GuardVerdict(
                decision="HALT",
                vector="unknown_action",
                reason=f"Blocked unknown action kind {action.kind!r} (deny by default)",
                abstained=True,
            )

        untrusted_source = action.source_trust not in _TRUSTED_SOURCES
        injection_in_context = bool(
            action.observed_context
            and _INJECTION_DIRECTIVE_PATTERN.search(action.observed_context)
        )
        all_paths = (action.target, *action.referenced_paths)
        touches_sensitive = any(self._is_sensitive_path(p) for p in all_paths) or bool(
            action.payload and _SENSITIVE_PATH_PATTERN.search(action.payload)
        )

        # 1. Indirect prompt injection / untrusted provenance defense (ABSTAIN -> HALT)
        if untrusted_source and (action.kind in _PRIVILEGED_KINDS or touches_sensitive):
            return GuardVerdict(
                decision="HALT",
                vector="indirect_prompt_injection",
                reason=(
                    f"Blocked {action.kind} with untrusted/missing provenance "
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
                    "directive in observed context (ABSTAIN -> HALT)"
                ),
                abstained=True,
            )

        # 2. Data exfiltration guard
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
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Authorized upload target"
            )

        if action.kind == "clipboard_paste":
            if touches_sensitive and not self._is_allowed_domain(action.target):
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason="Blocked pasting sensitive local data to external destination",
                )
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Benign clipboard paste"
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
            if recipient not in allowed_recips:
                if not self._operator_consents(
                    action, f"external email to {recipient}"
                ):
                    return GuardVerdict(
                        decision="HALT",
                        vector="destructive_action",
                        reason=f"Blocked external email to unauthorized recipient {recipient!r}",
                    )
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Authorized email recipient"
            )

        # 3. File write / delete boundary enforcement (strictly inside allowed_write_roots)
        if action.kind == "file_write":
            if touches_sensitive:
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked write to sensitive file path {action.target!r}",
                )
            if not self._is_within_allowed_write_roots(action.target):
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked file_write outside allowed_write_roots: {action.target!r}",
                )
            return GuardVerdict(
                decision="ALLOW",
                vector="none",
                reason="file_write within allowed_write_roots",
            )

        if action.kind == "file_delete":
            if not self._is_within_allowed_write_roots(action.target):
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked file_delete outside allowed_write_roots: {action.target!r}",
                )
            if not self.policy.allow_destructive and not self._operator_consents(
                action, f"file_delete {action.target}"
            ):
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked file_delete on {action.target!r} without policy/operator grant",
                )
            return GuardVerdict(
                decision="ALLOW",
                vector="none",
                reason="Authorized file_delete within root",
            )

        # 4. Shell execution gate
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
            if _DESTRUCTIVE_SHELL_PATTERN.search(cmd):
                if not (
                    self.policy.allow_destructive
                    and self.policy.allow_shell_exec
                    and self._operator_consents(action, f"destructive shell: {cmd}")
                ):
                    return GuardVerdict(
                        decision="HALT",
                        vector="destructive_action",
                        reason="Blocked destructive shell command",
                    )
            if not self.policy.allow_shell_exec:
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason="Blocked shell execution (policy.allow_shell_exec is False)",
                )
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Policy-permitted shell"
            )

        # 5. Explicitly allowlisted benign non-destructive UI & read actions
        if action.kind == "file_read":
            if touches_sensitive and untrusted_source:
                return GuardVerdict(
                    decision="HALT",
                    vector="data_exfiltration",
                    reason=f"Blocked sensitive file_read {action.target!r} from untrusted source",
                )
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Benign file_read"
            )

        if action.kind in {"click", "type", "hotkey"}:
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Benign UI action"
            )

        # Unreachable due to allowlist check above; fail closed defensively.
        return GuardVerdict(
            decision="HALT",
            vector="unknown_action",
            reason=f"Unhandled action kind {action.kind!r} (fail closed)",
            abstained=True,
        )

    def verify_checkpoint(self, checkpoint: CheckpointSpec) -> bool:
        """Run an independent read-back check (file, read-only SQLite, or custom probe).

        Fails closed (returns False) if custom_probe is None or any error occurs.
        """
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
                    recovery_calls_used=0,
                    halted_reason=verdict.reason,
                    verdicts=tuple(verdicts),
                )
            executor(step)
            steps_executed += 1

        # Independent post-subworkflow checkpoint read-back
        if self.verify_checkpoint(subflow.checkpoint):
            return SubWorkflowResult(
                workflow_id=workflow_id,
                completed=True,
                drift_detected=False,
                drift_recovered=False,
                steps_executed=steps_executed,
                recovery_calls_used=0,
                verdicts=tuple(verdicts),
            )

        # Mid-horizon drift detected -> bounded recovery + re-verification
        recovery_calls_used = 0
        for _ in range(max_recovery_attempts):
            if recover is None:
                break
            recovery_calls_used += 1
            if recover(subflow) and self.verify_checkpoint(subflow.checkpoint):
                return SubWorkflowResult(
                    workflow_id=workflow_id,
                    completed=True,
                    drift_detected=True,
                    drift_recovered=True,
                    steps_executed=steps_executed,
                    recovery_calls_used=recovery_calls_used,
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
            recovery_calls_used=recovery_calls_used,
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
