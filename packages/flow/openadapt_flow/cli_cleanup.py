"""CLI: ``openadapt-flow cleanup qualify`` -- a clean screen after a halt.

Derives the bundle's halt cleanup (Escape, the recordings' cancel, close and
discard controls, and the start screen), tries it on a declared test copy at
every halt point (after every step but the last), and stores it in the bundle when
every halt point reaches the start screen. See ``docs/HALT_CLEANUP.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "cleanup",
        help="A clean screen after a halted item: qualify the halt cleanup",
    )
    verbs = parser.add_subparsers(dest="cleanup_cmd", required=True)
    p = verbs.add_parser(
        "qualify",
        help=(
            "Stop the run after every step on a declared test copy, clean up, "
            "and check the start screen comes back"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--config",
        required=True,
        help="Deployment file that declares a test copy (test_copy.declared: true)",
    )
    p.add_argument(
        "--from-recording",
        action="append",
        default=[],
        dest="from_recordings",
        help="Another compiled bundle whose cancel or close controls to use",
    )
    p.add_argument("--work-dir", default=None, help="Keep the test-copy runs here")
    p.add_argument("--report", default=None, help="Write the report (JSON) here")
    p.add_argument(
        "--no-write", action="store_true", help="Qualify, but don't store it"
    )
    p.set_defaults(func=_cmd_cleanup_qualify)


def _cmd_cleanup_qualify(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.probing import probe_target_refusals
    from openadapt_flow.deployment import load_deployment
    from openadapt_flow.halt_cleanup import (
        derive_cleanup,
        qualify_cleanup,
        write_cleanup,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.undo_proposal import DeploymentUndoTestCopy

    bundle = Path(args.bundle)
    try:
        cfg = load_deployment(Path(args.config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"cleanup qualify REFUSED: {exc}", file=sys.stderr)
        return 3
    reasons = probe_target_refusals(cfg)
    if reasons:
        print(
            "cleanup qualify REFUSED: nothing was reset or run. It stops runs on "
            "purpose, so it runs only on a declared test copy:",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 3
    workflow = Workflow.load(bundle)
    derived = derive_cleanup(
        workflow, recordings=[Path(p) for p in args.from_recordings]
    )
    if derived is None:
        print(
            "cleanup qualify: the workflow has no anchored step to call its start "
            "screen; nothing to qualify",
            file=sys.stderr,
        )
        return 2
    target = DeploymentUndoTestCopy(cfg, deployment_path=Path(args.config))
    try:
        qualification, results = qualify_cleanup(
            workflow,
            derived.cleanup,
            target=target,
            bundle_dir=bundle,
            work_dir=Path(args.work_dir) if args.work_dir else None,
        )
    except ValueError as exc:
        print(f"cleanup qualify REFUSED: {exc}", file=sys.stderr)
        return 2
    payload = {
        "schema": "openadapt.halt-cleanup-qualification/v1",
        "workflow_name": workflow.name,
        "test_copy": target.name,
        "cleanup_steps": [s.intent for s in derived.cleanup.steps],
        "derived_from": derived.notes,
        "halt_points": [
            {"after_step": r.after_step, "passed": r.passed, "reason": r.reason}
            for r in results
        ],
        "qualified": qualification is not None,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r.passed)
    print(
        f"{passed} of {len(results)} halt point(s) came back to the start screen "
        f"after the cleanup ({len(derived.cleanup.steps)} step(s))."
    )
    for r in results:
        if not r.passed:
            print(f"  after {r.after_step}: {r.reason}")
    if qualification is None:
        print(
            "Not qualified: a pool keeps checking the start screen after a halt, "
            "but runs none of these cleanup steps."
        )
        return 1
    if not args.no_write:
        write_cleanup(
            bundle,
            derived.cleanup.model_copy(update={"qualification": qualification}),
            assets=derived.assets,
        )
        print(
            "Qualified and stored in the bundle (re-sealed; certify it again "
            "before production use)."
        )
    return 0


__all__ = ["register"]
