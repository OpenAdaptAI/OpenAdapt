"""Calibrate the feature model and choose its thresholds from labels.

The labels are people's decisions on reviewed saves (see
:mod:`openadapt_flow.decision.labels`). From them this module:

1. fits the feature model on out-of-fold splits, so the calibrator and the
   threshold are chosen on scores the model didn't train on;
2. calibrates the raw score with Platt scaling, or isotonic regression once
   there are enough labels;
3. picks ``t_low`` with a Learn-then-Test style bound: the largest threshold
   at which the rate of bad saves (``wrong_values`` or ``wrong_record``)
   scored below it is at most ``target_miss`` with confidence ``1 - delta``
   (a one-sided binomial test, applied in fixed sequence from the strictest
   threshold up, which is valid because the miss rate only grows with the
   threshold);
4. writes a digest-pinned :class:`CalibrationArtifact`.

A group with too few labels gets an artifact with ``eligible=False``; the
provider abstains on every item and triage sends them all to a person. The
bound assumes the labelled saves and future saves come from the same
distribution. A new application version, a new clinic or a changed form
breaks that, so refit on a rolling window and treat an old artifact as
uncalibrated.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.decision.feature_model import (
    PROVIDER_ID,
    LogisticParams,
    fit_logistic,
    predict_raw,
    vectorize,
)

if TYPE_CHECKING:
    from openadapt_flow.decision.labels import ApprovalLabelV1

ARTIFACT_SCHEMA: Literal["openadapt.decision-calibration/v1"] = (
    "openadapt.decision-calibration/v1"
)
#: Labels needed before a group is considered at all.
MIN_LABELS = 200
#: Labels needed before isotonic regression replaces Platt scaling.
ISOTONIC_MIN_LABELS = 1000
DEFAULT_TARGET_MISS = 0.05
DEFAULT_DELTA = 0.10
DEFAULT_WINDOW = 2000
_HEX64 = r"^[0-9a-f]{64}$"


class ArtifactMismatch(ValueError):
    """The artifact's digest isn't the pinned one, or it was edited."""


def min_bad_labels(target_miss: float, delta: float) -> int:
    """Bad labels needed to bound the miss rate at all, with zero misses.

    ``(1 - target)^n <= delta``: 45 for 5% at 90% confidence, 460 for 0.5%.
    """

    return math.ceil(math.log(delta) / math.log(1.0 - target_miss))


def binom_cdf(k: int, n: int, p: float) -> float:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``."""

    if n == 0 or k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(0, k + 1):
        log_term = (
            math.lgamma(n + 1)
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * log_p
            + (n - i) * log_q
        )
        total += math.exp(log_term)
    return min(1.0, total)


def ltt_threshold(
    scores: Sequence[float],
    is_bad: Sequence[bool],
    *,
    target_miss: float,
    delta: float,
) -> float:
    """Largest ``t`` such that ``P(score < t | bad) <= target`` at ``1-delta``.

    Returns ``0.0`` (nothing is clean) when even the strictest candidate
    fails. Saves with ``score < t`` are clean.
    """

    s = np.asarray(scores, dtype=float)
    bad = np.asarray(is_bad, dtype=bool)
    n_bad = int(bad.sum())
    if n_bad == 0:
        return 0.0
    bad_scores = np.sort(s[bad])
    accepted = 0.0
    for t in np.unique(np.concatenate([s, [1.0 + 1e-9]])):
        misses = int(np.searchsorted(bad_scores, t, side="left"))
        if binom_cdf(misses, n_bad, target_miss) <= delta:
            accepted = float(t)
        else:
            break
    return accepted


def brier(p: Sequence[float], y: Sequence[bool]) -> float:
    pa, ya = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((pa - ya) ** 2)) if pa.size else 0.0


def expected_calibration_error(
    p: Sequence[float], y: Sequence[bool], *, bins: int = 10
) -> float:
    pa, ya = np.asarray(p, float), np.asarray(y, float)
    if not pa.size:
        return 0.0
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(pa, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        mask = index == b
        if mask.any():
            total += mask.mean() * abs(pa[mask].mean() - ya[mask].mean())
    return float(total)


class CalibratorParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["platt", "isotonic"]
    a: float = 1.0
    b: float = 0.0
    xs: tuple[float, ...] = ()
    ys: tuple[float, ...] = ()


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def fit_platt(raw: np.ndarray, y: np.ndarray) -> CalibratorParams:
    params = fit_logistic(_logit(raw)[:, None], y, l2=1e-3)
    # Undo the standardisation fit_logistic applied to the single input.
    a = params.weights[0] / params.scale[0]
    b = params.bias - a * params.mean[0]
    return CalibratorParams(method="platt", a=float(a), b=float(b))


def fit_isotonic(raw: np.ndarray, y: np.ndarray) -> CalibratorParams:
    """Pool-adjacent-violators on the raw score, increasing."""

    order = np.argsort(raw, kind="mergesort")
    x_sorted, y_sorted = raw[order], y[order].astype(float)
    blocks: list[list[float]] = []  # [sum_y, count, x_right]
    for xv, yv in zip(x_sorted, y_sorted):
        blocks.append([yv, 1.0, xv])
        while len(blocks) > 1 and (
            blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]
        ):
            s, c, xr = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][2] = xr
    return CalibratorParams(
        method="isotonic",
        xs=tuple(float(b[2]) for b in blocks),
        ys=tuple(float(b[0] / b[1]) for b in blocks),
    )


def apply_calibrator(params: CalibratorParams, raw: np.ndarray) -> np.ndarray:
    raw = np.asarray(raw, dtype=float)
    if params.method == "platt":
        z = params.a * _logit(raw) + params.b
        return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))
    xs, ys = np.array(params.xs), np.array(params.ys)
    if not xs.size:
        return np.full(raw.shape, 0.5)
    index = np.clip(np.searchsorted(xs, raw, side="left"), 0, xs.size - 1)
    return ys[index]


class CalibrationArtifact(BaseModel):
    """A fitted model, its calibrator and thresholds, for one group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.decision-calibration/v1"] = ARTIFACT_SCHEMA
    group_key: str = Field(pattern=_HEX64)
    provider_id: str = PROVIDER_ID
    eligible: bool
    ineligible_reason: Optional[
        Literal["too_few_labels", "too_few_bad_labels", "no_threshold"]
    ] = None
    model: Optional[LogisticParams] = None
    calibrator: Optional[CalibratorParams] = None
    #: Below ``t_low`` a save is clean; at or above ``t_high`` it is flagged;
    #: between them the score abstains and the save goes to a person.
    t_low: float = Field(default=0.0, ge=0.0, le=1.0 + 1e-6)
    t_high: float = Field(default=0.5, ge=0.0, le=1.0 + 1e-6)
    target_miss: float = Field(gt=0.0, lt=1.0)
    delta: float = Field(gt=0.0, lt=1.0)
    labels_total: int = Field(ge=0)
    labels_bad: int = Field(ge=0)
    labels_declined: int = Field(ge=0)
    window: int = Field(ge=1)
    labels_sha256: str = Field(pattern=_HEX64)
    created_at: str
    #: Brier score and calibration error on held-out labels: the model out
    #: of fold, and the calibrator fitted on the other half.
    oof_brier: Optional[float] = None
    oof_ece: Optional[float] = None
    artifact_sha256: str = Field(default="0" * 64, pattern=_HEX64)

    def compute_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"artifact_sha256"})
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def sealed(self) -> "CalibrationArtifact":
        return self.model_copy(update={"artifact_sha256": self.compute_digest()})

    def verify(self, pinned_sha256: str) -> None:
        if self.compute_digest() != self.artifact_sha256:
            raise ArtifactMismatch("the calibration artifact was changed after sealing")
        if pinned_sha256 != self.artifact_sha256:
            raise ArtifactMismatch("the calibration artifact isn't the pinned one")

    def calibrate(self, raw: np.ndarray) -> np.ndarray:
        if self.calibrator is None:
            raise ArtifactMismatch("an ineligible artifact has no calibrator")
        return apply_calibrator(self.calibrator, raw)


def load_artifact(path: Path, *, pinned_sha256: str) -> CalibrationArtifact:
    artifact = CalibrationArtifact.model_validate_json(Path(path).read_text())
    artifact.verify(pinned_sha256)
    return artifact


def _folds(n: int, k: int) -> np.ndarray:
    return np.arange(n) % k


def fit_calibration(
    labels: Sequence["ApprovalLabelV1"],
    *,
    group_key: str,
    target_miss: float = DEFAULT_TARGET_MISS,
    delta: float = DEFAULT_DELTA,
    min_labels: int = MIN_LABELS,
    window: int = DEFAULT_WINDOW,
    isotonic_min_labels: int = ISOTONIC_MIN_LABELS,
    t_high: float = 0.5,
    folds: int = 5,
    now: Optional[datetime] = None,
) -> CalibrationArtifact:
    """Fit one group's artifact from its most recent ``window`` labels."""

    usable = [
        label
        for label in labels
        if label.group_key == group_key and label.bucket != "halted"
    ]
    usable.sort(key=lambda label: label.recorded_at)
    usable = usable[-window:]
    digest = hashlib.sha256(
        "\n".join(label.digest() for label in usable).encode()
    ).hexdigest()
    declined = np.array([label.decision == "declined" for label in usable], bool)
    bad = np.array([label.is_bad() for label in usable], bool)
    base: dict[str, Any] = dict(
        group_key=group_key,
        target_miss=target_miss,
        delta=delta,
        labels_total=len(usable),
        labels_bad=int(bad.sum()),
        labels_declined=int(declined.sum()),
        window=window,
        labels_sha256=digest,
        created_at=(now or datetime.now(timezone.utc)).isoformat(),
        t_high=t_high,
    )
    if len(usable) < min_labels:
        return CalibrationArtifact(
            eligible=False, ineligible_reason="too_few_labels", **base
        ).sealed()
    if int(bad.sum()) < min_bad_labels(target_miss, delta):
        return CalibrationArtifact(
            eligible=False, ineligible_reason="too_few_bad_labels", **base
        ).sealed()

    X = np.vstack([vectorize(label.features) for label in usable])
    fold = _folds(len(usable), folds)
    oof = np.zeros(len(usable))
    for f in range(folds):
        test = fold == f
        params = fit_logistic(X[~test], declined[~test])
        oof[test] = predict_raw(params, X[test])
    if len(usable) >= isotonic_min_labels:
        calibrator = fit_isotonic(oof, declined)
    else:
        calibrator = fit_platt(oof, declined)
    calibrated = apply_calibrator(calibrator, oof)
    t_low = ltt_threshold(calibrated, bad, target_miss=target_miss, delta=delta)
    # Quality numbers with the calibrator itself held out: fit it on one half
    # of the out-of-fold scores and score the other half, both ways.
    held_out = np.zeros(len(usable))
    half = np.arange(len(usable)) % 2 == 0
    fit_cal = fit_isotonic if len(usable) >= isotonic_min_labels else fit_platt
    for part in (half, ~half):
        held_out[~part] = apply_calibrator(
            fit_cal(oof[part], declined[part]), oof[~part]
        )
    model = fit_logistic(X, declined)
    artifact = CalibrationArtifact(
        eligible=t_low > 0.0,
        ineligible_reason=None if t_low > 0.0 else "no_threshold",
        model=model,
        calibrator=calibrator,
        t_low=min(t_low, t_high),
        oof_brier=brier(held_out, declined),
        oof_ece=expected_calibration_error(held_out, declined),
        **base,
    )
    return artifact.sealed()


class MissEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Estimated share of bad saves that triage called clean.
    miss_rate: Optional[float]
    bad_caught: int
    bad_found_in_audit: int
    audited: int
    estimated_bad_missed: float


def estimate_miss_rate(labels: Sequence["ApprovalLabelV1"]) -> MissEstimate:
    """Estimate how many bad saves the batch let through, from the audit.

    Only the audit sample sees clean saves one at a time, so each bad save it
    finds stands for ``1 / inclusion_probability`` clean saves.
    """

    caught = sum(
        1
        for label in labels
        if label.bucket in ("flagged", "abstain") and label.is_bad()
    )
    audit = [label for label in labels if label.bucket == "audit"]
    found = [label for label in audit if label.is_bad()]
    missed = float(sum(1.0 / label.inclusion_probability for label in found))
    total = caught + missed
    return MissEstimate(
        miss_rate=(missed / total) if total > 0 else None,
        bad_caught=caught,
        bad_found_in_audit=len(found),
        audited=len(audit),
        estimated_bad_missed=missed,
    )


__all__ = [
    "ARTIFACT_SCHEMA",
    "ArtifactMismatch",
    "CalibrationArtifact",
    "CalibratorParams",
    "MissEstimate",
    "apply_calibrator",
    "binom_cdf",
    "brier",
    "estimate_miss_rate",
    "expected_calibration_error",
    "fit_calibration",
    "fit_isotonic",
    "fit_platt",
    "load_artifact",
    "ltt_threshold",
    "min_bad_labels",
]
