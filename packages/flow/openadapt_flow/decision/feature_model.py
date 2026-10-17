"""A small logistic regression over the minimized features. No model calls.

Plain numpy, fitted by Newton's method with an L2 penalty. The fitted
weights live in a digest-pinned
:class:`~openadapt_flow.decision.calibration.CalibrationArtifact`, and
:class:`FeatureModelProvider` refuses an artifact whose digest isn't the one
it was given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict

from openadapt_flow.decision.minimize import FEATURE_NAMES, assert_minimized
from openadapt_flow.decision.provider import Answer, DecisionRequest

if TYPE_CHECKING:
    from openadapt_flow.decision.calibration import CalibrationArtifact

PROVIDER_ID = "openadapt.feature-logreg/v1"


class LogisticParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_names: tuple[str, ...] = FEATURE_NAMES
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    weights: tuple[float, ...]
    bias: float
    l2: float


def vectorize(features: Mapping[str, bool | int | float]) -> np.ndarray:
    checked = assert_minimized(features)
    return np.array([float(checked[name]) for name in FEATURE_NAMES], dtype=float)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))


def fit_logistic(
    X: np.ndarray,
    y: np.ndarray,
    *,
    sample_weight: Optional[np.ndarray] = None,
    l2: float = 1.0,
    iterations: int = 50,
) -> LogisticParams:
    """Weighted, L2-penalised logistic regression (bias not penalised)."""

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, d = X.shape
    w_s = np.ones(n) if sample_weight is None else np.asarray(sample_weight, float)
    mean = X.mean(axis=0) if n else np.zeros(d)
    scale = X.std(axis=0) if n else np.ones(d)
    scale = np.where(scale > 1e-12, scale, 1.0)
    Z = np.hstack([(X - mean) / scale, np.ones((n, 1))])
    beta = np.zeros(d + 1)
    penalty = np.full(d + 1, l2)
    penalty[-1] = 0.0
    for _ in range(iterations):
        p = _sigmoid(Z @ beta)
        grad = Z.T @ (w_s * (p - y)) + penalty * beta
        h = w_s * p * (1.0 - p)
        hess = (Z * h[:, None]).T @ Z + np.diag(penalty + 1e-9)
        step = np.linalg.solve(hess, grad)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return LogisticParams(
        mean=tuple(float(v) for v in mean),
        scale=tuple(float(v) for v in scale),
        weights=tuple(float(v) for v in beta[:-1]),
        bias=float(beta[-1]),
        l2=float(l2),
    )


def predict_raw(params: LogisticParams, X: np.ndarray) -> np.ndarray:
    X = np.atleast_2d(np.asarray(X, dtype=float))
    z = ((X - np.array(params.mean)) / np.array(params.scale)) @ np.array(
        params.weights
    ) + params.bias
    return _sigmoid(z)


class FeatureModelProvider:
    """The local :class:`~openadapt_flow.decision.provider.DecisionProvider`."""

    provider_id = PROVIDER_ID

    def __init__(self, artifact: "CalibrationArtifact", *, pinned_sha256: str) -> None:
        artifact.verify(pinned_sha256)
        self._artifact = artifact

    def answer(self, request: DecisionRequest) -> Answer:
        artifact = self._artifact
        digest = artifact.artifact_sha256
        if request.group_key != artifact.group_key:
            return Answer(
                provider_id=self.provider_id,
                abstain=True,
                abstain_reason="group_mismatch",
                artifact_sha256=digest,
            )
        if not artifact.eligible or artifact.model is None:
            return Answer(
                provider_id=self.provider_id,
                abstain=True,
                abstain_reason="uncalibrated",
                artifact_sha256=digest,
            )
        raw = float(predict_raw(artifact.model, vectorize(request.features))[0])
        return Answer(
            provider_id=self.provider_id,
            p_bad=float(artifact.calibrate(np.array([raw]))[0]),
            artifact_sha256=digest,
        )


def score_many(
    params: LogisticParams, rows: Sequence[Mapping[str, bool | int | float]]
) -> np.ndarray:
    if not rows:
        return np.zeros(0)
    return predict_raw(params, np.vstack([vectorize(row) for row in rows]))


__all__ = [
    "PROVIDER_ID",
    "FeatureModelProvider",
    "LogisticParams",
    "fit_logistic",
    "predict_raw",
    "score_many",
    "vectorize",
]
