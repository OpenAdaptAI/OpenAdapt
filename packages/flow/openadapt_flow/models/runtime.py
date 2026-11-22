"""Hardware-tiered local model policy, router, and pre/post-submit visual veto.

Routes verification and grounding work by measured hardware capability instead
of running 4B+ VLMs interactively on CPU-only hosts:

* **Tier 1 (``DETERMINISTIC_ONLY``):** Zero-model fast path (~10-100 ms).
  Default whenever deterministic API, DB, SQL, or DOM evidence covers the check.
* **Tier 2 (``LOCAL_ACCELERATED`` / ``LAN_APPLIANCE``):** Apple Silicon MLX or
  local GPU, or an opted-in customer LAN GPU appliance, paired with
  Global-to-Local (GOLD) ROI cropping and :class:`VetoOnlyVLMGuard` under
  strict ``MAY_EGRESS`` enforcement.
* **Tier 3 (``CPU_LIGHTWEIGHT``):** CPU-only hosts allow lightweight ONNX
  locate-only grounding (~55 ms) and **explicitly refuse** interactive 4B+ VLM
  calls unless ``allow_slow_cpu_vlm=True`` is opted in for offline/async jobs.

All visual checks are strictly **veto-only**: ``VETO`` and ``ABSTAIN`` halt;
``PASS`` never overrides a failing deterministic check.
"""

from __future__ import annotations

import importlib.util
import os
import platform
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from openadapt_flow.ir import Region
from openadapt_flow.runtime.grounder import component_may_egress
from openadapt_flow.runtime.identity_vlm import (
    MLXIdentityVLM,
    VetoDecision,
    VetoOnlyVLMGuard,
    VetoVerdict,
)
from openadapt_flow.runtime.remote_vlm import (
    _MAX_MODEL_IMAGE_DIM,
    RemoteAppliance,
    appliance_from_env,
)


class HardwareTier(str, Enum):
    """Execution tier for local verification and grounding."""

    DETERMINISTIC_ONLY = "deterministic_only"
    LOCAL_ACCELERATED = "local_accelerated"
    LAN_APPLIANCE = "lan_appliance"
    CPU_LIGHTWEIGHT = "cpu_lightweight"


class CpuVlmRefusedError(RuntimeError):
    """Raised when an interactive 4B+ VLM call is requested on a CPU-only host
    without explicit offline/async opt-in (``allow_slow_cpu_vlm=True``).
    """


@dataclass(frozen=True)
class HardwareCapabilities:
    """Immutable snapshot of host model-execution capabilities."""

    platform_system: str
    machine: str
    has_apple_silicon_mlx: bool = False
    has_cuda_gpu: bool = False
    has_lan_appliance: bool = False
    has_onnxruntime: bool = False
    available_ram_gib: Optional[float] = None


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def detect_hardware_capabilities(
    env: Optional[Mapping[str, str]] = None,
    *,
    probe_imports: bool = True,
) -> HardwareCapabilities:
    """Detect local hardware capabilities without importing heavy ML stacks."""
    src = os.environ if env is None else env
    sys_name = platform.system()
    machine = platform.machine().lower()

    has_mlx = False
    has_cuda = False
    has_onnx = False
    if probe_imports:
        is_apple_arm = sys_name == "Darwin" and machine in ("arm64", "aarch64")
        has_mlx = is_apple_arm and _module_available("mlx_vlm")
        has_onnx = _module_available("onnxruntime")
        if _module_available("torch"):
            try:
                import torch

                has_cuda = bool(torch.cuda.is_available())
            except Exception:
                has_cuda = False

    has_lan = bool((src.get("OPENADAPT_FLOW_VLM_URL") or "").strip())
    return HardwareCapabilities(
        platform_system=sys_name,
        machine=machine,
        has_apple_silicon_mlx=has_mlx,
        has_cuda_gpu=has_cuda,
        has_lan_appliance=has_lan,
        has_onnxruntime=has_onnx,
    )


@dataclass(frozen=True)
class TierPolicy:
    """Routing and safety policy for :class:`TieredLocalModelRouter`.

    * ``prefer_deterministic`` -- route to ``DETERMINISTIC_ONLY`` whenever
      deterministic evidence covers the check.
    * ``allow_lan_appliance`` -- explicit opt-in to route to a configured LAN
      GPU appliance (``MAY_EGRESS = True``). Off by default so screenshots
      never silently leave the local host.
    * ``allow_slow_cpu_vlm`` -- explicit opt-in to allow 4B+ VLM calls on
      CPU-only hosts for offline/async jobs. Off by default to prevent 40s+
      interactive UI stalls and multi-GB multimodal RAM spikes.
    * ``interactive`` -- marks interactive UI replay paths (default ``True``).
    * ``max_image_dim`` -- GOLD crop / downscale pixel ceiling (default 1024).
    """

    prefer_deterministic: bool = True
    allow_lan_appliance: bool = False
    allow_slow_cpu_vlm: bool = False
    interactive: bool = True
    max_image_dim: int = _MAX_MODEL_IMAGE_DIM


@dataclass(frozen=True)
class TierRouteDecision:
    """Outcome of a single hardware-tier routing decision."""

    tier: HardwareTier
    allow_vlm_veto: bool
    allow_lightweight_locate: bool
    may_egress: bool
    reason: str


class TieredLocalModelRouter:
    """Hardware-tiered local model policy router.

    Selects the lowest-cost safe execution tier for each check and builds
    fail-closed :class:`VetoOnlyVLMGuard` / :class:`PrePostSubmitVisualVeto`
    handles that preserve ``MAY_EGRESS`` classification.
    """

    def __init__(
        self,
        capabilities: Optional[HardwareCapabilities] = None,
        policy: Optional[TierPolicy] = None,
        *,
        local_identity_vlm: Optional[Any] = None,
        local_state_verifier: Optional[Any] = None,
        lan_appliance: Optional[RemoteAppliance] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._env = os.environ if env is None else env
        self.capabilities = (
            detect_hardware_capabilities(self._env)
            if capabilities is None
            else capabilities
        )
        self.policy = TierPolicy() if policy is None else policy
        self._local_identity_vlm = local_identity_vlm
        self._local_state_verifier = local_state_verifier
        self._lan_appliance = lan_appliance

    @property
    def MAY_EGRESS(self) -> bool:
        """Whether this router's configured VLM tier can send screenshots off-box."""
        decision = self.route(deterministic_available=False, needs_vlm_veto=True)
        if decision.may_egress:
            return True
        if decision.tier is HardwareTier.LOCAL_ACCELERATED or decision.allow_vlm_veto:
            return component_may_egress(
                self._local_identity_vlm
            ) or component_may_egress(self._local_state_verifier)
        return False

    def route(
        self,
        *,
        deterministic_available: bool = False,
        needs_vlm_veto: bool = False,
        needs_locate: bool = False,
    ) -> TierRouteDecision:
        """Choose the execution tier for a check or grounding request."""
        if deterministic_available and self.policy.prefer_deterministic:
            return TierRouteDecision(
                tier=HardwareTier.DETERMINISTIC_ONLY,
                allow_vlm_veto=False,
                allow_lightweight_locate=False,
                may_egress=False,
                reason="deterministic_authority_available",
            )

        if self.capabilities.has_apple_silicon_mlx or self.capabilities.has_cuda_gpu:
            accel = (
                "apple_silicon_mlx"
                if self.capabilities.has_apple_silicon_mlx
                else "cuda_gpu"
            )
            return TierRouteDecision(
                tier=HardwareTier.LOCAL_ACCELERATED,
                allow_vlm_veto=True,
                allow_lightweight_locate=True,
                may_egress=False,
                reason=f"local_accelerator:{accel}",
            )

        lan_configured = (
            self._lan_appliance is not None or self.capabilities.has_lan_appliance
        )
        if lan_configured and self.policy.allow_lan_appliance:
            return TierRouteDecision(
                tier=HardwareTier.LAN_APPLIANCE,
                allow_vlm_veto=True,
                allow_lightweight_locate=True,
                may_egress=True,
                reason="lan_appliance_opted_in",
            )

        # Tier 3: CPU-only host (or LAN configured without egress opt-in).
        if self.policy.allow_slow_cpu_vlm and not self.policy.interactive:
            reason = "cpu_vlm_offline_opt_in"
            allow_vlm = True
        elif self.policy.allow_slow_cpu_vlm:
            reason = "cpu_vlm_explicit_opt_in"
            allow_vlm = True
        elif lan_configured and not self.policy.allow_lan_appliance:
            reason = "lan_appliance_requires_egress_opt_in;cpu_refuses_interactive_vlm"
            allow_vlm = False
        elif needs_vlm_veto:
            reason = "cpu_only_refuses_interactive_vlm"
            allow_vlm = False
        elif needs_locate and self.capabilities.has_onnxruntime:
            reason = "cpu_lightweight_onnx_locate_only"
            allow_vlm = False
        else:
            reason = "cpu_lightweight_no_interactive_vlm"
            allow_vlm = False

        return TierRouteDecision(
            tier=HardwareTier.CPU_LIGHTWEIGHT,
            allow_vlm_veto=allow_vlm,
            allow_lightweight_locate=self.capabilities.has_onnxruntime,
            may_egress=False,
            reason=reason,
        )

    def _resolve_lan_appliance(self) -> Optional[RemoteAppliance]:
        if self._lan_appliance is not None:
            return self._lan_appliance
        return appliance_from_env(dict(self._env))

    def build_veto_guard(self, *, require_vlm: bool = False) -> VetoOnlyVLMGuard:
        """Build a tier-appropriate :class:`VetoOnlyVLMGuard`.

        On CPU-only hosts without ``allow_slow_cpu_vlm=True``:
        * raises :class:`CpuVlmRefusedError` when ``require_vlm=True``;
        * otherwise returns an empty fail-closed guard that abstains without
          loading or running a slow 4B+ model on CPU.
        """
        decision = self.route(deterministic_available=False, needs_vlm_veto=True)

        if decision.tier is HardwareTier.LOCAL_ACCELERATED:
            id_vlm = (
                self._local_identity_vlm
                if self._local_identity_vlm is not None
                else (
                    MLXIdentityVLM()
                    if self.capabilities.has_apple_silicon_mlx
                    else None
                )
            )
            return VetoOnlyVLMGuard(
                identity_vlm=id_vlm,
                state_verifier=self._local_state_verifier,
                max_dim=self.policy.max_image_dim,
            )

        if decision.tier is HardwareTier.LAN_APPLIANCE:
            appliance = self._resolve_lan_appliance()
            if appliance is None:
                return VetoOnlyVLMGuard(max_dim=self.policy.max_image_dim)
            return VetoOnlyVLMGuard(
                identity_vlm=appliance.identity_vlm,
                state_verifier=appliance.state_verifier,
                max_dim=self.policy.max_image_dim,
            )

        if not decision.allow_vlm_veto:
            if require_vlm:
                raise CpuVlmRefusedError(
                    "Refusing interactive 4B+ VLM execution on CPU-only host "
                    f"({decision.reason}). Pass TierPolicy(allow_slow_cpu_vlm=True) "
                    "for offline/async CPU jobs, or opt in to a LAN appliance."
                )
            return VetoOnlyVLMGuard(max_dim=self.policy.max_image_dim)

        return VetoOnlyVLMGuard(
            identity_vlm=self._local_identity_vlm,
            state_verifier=self._local_state_verifier,
            max_dim=self.policy.max_image_dim,
        )

    def build_submit_veto(
        self, *, require_vlm: bool = False
    ) -> PrePostSubmitVisualVeto:
        """Build a :class:`PrePostSubmitVisualVeto` wired for the routed tier."""
        decision = self.route(deterministic_available=False, needs_vlm_veto=True)
        guard = self.build_veto_guard(require_vlm=require_vlm)
        return PrePostSubmitVisualVeto(guard=guard, tier=decision.tier)


class VisualVetoStage(str, Enum):
    """Lifecycle stage guarded by :class:`PrePostSubmitVisualVeto`."""

    PRE_SUBMIT = "pre_submit"
    POST_SUBMIT = "post_submit"


@dataclass(frozen=True)
class SubmitVetoResult:
    """Combined deterministic + veto-only visual outcome for a submit boundary."""

    stage: VisualVetoStage
    decision: VetoDecision
    deterministic_passed: bool
    allowed: bool
    tier: HardwareTier


class PrePostSubmitVisualVeto:
    """Domain-agnostic pre-submit and post-submit visual veto guard.

    Wraps :class:`VetoOnlyVLMGuard` (with GOLD ROI cropping on >1024px frames)
    and enforces strict veto-only semantics at form/GUI submit boundaries:

    * ``VETO`` or ``ABSTAIN`` always sets ``allowed = False`` (halt).
    * ``PASS`` lets a passing deterministic check stand, and **never**
      overrides ``deterministic_passed = False``.
    * Propagates ``MAY_EGRESS`` from the underlying guard.
    """

    def __init__(
        self,
        guard: VetoOnlyVLMGuard,
        *,
        tier: HardwareTier = HardwareTier.LOCAL_ACCELERATED,
    ) -> None:
        self._guard = guard
        self._tier = tier

    @property
    def MAY_EGRESS(self) -> bool:
        """Propagate screenshot-egress classification from the wrapped guard."""
        return component_may_egress(self._guard)

    def check_pre_submit(
        self,
        *,
        recorded_png: bytes,
        live_png: bytes,
        deterministic_passed: bool,
        roi: Optional[Region] = None,
        expected_state: Optional[str] = None,
    ) -> SubmitVetoResult:
        """Run a veto-only visual identity (and optional state) check before submit."""
        id_decision = self._guard.check_identity(recorded_png, live_png, roi=roi)
        decision = id_decision
        if not id_decision.should_halt and expected_state is not None:
            state_decision = self._guard.check_state(live_png, expected_state, roi=roi)
            if state_decision.should_halt:
                decision = state_decision
            else:
                decision = VetoDecision(
                    verdict=VetoVerdict.PASS,
                    used_gold_crop=(
                        id_decision.used_gold_crop or state_decision.used_gold_crop
                    ),
                )

        return SubmitVetoResult(
            stage=VisualVetoStage.PRE_SUBMIT,
            decision=decision,
            deterministic_passed=bool(deterministic_passed),
            allowed=decision.apply_to_deterministic(deterministic_passed),
            tier=self._tier,
        )

    def check_post_submit(
        self,
        *,
        screenshot: bytes,
        expected_state: str,
        deterministic_passed: bool,
        roi: Optional[Region] = None,
    ) -> SubmitVetoResult:
        """Run a veto-only visual state check after submit."""
        decision = self._guard.check_state(screenshot, expected_state, roi=roi)
        return SubmitVetoResult(
            stage=VisualVetoStage.POST_SUBMIT,
            decision=decision,
            deterministic_passed=bool(deterministic_passed),
            allowed=decision.apply_to_deterministic(deterministic_passed),
            tier=self._tier,
        )
