"""``openadapt-flow triage``: order verified saves for review, record answers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from openadapt_flow.approval_mode.model import DECLINE_REASONS


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _refuse(code: str, message: str) -> int:
    print(json.dumps({"ok": False, "code": code, "message": message}))
    return 2


def _approver(args: argparse.Namespace) -> tuple[str | None, str] | None:
    if bool(args.unauthenticated) == bool(args.approver_id):
        return None
    if args.unauthenticated:
        return None, "none"
    if args.auth_method not in ("cloud_aal2", "cloud_aal1"):
        return None
    return args.approver_id, args.auth_method


def _cmd_plan(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.calibration import CalibrationArtifact
    from openadapt_flow.decision.features import (
        SaveObservation,
        observation_from_report,
    )
    from openadapt_flow.decision.triage import TriageConfig, build_plan, reviewer_view

    observations: list[SaveObservation] = []
    if args.observations:
        for line in Path(args.observations).read_text(encoding="utf-8").splitlines():
            if line.strip():
                observations.append(SaveObservation.model_validate_json(line))
    for run_dir in args.run_dir or []:
        from openadapt_flow.ir import RunReport

        path = Path(run_dir)
        report = RunReport.model_validate_json(
            (path / "report.json").read_text(encoding="utf-8")
        )
        observations.append(observation_from_report(report, run_dir=path))
    artifacts: dict[str, CalibrationArtifact] = {}
    if args.artifact:
        artifact = CalibrationArtifact.model_validate_json(
            Path(args.artifact).read_text(encoding="utf-8")
        )
        artifacts[artifact.group_key] = artifact
    config = TriageConfig(
        policy=args.policy,
        audit_rate=args.audit_rate,
        pinned_artifact_sha256=args.artifact_sha256,
    )
    plan = build_plan(observations, mode=args.mode, config=config, artifacts=artifacts)
    _write_private(Path(args.out), plan.model_dump_json(indent=2))
    _emit(reviewer_view(plan))
    return 0


def _load_plan(path: str):
    from openadapt_flow.decision.triage import TriagePlan

    return TriagePlan.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _cmd_approve_batch(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.labels import LabelLog
    from openadapt_flow.decision.review import ReviewRefused, approve_batch

    approver = _approver(args)
    if approver is None:
        return _refuse(
            "approver_invalid",
            "pass --approver-id with --auth-method, or --unauthenticated",
        )
    try:
        result = approve_batch(
            _load_plan(args.plan),
            LabelLog(Path(args.labels)),
            batch_sha256=args.batch_sha256,
            decided_by=args.decided_by,
            approver_id=approver[0],
            auth_method=approver[1],
            pull_back=args.pull_back or [],
        )
    except ReviewRefused as exc:
        return _refuse(exc.code, str(exc))
    _emit({"ok": True, **result.model_dump()})
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.labels import LabelLog
    from openadapt_flow.decision.review import ReviewRefused, decide_item

    approver = _approver(args)
    if approver is None:
        return _refuse(
            "approver_invalid",
            "pass --approver-id with --auth-method, or --unauthenticated",
        )
    try:
        label = decide_item(
            _load_plan(args.plan),
            LabelLog(Path(args.labels)),
            item_id=args.item,
            decision="declined" if args.decline else "approved",
            decline_reason=args.decline,
            decided_by=args.decided_by,
            approver_id=approver[0],
            auth_method=approver[1],
        )
    except ReviewRefused as exc:
        return _refuse(exc.code, str(exc))
    _emit({"ok": True, "item_id": label.item_id, "label_sha256": label.digest()})
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.calibration import fit_calibration
    from openadapt_flow.decision.labels import LabelLog

    artifact = fit_calibration(
        LabelLog(Path(args.labels)).read(),
        group_key=args.group,
        target_miss=args.target_miss,
        delta=args.delta,
        window=args.window,
    )
    _write_private(Path(args.out), artifact.model_dump_json(indent=2))
    _emit(
        {
            "ok": True,
            "artifact_sha256": artifact.artifact_sha256,
            "eligible": artifact.eligible,
            "ineligible_reason": artifact.ineligible_reason,
            "labels_total": artifact.labels_total,
            "labels_bad": artifact.labels_bad,
            "t_low": artifact.t_low,
        }
    )
    return 0


def _cmd_estimate(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.calibration import estimate_miss_rate
    from openadapt_flow.decision.labels import LabelLog

    _emit(estimate_miss_rate(LabelLog(Path(args.labels)).read()).model_dump())
    return 0


def _cmd_eligibility(args: argparse.Namespace) -> int:
    from openadapt_flow.decision.calibration import CalibrationArtifact
    from openadapt_flow.decision.eligibility import (
        EligibilityUnavailable,
        TriageEligibilityProvider,
    )
    from openadapt_flow.decision.labels import LabelLog

    try:
        artifact = CalibrationArtifact.model_validate_json(
            Path(args.artifact).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        return _refuse("calibration_artifact_unreadable", str(exc))
    provider = TriageEligibilityProvider(
        artifacts={args.workflow: (artifact, args.artifact_sha256)},
        labels=LabelLog(Path(args.labels)).read(),
        verified_runs={args.workflow: args.verified_runs},
        target_miss_rate=args.target_miss,
        delta=args.delta,
        audit_fraction=args.audit_rate,
    )
    try:
        result = provider.eligibility(args.workflow)
    except EligibilityUnavailable as exc:
        return _refuse(exc.code, str(exc))
    _emit({"ok": True, "eligible": result.eligible, **result.model_dump(mode="json")})
    return 0 if result.eligible else 3


def _approver_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--labels", required=True, help="Label log (JSON lines)")
    p.add_argument("--decided-by", required=True, help="Must be 'human'")
    p.add_argument("--approver-id", help="The reviewer's Cloud user id")
    p.add_argument("--auth-method", choices=("cloud_aal2", "cloud_aal1"))
    p.add_argument("--unauthenticated", action="store_true")


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "triage",
        help="Order verified saves for review and propose one batch approval",
    )
    tsub = p.add_subparsers(dest="triage_cmd", required=True)

    plan = tsub.add_parser("plan", help="Write a triage plan; print the review view")
    plan.add_argument("--observations", help="SaveObservation JSON lines")
    plan.add_argument("--run-dir", action="append", help="A finished run (repeatable)")
    plan.add_argument(
        "--mode",
        required=True,
        choices=("each_item", "problems_only", "not_gated"),
        help="The approval mode the runs used. Only problems_only is triaged.",
    )
    plan.add_argument(
        "--policy", default="rule_v0", choices=("off", "rule_v0", "learned")
    )
    plan.add_argument("--artifact", help="Calibration artifact (learned only)")
    plan.add_argument("--artifact-sha256", help="Pinned artifact digest")
    plan.add_argument("--audit-rate", type=float, default=0.1)
    plan.add_argument("--out", required=True, help="Where to write the plan")
    plan.set_defaults(func=_cmd_plan)

    batch = tsub.add_parser("approve-batch", help="Approve a plan's clean batch")
    batch.add_argument("plan")
    batch.add_argument("--batch-sha256", required=True)
    batch.add_argument(
        "--pull-back", action="append", metavar="ITEM_ID", help="Review this one alone"
    )
    _approver_args(batch)
    batch.set_defaults(func=_cmd_approve_batch)

    decide = tsub.add_parser("decide", help="Approve or decline one save")
    decide.add_argument("plan")
    decide.add_argument("--item", required=True)
    group = decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--decline", choices=DECLINE_REASONS)
    _approver_args(decide)
    decide.set_defaults(func=_cmd_decide)

    cal = tsub.add_parser("calibrate", help="Fit a calibration artifact from labels")
    cal.add_argument("--labels", required=True)
    cal.add_argument("--group", required=True, help="Group key (64 hex)")
    cal.add_argument("--target-miss", type=float, default=0.05)
    cal.add_argument("--delta", type=float, default=0.10)
    cal.add_argument("--window", type=int, default=2000)
    cal.add_argument("--out", required=True)
    cal.set_defaults(func=_cmd_calibrate)

    est = tsub.add_parser("estimate", help="Estimate the miss rate from audit labels")
    est.add_argument("--labels", required=True)
    est.set_defaults(func=_cmd_estimate)

    elig = tsub.add_parser(
        "eligibility",
        help=(
            "Check whether a workflow may switch to only asking about problems: "
            "a pinned, eligible calibration artifact and an audit miss-rate "
            "bound under the target. Exit 0 eligible, 3 not yet, 2 no evidence."
        ),
    )
    elig.add_argument("--workflow", required=True, help="Opaque workflow id")
    elig.add_argument("--artifact", required=True, help="Calibration artifact")
    elig.add_argument("--artifact-sha256", required=True, help="Pinned digest")
    elig.add_argument("--labels", required=True, help="Label log (JSON lines)")
    elig.add_argument("--verified-runs", type=int, default=0)
    elig.add_argument("--target-miss", type=float, default=0.05)
    elig.add_argument("--delta", type=float, default=0.10)
    elig.add_argument("--audit-rate", type=float, default=0.1)
    elig.set_defaults(func=_cmd_eligibility)


__all__ = ["add_parser"]
