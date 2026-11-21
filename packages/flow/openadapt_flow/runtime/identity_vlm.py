"""Optional local-VLM identity comparator (tier 3 of the identity ladder).

Ships :class:`MLXIdentityVLM`, an :class:`~openadapt_flow.runtime.identity.IdentityVLM`
implementation backed by a LOCAL open VLM served through MLX (Apple-silicon).
It is the runtime promotion of the validated experiment in
``openadapt_flow.validation.vlm_identity_probe`` (benchmark/vlm_identity,
PR #28): give the model the two magnified identifier crops stacked A-over-B
and ask "same characters or different?", VETO-ONLY.

It is OPTIONAL and OFF by default -- exactly like the grounder. The default
install pulls no model: the identity ladder runs structured-text +
pixel-compare + OCR + halt with zero extra dependencies. A caller that wants
the veto passes an instance into ``Replayer(identity_vlm=...)``; the mlx-vlm
import is guarded so importing this module never requires the package, and the
model loads lazily on first use.

ZERO Anthropic / cloud API calls: the model is a local open checkpoint (default
``mlx-community/Qwen3-VL-4B-Instruct-4bit``) served entirely on-device.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import Region
from openadapt_flow.runtime.remote_vlm import (
    _MAX_MODEL_IMAGE_DIM,
    IdentityVerdict,
    gold_crop_for_model,
)

DEFAULT_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"


class VetoVerdict(str, Enum):
    """Unified veto-only verdict for local and remote VLM guards.

    * ``PASS``    -- confident affirmative match/state; fail-to-veto ONLY.
      NEVER overrides a failed deterministic check.
    * ``VETO``    -- explicit mismatch or violated state; halts execution.
    * ``ABSTAIN`` -- uncertain, unreachable, timeout, exception, or malformed
      output; halts execution.
    """

    PASS = "pass"
    VETO = "veto"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class VetoDecision:
    """Outcome of a single veto-only VLM guard check."""

    verdict: VetoVerdict
    reason: str = ""
    used_gold_crop: bool = False

    @property
    def should_halt(self) -> bool:
        """True for ``VETO`` and ``ABSTAIN``; False only for ``PASS``."""
        return self.verdict is not VetoVerdict.PASS

    def apply_to_deterministic(self, deterministic_passed: bool) -> bool:
        """Combine with a deterministic check under strict veto-only semantics.

        ``VETO`` / ``ABSTAIN`` always halt (return ``False``). ``PASS`` lets a
        passing deterministic check stand, and NEVER overrides a failing
        deterministic check.
        """
        return bool(deterministic_passed) and not self.should_halt


class VetoOnlyVLMGuard:
    """Unified veto-only guard over local (MLX) or remote VLM backends.

    Enforces two invariants across identity and state checks:

    1. **Strict veto-only semantics**: ``VETO`` and ``ABSTAIN`` (including any
       transport error, model exception, or unrecognized answer) halt; ``PASS``
       only declines to veto and never overrides deterministic checks.
    2. **GOLD ROI cropping**: when full-frame screenshots exceed ``max_dim``
       (1024px) and a target/alert ``roi`` is provided, the guard extracts
       native-resolution ROI crops (or GOLD global+local composites) within the
       ``max_dim`` budget instead of uniformly downscaling away small text.
    """

    def __init__(
        self,
        *,
        identity_vlm: Optional[Any] = None,
        state_verifier: Optional[Any] = None,
        max_dim: int = _MAX_MODEL_IMAGE_DIM,
    ) -> None:
        self._identity_vlm = identity_vlm
        self._state_verifier = state_verifier
        self._max_dim = max_dim

    def check_identity(
        self,
        recorded_png: bytes,
        live_png: bytes,
        *,
        roi: Optional[Region] = None,
    ) -> VetoDecision:
        """Run a veto-only identity comparison, applying GOLD cropping if ``roi`` is set."""
        if self._identity_vlm is None:
            return VetoDecision(VetoVerdict.ABSTAIN, reason="no_identity_backend")

        used_gold = False
        rec_sent = recorded_png
        live_sent = live_png
        if roi is not None:
            rec_gold = gold_crop_for_model(recorded_png, roi=roi, max_dim=self._max_dim)
            live_gold = gold_crop_for_model(live_png, roi=roi, max_dim=self._max_dim)
            if rec_gold.used_gold:
                rec_sent = rec_gold.local_png
                used_gold = True
            if live_gold.used_gold:
                live_sent = live_gold.local_png
                used_gold = True

        try:
            compare_fn = getattr(self._identity_vlm, "compare", None)
            if callable(compare_fn):
                raw = compare_fn(rec_sent, live_sent)
                if raw is IdentityVerdict.VERIFY:
                    return VetoDecision(VetoVerdict.PASS, used_gold_crop=used_gold)
                if raw is IdentityVerdict.MISMATCH:
                    return VetoDecision(
                        VetoVerdict.VETO,
                        reason="identity_mismatch",
                        used_gold_crop=used_gold,
                    )
                return VetoDecision(
                    VetoVerdict.ABSTAIN,
                    reason="identity_abstain",
                    used_gold_crop=used_gold,
                )

            same_fn = getattr(self._identity_vlm, "same_or_different", None)
            if callable(same_fn):
                ans = same_fn(rec_sent, live_sent)
                if ans == "same":
                    return VetoDecision(VetoVerdict.PASS, used_gold_crop=used_gold)
                if ans == "different":
                    return VetoDecision(
                        VetoVerdict.VETO,
                        reason="identity_different",
                        used_gold_crop=used_gold,
                    )
                return VetoDecision(
                    VetoVerdict.ABSTAIN,
                    reason="identity_unrecognized",
                    used_gold_crop=used_gold,
                )
        except Exception as exc:
            return VetoDecision(
                VetoVerdict.ABSTAIN,
                reason=f"identity_error:{type(exc).__name__}",
                used_gold_crop=used_gold,
            )

        return VetoDecision(
            VetoVerdict.ABSTAIN,
            reason="unsupported_identity_backend",
            used_gold_crop=used_gold,
        )

    def check_state(
        self,
        screenshot: bytes,
        expected_state: str,
        *,
        roi: Optional[Region] = None,
    ) -> VetoDecision:
        """Run a veto-only state check, applying GOLD cropping when ``roi`` is set."""
        if self._state_verifier is None:
            return VetoDecision(VetoVerdict.ABSTAIN, reason="no_state_backend")

        used_gold = False
        sent = screenshot
        if roi is not None:
            gold = gold_crop_for_model(screenshot, roi=roi, max_dim=self._max_dim)
            if gold.used_gold:
                sent = gold.composite_png
                used_gold = True

        try:
            verify_fn = getattr(self._state_verifier, "verify", None)
            if not callable(verify_fn):
                return VetoDecision(
                    VetoVerdict.ABSTAIN,
                    reason="unsupported_state_backend",
                    used_gold_crop=used_gold,
                )
            raw = verify_fn(sent, expected_state)
        except Exception as exc:
            return VetoDecision(
                VetoVerdict.ABSTAIN,
                reason=f"state_error:{type(exc).__name__}",
                used_gold_crop=used_gold,
            )

        if raw == "yes":
            return VetoDecision(VetoVerdict.PASS, used_gold_crop=used_gold)
        if raw == "no":
            return VetoDecision(
                VetoVerdict.VETO,
                reason="state_not_held",
                used_gold_crop=used_gold,
            )
        return VetoDecision(
            VetoVerdict.ABSTAIN,
            reason="state_uncertain",
            used_gold_crop=used_gold,
        )

    def same_or_different(
        self,
        recorded_png: bytes,
        live_png: bytes,
        *,
        roi: Optional[Region] = None,
    ) -> str:
        """Satisfy the ``IdentityVLM`` protocol: ``"same"`` iff ``PASS``, else ``"different"``."""
        decision = self.check_identity(recorded_png, live_png, roi=roi)
        return "same" if decision.verdict is VetoVerdict.PASS else "different"


class MLXIdentityVLM:
    """Local MLX VLM used as a veto-only same/different identity comparator.

    Wraps the validated ``vlm_identity_probe.Comparator`` so the runtime tier
    reuses the exact prompt, stacked-pair composition, and veto-only parsing
    that were measured (0% false-accept + 100% detection on the digit-flanked
    O/0 collapse surface, ~0.8s/call). The model loads lazily on first call.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._comparator: Optional[Any] = None

    def _ensure_loaded(self) -> Any:
        if self._comparator is None:
            # Lazy + import-guarded: mlx-vlm is an optional, Apple-silicon-only
            # dependency; importing this module must never require it.
            from openadapt_flow.validation.vlm_identity_probe import Comparator

            self._comparator = Comparator(self.model_name).load()
        return self._comparator

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        """Return ``"same"`` or ``"different"`` for the two identifier crops.

        VETO-ONLY: the underlying comparator folds any non-confident answer to
        ``"different"`` (see ``vlm_identity_probe.parse_veto``), so an unsure
        model halts rather than passing a wrong patient.
        """
        comparator = self._ensure_loaded()
        with tempfile.TemporaryDirectory() as td:
            result = comparator.compare(recorded_png, live_png, Path(td))
        return str(result.get("verdict", "different"))
