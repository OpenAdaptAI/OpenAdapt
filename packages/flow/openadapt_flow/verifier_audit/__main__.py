"""``python -m openadapt_flow.verifier_audit``: audit an environment's scorer.

    python -m openadapt_flow.verifier_audit families
    python -m openadapt_flow.verifier_audit run \\
        --target openadapt_flow.verifier_audit.targets.agentdojo:make_target \\
        --target-arg suite=banking --out audit-out/agentdojo-banking

``--target`` names a factory ``module:function`` that returns an
:class:`~openadapt_flow.verifier_audit.model.AuditTarget`; each
``--target-arg key=value`` is passed to it as a keyword argument.
``--families-module`` adds a private family set (its ``FAMILIES`` tuple).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from openadapt_flow.verifier_audit.families import (
    DEFAULT_FAMILIES,
    families_spec,
    load_families,
)
from openadapt_flow.verifier_audit.runner import AuditConfig, run_audit


def _factory(spec: str):
    module, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--target must be module:function, got {spec!r}")
    return getattr(importlib.import_module(module), attr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m openadapt_flow.verifier_audit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fam = sub.add_parser("families", help="print the family set as JSON")
    fam.add_argument("--families-module")
    run = sub.add_parser("run", help="audit a target")
    run.add_argument("--target", required=True)
    run.add_argument("--target-arg", action="append", default=[])
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--tasks", help="comma-separated task ids (default: all)")
    run.add_argument(
        "--families-module", help="module whose FAMILIES are added to the defaults"
    )
    run.add_argument("--only-families", help="comma-separated family ids to run")
    run.add_argument("--max-candidates", type=int, default=6)
    run.add_argument(
        "--single-replay",
        action="store_true",
        help="score the census replay instead of replaying the chosen edit again",
    )
    run.add_argument(
        "--redact", action="store_true", help="digest call arguments and effect rows"
    )
    args = parser.parse_args(argv)

    families = tuple(DEFAULT_FAMILIES)
    if args.families_module:
        families += load_families(args.families_module)
    if args.cmd == "families":
        print(json.dumps(families_spec(families), indent=2))
        return 0
    if args.only_families:
        wanted = set(args.only_families.split(","))
        families = tuple(f for f in families if f.id in wanted)
    kwargs = dict(kv.split("=", 1) for kv in args.target_arg)
    target = _factory(args.target)(**kwargs)
    cfg = AuditConfig(
        families=families,
        tasks=args.tasks.split(",") if args.tasks else None,
        max_candidates=args.max_candidates,
        single_replay=args.single_replay,
        redact=args.redact,
        out_dir=args.out,
        log=lambda msg: print(msg, file=sys.stderr, flush=True),
    )
    report = run_audit(target, cfg)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
