"""CLI: ``openadapt-flow undo`` -- declared undo steps after a stop.

``declare`` adds an undo to a bundle. ``list`` shows what a stopped run
offers. ``approve`` records a person's approval at an interactive terminal.
``run`` runs one approved undo, once, through the normal replay path and
checks its declared effects in the system of record. See
``docs/AFTER_A_STOP.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Literal, Optional, Sequence

from openadapt_flow import sealed_set
from openadapt_flow.after_stop import (
    UndoRefused,
    approve_undo,
    declare_undo,
    finish_undo_run,
    offer_undos,
    prepare_undo_run,
    read_undo_approval,
    read_undo_result,
    steps_from_demonstration,
)
from openadapt_flow.cli_hints import command
from openadapt_flow.ir import RunReport, UndoStep, Workflow


def register(
    sub: argparse._SubParsersAction,
    *,
    replay: Callable[[argparse.Namespace], int],
    target_problem: Callable[[argparse.Namespace, Workflow], Optional[str]],
    add_run_flags: Sequence[Callable[[argparse.ArgumentParser], None]],
) -> None:
    """Add the ``undo`` command group.

    ``replay`` is the CLI's replay command and ``add_run_flags`` its backend,
    browser and deployment flag builders, so ``undo run`` drives the app the
    same way ``replay`` does. ``target_problem`` says why the resolved
    backend names no real application (it would fall back to the bundled
    demo), or returns None.
    """
    parser = sub.add_parser(
        "undo",
        help=(
            "Declared undo steps after a stop: declare, list what a stopped "
            "run offers, approve (a person), and run once with verification"
        ),
    )
    verbs = parser.add_subparsers(dest="undo_cmd", required=True)

    p = verbs.add_parser(
        "declare",
        help="Add a compensating step for one step's effect to a bundle",
    )
    p.add_argument("bundle", help="Workflow bundle directory to add the undo to")
    p.add_argument("--id", required=True, dest="undo_id", help="Undo id")
    p.add_argument("--undoes", required=True, help="Step id whose effect it undoes")
    p.add_argument("--intent", required=True, help="Plain words a person approves")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-bundle",
        default=None,
        help="Bundle compiled from a recording of a person doing the undo",
    )
    source.add_argument(
        "--from-file",
        default=None,
        help="JSON list of hand-written undo steps (the Step schema)",
    )
    p.add_argument(
        "--effects-file",
        default=None,
        help=(
            "JSON list of effects the system of record must show after the "
            "undo; added to the last undo step"
        ),
    )
    p.set_defaults(func=_cmd_undo_declare)

    p = verbs.add_parser("list", help="Show the undos a stopped run offers")
    p.add_argument("run_dir", help="The stopped run's directory")
    p.add_argument("--bundle", required=True, help="The run's bundle directory")
    p.add_argument("--json", action="store_true", help="Print JSON")
    p.set_defaults(func=_cmd_undo_list)

    p = verbs.add_parser(
        "approve",
        help="A person approves one offered undo (interactive terminal only)",
    )
    p.add_argument("run_dir", help="The stopped run's directory")
    p.add_argument("--bundle", required=True, help="The run's bundle directory")
    p.add_argument("--undo", required=True, dest="undo_id", help="Undo id")
    p.add_argument("--note", default="", help="Optional note for the record")
    p.set_defaults(func=_cmd_undo_approve)

    p = verbs.add_parser(
        "run",
        help="Run one approved undo once and check it in the system of record",
    )
    p.add_argument("run_dir", help="The stopped run's directory")
    p.add_argument("--bundle", required=True, help="The run's bundle directory")
    p.add_argument("--undo", required=True, dest="undo_id", help="Undo id")
    p.add_argument(
        "--url",
        default=None,
        help="URL of the target app (default: the bundled MockMed demo app)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    for add in add_run_flags:
        add(p)
    p.set_defaults(
        func=_cmd_undo_run, _undo_replay=replay, _undo_target_problem=target_problem
    )


def _load_json_list(path: str, what: str) -> list:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{what} could not be read as JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{what} must hold a non-empty JSON list")
    return raw


def _cmd_undo_declare(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Step
    from openadapt_flow.runtime.effects.effect import Effect

    bundle = Path(args.bundle)
    source: Literal["demonstration", "hand_written"]
    try:
        assets = None
        if args.from_bundle:
            demonstrated = steps_from_demonstration(args.from_bundle, args.undo_id)
            steps, assets = demonstrated.steps, demonstrated.assets
            source = "demonstration"
        else:
            steps = [
                Step.model_validate(item)
                for item in _load_json_list(args.from_file, "--from-file")
            ]
            source = "hand_written"
        if args.effects_file:
            effects = [
                Effect.model_validate(item)
                for item in _load_json_list(args.effects_file, "--effects-file")
            ]
            steps[-1] = steps[-1].model_copy(
                update={"effects": [*steps[-1].effects, *effects]}
            )
        undo = UndoStep(
            id=args.undo_id,
            undoes=args.undoes,
            intent=args.intent,
            source=source,
            steps=steps,
        )
        declare_undo(bundle, undo, assets=assets)
    except ValueError as exc:
        print(f"undo declare REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Declared undo {undo.id!r} for step {undo.undoes!r} ({source}, "
        f"{len(undo.steps)} step(s)). The bundle was re-sealed and any "
        "certification is now expired: certify it again before production use."
    )
    return 0


def _cmd_undo_list(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    workflow = Workflow.load(args.bundle)
    try:
        offers = offer_undos(run_dir, workflow, bundle_dir=args.bundle)
    except UndoRefused as exc:
        print(f"undo list: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = []
        for offer in offers:
            result = read_undo_result(run_dir, offer.undo_id)
            item = offer.model_dump(mode="json")
            item["result"] = result.model_dump(mode="json") if result else None
            payload.append(item)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not offers:
        print("No undo is offered for this run.")
        return 0
    print("Undos offered for this run (undo the latest change first):")
    quoted_run = shlex.quote(str(run_dir))
    quoted_bundle = shlex.quote(str(args.bundle))
    for offer in offers:
        entry = offer.ledger_entry
        result = read_undo_result(run_dir, offer.undo_id)
        approval = read_undo_approval(run_dir, offer.undo_id)
        approved = approval is not None and approval.offer_digest == offer.offer_digest
        claimed = sealed_set.exists(run_dir / "undo" / offer.undo_id / "claim.json")
        state = (
            f"ran: {result.status}"
            if result
            else "running or interrupted"
            if claimed
            else f"approved by {approval.approver}, not run"
            if approved and approval is not None
            else "not approved"
        )
        print(
            f"  {offer.undo_id}: {offer.intent}\n"
            f"    undoes step `{offer.undoes}` (position {entry.position}, "
            f"{entry.status}); source: {offer.source}; {state}"
        )
        if approved and not claimed:
            print(
                "    run: "
                + command(
                    f"undo run {quoted_run} --bundle {quoted_bundle} "
                    f"--undo {offer.undo_id} --url <app-url>"
                )
            )
        elif not approved and not claimed and result is None:
            print(
                "    approve: "
                + command(
                    f"undo approve {quoted_run} --bundle {quoted_bundle} "
                    f"--undo {offer.undo_id}"
                )
            )
    return 0


def _cmd_undo_approve(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    workflow = Workflow.load(args.bundle)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "undo approve needs a person at an interactive terminal; an agent "
            "or script can't approve an undo. Nothing was approved.",
            file=sys.stderr,
        )
        return 2
    offers = {
        offer.undo_id: offer
        for offer in offer_undos(run_dir, workflow, bundle_dir=args.bundle)
    }
    offer = offers.get(args.undo_id)
    if offer is None:
        print(f"Undo {args.undo_id!r} is not offered for this run.", file=sys.stderr)
        return 1
    undo = next(u for u in workflow.undo_steps if u.id == args.undo_id)
    print(f"Undo {undo.id}: {undo.intent}")
    print(
        f"It undoes step `{undo.undoes}`, which the run recorded as "
        f"{offer.ledger_entry.status}. It runs these steps:"
    )
    for step in undo.steps:
        print(f"  - {step.id}: {step.intent}")
    print("Afterwards Flow checks that the system of record shows:")
    for step in undo.steps:
        for effect in step.effects:
            print(f"  - {effect.kind.value}: {effect.probe or 'declared effect'}")
    typed = input(f"Type the undo id ({undo.id}) to approve it, or press Enter: ")
    if typed.strip() != undo.id:
        print("Not approved. Nothing was recorded.")
        return 1
    try:
        approval = approve_undo(
            run_dir, workflow, undo.id, note=args.note, bundle_dir=args.bundle
        )
    except UndoRefused as exc:
        print(f"Approval refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"Approved by {approval.approver}. Run it once with: "
        + command(
            f"undo run {shlex.quote(str(run_dir))} --bundle "
            f"{shlex.quote(str(args.bundle))} --undo {undo.id}"
        )
    )
    return 0


def _cmd_undo_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    workflow = Workflow.load(args.bundle)
    # Without a target, replay would start the bundled MockMed demo app: the
    # undo would act on the demo, leave the real record alone and use up the
    # approval. Refuse before claiming it.
    problem = args._undo_target_problem(args, workflow)
    if problem:
        print(
            f"undo run REFUSED: {problem}. Nothing was executed and the "
            "approval is unused.",
            file=sys.stderr,
        )
        return 2
    try:
        prepared = prepare_undo_run(
            run_dir, workflow, args.undo_id, bundle_dir=args.bundle
        )
    except UndoRefused as exc:
        print(f"undo run REFUSED: {exc}. Nothing was executed.", file=sys.stderr)
        return 3
    params_path = prepared.undo_run_dir.parent / "params.json"
    if sealed_set.is_sealed_path(params_path):
        # Parameter values are patient data; keep them sealed with the run.
        sealed_set.write_text(params_path, json.dumps(prepared.params))
    else:
        fd = os.open(params_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(prepared.params, handle)
    replay_args = argparse.Namespace(**vars(args))
    replay_args.run_dir = str(prepared.undo_run_dir)
    replay_args.param = None
    replay_args.params_file = str(params_path)
    replay_args.drift = None
    replay_args.allow_model_grounding = False
    replay_args.save_healed_to = None
    replay_args.record_video = None
    replay_args.worklist = None
    replay_args._undo_workflow = prepared.workflow
    report = None
    try:
        args._undo_replay(replay_args)
    finally:
        report_path = prepared.undo_run_dir / "report.json"
        if sealed_set.is_file(report_path):
            report = RunReport.model_validate_json(sealed_set.read_text(report_path))
        result = finish_undo_run(run_dir, prepared, report)
    if result.status == "verified":
        print(f"Undo {result.undo_id} VERIFIED: {result.reason}.")
        return 0
    print(
        f"Undo {result.undo_id} NOT VERIFIED: {result.reason}. The app may "
        "still need a person's repair; check the record by hand."
    )
    return 1


__all__ = ["register"]
