"""Cost model + hard guardrails for evaluating openadapt-flow on WAA.

This module is **stdlib-only** (``math``, ``dataclasses``) and imports nothing
from ``openadapt_flow`` or any heavy dependency, so it is safe to import in
light CI and to drive the ``--dry-run`` cost estimate without a VM, an API
key, or the ``flow`` extra installed.

Two responsibilities:

1. **Cost estimation** (:func:`estimate_flow_waa_cost`) -- dollars for a run of
   ``N`` WAA tasks in either eval mode:

   - ``replay`` (demonstrate-then-replay): the compiled bundle replays with
     ~0 model calls, so the *only* cost is Azure VM-hours. Token cost ~= $0.
   - ``hybrid`` (compiled-first, agent-fallback-on-halt): the compiled arm is
     free; only the fraction of tasks that *halt* pay for a computer-use agent
     to finish, and each fallback starts mid-workflow so it runs fewer steps
     than a from-scratch agent. Azure VM-hours apply to all tasks.

   A pure-agent baseline cost (every task, every step, paid) is computed too so
   the compiled/hybrid-vs-agent ratio is visible.

2. **Hard guardrails** (:class:`SpendLedger`) -- mirrors openadapt-flow's
   benchmark ``SpendLedger``: a per-run $ cap, a hard total $ ceiling, a
   per-task token cap, and abort-on-repeated-billing-error. There was a prior
   $40-70 uncapped-run incident; these caps are mandatory for any paid run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Vision-model pricing (list price, February 2026). Sources:
#   OpenAI:    https://platform.openai.com/docs/pricing
#   Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
# ---------------------------------------------------------------------------


def openai_image_tokens(width: int, height: int, detail: str = "high") -> int:
    """OpenAI GPT-4o image tokens: 85 + 170*tiles over 512px tiles at <=768 short side."""
    if detail == "low":
        return 85
    max_dim = 2048
    if width > max_dim or height > max_dim:
        scale = max_dim / max(width, height)
        width = int(width * scale)
        height = int(height * scale)
    short_side = min(width, height)
    if short_side > 768:
        scale = 768 / short_side
        width = int(width * scale)
        height = int(height * scale)
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return 85 + (170 * tiles)


def claude_image_tokens(width: int, height: int) -> int:
    """Claude image tokens: (W*H)/750, resized so the long edge is <=1568px."""
    long_edge = max(width, height)
    if long_edge > 1568:
        scale = 1568 / long_edge
        width = int(width * scale)
        height = int(height * scale)
    return int((width * height) / 750)


@dataclass(frozen=True)
class ModelPricing:
    """List-price pricing for one vision model."""

    name: str
    input_per_million: float
    output_per_million: float
    image_token_calculator: Callable[[int, int], int]
    notes: str = ""


MODELS: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing("GPT-4o", 2.50, 10.00, openai_image_tokens),
    "gpt-4o-mini": ModelPricing("GPT-4o-mini", 0.15, 0.60, openai_image_tokens),
    "claude-sonnet-4": ModelPricing(
        "Claude Sonnet 4", 3.00, 15.00, claude_image_tokens, "Balanced cost/capability."
    ),
    "claude-opus-4.5": ModelPricing(
        "Claude Opus 4.5", 15.00, 75.00, claude_image_tokens, "Most capable, highest cost."
    ),
    "claude-haiku-4.5": ModelPricing(
        "Claude Haiku 4.5", 1.00, 5.00, claude_image_tokens, "Cheapest Claude model."
    ),
}

DEFAULT_MODEL = "claude-sonnet-4"

# ---------------------------------------------------------------------------
# WAA run parameters (defaults; all overridable).
# ---------------------------------------------------------------------------

#: Azure VM $/hour. The existing ``estimate_cost`` default (D4_v3, East US).
#: AWS m8i.2xlarge (nested-virt) is ~$0.46/hr -- pass that via ``vm_hourly``.
DEFAULT_VM_HOURLY = 0.19

#: WAA screenshot resolution.
DEFAULT_SCREEN = (1920, 1080)

#: Average steps a from-scratch agent takes per WAA task (WAA paper: 15-30).
DEFAULT_AGENT_STEPS_PER_TASK = 22

#: A hybrid fallback starts mid-workflow (after the compiled prefix ran), so it
#: needs fewer steps than a from-scratch agent.
DEFAULT_FALLBACK_STEPS = 10

#: Text tokens (task instruction + a11y context) and output tokens per step.
DEFAULT_TEXT_TOKENS_PER_STEP = 500
DEFAULT_OUTPUT_TOKENS_PER_STEP = 300

#: Per-task minutes on the VM. Replay is deterministic + model-free (fast);
#: an agent fallback is slower.
DEFAULT_REPLAY_MINUTES_PER_TASK = 1.0
DEFAULT_AGENT_MINUTES_PER_TASK = 3.0

#: Provisioning/cleanup overhead added once to the whole run (~15 min).
DEFAULT_OVERHEAD_HOURS = 0.25


def tokens_per_step(
    model: ModelPricing,
    screen: tuple[int, int] = DEFAULT_SCREEN,
    text_tokens: int = DEFAULT_TEXT_TOKENS_PER_STEP,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS_PER_STEP,
) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) for one agent step (image + text)."""
    image_tokens = model.image_token_calculator(*screen)
    return image_tokens + text_tokens, output_tokens


def cost_per_step_usd(model: ModelPricing, **kw) -> float:
    """List-price $ for one agent step (one screenshot + reasoning + action)."""
    in_tokens, out_tokens = tokens_per_step(model, **kw)
    return (in_tokens / 1_000_000) * model.input_per_million + (
        out_tokens / 1_000_000
    ) * model.output_per_million


# ---------------------------------------------------------------------------
# Full-run estimate.
# ---------------------------------------------------------------------------


@dataclass
class FlowRunCostEstimate:
    """Estimated cost breakdown for a flow-on-WAA run of ``num_tasks`` tasks."""

    mode: str
    num_tasks: int
    model_name: str
    vm_hourly: float
    vm_hours: float
    vm_cost_usd: float
    fallback_rate: float
    paid_tasks: float
    agent_steps_per_paid_task: int
    token_cost_usd: float
    total_cost_usd: float
    cost_per_task_usd: float
    baseline_agent_cost_usd: float
    baseline_agent_steps_per_task: int

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "num_tasks": self.num_tasks,
            "model": self.model_name,
            "vm_hourly_usd": round(self.vm_hourly, 4),
            "vm_hours": round(self.vm_hours, 3),
            "vm_cost_usd": round(self.vm_cost_usd, 4),
            "fallback_rate": round(self.fallback_rate, 3),
            "paid_tasks": round(self.paid_tasks, 2),
            "agent_steps_per_paid_task": self.agent_steps_per_paid_task,
            "token_cost_usd": round(self.token_cost_usd, 4),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "cost_per_task_usd": round(self.cost_per_task_usd, 5),
            "baseline_pure_agent_cost_usd": round(self.baseline_agent_cost_usd, 4),
            "baseline_agent_steps_per_task": self.baseline_agent_steps_per_task,
            "savings_vs_baseline_usd": round(
                self.baseline_agent_cost_usd - self.total_cost_usd, 4
            ),
        }


def estimate_flow_waa_cost(
    num_tasks: int,
    mode: str = "replay",
    model_name: str = DEFAULT_MODEL,
    *,
    fallback_rate: float = 0.30,
    vm_hourly: float = DEFAULT_VM_HOURLY,
    screen: tuple[int, int] = DEFAULT_SCREEN,
    agent_steps_per_task: int = DEFAULT_AGENT_STEPS_PER_TASK,
    fallback_steps: int = DEFAULT_FALLBACK_STEPS,
    replay_minutes_per_task: float = DEFAULT_REPLAY_MINUTES_PER_TASK,
    agent_minutes_per_task: float = DEFAULT_AGENT_MINUTES_PER_TASK,
    overhead_hours: float = DEFAULT_OVERHEAD_HOURS,
    num_workers: int = 1,
) -> FlowRunCostEstimate:
    """Estimate the dollar cost of a flow-on-WAA run.

    Args:
        num_tasks: Number of WAA tasks in the run.
        mode: ``"replay"`` (demonstrate-then-replay, ~0 model calls) or
            ``"hybrid"`` (compiled-first, agent-fallback-on-halt).
        model_name: Key into :data:`MODELS` for the (fallback) agent arm.
        fallback_rate: Fraction of tasks expected to halt and invoke the paid
            fallback agent (hybrid only; ignored for replay).
        vm_hourly: Azure/AWS VM $/hour.
        num_workers: Parallel VMs (total VM-hours unchanged; kept for reporting).

    Returns:
        A :class:`FlowRunCostEstimate`.
    """
    if model_name not in MODELS:
        raise ValueError(f"unknown model {model_name!r}; choose from {sorted(MODELS)}")
    model = MODELS[model_name]
    step_cost = cost_per_step_usd(model, screen=screen)

    if mode == "replay":
        paid_tasks = 0.0
        agent_steps = 0
        token_cost = 0.0
        minutes_per_task = replay_minutes_per_task
        eff_fallback_rate = 0.0
    elif mode == "hybrid":
        eff_fallback_rate = max(0.0, min(1.0, fallback_rate))
        paid_tasks = num_tasks * eff_fallback_rate
        agent_steps = fallback_steps
        token_cost = paid_tasks * agent_steps * step_cost
        minutes_per_task = (
            (1 - eff_fallback_rate) * replay_minutes_per_task
            + eff_fallback_rate * agent_minutes_per_task
        )
    else:
        raise ValueError(f"unknown mode {mode!r}; expected 'replay' or 'hybrid'")

    wall_minutes = (num_tasks * minutes_per_task) / max(1, num_workers)
    vm_hours = (wall_minutes / 60 + overhead_hours) * max(1, num_workers)
    vm_cost = vm_hours * vm_hourly
    total = vm_cost + token_cost
    baseline_cost = num_tasks * agent_steps_per_task * step_cost

    return FlowRunCostEstimate(
        mode=mode,
        num_tasks=num_tasks,
        model_name=model.name,
        vm_hourly=vm_hourly,
        vm_hours=vm_hours,
        vm_cost_usd=vm_cost,
        fallback_rate=eff_fallback_rate,
        paid_tasks=paid_tasks,
        agent_steps_per_paid_task=agent_steps,
        token_cost_usd=token_cost,
        total_cost_usd=total,
        cost_per_task_usd=(total / num_tasks if num_tasks else 0.0),
        baseline_agent_cost_usd=baseline_cost,
        baseline_agent_steps_per_task=agent_steps_per_task,
    )


# ---------------------------------------------------------------------------
# Hard guardrails (mirrors openadapt_flow.benchmark.hybrid_benchmark.SpendLedger).
# ---------------------------------------------------------------------------

#: Consecutive auth/billing-looking failures after which paid spending aborts.
BILLING_ABORT_AFTER = 2

_BILLING_ERROR_MARKERS = (
    "billing",
    "quota",
    "insufficient_quota",
    "insufficient funds",
    "payment",
    "credit",
    "rate_limit",
    "rate limit",
    "401",
    "403",
    "429",
    "unauthorized",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "permission",
)


def looks_like_billing_error(error: str) -> bool:
    """True when an error string looks like an auth/billing/quota failure."""
    low = error.lower()
    return any(marker in low for marker in _BILLING_ERROR_MARKERS)


@dataclass
class CostGuardConfig:
    """Hard cost guardrails for a paid run. All caps are list-price dollars."""

    per_run_usd: float = 0.50
    total_usd: float = 5.00
    per_task_tokens: int = 60_000
    billing_abort_after: int = BILLING_ABORT_AFTER


class SpendLedger:
    """One budget across all paid runs, with hard caps and billing-abort.

    Enforces the total ceiling BEFORE each paid run (a run may not start unless
    its full per-run cap still fits under the ceiling), caps per-task tokens,
    and aborts all further paid spending after ``billing_abort_after``
    consecutive auth/billing-looking failures.
    """

    def __init__(self, config: Optional[CostGuardConfig] = None) -> None:
        self.config = config or CostGuardConfig()
        self.spent = 0.0
        self.tokens_used = 0
        self.aborted: Optional[str] = None
        self._consecutive_billing_errors = 0

    def can_start(self) -> bool:
        """True when a new paid run may start under the ceiling."""
        return (
            self.aborted is None
            and self.spent + self.config.per_run_usd <= self.config.total_usd
        )

    def blocked_reason(self) -> str:
        """Why a paid run may not start right now."""
        if self.aborted is not None:
            return self.aborted
        return (
            f"total budget ceiling: ${self.spent:.2f} of ${self.config.total_usd:.2f} "
            f"spent; the next run's ${self.config.per_run_usd:.2f} cap could exceed it"
        )

    def token_cap_exceeded(self, tokens: int) -> bool:
        """True when a single task's token count exceeds the per-task cap."""
        return tokens > self.config.per_task_tokens

    def record(
        self, cost_usd: float, *, tokens: int = 0, error: Optional[str] = None
    ) -> None:
        """Record a finished paid run's cost, token usage, and error status."""
        self.spent += cost_usd
        self.tokens_used += tokens
        if error and looks_like_billing_error(error):
            self._consecutive_billing_errors += 1
            if self._consecutive_billing_errors >= self.config.billing_abort_after:
                self.aborted = (
                    f"paid spending aborted after {self._consecutive_billing_errors} "
                    f"consecutive auth/billing errors -- last error: {error}"
                )
        else:
            self._consecutive_billing_errors = 0

    def summary(self) -> dict:
        return {
            "spent_usd": round(self.spent, 4),
            "tokens_used": self.tokens_used,
            "per_run_cap_usd": self.config.per_run_usd,
            "total_cap_usd": self.config.total_usd,
            "per_task_token_cap": self.config.per_task_tokens,
            "aborted": self.aborted,
        }
