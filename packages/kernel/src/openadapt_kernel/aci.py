"""Zero-label session-start normalization and Online Adaptive Conformal Inference (ACI).

Implements:
  - SessionStartNormalizer: estimates (mu, sigma) from K benign read-only
    navigation frames to normalize commit anomaly scores under unseen UI themes.
  - OnlineACICalibrator: Gibbs & Candes (NeurIPS 2021) online quantile update
    alpha_{t+1} = alpha_t + gamma * (target_alpha - err_t), triggered strictly
    when independent post-commit read-back resolves ground-truth Y_t.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Sequence


@dataclass
class SessionStartNormalizer:
    min_frames: int = 3
    scores: List[float] = field(default_factory=list)

    def observe_benign_nav(self, score: float) -> None:
        self.scores.append(float(score))

    @property
    def ready(self) -> bool:
        return len(self.scores) >= self.min_frames

    @property
    def mean(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores) / len(self.scores)

    @property
    def std(self) -> float:
        if len(self.scores) < 2:
            return 1.0
        mu = self.mean
        var = sum((x - mu) ** 2 for x in self.scores) / len(self.scores)
        return max(math.sqrt(var), 1e-4)

    def z_score(self, commit_score: float) -> float:
        if not self.ready:
            return float(commit_score)
        return (float(commit_score) - self.mean) / self.std


@dataclass
class OnlineACICalibrator:
    target_alpha: float = 0.02
    gamma: float = 0.05
    alpha_t: float = 0.02
    history_scores: List[float] = field(default_factory=list)
    errors: List[int] = field(default_factory=list)

    def threshold(self) -> float:
        if not self.history_scores:
            return 2.0
        sorted_s = sorted(self.history_scores)
        q = min(max(1.0 - self.alpha_t, 0.0), 1.0)
        idx = min(int(math.ceil(q * len(sorted_s))) - 1, len(sorted_s) - 1)
        idx = max(idx, 0)
        return sorted_s[idx]

    def should_flag(self, score: float) -> bool:
        return float(score) > self.threshold()

    def update_from_readback(self, score: float, realized_error: bool) -> float:
        """Update alpha_t once independent read-back resolves whether a false alarm occurred."""
        err_t = 1 if realized_error else 0
        self.history_scores.append(float(score))
        self.errors.append(err_t)
        self.alpha_t = min(max(self.alpha_t + self.gamma * (self.target_alpha - err_t), 0.0), 1.0)
        return self.alpha_t

    @property
    def empirical_error_rate(self) -> float:
        if not self.errors:
            return 0.0
        return sum(self.errors) / len(self.errors)


def calibrate_conformal_abstain_threshold(
    calibration_scores: Sequence[float],
    calibration_losses: Sequence[float],
    *,
    alpha: float = 0.05,
) -> float:
    """Finite-sample Conformal Risk Control (CRC) threshold calibration for risk-gated abstention.

    Returns the largest threshold tau such that accepting actions with risk_score <= tau satisfies
    the finite-sample upper bound (sum(loss_i for score_i <= tau) + 1.0) / (n + 1.0) <= alpha,
    or -inf (abstain-all) when no threshold can satisfy the risk budget.
    """
    if len(calibration_scores) != len(calibration_losses):
        raise ValueError("calibration_scores and calibration_losses must have equal length")
    if not (0.0 < alpha <= 1.0):
        raise ValueError(f"alpha must be in (0, 1], got {alpha!r}")
    n = len(calibration_scores)
    if n == 0:
        return -math.inf

    pairs = sorted((float(s), float(loss)) for s, loss in zip(calibration_scores, calibration_losses))
    for score, loss in pairs:
        if math.isnan(score) or math.isnan(loss) or loss < 0.0 or loss > 1.0:
            raise ValueError("Scores must be finite and losses must be bounded in [0, 1]")

    if 1.0 / (n + 1.0) > alpha:
        return -math.inf

    best_tau = -math.inf
    running_loss = 0.0
    idx = 0
    while idx < n:
        cand_tau = pairs[idx][0]
        while idx < n and pairs[idx][0] == cand_tau:
            running_loss += pairs[idx][1]
            idx += 1
        if (running_loss + 1.0) / (n + 1.0) <= alpha:
            best_tau = cand_tau
        else:
            break
    return best_tau


@dataclass
class ConformalRiskGate:
    """Distribution-free finite-sample risk gate routing high-risk actions to PrepareCard."""

    alpha: float = 0.05
    threshold: float = -math.inf
    calibration_size: int = 0
    empirical_risk_bound: float = 1.0

    @classmethod
    def from_calibration(
        cls,
        calibration_scores: Sequence[float],
        calibration_losses: Sequence[float],
        *,
        alpha: float = 0.05,
    ) -> "ConformalRiskGate":
        tau = calibrate_conformal_abstain_threshold(calibration_scores, calibration_losses, alpha=alpha)
        n = len(calibration_scores)
        if n == 0:
            bound = 1.0
        elif math.isinf(tau):
            bound = 1.0 / (n + 1.0)
        else:
            loss_sum = sum(
                float(loss) for s, loss in zip(calibration_scores, calibration_losses) if float(s) <= tau
            )
            bound = (loss_sum + 1.0) / (n + 1.0)
        return cls(alpha=alpha, threshold=tau, calibration_size=n, empirical_risk_bound=bound)

    def should_abstain(self, risk_score: float) -> bool:
        """Return True when risk_score exceeds the calibrated CRC threshold (route to PrepareCard)."""
        val = float(risk_score)
        if math.isnan(val):
            return True
        return val > self.threshold
