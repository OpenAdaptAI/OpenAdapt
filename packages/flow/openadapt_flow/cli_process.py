"""CLI handlers for openadapt-flow process and process certify/run/replay."""

from __future__ import annotations

import argparse
import copy
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from openadapt_flow.admitted_composition import (
    ProcessContract,
    ProcessContractError,
    author_process_contract,
    is_process_contract_artifact,
    resolve_pointer,
)
from openadapt_flow.cli_compose import parse_handoff, parse_named_path
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification_admission import (
    QualificationAdmissionEnvelope,
    load_qualification_signer_trust,
)
from openadapt_flow.runtime.admitted_composition import (
    AdmittedCapability,
    AdmittedChildExecutor,
    child_run_via_execute_client,
    execute_process_contract,
)
from openadapt_flow.runtime.composition import ChildRunResult


def _load_trust(parent: Path, args: argparse.Namespace) -> dict:
    explicit = getattr(args, "qualification_trust", None)
    env = os.environ.get("OPENADAPT_QUALIFICATION_TRUST")
    sibling = parent / "qualification-trust.json"
    path: Path | None = None
    if explicit:
        path = Path(str(explicit))
    elif env:
        path = Path(env)
    elif sibling.is_file():
        path = sibling
    if path is None or not path.is_file():
        raise SystemExit(
            "process run needs a qualification signer trust registry: pass "
            "--qualification-trust, set OPENADAPT_QUALIFICATION_TRUST, or "
            "place qualification-trust.json beside process-contract.json. "
            "Do not use a test key."
        )
    try:
        return load_qualification_signer_trust(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(
            f"qualification signer trust registry is invalid: {exc}"
        ) from exc


def cmd_process(args: argparse.Namespace) -> int:
    children_raw = list(getattr(args, "child", None) or [])
    admissions_raw = list(getattr(args, "admission", None) or [])
    if len(children_raw) < 2:
        raise SystemExit("process requires at least two --child NAME=BUNDLE flags")
    seen: set[str] = set()
    bundles: dict[str, Path] = {}
    for raw in children_raw:
        name, path = parse_named_path(raw, flag="--child")
        if name in seen:
            raise SystemExit(f"--child name {name!r} given more than once")
        seen.add(name)
        bundles[name] = Path(path)
    admissions: dict[str, Path] = {}
    for raw in admissions_raw:
        name, path = parse_named_path(raw, flag="--admission")
        if name in admissions:
            raise SystemExit(f"--admission name {name!r} given more than once")
        admissions[name] = Path(path)
    missing = [name for name in bundles if name not in admissions]
    extra = [name for name in admissions if name not in bundles]
    if missing or extra:
        raise SystemExit(
            "process requires --admission NAME=ENVELOPE for each --child. "
            "A compiled recording (including a compose child under "
            "composition.json) is not an independently admitted capability. "
            f"Missing admission: {missing or 'none'}; extra: {extra or 'none'}."
        )
    children = [(name, admissions[name], bundles[name]) for name in bundles]
    handoffs = [parse_handoff(raw) for raw in (getattr(args, "handoff", None) or [])]
    after: dict[str, list[str]] = {}
    for raw in getattr(args, "after", None) or []:
        name, preds = parse_named_path(raw, flag="--after")
        after[name] = [item.strip() for item in preds.split(",") if item.strip()]
    allowed: dict[str, list[str]] = {}
    for raw in getattr(args, "allow_halt", None) or []:
        name, outcome = parse_named_path(raw, flag="--allow-halt")
        allowed.setdefault(name, []).append(outcome)
    inputs = list(getattr(args, "input", None) or [])

    try:
        contract = author_process_contract(
            children,
            handoffs=handoffs,
            after=after or None,
            allow_halt=allowed or None,
            inputs=inputs or None,
            name=getattr(args, "name", None),
            out=Path(args.out),
        )
    except ProcessContractError as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"Authored process contract at {args.out} (name: {contract.name!r}): "
        f"{len(contract.children)} admitted child(ren), "
        f"{len(contract.handoffs)} handoff(s). "
        "certify/run this directory; replay refuses it. "
        "Each child keeps its own QualificationAdmissionEnvelope."
    )
    return 0


def cmd_certify_process(args: argparse.Namespace) -> int:
    from openadapt_flow.policy import evaluate_policy, load_policy

    parent = Path(args.bundle)
    contract = ProcessContract.load(parent)
    policy_source = getattr(args, "policy", None)
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
    for spec in contract.children:
        try:
            envelope = QualificationAdmissionEnvelope.model_validate_json(
                resolve_pointer(parent, spec.envelope).read_text(encoding="utf-8")
            )
        except Exception as exc:
            print(
                f"child {spec.name}: REFUSED (no valid QualificationAdmissionEnvelope: "
                f"{type(exc).__name__})"
            )
            child_results[spec.name] = False
            all_passed = False
            continue
        if (
            envelope.payload.admission_id != spec.admission_id
            or envelope.payload.bundle_content_digest != spec.bundle_content_digest
        ):
            print(
                f"child {spec.name}: REFUSED (envelope does not match parent pointer)"
            )
            child_results[spec.name] = False
            all_passed = False
            continue
        workflow = Workflow.load(resolve_pointer(parent, spec.bundle))
        report = evaluate_policy(workflow, policy)
        print(f"child {spec.name} ({spec.surface or 'unbound surface'}):")
        print(report.render())
        child_results[spec.name] = report.passed
        all_passed = all_passed and report.passed

    if all_passed:
        print(f"Process contract certified: {parent}")
        return 0
    print(f"Process contract REFUSED: a child failed policy {policy_source}")
    return 2


def _governed_child_run(
    parent_args: argparse.Namespace, run_child
) -> AdmittedChildExecutor:
    """Bind process execute() to governed run (not raw replay)."""

    def child_run(
        capability: AdmittedCapability,
        admission: QualificationAdmissionEnvelope,
        inputs: Mapping[str, str],
        *,
        workflow: Workflow,
        bundle_dir: Path,
        run_dir: Path,
        child: str,
    ) -> ChildRunResult:
        del workflow
        child_args = copy.copy(parent_args)
        child_args.bundle = str(bundle_dir)
        child_args.run_dir = str(run_dir)
        child_args.param = [f"{key}={value}" for key, value in inputs.items()]
        child_args.params_file = None
        child_args.command = "run"
        child_args.dry_run = False
        child_args.explain = False
        # Keep the admission object on the child args so a later Execute bind
        # can read it. Do not drop it the way compose currently does.
        child_args._process_capability = capability
        child_args._process_admission = admission
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

    child_run.execute_via = "governed_run"  # type: ignore[attr-defined]
    return child_run


def _execute_env(args: argparse.Namespace, name: str) -> str:
    attr = name.lower()
    explicit = getattr(args, attr, None)
    if explicit:
        return str(explicit)
    return os.environ.get(f"OPENADAPT_{name}", "") or ""


def bind_process_child_run(
    args: argparse.Namespace, run_child
) -> AdmittedChildExecutor:
    """Cloud ExecuteClient when provisioned; otherwise governed local run."""

    execute_url = _execute_env(args, "EXECUTE_URL")
    execute_token = _execute_env(args, "EXECUTE_TOKEN")
    if execute_url or execute_token:
        if not execute_url or not execute_token:
            raise SystemExit(
                "process Cloud Execute needs both OPENADAPT_EXECUTE_URL "
                "(or --execute-url) and OPENADAPT_EXECUTE_TOKEN"
            )
        environment_id = _execute_env(args, "EXECUTE_ENVIRONMENT_ID")
        if not environment_id:
            raise SystemExit(
                "process Cloud Execute needs OPENADAPT_EXECUTE_ENVIRONMENT_ID "
                "(or --execute-environment-id)"
            )
        try:
            from openadapt_types.execute_client import ExecuteClient
        except ImportError as exc:
            raise SystemExit(
                "process Cloud Execute requires openadapt-types "
                "(install openadapt-flow[interop])"
            ) from exc
        actor_id = _execute_env(args, "EXECUTE_ACTOR_ID") or "process-parent"
        return child_run_via_execute_client(
            ExecuteClient(execute_url, execute_token),
            environment_id=environment_id,
            actor_id=actor_id,
        )
    return _governed_child_run(args, run_child)


def cmd_run_process(args: argparse.Namespace, *, run_child) -> int:
    parent = Path(args.bundle)
    contract = ProcessContract.load(parent)
    run_dir = (
        Path(args.run_dir)
        if getattr(args, "run_dir", None)
        else Path("runs") / "process"
    )
    from openadapt_flow.__main__ import _replay_params

    inputs = _replay_params(
        getattr(args, "param", None), getattr(args, "params_file", None)
    )
    revoked_raw = os.environ.get("OPENADAPT_REVOKED_ADMISSION_IDS", "")
    revoked = {item.strip() for item in revoked_raw.split(",") if item.strip()}
    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=run_dir,
        inputs={str(k): str(v) for k, v in inputs.items()},
        child_run=bind_process_child_run(args, run_child),
        trusted_signers=_load_trust(parent, args),
        revoked_admission_ids=revoked,
        now=datetime.now(timezone.utc),
    )
    print(f"Process {report.outcome}: {run_dir / 'process-report.json'}")
    if report.reason:
        print(report.reason)
    if getattr(args, "profile", None) in {"standard", "regulated"}:
        return 0 if report.outcome == "VERIFIED" else 1
    return 0 if report.success else 1


def refuse_replay_process(bundle: Path) -> None:
    if is_process_contract_artifact(bundle):
        raise SystemExit(
            "replay refuses a process contract; use "
            "`openadapt-flow run` (the governed path). Raw replay is not "
            "the public primitive for admitted children."
        )
