"""CLI handlers for openadapt-flow compose and composition certify/run."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import (
    Composition,
    CompositionError,
    HandoffBinding,
    child_bundle_path,
    is_composition_artifact,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.composition import (
    ChildRunResult,
    execute_composition,
)


def parse_named_path(raw: str, *, flag: str) -> tuple[str, str]:
    if "=" not in raw:
        raise SystemExit(f"{flag} expects NAME=PATH, got {raw!r}")
    name, path = raw.split("=", 1)
    name, path = name.strip(), path.strip()
    if not name or not path:
        raise SystemExit(f"{flag} expects NAME=PATH, got {raw!r}")
    return name, path


def parse_handoff(raw: str) -> HandoffBinding:
    """Parse FROM.source=TO.target."""

    if "=" not in raw:
        raise SystemExit(f"--handoff expects FROM.source=TO.target, got {raw!r}")
    left, right = raw.split("=", 1)
    if "." not in left or "." not in right:
        raise SystemExit(f"--handoff expects FROM.source=TO.target, got {raw!r}")
    from_child, source = left.split(".", 1)
    to_child, target = right.split(".", 1)
    from_child, source = from_child.strip(), source.strip()
    to_child, target = to_child.strip(), target.strip()
    if not all((from_child, source, to_child, target)):
        raise SystemExit(f"--handoff expects FROM.source=TO.target, got {raw!r}")
    return HandoffBinding(
        from_child=from_child,
        source=source,
        to_child=to_child,
        target=target,
    )


def cmd_compose(args: argparse.Namespace) -> int:
    children_raw = list(args.child or [])
    if len(children_raw) < 2:
        raise SystemExit("compose requires at least two --child NAME=PATH flags")
    seen: set[str] = set()
    children: list[tuple[str, Path]] = []
    for raw in children_raw:
        name, path = parse_named_path(raw, flag="--child")
        if name in seen:
            raise SystemExit(f"--child name {name!r} given more than once")
        seen.add(name)
        children.append((name, Path(path)))

    handoffs = [parse_handoff(raw) for raw in (args.handoff or [])]
    after: dict[str, list[str]] = {}
    for raw in args.after or []:
        name, preds = parse_named_path(raw, flag="--after")
        after[name] = [item.strip() for item in preds.split(",") if item.strip()]
    allowed: dict[str, list[str]] = {}
    for raw in args.allow_halt or []:
        name, outcome = parse_named_path(raw, flag="--allow-halt")
        allowed.setdefault(name, []).append(outcome)

    try:
        composition = author_composition(
            children,
            handoffs=handoffs,
            after=after or None,
            allowed_halt_classes=allowed or None,
            name=args.name,
            out=Path(args.out),
        )
    except CompositionError as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"Authored composition at {args.out} (name: {composition.name!r}): "
        f"{len(composition.children)} child bundle(s), "
        f"{len(composition.handoffs)} handoff(s). "
        "certify/run this directory; each child keeps its recorded surface."
    )
    return 0


def cmd_certify_composition(args: argparse.Namespace) -> int:
    from openadapt_flow.policy import evaluate_policy, load_policy

    parent = Path(args.bundle)
    composition = Composition.load(parent)
    policy_source = args.policy
    if policy_source is None and getattr(args, "config", None):
        from openadapt_flow.deployment import load_deployment

        policy_source = load_deployment(args.config).policy.policy
    if policy_source is None:
        raise SystemExit(
            "certify needs a policy: pass --policy or set policy.policy in --config."
        )
    try:
        policy = load_policy(policy_source)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    child_results: dict[str, bool] = {}
    all_passed = True
    for spec in composition.children:
        workflow = Workflow.load(child_bundle_path(parent, spec))
        report = evaluate_policy(workflow, policy)
        print(f"child {spec.name} ({spec.surface or 'unbound surface'}):")
        print(report.render())
        child_results[spec.name] = report.passed
        all_passed = all_passed and report.passed

    composition.provenance.certified = all_passed
    composition.provenance.policy_name = str(policy_source)
    composition.provenance.certified_at = datetime.now(timezone.utc).isoformat()
    composition.provenance.child_results = child_results
    composition.save(parent)
    if all_passed:
        print(f"Composition certified: {parent}")
        return 0
    print(f"Composition REFUSED: a child failed policy {policy_source}")
    return 2


def _governed_child_run(parent_args: argparse.Namespace, run_child):
    """Bind compose execute() to governed run (not raw replay)."""

    def child_run(
        capability: object | None,
        admission: object | None,
        inputs: Mapping[str, str],
        *,
        workflow: Workflow,
        bundle_dir: Path,
        run_dir: Path,
        child: str,
    ) -> ChildRunResult:
        del capability, admission, workflow
        child_args = copy.copy(parent_args)
        child_args.bundle = str(bundle_dir)
        child_args.run_dir = str(run_dir)
        child_args.param = [f"{key}={value}" for key, value in inputs.items()]
        child_args.params_file = None
        child_args.command = "run"
        child_args.dry_run = False
        child_args.explain = False
        for attr in (
            "_surface_selection_done",
            "_surface_binding_done",
            "_governed_run_authorization",
            "_managed_dispatch_binding",
            "_delivery_authority_kind",
            "_remote_delivery_run_id",
            "_qualification_case_execution",
            "_qualification_run_id",
            "_production_qualification_guard",
            "_qualification_campaign_guard",
            "_surface_override",
            "_execution_profile",
        ):
            if hasattr(child_args, attr):
                delattr(child_args, attr)
        rc = run_child(child_args)
        report_path = Path(run_dir) / "report.json"
        bound = {str(key): str(value) for key, value in inputs.items()}
        outcome = "FAILED"
        model_calls = 0
        success = False
        if report_path.is_file():
            from openadapt_flow.ir import RunReport

            report = RunReport.model_validate_json(report_path.read_text())
            outcome = report.execution_outcome or (
                "VERIFIED" if report.success else "HALTED"
            )
            model_calls = report.model_calls
            success = bool(report.success)
        elif rc == 2:
            outcome = "FAILED"
        return ChildRunResult(
            child=child,
            outcome=outcome,
            bound_params=bound,
            effect_facts=dict(bound),
            model_calls=model_calls,
            success=success,
            report_path=str(report_path) if report_path.is_file() else None,
        )

    return child_run


def cmd_run_composition(args: argparse.Namespace, *, run_child) -> int:
    parent = Path(args.bundle)
    composition = Composition.load(parent)
    run_dir = (
        Path(args.run_dir)
        if getattr(args, "run_dir", None)
        else Path("runs") / "compose"
    )
    from openadapt_flow.__main__ import _replay_params

    inputs = _replay_params(
        getattr(args, "param", None), getattr(args, "params_file", None)
    )
    report = execute_composition(
        composition,
        parent_dir=parent,
        run_dir=run_dir,
        inputs={str(k): str(v) for k, v in inputs.items()},
        child_run=_governed_child_run(args, run_child),
        capability=None,
        admission=None,
    )
    print(f"Compose {report.outcome}: {run_dir / 'composition-report.json'}")
    if report.reason:
        print(report.reason)
    if getattr(args, "profile", None) in {"standard", "regulated"}:
        return 0 if report.outcome == "VERIFIED" else 1
    return 0 if report.success else 1


def refuse_replay_composition(bundle: Path) -> None:
    if is_composition_artifact(bundle):
        raise SystemExit(
            "replay refuses a composition artifact; use "
            "`openadapt-flow run` (the governed path). Raw replay is not "
            "the public primitive for composed children."
        )
