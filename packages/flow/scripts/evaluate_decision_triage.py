#!/usr/bin/env python3
"""Evaluate save triage on synthetic saves with known faults.

Reports, for per-item review (today's ``each_item``), the v0 rule and the
learned score:

- human reviews per verified save, counting each batch approval as one
  review and each per-item save as one;
- bad saves (``wrong_values`` or ``wrong_record``) that ended in a batch,
  per verified save, per batch save, and per bad save;
- for the learned score, Brier score and expected calibration error on the
  test saves, and the reviews per save at the best threshold that keeps bad
  batch saves at or under 0.5% of the batch (chosen on the test set, so an
  upper bound on what the method can do);
- the audit sample's inverse-probability estimate of the miss rate next to
  the true one.

The data is synthetic (``openadapt_flow.decision.synthetic``). The numbers
show how the mechanism behaves on that data, not on a real workflow.

    python scripts/evaluate_decision_triage.py --out results.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from openadapt_flow.decision.calibration import (
    brier,
    estimate_miss_rate,
    expected_calibration_error,
    fit_calibration,
)
from openadapt_flow.decision.feature_model import predict_raw, vectorize
from openadapt_flow.decision.features import extract_features
from openadapt_flow.decision.labels import ApprovalLabelV1
from openadapt_flow.decision.rules import HARD_REASONS, rule_v0
from openadapt_flow.decision.synthetic import GROUP, SyntheticSave, generate
from openadapt_flow.decision.triage import TriageConfig, TriagePlan, build_plan

BAD = ("wrong_values", "wrong_record")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _label(item: Any, plan: TriagePlan, truth: str, index: int) -> ApprovalLabelV1:
    return ApprovalLabelV1(
        label_id=uuid.uuid4().hex,
        plan_id=plan.plan_id,
        item_id=item.item_id,
        group_key=item.group_key,
        observation_sha256=item.observation_sha256,
        decision="approved" if truth == "approved" else "declined",
        decline_reason=None if truth == "approved" else truth,  # type: ignore[arg-type]
        bucket=item.bucket,
        route="review_item",
        inclusion_probability=item.inclusion_probability,
        features=item.features,
        reasons=item.reasons,
        score=item.score,
        approver_kind="unauthenticated",
        auth_method="none",
        recorded_at=(T0 + timedelta(seconds=index)).isoformat(),
    )


def _plans(saves: list[SyntheticSave], size: int, **kwargs: Any) -> list[TriagePlan]:
    return [
        build_plan([s.observation for s in saves[i : i + size]], **kwargs)
        for i in range(0, len(saves), size)
    ]


def _score(plans: list[TriagePlan], truth: dict[str, str]) -> dict[str, Any]:
    verified = sum(len(p.per_item) + len(p.batch) for p in plans)
    per_item = sum(len(p.per_item) for p in plans)
    batches = sum(1 for p in plans if p.batch)
    batch_items = sum(len(p.batch) for p in plans)
    bad_total = sum(
        1 for p in plans for i in (*p.per_item, *p.batch) if truth[i.item_id] in BAD
    )
    bad_batch = sum(1 for p in plans for i in p.batch if truth[i.item_id] in BAD)
    labels = [
        _label(item, p, truth[item.item_id], n)
        for p in plans
        for n, item in enumerate(p.per_item)
    ]
    estimate = estimate_miss_rate(labels)
    return {
        "verified_saves": verified,
        "per_item_reviews": per_item,
        "batch_approvals": batches,
        "batch_saves": batch_items,
        "reviews_per_verified_save": (per_item + batches) / verified
        if verified
        else None,
        "bad_saves": bad_total,
        "bad_in_batch": bad_batch,
        "miss_per_verified_save": bad_batch / verified if verified else None,
        "miss_per_batch_save": bad_batch / batch_items if batch_items else 0.0,
        "miss_per_bad_save": bad_batch / bad_total if bad_total else 0.0,
        "audit_estimated_miss_per_bad_save": estimate.miss_rate,
        "audit_saves": estimate.audited,
        "halted": sum(len(p.halted) for p in plans),
    }


def _oracle_sweep(
    saves: list[SyntheticSave], artifact: Any, size: int, max_contamination: float
) -> dict[str, Any]:
    rows = [s for s in saves if not s.observation.halted()]
    feats = [extract_features(s.observation).values for s in rows]
    hard = np.array([any(r in HARD_REASONS for r in rule_v0(f)) for f in feats])
    raw = predict_raw(artifact.model, np.vstack([vectorize(f) for f in feats]))
    p = artifact.calibrate(raw)
    bad = np.array([s.bad for s in rows])
    best: dict[str, Any] = {"threshold": 0.0, "reviews_per_verified_save": 1.0}
    for t in np.unique(p):
        clean = (~hard) & (p < t)
        n_clean = int(clean.sum())
        if not n_clean:
            continue
        contamination = float((clean & bad).sum()) / n_clean
        if contamination > max_contamination:
            continue
        lists = int(np.ceil(len(rows) / size))
        reviews = (len(rows) - n_clean + lists) / len(rows)
        if reviews < best["reviews_per_verified_save"]:
            best = {
                "threshold": float(t),
                "reviews_per_verified_save": reviews,
                "miss_per_batch_save": contamination,
                "miss_per_verified_save": float((clean & bad).sum()) / len(rows),
            }
    best["brier"] = brier(p, bad | np.array([s.truth != "approved" for s in rows]))
    best["ece"] = expected_calibration_error(
        p, np.array([s.truth != "approved" for s in rows])
    )
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--train", type=int, default=8000)
    parser.add_argument("--test", type=int, default=5000)
    parser.add_argument("--list-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--audit-rate", type=float, default=0.1)
    parser.add_argument("--max-contamination", type=float, default=0.005)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    train = generate(args.train, seed=args.seed)
    last = train[-1].observation
    test = generate(
        args.test,
        seed=args.seed + 1,
        history=last.history,
        recent=[s.truth for s in train if not s.observation.halted()][-20:],
    )
    truth = {s.observation.item_id: s.truth for s in (*train, *test)}

    each_item = _plans(test, args.list_size, mode="each_item")
    rule = _plans(
        test,
        args.list_size,
        mode="problems_only",
        config=TriageConfig(policy="rule_v0", audit_rate=args.audit_rate),
    )

    # Labels as if every training save had been reviewed one at a time.
    train_plans = _plans(train, args.list_size, mode="each_item")
    labels = [
        _label(item, p, truth[item.item_id], k * 1000 + n)
        for k, p in enumerate(train_plans)
        for n, item in enumerate(p.per_item)
    ]
    artifact = fit_calibration(labels, group_key=GROUP, window=len(labels))
    learned = _plans(
        test,
        args.list_size,
        mode="problems_only",
        config=TriageConfig(
            policy="learned",
            audit_rate=args.audit_rate,
            pinned_artifact_sha256=artifact.artifact_sha256,
        ),
        artifacts={GROUP: artifact},
    )
    result: dict[str, Any] = {
        "data": "synthetic",
        "seed": args.seed,
        "train_saves": args.train,
        "test_saves": args.test,
        "list_size": args.list_size,
        "audit_rate": args.audit_rate,
        "each_item": _score(each_item, truth),
        "rule_v0": _score(rule, truth),
        "artifact": {
            "eligible": artifact.eligible,
            "ineligible_reason": artifact.ineligible_reason,
            "labels_total": artifact.labels_total,
            "labels_bad": artifact.labels_bad,
            "t_low": artifact.t_low,
            "calibrator": artifact.calibrator.method if artifact.calibrator else None,
            "oof_brier": artifact.oof_brier,
            "oof_ece": artifact.oof_ece,
        },
        "learned": _score(learned, truth),
    }
    if artifact.eligible:
        result["learned_oracle_at_max_contamination"] = {
            "max_contamination": args.max_contamination,
            **_oracle_sweep(test, artifact, args.list_size, args.max_contamination),
        }
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
