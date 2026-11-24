"""Online Adaptive Conformal Inference and learned-score triage.

Implements Gibbs & Candes (2021) Adaptive Conformal Inference (ACI) for
bounding the long-run false-stop rate of learned uncertainty scores under
UI distribution shift:

    alpha_{t+1} = alpha_t + gamma * (target_alpha - err_t)

Pairs the online threshold with the rule "Learned Scores Never Veto Alone":
when deterministic checks pass, high learned uncertainty routes to
``SILENT_SELF_REPAIR`` instead of waking a human. ``HUMAN_ESCALATE`` is
reserved for deterministic invariant failures or persistent conflicts on
irreversible commits.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TriageAction(str, Enum):
    """Routing decision for an action before execution."""

    PROCEED = "PROCEED"
    SILENT_SELF_REPAIR = "SILENT_SELF_REPAIR"
    HUMAN_ESCALATE = "HUMAN_ESCALATE"


@dataclass(frozen=True)
class TriageDecision:
    """Structured triage outcome pairing deterministic checks and ACI."""

    action: TriageAction
    reason: str
    deterministic_ok: bool
    learned_uncertainty: float
    threshold: float
    alpha_t: float
    is_irreversible: bool


class AdaptiveConformalCalibrator:
    """Online Adaptive Conformal Inference (Gibbs & Candes, 2021).

    Maintains a rolling calibration window of recent nonconformity scores
    and updates the miscoverage level ``alpha_t`` after each verified
    outcome or human override so the long-run false-stop rate tracks
    ``target_alpha`` even across UI redesigns and distribution shifts.
    """

    def __init__(
        self,
        target_alpha: float = 0.02,
        gamma: float = 0.05,
        *,
        window_size: int = 200,
        min_alpha: float | None = None,
        max_alpha: float | None = None,
        initial_alpha: float | None = None,
        default_threshold: float = 0.5,
    ) -> None:
        if not (0.0 < target_alpha < 1.0):
            raise ValueError("target_alpha must be strictly between 0 and 1")
        if gamma <= 0.0 or not math.isfinite(gamma):
            raise ValueError("gamma must be a positive finite float")
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if not math.isfinite(default_threshold):
            raise ValueError("default_threshold must be finite")

        # Gibbs & Candes (2021) Proposition 4.1: alpha_t naturally stays in
        # [-gamma, 1 + gamma]; clipping at 0.0 would discard negative credit
        # after an error and bias the empirical error rate above target_alpha.
        resolved_min = -float(gamma) if min_alpha is None else float(min_alpha)
        resolved_max = 1.0 + float(gamma) if max_alpha is None else float(max_alpha)
        if not (resolved_min < resolved_max):
            raise ValueError("require min_alpha < max_alpha")

        self.target_alpha = float(target_alpha)
        self.gamma = float(gamma)
        self.window_size = int(window_size)
        self.min_alpha = resolved_min
        self.max_alpha = resolved_max
        self.default_threshold = float(default_threshold)

        start_alpha = target_alpha if initial_alpha is None else float(initial_alpha)
        self.alpha_t: float = min(max(start_alpha, self.min_alpha), self.max_alpha)
        self._scores: deque[float] = deque(maxlen=self.window_size)
        self.steps: int = 0
        self.error_sum: float = 0.0

    @property
    def empirical_error_rate(self) -> float:
        """Observed average error indicator across all updates so far."""
        if self.steps == 0:
            return 0.0
        return self.error_sum / self.steps

    def threshold(self) -> float:
        """Return the current ``(1 - alpha_t)`` conformal score threshold.

        Scores strictly above this threshold are treated as uncertain.
        """
        if self.alpha_t <= 0.0:
            return math.inf
        if self.alpha_t >= 1.0:
            return -math.inf
        if not self._scores:
            return self.default_threshold

        ordered = sorted(self._scores)
        n = len(ordered)
        # Finite-sample conformal quantile index: ceil((n + 1) * (1 - alpha_t))
        rank = math.ceil((n + 1) * (1.0 - self.alpha_t))
        if rank > n:
            return ordered[-1]
        idx = max(0, rank - 1)
        return ordered[idx]

    def is_uncertain(self, score: float) -> bool:
        """Return True if ``score`` exceeds the current conformal threshold."""
        if not math.isfinite(score):
            return True
        return score > self.threshold()

    def update(
        self,
        score: float,
        *,
        err_t: int | float | None = None,
        human_override: bool | None = None,
        verified_ok: bool | None = None,
    ) -> float:
        """Update ``alpha_t`` and the rolling window after a verified outcome.

        Follows ``alpha_{t+1} = alpha_t + gamma * (target_alpha - err_t)``
        clipped to ``[min_alpha, max_alpha]``.

        When ``err_t`` is omitted:
        - ``human_override=True`` counts as ``err_t = 1`` (false stop).
        - Otherwise, if ``verified_ok=True``, ``err_t = 1`` iff the score
          exceeded the pre-update threshold (a false stop on a valid action).
        - Otherwise, ``err_t = 1`` iff ``score > threshold`` (standard
          conformal miscoverage indicator).
        """
        pre_threshold = self.threshold()
        if err_t is not None:
            indicator = float(err_t)
            if not (0.0 <= indicator <= 1.0):
                raise ValueError("err_t must be in [0, 1]")
        elif human_override is True:
            indicator = 1.0
        elif verified_ok is True:
            indicator = (
                1.0 if (not math.isfinite(score) or score > pre_threshold) else 0.0
            )
        else:
            indicator = (
                1.0 if (not math.isfinite(score) or score > pre_threshold) else 0.0
            )

        next_alpha = self.alpha_t + self.gamma * (self.target_alpha - indicator)
        self.alpha_t = min(max(next_alpha, self.min_alpha), self.max_alpha)
        if math.isfinite(score):
            self._scores.append(float(score))
        self.steps += 1
        self.error_sum += indicator
        return self.alpha_t

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of calibrator state."""
        thr = self.threshold()
        return {
            "target_alpha": self.target_alpha,
            "gamma": self.gamma,
            "alpha_t": self.alpha_t,
            "threshold": thr if math.isfinite(thr) else str(thr),
            "window_count": len(self._scores),
            "window_size": self.window_size,
            "steps": self.steps,
            "empirical_error_rate": self.empirical_error_rate,
        }


def triage_action(
    deterministic_ok: bool,
    learned_uncertainty: float,
    calibrator: AdaptiveConformalCalibrator,
    is_irreversible: bool,
    *,
    persistent_conflict: bool = False,
) -> TriageDecision:
    """Route an action while enforcing 'Learned Scores Never Veto Alone'.

    - Deterministic invariant failure -> ``HUMAN_ESCALATE``.
    - Persistent conflict on an irreversible commit -> ``HUMAN_ESCALATE``.
    - Passing deterministic checks + high learned uncertainty ->
      ``SILENT_SELF_REPAIR`` (never wakes a human).
    - Passing deterministic checks + calibrated uncertainty -> ``PROCEED``.
    """
    thr = calibrator.threshold()
    alpha = calibrator.alpha_t

    if not deterministic_ok:
        reason = (
            "deterministic_invariant_failed_irreversible"
            if is_irreversible
            else "deterministic_invariant_failed"
        )
        return TriageDecision(
            action=TriageAction.HUMAN_ESCALATE,
            reason=reason,
            deterministic_ok=False,
            learned_uncertainty=learned_uncertainty,
            threshold=thr,
            alpha_t=alpha,
            is_irreversible=is_irreversible,
        )

    if persistent_conflict and is_irreversible:
        return TriageDecision(
            action=TriageAction.HUMAN_ESCALATE,
            reason="persistent_conflict_on_irreversible_commit",
            deterministic_ok=True,
            learned_uncertainty=learned_uncertainty,
            threshold=thr,
            alpha_t=alpha,
            is_irreversible=True,
        )

    if calibrator.is_uncertain(learned_uncertainty):
        return TriageDecision(
            action=TriageAction.SILENT_SELF_REPAIR,
            reason="learned_uncertainty_above_conformal_threshold",
            deterministic_ok=True,
            learned_uncertainty=learned_uncertainty,
            threshold=thr,
            alpha_t=alpha,
            is_irreversible=is_irreversible,
        )

    return TriageDecision(
        action=TriageAction.PROCEED,
        reason="deterministic_checks_and_conformal_threshold_satisfied",
        deterministic_ok=True,
        learned_uncertainty=learned_uncertainty,
        threshold=thr,
        alpha_t=alpha,
        is_irreversible=is_irreversible,
    )
