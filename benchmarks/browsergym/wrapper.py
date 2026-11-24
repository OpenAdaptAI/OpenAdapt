"""Gymnasium/BrowserGym-compatible verification wrapper for OpenAdapt.

Standalone adapter kept separate from `openadapt_flow`'s governed runtime so it
can wrap external BrowserGym environments (ST-WebAgentBench, WebArena-Verified,
WorkArena) without requiring `browsergym` or heavy engine dependencies during
offline contract tests.

Fail-closed invariants:
1. BrowserGym bid actions (`click('a42')`, `fill('b7', '...')`, `press('a42', 'Enter')`)
   are resolved against `axtree_object`, `extra_element_properties`, or `axtree_txt`
   before policy matching. Unresolved bids fail closed (`ABSTAIN`).
2. Empty `PolicyConfig.allowed_origins` denies navigation/origins (fail closed)
   rather than skipping the origin check.
3. `default_state_readback` fails closed (`HALT`) on empty `expected_state={}` or
   missing `persisted_state`.
4. When `PolicyConfig.require_readback_on_write=True`, any write action (matched on
   action text or resolved bid label) without a non-empty `expected_state` fails
   closed (`HALT`, reward `0.0`).
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
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
    r"(\b(ssn|social\s+security|credit\s+card|cvv|password|api[_ -]?key|secret[_ -]?token|routing\s+number)\b"
    r"|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]*?){13,19}\b)",
    re.IGNORECASE,
)
SAFE_READONLY_PATTERNS = re.compile(
    r"^(scroll|noop|mouse_move|hover|wait|get_|read_|inspect_|search\b|filter\b|sort\b)",
    re.IGNORECASE,
)
WRITE_ACTION_PATTERNS = re.compile(
    r"\b(save|submit|create|update|commit|apply|confirm|click\s+save|click\s+submit)\b",
    re.IGNORECASE,
)

_SAFE_READONLY_CALLS: frozenset[str] = frozenset({"noop", "scroll", "wait", "search"})
_SINGLE_BID_CALLS: frozenset[str] = frozenset(
    {
        "click",
        "dblclick",
        "fill",
        "press",
        "select_option",
        "clear",
        "focus",
        "check",
        "uncheck",
        "hover",
    }
)
_TWO_BID_CALLS: frozenset[str] = frozenset({"drag_and_drop"})
_AXTREE_TXT_LINE_RE = re.compile(
    r"\[([^\]]+)\]\s+([A-Za-z0-9_-]+)(?:\s+['\"]([^'\"]*)['\"])?"
)


@dataclass(frozen=True)
class PolicyConfig:
    """Declarative policy boundary for BrowserGym safety gating."""

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


def _parse_browsergym_action(
    action: str,
) -> tuple[str, tuple[str, ...], str | None] | None:
    """Strictly parse a BrowserGym action string with `ast`.

    Requires exactly one top-level call to a known verb with literal arguments.
    Returns `(verb, bids, goto_url)` or `None` (fail closed -> ABSTAIN) for
    multi-statement strings, unknown/unbound verbs (e.g. `mouse_click`,
    `keyboard_press`), non-literal args, or missing bid arguments.
    """
    if not isinstance(action, str) or not action.strip():
        return None
    try:
        tree = ast.parse(action.strip(), mode="exec")
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return None
    call = tree.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None

    verb = call.func.id.lower()
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

    if verb in _SAFE_READONLY_CALLS:
        return (verb, (), None)
    if verb == "goto":
        url = kw_args.get("url", pos_args[0] if pos_args else None)
        if not isinstance(url, str) or not url.strip():
            return None
        return (verb, (), url.strip())
    if verb in _SINGLE_BID_CALLS:
        bid = kw_args.get("bid", pos_args[0] if pos_args else None)
        if not isinstance(bid, str) or not bid.strip():
            return None
        return (verb, (bid.strip(),), None)
    if verb in _TWO_BID_CALLS:
        from_bid = kw_args.get("from_bid", pos_args[0] if len(pos_args) >= 1 else None)
        to_bid = kw_args.get("to_bid", pos_args[1] if len(pos_args) >= 2 else None)
        if (
            not isinstance(from_bid, str)
            or not from_bid.strip()
            or not isinstance(to_bid, str)
            or not to_bid.strip()
        ):
            return None
        return (verb, (from_bid.strip(), to_bid.strip()), None)
    return None


def _ax_val(raw: Any) -> str:
    if isinstance(raw, Mapping):
        return str(raw.get("value", "") or "")
    return str(raw or "")


def _norm_label(label: str) -> str:
    return " ".join(label.lower().split())


def resolve_bid_label(bid: str, obs: Mapping[str, Any]) -> str | None:
    """Resolve a BrowserGym element bid to '<role> <name>' from AXTree or properties.

    Fail-closed rules:
    - Matches ONLY on `browsergym_id` or `bid` (never CDP `nodeId`).
    - Treats empty role+name labels as unresolved (`None`).
    - Returns `None` on duplicate bids within a source or when sources disagree.
    """
    source_labels: list[str] = []

    # 1. axtree_object (standard BrowserGym CDP AXTree dict)
    ax_obj = obs.get("axtree_object")
    if isinstance(ax_obj, Mapping):
        nodes = ax_obj.get("nodes")
        if isinstance(nodes, list):
            obj_matches: list[str] = []
            for node in nodes:
                if not isinstance(node, Mapping):
                    continue
                node_bid = str(node.get("browsergym_id") or node.get("bid") or "")
                if node_bid == bid:
                    role = _ax_val(node.get("role")).strip()
                    name = _ax_val(node.get("name")).strip()
                    label = f"{role} {name}".strip()
                    obj_matches.append(label)
            if len(obj_matches) > 1:
                return None
            if len(obj_matches) == 1:
                if not obj_matches[0]:
                    return None
                source_labels.append(obj_matches[0])

    # 2. extra_element_properties (BrowserGym dict keyed by bid)
    extra = obs.get("extra_element_properties")
    if isinstance(extra, Mapping) and bid in extra:
        props = extra.get(bid)
        if isinstance(props, Mapping):
            role = str(props.get("role") or props.get("tag") or "").strip()
            name = str(
                props.get("name")
                or props.get("label")
                or props.get("text")
                or props.get("aria-label")
                or ""
            ).strip()
            label = f"{role} {name}".strip()
            if not label:
                return None
            source_labels.append(label)

    # 3. axtree_txt (BrowserGym flattened AXTree string, e.g. "[a42] button 'Delete account'")
    ax_txt = obs.get("axtree_txt")
    if isinstance(ax_txt, str):
        txt_matches: list[str] = []
        for match in _AXTREE_TXT_LINE_RE.finditer(ax_txt):
            if match.group(1).strip() == bid:
                role = match.group(2).strip()
                name = (match.group(3) or "").strip()
                txt_matches.append(f"{role} {name}".strip())
        if len(txt_matches) > 1:
            return None
        if len(txt_matches) == 1:
            if not txt_matches[0]:
                return None
            source_labels.append(txt_matches[0])

    if not source_labels:
        return None
    # Fail closed if multiple AXTree/property sources disagree on the bid's label
    if len({_norm_label(lbl) for lbl in source_labels}) > 1:
        return None
    return source_labels[0]


def _is_write_action(action: str, obs: Mapping[str, Any]) -> bool:
    if WRITE_ACTION_PATTERNS.search(action):
        return True
    parsed = _parse_browsergym_action(action)
    if parsed is not None:
        _verb, bids, _url = parsed
        for bid in bids:
            resolved = resolve_bid_label(bid, obs)
            if resolved and WRITE_ACTION_PATTERNS.search(resolved):
                return True
    return False


def evaluate_policy(
    action: str,
    obs: Mapping[str, Any],
    policy: PolicyConfig,
    *,
    consent_callback: ConsentCallback | None = None,
) -> PolicyDecision:
    """Evaluate an agent action against the policy & consent firewall (fail-closed)."""
    parsed = _parse_browsergym_action(action)
    if parsed is None:
        return PolicyDecision(
            allowed=False,
            verdict="ABSTAIN",
            reason=f"unbound_or_unsupported_action: {action!r}",
            policy_category="unresolved_target",
        )
    verb, bids, goto_url = parsed

    origin = _extract_url_origin(goto_url or action, obs)
    is_goto = verb == "goto"

    # 1. Unauthorized origin / exfiltration navigation check (empty allowlist denies)
    if origin is not None or is_goto:
        if not policy.allowed_origins:
            return PolicyDecision(
                allowed=False,
                verdict="HALT",
                reason=f"empty_allowed_origins: denied origin {origin!r}",
                policy_category="unauthorized_navigation",
            )
        allowed_set = {o.lower().rstrip("/") for o in policy.allowed_origins}
        if origin is None or origin not in allowed_set:
            return PolicyDecision(
                allowed=False,
                verdict="HALT",
                reason=f"unauthorized_origin: {origin}",
                policy_category="unauthorized_navigation",
            )

    # 2. Safe read-only fast path (ONLY for AST-verified single safe read-only calls)
    if verb in _SAFE_READONLY_CALLS and not (
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

    # 3. Resolve EVERY BrowserGym bid to AXTree role/name; fail closed (no focused_label fallback)
    resolved_labels: list[str] = []
    for bid in bids:
        looked_up = resolve_bid_label(bid, obs)
        if looked_up is None:
            return PolicyDecision(
                allowed=False,
                verdict="ABSTAIN",
                reason=f"unresolved_bid: {bid!r} missing, duplicate, empty, or conflicting in AXTree",
                policy_category="unresolved_target",
            )
        resolved_labels.append(looked_up)

    resolved_label = " ".join(resolved_labels)
    text = f"{action} {resolved_label}".strip()

    # 4. Destructive operations
    if policy.block_destructive and DESTRUCTIVE_PATTERNS.search(text):
        return PolicyDecision(
            allowed=False,
            verdict="HALT",
            reason="destructive_action_blocked",
            policy_category="destructive",
        )

    # 5. PII / credential leakage or unauthorized entry
    if policy.block_pii_entry and PII_CREDENTIAL_PATTERNS.search(text):
        return PolicyDecision(
            allowed=False,
            verdict="ABSTAIN",
            reason="pii_or_credential_field_gated",
            policy_category="pii_credential",
        )

    # 6. Financial submit / irreversible payment consent gate
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

    # 7. External send / publish consent gate
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
    """Compare persisted state against non-empty expected values; fail closed on empty or drift."""
    if not expected_state:
        return ReadbackResult(
            verified=False,
            verdict="HALT",
            reason="empty_expected_state",
            mismatches=("expected_state must be non-empty for read-back verification",),
            observed=obs.get("persisted_state"),
        )

    persisted = obs.get("persisted_state")
    if not isinstance(persisted, Mapping):
        return ReadbackResult(
            verified=False,
            verdict="HALT",
            reason="missing_persisted_state",
            mismatches=("no persisted_state available for read-back",),
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
        prior_obs = self.last_obs
        decision = evaluate_policy(
            action,
            prior_obs,
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

        is_write = _is_write_action(action, prior_obs)
        obs, reward, terminated, truncated, raw_info = self.env.step(action)
        obs_dict = dict(obs)
        if self.silent_fail_fault and (expected_state is not None or is_write):
            obs_dict = inject_silent_fail(obs_dict, self.silent_fail_fault)

        self.last_obs = obs_dict
        self._recorded_actions.append(action)

        readback: ReadbackResult | None = None
        info = dict(raw_info)
        if self.policy.require_readback_on_write and (
            expected_state is not None or is_write
        ):
            if not expected_state:
                readback = ReadbackResult(
                    verified=False,
                    verdict="HALT",
                    reason=(
                        "empty_expected_state"
                        if expected_state is not None
                        else "missing_expected_state_on_write"
                    ),
                    mismatches=(
                        "require_readback_on_write=True requires non-empty expected_state",
                    ),
                    observed=obs_dict.get("persisted_state"),
                )
            else:
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
                expected_state=trajectory.expected_state if is_last else None,
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
