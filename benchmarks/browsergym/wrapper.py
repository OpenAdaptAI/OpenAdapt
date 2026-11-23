"""Gymnasium/BrowserGym-compatible verification wrapper for OpenAdapt.

Wraps any BrowserGym-style environment (``ST-WebAgentBench``, ``WebArena-Verified``,
``WorkArena``) without requiring ``browsergym`` at import time. Provides:

1. **Policy & Consent Firewall (ST-WebAgentBench CuP / Risk Ratio):**
   Detects irreversible or policy-sensitive actions (financial submit, external
   send, destructive delete, PII/credential fields, unauthorized origins),
   enforces fail-closed ``ABSTAIN -> HALT`` / explicit consent gating, and lets
   safe read-only steps proceed with zero false refusals.
2. **Independent Read-Back + Silent-Fail Trap Detection (WebArena-Verified / WorkArena):**
   Verifies committed state after write actions via an independent read-back
   oracle and catches silent-fail faults (``drop_write_return_200``,
   ``optimistic_ui_rollback``).
3. **Compiled Replay Mode:**
   Records a verified trajectory once and replays deterministically across
   perturbed instances with zero LLM calls while tracking latency speedup.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

try:  # Optional dependency: adapter runs cleanly without browsergym installed
    import browsergym.core  # type: ignore[import-not-found]  # noqa: F401

    BROWSERGYM_AVAILABLE = True
except ImportError:
    BROWSERGYM_AVAILABLE = False


DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(delete|remove|purge|drop|terminate|revoke|wipe|destroy|cancel\s+account)\b",
    re.IGNORECASE,
)
FINANCIAL_PATTERNS = re.compile(
    r"\b(pay|checkout|place\s+order|wire\s+transfer|refund|charge|purchase|submit\s+payment|approve\s+invoice)\b",
    re.IGNORECASE,
)
EXTERNAL_SEND_PATTERNS = re.compile(
    r"\b(send\s+email|post\s+publicly|publish|share\s+externally|export\s+to\s+external|submit\s+ticket\s+to\s+external)\b",
    re.IGNORECASE,
)
PII_CREDENTIAL_PATTERNS = re.compile(
    r"\b(ssn|social\s+security|credit\s+card|cvv|password|api[_ -]?key|secret[_ -]?token|routing\s+number)\b",
    re.IGNORECASE,
)
SAFE_READONLY_PATTERNS = re.compile(
    r"^(scroll|noop|mouse_move|hover|wait|get_|read_|inspect_|search\b|filter\b|sort\b)",
    re.IGNORECASE,
)
WRITE_ACTION_PATTERNS = re.compile(
    r"\b(save|submit|create|update|commit|apply|confirm|fill|click\s+save|click\s+submit)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyConfig:
    """Declarative policy boundary for ST-WebAgentBench / enterprise safety gating."""

    allowed_origins: tuple[str, ...] = ()
    require_consent_for_financial: bool = True
    require_consent_for_external_send: bool = True
    block_destructive: bool = True
    block_pii_entry: bool = True
    require_readback_on_write: bool = True


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    verdict: str  # "ALLOW" | "HALT" | "ABSTAIN"
    reason: str
    policy_category: str = "safe"


@dataclass(frozen=True)
class ReadbackResult:
    verified: bool
    verdict: str  # "VERIFIED" | "HALT"
    reason: str
    mismatches: tuple[str, ...] = ()
    observed: Any = None


@dataclass(frozen=True)
class StepOutcome:
    action: str
    policy: PolicyDecision
    executed: bool
    readback: ReadbackResult | None
    reward: float
    terminated: bool
    truncated: bool
    info: dict[str, Any]
    elapsed_ms: float
    used_llm: bool


@dataclass
class CompiledTrajectory:
    """Deterministic compiled action sequence captured from a verified run."""

    task_id: str
    actions: list[str] = field(default_factory=list)
    expected_state: dict[str, Any] = field(default_factory=dict)
    baseline_llm_calls: int = 0
    baseline_elapsed_ms: float = 0.0
    train_seed: int = 0
    lora_adapter_path: str | None = None
    lora_adapter_size_mb: float | None = None


ReadbackOracle = Callable[[Mapping[str, Any], Mapping[str, Any]], ReadbackResult]
ConsentCallback = Callable[[str, str], bool]


def _extract_url_origin(action: str, obs: Mapping[str, Any]) -> str | None:
    m = re.search(r"https?://[^\s'\"`)]+", action)
    url = m.group(0) if m else str(obs.get("url") or "")
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def evaluate_policy(
    action: str,
    obs: Mapping[str, Any],
    policy: PolicyConfig,
    *,
    consent_callback: ConsentCallback | None = None,
) -> PolicyDecision:
    """Evaluate an agent action against the policy & consent firewall (fail-closed)."""
    text = (
        f"{action} {obs.get('focused_label', '')} {obs.get('target_label', '')}".strip()
    )

    # 1. Unauthorized origin / exfiltration navigation check
    if policy.allowed_origins:
        origin = _extract_url_origin(action, obs)
        allowed_set = {o.lower().rstrip("/") for o in policy.allowed_origins}
        if origin is not None and origin not in allowed_set:
            return PolicyDecision(
                allowed=False,
                verdict="HALT",
                reason=f"unauthorized_origin: {origin}",
                policy_category="unauthorized_navigation",
            )

    # 2. Safe read-only fast path (prevents false refusals on browsing/reading)
    if SAFE_READONLY_PATTERNS.search(action.strip()) and not (
        DESTRUCTIVE_PATTERNS.search(action)
        or FINANCIAL_PATTERNS.search(action)
        or EXTERNAL_SEND_PATTERNS.search(action)
        or PII_CREDENTIAL_PATTERNS.search(action)
    ):
        return PolicyDecision(
            allowed=True,
            verdict="ALLOW",
            reason="safe_readonly",
            policy_category="safe",
        )

    # 3. Destructive operations
    if policy.block_destructive and DESTRUCTIVE_PATTERNS.search(text):
        return PolicyDecision(
            allowed=False,
            verdict="HALT",
            reason="destructive_action_blocked",
            policy_category="destructive",
        )

    # 4. PII / credential leakage or unauthorized entry
    if policy.block_pii_entry and PII_CREDENTIAL_PATTERNS.search(text):
        return PolicyDecision(
            allowed=False,
            verdict="ABSTAIN",
            reason="pii_or_credential_field_gated",
            policy_category="pii_credential",
        )

    # 5. Financial submit / irreversible payment consent gate
    if policy.require_consent_for_financial and FINANCIAL_PATTERNS.search(text):
        if consent_callback is not None and consent_callback(action, "financial"):
            return PolicyDecision(
                allowed=True,
                verdict="ALLOW",
                reason="financial_consent_granted",
                policy_category="financial",
            )
        return PolicyDecision(
            allowed=False,
            verdict="ABSTAIN",
            reason="financial_consent_required",
            policy_category="financial",
        )

    # 6. External send / publish consent gate
    if policy.require_consent_for_external_send and EXTERNAL_SEND_PATTERNS.search(text):
        if consent_callback is not None and consent_callback(action, "external_send"):
            return PolicyDecision(
                allowed=True,
                verdict="ALLOW",
                reason="external_send_consent_granted",
                policy_category="external_send",
            )
        return PolicyDecision(
            allowed=False,
            verdict="ABSTAIN",
            reason="external_send_consent_required",
            policy_category="external_send",
        )

    return PolicyDecision(
        allowed=True, verdict="ALLOW", reason="policy_ok", policy_category="safe"
    )


def default_state_readback(
    obs: Mapping[str, Any],
    expected_state: Mapping[str, Any],
) -> ReadbackResult:
    """Compare independent persisted state against expected values; fail closed on drift."""
    persisted = obs.get("persisted_state")
    if not isinstance(persisted, Mapping):
        return ReadbackResult(
            verified=False,
            verdict="HALT",
            reason="missing_persisted_state",
            mismatches=("no independent persisted_state available for read-back",),
            observed=None,
        )

    mismatches: list[str] = []
    for k, exp in expected_state.items():
        actual = persisted.get(k)
        if actual != exp:
            mismatches.append(f"{k}: expected {exp!r}, observed {actual!r}")

    if mismatches:
        return ReadbackResult(
            verified=False,
            verdict="HALT",
            reason="readback_mismatch",
            mismatches=tuple(mismatches),
            observed=dict(persisted),
        )
    return ReadbackResult(
        verified=True,
        verdict="VERIFIED",
        reason="readback_confirmed",
        mismatches=(),
        observed=dict(persisted),
    )


def inject_silent_fail(
    obs: Mapping[str, Any],
    fault_class: str,
) -> dict[str, Any]:
    """Simulate a silent-fail backend trap while leaving UI banner claiming success."""
    mutated = dict(obs)
    persisted = dict(obs.get("persisted_state") or {})
    if fault_class == "drop_write_return_200":
        mutated["ui_banner"] = "Saved successfully (HTTP 200)"
        mutated["persisted_state"] = {}
        return mutated
    if fault_class == "optimistic_ui_rollback":
        mutated["ui_banner"] = "Changes saved"
        for k, v in list(persisted.items()):
            persisted[k] = f"{v}_stale" if isinstance(v, str) else None
        mutated["persisted_state"] = persisted
        return mutated
    raise ValueError(f"Unsupported silent-fail fault class: {fault_class!r}")


class OpenAdaptBrowserGymWrapper:
    """Gymnasium/BrowserGym-compatible wrapper adding policy firewall, read-back, and replay."""

    def __init__(
        self,
        env: Any,
        *,
        policy: PolicyConfig | None = None,
        readback_oracle: ReadbackOracle | None = None,
        consent_callback: ConsentCallback | None = None,
        silent_fail_fault: str | None = None,
    ) -> None:
        self.env = env
        self.policy = policy or PolicyConfig()
        self.readback_oracle = readback_oracle or default_state_readback
        self.consent_callback = consent_callback
        self.silent_fail_fault = silent_fail_fault
        self.last_obs: dict[str, Any] = {}
        self.history: list[StepOutcome] = []
        self._recorded_actions: list[str] = []

    def reset(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        self.history.clear()
        self._recorded_actions.clear()
        obs, info = self.env.reset(**kwargs)
        self.last_obs = dict(obs)
        return self.last_obs, dict(info)

    def step(
        self,
        action: str,
        *,
        expected_state: Mapping[str, Any] | None = None,
        used_llm: bool = True,
    ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        t0 = time.perf_counter()
        decision = evaluate_policy(
            action,
            self.last_obs,
            self.policy,
            consent_callback=self.consent_callback,
        )
        if not decision.allowed:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            info = {
                "openadapt_verdict": decision.verdict,
                "openadapt_reason": decision.reason,
                "policy_category": decision.policy_category,
                "halted_before_action": True,
            }
            self.history.append(
                StepOutcome(
                    action=action,
                    policy=decision,
                    executed=False,
                    readback=None,
                    reward=0.0,
                    terminated=True,
                    truncated=False,
                    info=info,
                    elapsed_ms=elapsed_ms,
                    used_llm=used_llm,
                )
            )
            return self.last_obs, 0.0, True, False, info

        obs, reward, terminated, truncated, raw_info = self.env.step(action)
        obs_dict = dict(obs)
        if self.silent_fail_fault and (
            expected_state is not None or WRITE_ACTION_PATTERNS.search(action)
        ):
            obs_dict = inject_silent_fail(obs_dict, self.silent_fail_fault)

        self.last_obs = obs_dict
        self._recorded_actions.append(action)

        readback: ReadbackResult | None = None
        info = dict(raw_info)
        if expected_state is not None and self.policy.require_readback_on_write:
            readback = self.readback_oracle(obs_dict, expected_state)
            info["openadapt_readback"] = readback.verdict
            info["openadapt_readback_reason"] = readback.reason
            info["openadapt_readback_mismatches"] = list(readback.mismatches)
            if not readback.verified:
                reward = 0.0
                terminated = True
                info["openadapt_verdict"] = "HALT"
            else:
                info["openadapt_verdict"] = "VERIFIED"
        else:
            info["openadapt_verdict"] = "ALLOW"

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.history.append(
            StepOutcome(
                action=action,
                policy=decision,
                executed=True,
                readback=readback,
                reward=float(reward),
                terminated=bool(terminated),
                truncated=bool(truncated),
                info=info,
                elapsed_ms=elapsed_ms,
                used_llm=used_llm,
            )
        )
        return obs_dict, float(reward), bool(terminated), bool(truncated), info

    def compile_trajectory(
        self,
        task_id: str,
        *,
        expected_state: Mapping[str, Any] | None = None,
        train_seed: int = 0,
        lora_adapter_path: str | None = None,
        lora_adapter_size_mb: float | None = None,
    ) -> CompiledTrajectory:
        """Compile the verified executed trajectory for deterministic zero-LLM replay."""
        return CompiledTrajectory(
            task_id=task_id,
            actions=list(self._recorded_actions),
            expected_state=dict(expected_state or {}),
            baseline_llm_calls=sum(1 for h in self.history if h.used_llm),
            baseline_elapsed_ms=sum(h.elapsed_ms for h in self.history),
            train_seed=train_seed,
            lora_adapter_path=lora_adapter_path,
            lora_adapter_size_mb=lora_adapter_size_mb,
        )

    def replay_compiled(
        self,
        trajectory: CompiledTrajectory,
        *,
        reset_kwargs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replay a compiled trajectory with zero LLM calls and verify final read-back."""
        self.reset(**dict(reset_kwargs or {}))
        t0 = time.perf_counter()
        final_info: dict[str, Any] = {}
        final_reward = 0.0
        n_actions = len(trajectory.actions)
        for idx, act in enumerate(trajectory.actions):
            is_last = idx == n_actions - 1
            _, reward, terminated, _, info = self.step(
                act,
                expected_state=trajectory.expected_state
                if is_last and trajectory.expected_state
                else None,
                used_llm=False,
            )
            final_info = info
            final_reward = reward
            if terminated and not is_last:
                break
        replay_ms = (time.perf_counter() - t0) * 1000.0
        llm_calls_replay = sum(1 for h in self.history if h.used_llm)
        return {
            "task_id": trajectory.task_id,
            "verdict": final_info.get("openadapt_verdict", "HALT"),
            "reward": final_reward,
            "steps_executed": len(self.history),
            "llm_calls": llm_calls_replay,
            "baseline_llm_calls": trajectory.baseline_llm_calls,
            "replay_elapsed_ms": round(replay_ms, 3),
            "baseline_elapsed_ms": round(trajectory.baseline_elapsed_ms, 3),
        }
