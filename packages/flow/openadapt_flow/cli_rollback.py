"""CLI: ``openadapt-flow rollback qualify`` -- a stop at every halt point.

Runs the halt matrix (``openadapt_flow.rollback_qualify``) on a declared test
copy and writes the compensations that pass as proposals a person approves
once (``undo accept``). See ``docs/ROLLBACK.md``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from openadapt_flow.cli_hints import command


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "rollback",
        help=(
            "Compensating rollback after a stop: qualify it on a test copy "
            "(a stop before and after every save)"
        ),
    )
    verbs = parser.add_subparsers(dest="rollback_cmd", required=True)
    p = verbs.add_parser(
        "qualify",
        help=(
            "Stop the run before and after every save on a declared test copy, "
            "roll back, and keep the compensations that restore the whole store"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--config",
        required=True,
        help=(
            "Deployment file that declares a test copy (test_copy.declared: "
            "true); rollback.database, when set, is the whole-database read"
        ),
    )
    p.add_argument(
        "--recording",
        default=None,
        help="The bundle's own recording directory (its network.jsonl)",
    )
    p.add_argument(
        "--from-recording",
        action="append",
        default=[],
        dest="from_recordings",
        help="A compiled bundle of a person deleting or cancelling (repeatable)",
    )
    p.add_argument("--work-dir", default=None, help="Keep the test-copy runs here")
    p.add_argument("--report", default=None, help="Write the report (JSON) here")
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Qualify, but don't write new compensations into the bundle",
    )
    p.add_argument("--json", action="store_true", help="Print the report as JSON")
    p.set_defaults(func=_cmd_rollback_qualify)


def _cmd_rollback_qualify(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.probing import probe_target_refusals
    from openadapt_flow.deployment import load_deployment
    from openadapt_flow.ir import Workflow
    from openadapt_flow.rollback_qualify import qualify_rollback, reader_for
    from openadapt_flow.undo_proposal import (
        DeploymentUndoTestCopy,
        Sources,
        load_network_lines,
        write_proposals,
    )

    bundle = Path(args.bundle)
    try:
        cfg = load_deployment(Path(args.config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"rollback qualify REFUSED: {exc}", file=sys.stderr)
        return 3
    reasons = probe_target_refusals(cfg)
    if reasons:
        print(
            "rollback qualify REFUSED: nothing was reset or run. It stops runs "
            "on purpose, so it runs only on a declared test copy:",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 3
    target = DeploymentUndoTestCopy(cfg, deployment_path=Path(args.config))
    workflow = Workflow.load(bundle)
    section = getattr(cfg, "rollback", None)
    try:
        report, new = qualify_rollback(
            workflow,
            bundle_dir=bundle,
            target=target,
            reader=reader_for(cfg, target),
            sources=Sources(
                recordings=[Path(p) for p in args.from_recordings],
                network_lines=load_network_lines(args.recording),
            ),
            work_dir=Path(args.work_dir) if args.work_dir else None,
            allow_delete=bool(section.allow_delete) if section else False,
        )
    except ValueError as exc:
        print(f"rollback qualify REFUSED: {exc}", file=sys.stderr)
        return 2
    if new and not args.no_write:
        try:
            write_proposals(bundle, new)
        except ValueError as exc:
            print(f"rollback qualify: nothing written: {exc}", file=sys.stderr)
            return 2
        report.written = sorted(c.undo.id for c, _r in new.values() if c.undo)
    payload = report.model_dump(mode="json", by_alias=True)
    payload["summary"] = report.summary()
    if args.report:
        Path(args.report).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    s = report.summary()
    print(
        f"{s['saving_steps']} saving step(s), {s['halt_points']} halt point(s): "
        f"{s['halt_points_rolled_back']} rolled back to the read from before the "
        f"run ({report.scope} read on {report.test_copy!r})."
    )
    for point in report.halt_points:
        mark = "rolled back" if point.passed else "NOT rolled back"
        what = f" with {point.compensation}" if point.compensation else ""
        print(f"  stop {point.when.replace('_', ' ')} {point.step_id}{what}: {mark}")
        if not point.passed:
            print(f"    {point.reason}")
    if report.written:
        quoted = shlex.quote(str(bundle))
        print(
            "New compensations were written as proposals. Rollback uses none "
            "of them until a person approves each one once: "
            + command(f"undo accept {quoted} --undo <undo-id>")
        )
    return 0


__all__ = ["register"]
