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

import ast
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

_TERMINAL_TARGET_PATTERN = re.compile(
    r"\b(terminal|bash|zsh|sh|cmd|powershell|console|iterm|konsole|xterm|shell)\b",
    re.IGNORECASE,
)

_SECRET_CONTENT_PATTERN = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

_SAFE_PYAUTOGUI_FUNCS: dict[str, str] = {
    "click": "click",
    "doubleClick": "click",
    "rightClick": "click",
    "moveTo": "click",
    "write": "type",
    "typewrite": "type",
    "hotkey": "hotkey",
    "press": "press",
}

_MODIFIER_KEYS: frozenset[str] = frozenset(
    {
        "ctrl",
        "control",
        "alt",
        "shift",
        "win",
        "super",
        "command",
        "cmd",
        "option",
        "meta",
    }
)

_TERMINAL_LAUNCHER_HOTKEYS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"ctrl", "alt", "t"}),
        frozenset({"control", "alt", "t"}),
        frozenset({"super", "t"}),
        frozenset({"win", "r"}),
        frozenset({"super", "r"}),
        frozenset({"alt", "f2"}),
        frozenset({"cmd", "space"}),
        frozenset({"command", "space"}),
        frozenset({"ctrl", "shift", "p"}),
    }
)

_ALLOWED_DICT_KEYS: frozenset[str] = frozenset({"kind", "target", "payload"})


def _parse_raw_env_action(raw_env_action: Any) -> tuple[str, str, str] | None:
    """Strictly parse a raw OSWorld env action into (kind, target, payload).

    Returns None (fail closed) for any arbitrary Python code, multi-statement
    string, non-literal argument, unexpected dict keys, or unsupported structure.
    """
    if isinstance(raw_env_action, dict):
        if not set(raw_env_action.keys()).issubset(_ALLOWED_DICT_KEYS):
            return None
        kind = raw_env_action.get("kind")
        if not isinstance(kind, str) or kind not in ALLOWED_ACTION_KINDS:
            return None
        target = raw_env_action.get("target", "")
        payload = raw_env_action.get("payload", "")
        if not isinstance(target, str) or not isinstance(payload, str):
            return None
        return (kind, target, payload)

    if not isinstance(raw_env_action, str):
        return None
    stripped = raw_env_action.strip()
    if not stripped:
        return None
    if stripped in {"WAIT", "DONE", "FAIL"}:
        return None

    try:
        tree = ast.parse(stripped, mode="exec")
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return None
    call = tree.body[0].value
    if not isinstance(call, ast.Call):
        return None
    func = call.func
    if (
        not isinstance(func, ast.Attribute)
        or not isinstance(func.value, ast.Name)
        or func.value.id != "pyautogui"
        or func.attr not in _SAFE_PYAUTOGUI_FUNCS
    ):
        return None

    try:
        pos_args = [ast.literal_eval(arg) for arg in call.args]
        kw_args = {
            kw.arg: ast.literal_eval(kw.value)
            for kw in call.keywords
            if kw.arg is not None
        }
    except (ValueError, SyntaxError):
        return None
    if len(kw_args) != len(call.keywords):
        return None

    kind = _SAFE_PYAUTOGUI_FUNCS[func.attr]
    if kind == "click":
        x = kw_args.get("x", pos_args[0] if len(pos_args) >= 1 else None)
        y = kw_args.get("y", pos_args[1] if len(pos_args) >= 2 else None)
        target = f"{x},{y}" if x is not None and y is not None else ""
        return ("click", target, "")
    if kind == "type":
        text = kw_args.get("message", pos_args[0] if pos_args else "")
        if not isinstance(text, str):
            return None
        return ("type", "", text)
    if kind == "press":
        raw_keys = kw_args.get("keys", pos_args[0] if pos_args else None)
        if isinstance(raw_keys, (list, tuple)):
            if not raw_keys or not all(isinstance(k, str) for k in raw_keys):
                return None
            typed = "".join(
                "\n" if k.lower() in {"enter", "return"} else k for k in raw_keys
            )
            return ("type", "", typed)
        if not isinstance(raw_keys, str) or not raw_keys:
            return None
        if raw_keys.lower() in {"enter", "return"}:
            return ("type", "", "\n")
        if len(raw_keys) == 1:
            return ("type", "", raw_keys)
        return ("hotkey", raw_keys, "")
    if kind == "hotkey":
        if not pos_args or len(pos_args) > 3:
            return None
        if not all(isinstance(a, str) and a for a in pos_args):
            return None
        lowered = [a.lower() for a in pos_args]
        # Require at least one modifier key and reject enter/return/whitespace
        # so hotkey() cannot be abused to type shell commands.
        if not any(k in _MODIFIER_KEYS for k in lowered):
            return None
        if any(
            k in {"enter", "return", "space"} or any(c.isspace() for c in k)
            for k in lowered
        ):
            if frozenset(lowered) not in _TERMINAL_LAUNCHER_HOTKEYS:
                return None
        return ("hotkey", "+".join(pos_args), "")
    return None


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
        provenance_provider: Callable[[AgentAction], SourceTrust] | None = None,
    ) -> None:
        self.policy = policy
        self._consent_callback = consent_callback
        self._provenance_provider = provenance_provider
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
        return host in {d.lower() for d in self.policy.allowed_domains}

    def _operator_consents(self, action: AgentAction, reason: str) -> bool:
        if self._consent_callback is None:
            return False
        return bool(self._consent_callback(action, reason))

    def _resolve_provenance(
        self, action: AgentAction, provenance: SourceTrust | None
    ) -> SourceTrust:
        """Resolve effective provenance from operator/adapter side when supplied."""
        if provenance is not None:
            return provenance
        if self._provenance_provider is not None:
            return self._provenance_provider(action)
        return action.source_trust

    def check_action(
        self,
        action: AgentAction,
        *,
        provenance: SourceTrust | None = None,
    ) -> GuardVerdict:
        """Evaluate one proposed agent action fail-closed.

        In live wrappers, pass `provenance` from the adapter/environment or
        configure `provenance_provider` so the agent cannot self-attest trust.
        """
        # 0. Strict runtime allowlist on ActionKind (deny by default)
        if action.kind not in ALLOWED_ACTION_KINDS:
            return GuardVerdict(
                decision="HALT",
                vector="unknown_action",
                reason=f"Blocked unknown action kind {action.kind!r} (deny by default)",
                abstained=True,
            )

        effective_trust = self._resolve_provenance(action, provenance)
        untrusted_source = effective_trust not in _TRUSTED_SOURCES
        injection_in_context = bool(
            action.observed_context
            and _INJECTION_DIRECTIVE_PATTERN.search(action.observed_context)
        )
        all_paths = (action.target, *action.referenced_paths)
        touches_sensitive = (
            any(self._is_sensitive_path(p) for p in all_paths)
            or bool(action.payload and _SENSITIVE_PATH_PATTERN.search(action.payload))
            or bool(action.payload and _SECRET_CONTENT_PATTERN.search(action.payload))
        )

        # 1. Indirect prompt injection / untrusted provenance defense (ABSTAIN -> HALT)
        if untrusted_source and (action.kind in _PRIVILEGED_KINDS or touches_sensitive):
            return GuardVerdict(
                decision="HALT",
                vector="indirect_prompt_injection",
                reason=(
                    f"Blocked {action.kind} with untrusted/missing provenance "
                    f"{effective_trust!r} (ABSTAIN -> HALT)"
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

        if action.kind in {"type", "hotkey"}:
            combined_ui = f"{action.target} {action.payload}".strip()
            has_shell_danger = bool(
                _DESTRUCTIVE_SHELL_PATTERN.search(combined_ui)
                or _EGRESS_SHELL_PATTERN.search(combined_ui)
            )
            targets_terminal = bool(_TERMINAL_TARGET_PATTERN.search(action.target))
            # P1: untrusted/injection-tainted type containing newline/Enter fails closed
            # regardless of caller-supplied target or shell obfuscation.
            submits_line = action.kind == "type" and (
                "\n" in action.payload or "\r" in action.payload
            )
            hotkey_parts = frozenset(
                p.strip().lower() for p in action.target.split("+") if p.strip()
            )
            opens_terminal = action.kind == "hotkey" and (
                hotkey_parts in _TERMINAL_LAUNCHER_HOTKEYS
                or "enter" in hotkey_parts
                or "return" in hotkey_parts
            )
            if (untrusted_source or injection_in_context) and (
                has_shell_danger
                or targets_terminal
                or touches_sensitive
                or submits_line
                or opens_terminal
            ):
                return GuardVerdict(
                    decision="HALT",
                    vector="indirect_prompt_injection",
                    reason=(
                        f"Blocked untrusted/injection-tainted {action.kind} "
                        f"(terminal/newline/launcher/shell risk on {action.target!r})"
                    ),
                    abstained=True,
                )
            if has_shell_danger and (
                not self.policy.allow_shell_exec or not self.policy.allow_destructive
            ):
                return GuardVerdict(
                    decision="HALT",
                    vector="destructive_action",
                    reason=f"Blocked destructive/egress {action.kind} payload without shell policy grant",
                )
            return GuardVerdict(
                decision="ALLOW", vector="none", reason="Benign UI action"
            )

        if action.kind == "click":
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
        *,
        provenance: SourceTrust | None = None,
    ) -> tuple[GuardVerdict, Any]:
        """Bridge helper for live OSWorld / OS-Harm VM environments.

        Binds `raw_env_action` to the checked `action` (fail-closed) and uses
        adapter-assigned `provenance` (default `"untrusted"`), ignoring any
        caller-supplied `action.source_trust`. If `raw_env_action` cannot be
        strictly parsed into a safe primitive or disagrees with `action`, the
        VM environment is NEVER mutated and `(verdict, None)` is returned.
        """
        parsed = _parse_raw_env_action(raw_env_action)
        if parsed is None:
            return (
                GuardVerdict(
                    decision="HALT",
                    vector="unknown_action",
                    reason=(
                        "Unbound subject: raw_env_action could not be parsed as a "
                        "safe primitive (fail closed)"
                    ),
                    abstained=True,
                ),
                None,
            )

        raw_kind, raw_target, raw_payload = parsed
        if raw_kind != action.kind:
            return (
                GuardVerdict(
                    decision="HALT",
                    vector="unknown_action",
                    reason=(
                        f"Unbound subject: raw_env_action kind {raw_kind!r} does not "
                        f"match checked action kind {action.kind!r}"
                    ),
                    abstained=True,
                ),
                None,
            )
        if raw_kind == "type" and raw_payload != action.payload:
            return (
                GuardVerdict(
                    decision="HALT",
                    vector="unknown_action",
                    reason="Unbound subject: raw_env_action payload differs from checked action",
                    abstained=True,
                ),
                None,
            )
        if (
            raw_kind in {"click", "hotkey"}
            and raw_target
            and action.target
            and raw_target != action.target
        ):
            return (
                GuardVerdict(
                    decision="HALT",
                    vector="unknown_action",
                    reason="Unbound subject: raw_env_action target differs from checked action",
                    abstained=True,
                ),
                None,
            )
        if isinstance(raw_env_action, dict) and (
            raw_target != action.target or raw_payload != action.payload
        ):
            return (
                GuardVerdict(
                    decision="HALT",
                    vector="unknown_action",
                    reason="Unbound subject: raw dict action differs from checked action",
                    abstained=True,
                ),
                None,
            )

        effective_prov: SourceTrust = (
            provenance
            if provenance is not None
            else (
                self._provenance_provider(action)
                if self._provenance_provider is not None
                else "untrusted"
            )
        )
        bound_action = AgentAction(
            kind=raw_kind,
            target=action.target or raw_target,
            payload=raw_payload if raw_payload else action.payload,
            source_trust=effective_prov,
            observed_context=action.observed_context,
            referenced_paths=action.referenced_paths,
        )
        verdict = self.check_action(bound_action, provenance=effective_prov)
        if verdict.decision != "ALLOW":
            return verdict, None
        safe_dispatch = (
            {"kind": raw_kind, "target": raw_target, "payload": raw_payload}
            if isinstance(raw_env_action, dict)
            else raw_env_action
        )
        return verdict, env.step(safe_dispatch)
