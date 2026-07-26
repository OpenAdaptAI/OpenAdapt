"""Implementation of the ``openadapt-flow repair`` subcommands.

Thin, printable drivers over :mod:`openadapt_flow.repair`. The parser wiring
lives in ``openadapt_flow.__main__``; every handler here takes the parsed
``argparse.Namespace`` and returns a process exit code. Refusals (a failed
gate, an illegal transition, a weakened contract) are printed and exit
nonzero; they never raise a traceback at the operator.
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Optional

from openadapt_flow.ir import Anchor, Point, RunReport, Workflow
from openadapt_flow.repair.campaign import (
    merge_campaign_results,
    run_fault_campaign,
    run_replay_campaign,
)
from openadapt_flow.repair.candidate import (
    Actor,
    CampaignResult,
    RepairCandidate,
)
from openadapt_flow.repair.lifecycle import (
    RepairLifecycleError,
    RepairStore,
    observation_from_report,
)
from openadapt_flow.repair.registration import (
    build_candidate,
    detached_candidate_path,
    load_detached_candidate,
    register_bundle_candidate,
)

DEFAULT_STORE = "repair-store"


def _store(args: argparse.Namespace) -> RepairStore:
    return RepairStore(Path(getattr(args, "store", None) or DEFAULT_STORE))


def _human(name: Optional[str]) -> Actor:
    return Actor(kind="human", id=name or getpass.getuser())


def _print_refusal(exc: Exception) -> int:
    print(f"REFUSED: {exc}")
    return 1


def cmd_register(args: argparse.Namespace) -> int:
    """Register a (prior, proposed) bundle pair as a lifecycle candidate."""
    store = _store(args)
    proposed = Path(args.proposed)
    if args.prior is None:
        try:
            candidate = load_detached_candidate(proposed)
        except FileNotFoundError as exc:
            print(f"cannot register: {exc}")
            return 2
    else:
        register_bundle_candidate(
            Path(args.prior),
            proposed,
            source=args.source,
            evidence_run_dir=Path(args.evidence) if args.evidence else None,
        )
        candidate = load_detached_candidate(proposed)
    candidate = store.add_candidate(candidate)
    print(candidate.diff_summary())
    print(f"\ncandidate {candidate.candidate_id} recorded in {store.root}")
    if candidate.state == "rejected":
        print(
            "\nThe candidate was REJECTED at creation (contract weakening "
            "without a new qualification revision); it can never advance."
        )
        return 1
    print(
        "\nNext: review the diff, run both campaigns, then approve:\n"
        f"  openadapt-flow repair show {candidate.candidate_id} --store {store.root}\n"
        f"  openadapt-flow repair review {candidate.candidate_id} "
        f"--reviewed-by <you> --store {store.root}"
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    candidates = store.list_candidates()
    if not candidates:
        print(f"no repair candidates in {store.root}")
        return 0
    for candidate in candidates:
        print(
            f"{candidate.candidate_id}  [{candidate.state:>13}]  "
            f"{candidate.source:<16} {candidate.workflow_name}  "
            f"{candidate.proposed_content_digest[:16]}..."
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.load_candidate(args.candidate_id)
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(candidate.diff_summary())
    if args.json:
        print()
        print(candidate.model_dump_json(indent=2))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.review(args.candidate_id, _human(args.reviewed_by))
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(
        f"candidate {candidate.candidate_id} reviewed by "
        f"{candidate.reviewed_by}; state: {candidate.state}"
    )
    return 0


def _campaign_inputs(
    candidate: RepairCandidate,
) -> list[tuple[str, Anchor, bytes, Optional[bytes]]]:
    """(step_id, repaired anchor, evidence frame, template crop) per step.

    Fails closed: refuses when the candidate carries no evidence run
    directory or no heal frame exists for a changed step.
    """
    if not candidate.evidence_run_dir:
        raise RepairLifecycleError(
            "refused (fail closed): candidate has no evidence run directory; "
            "campaigns need the heal evidence frames "
            "(run_dir/heals/<step>/screen.png)"
        )
    if not candidate.proposed_bundle_path:
        raise RepairLifecycleError(
            "refused (fail closed): candidate has no proposed bundle path"
        )
    run_dir = Path(candidate.evidence_run_dir)
    bundle = Path(candidate.proposed_bundle_path)
    workflow = Workflow.load(bundle)
    steps = {step.id: step for step in workflow.steps}
    changed_ids = sorted({change.step_id for change in candidate.binding_changes})
    inputs: list[tuple[str, Anchor, bytes, Optional[bytes]]] = []
    for step_id in changed_ids:
        step = steps.get(step_id)
        if step is None or step.anchor is None:
            continue
        frame_path = run_dir / "heals" / step_id / "screen.png"
        if not frame_path.is_file():
            raise RepairLifecycleError(
                f"refused (fail closed): no evidence frame for changed step "
                f"{step_id!r} at {frame_path}"
            )
        template_path = bundle / step.anchor.template
        template = template_path.read_bytes() if template_path.is_file() else None
        inputs.append((step_id, step.anchor, frame_path.read_bytes(), template))
    if not inputs:
        raise RepairLifecycleError(
            "refused (fail closed): the candidate has no changed anchored "
            "steps with evidence frames to campaign against"
        )
    return inputs


def _run_campaign(candidate: RepairCandidate, kind: str) -> CampaignResult:
    """Run one campaign with the production resolver + OCR wiring."""
    from openadapt_flow import vision
    from openadapt_flow.runtime import resolver as resolver_mod
    from openadapt_flow.runtime.healing.perturbation import band_sampler

    results: list[CampaignResult] = []
    for step_id, anchor, frame_png, template_png in _campaign_inputs(candidate):

        def resolve(
            png: bytes,
            _anchor: Anchor = anchor,
            _template: Optional[bytes] = template_png,
        ) -> Optional[Point]:
            resolved = resolver_mod.resolve(
                _anchor, png, vision, template_png=_template
            )
            return None if resolved is None else resolved[0].point

        viewport = resolver_mod.png_size(frame_png)
        sample_band = band_sampler(viewport, vision)
        if kind == "replay":

            def safe_resolve(png: bytes) -> Optional[Point]:
                try:
                    return resolve(png)
                except Exception:
                    # A refusal during the HEALTHY battery is a miss, not a
                    # crash: the case fails as "not located".
                    return None

            results.append(
                run_replay_campaign(
                    step_id,
                    anchor,
                    frame_png,
                    resolve=safe_resolve,
                    sample_band=sample_band,
                )
            )
        else:
            results.append(
                run_fault_campaign(
                    step_id,
                    anchor,
                    frame_png,
                    resolve=resolve,
                    sample_band=sample_band,
                )
            )
    return merge_campaign_results(results)


def cmd_campaign(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.load_candidate(args.candidate_id)
        result = _run_campaign(candidate, args.kind)
        candidate = store.record_campaign(
            args.candidate_id, result, Actor(kind="automation", id="repair-cli")
        )
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(result.summary())
    for case in result.cases:
        marker = "PASS" if case.passed else "FAIL"
        print(f"  [{marker}] {case.label}: {case.detail}")
    print(f"candidate state: {candidate.state}")
    return 0 if result.passed else 1


def cmd_approve(args: argparse.Namespace) -> int:
    store = _store(args)
    non_interactive = bool(args.non_interactive)
    if non_interactive and not args.approved_by:
        print(
            "REFUSED: --non-interactive requires an explicit --approved-by "
            "identity (automation may not approve on its own authority)"
        )
        return 1
    try:
        candidate = store.load_candidate(args.candidate_id)
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    if not non_interactive:
        print(candidate.diff_summary())
        answer = input("\nApprove this repair for promotion? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("not approved; candidate unchanged")
            return 1
    try:
        candidate = store.approve(
            args.candidate_id,
            _human(args.approved_by),
            non_interactive=non_interactive,
        )
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    approval = candidate.approval
    assert approval is not None
    print(
        f"candidate {candidate.candidate_id} APPROVED by "
        f"{approval.approved_by}; binds proposed bundle "
        f"{approval.proposed_content_digest[:16]}..."
    )
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.stage(
            args.candidate_id, Actor(kind="automation", id="repair-cli")
        )
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(
        f"candidate {candidate.candidate_id} STAGED: bundles "
        f"{candidate.prior_content_digest[:16]}... and "
        f"{candidate.proposed_content_digest[:16]}... verified in {store.root}"
    )
    return 0


def cmd_canary(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.start_canary(
            args.candidate_id,
            Actor(kind="automation", id="repair-cli"),
            max_runs=args.max_runs,
        )
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(
        f"candidate {candidate.candidate_id} ACTIVE (canary): first "
        f"{candidate.canary.max_runs} runs are verified per-run; any "
        "regression auto-reverts to the prior bundle"
    )
    return 0


def cmd_canary_record(args: argparse.Namespace) -> int:
    store = _store(args)
    report_path = Path(args.run_dir) / "report.json"
    if not report_path.is_file():
        print(f"REFUSED: no report.json under {args.run_dir}")
        return 1
    report = RunReport.model_validate_json(report_path.read_text())
    observation = observation_from_report(report)
    try:
        candidate = store.record_canary_run(args.candidate_id, observation)
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    metrics = candidate.canary_metrics
    print(
        f"canary run recorded: verified={observation.verified} "
        f"silent_incorrect={observation.silent_incorrect} "
        f"({metrics.verified_runs}/{candidate.canary.max_runs} verified)"
    )
    if metrics.halted:
        print(f"CANARY HALTED: {metrics.halt_reason}")
        print("the active pointer was reverted to the prior bundle")
        return 1
    print(f"candidate state: {candidate.state}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        candidate = store.rollback(_human(args.by), candidate_id=args.candidate_id)
    except RepairLifecycleError as exc:
        return _print_refusal(exc)
    print(
        f"ROLLED BACK: prior bundle "
        f"{candidate.prior_content_digest[:16]}... is active again "
        f"(candidate {candidate.candidate_id} -> {candidate.state})"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    pointer = store.active_pointer()
    if pointer is None:
        print(f"no active repair pointer in {store.root}")
        return 0
    print(
        f"workflow {pointer.workflow_name!r}: {pointer.mode} bundle "
        f"{pointer.active_digest} (candidate {pointer.candidate_id})"
    )
    if pointer.prior_digest:
        print(f"  rollback target: {pointer.prior_digest}")
    if pointer.note:
        print(f"  note: {pointer.note}")
    return 0


_HANDLERS = {
    "register": cmd_register,
    "list": cmd_list,
    "show": cmd_show,
    "review": cmd_review,
    "campaign": cmd_campaign,
    "approve": cmd_approve,
    "stage": cmd_stage,
    "canary": cmd_canary,
    "canary-record": cmd_canary_record,
    "rollback": cmd_rollback,
    "status": cmd_status,
}


def run_repair_command(args: argparse.Namespace) -> int:
    """Dispatch an ``openadapt-flow repair <verb>`` invocation."""
    handler = _HANDLERS.get(args.repair_cmd)
    if handler is None:  # pragma: no cover - argparse enforces choices
        print(f"unknown repair subcommand {args.repair_cmd!r}")
        return 2
    return handler(args)


__all__ = ["run_repair_command", "build_candidate", "detached_candidate_path"]
