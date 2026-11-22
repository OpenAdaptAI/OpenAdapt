"""CLI entry point for ``openadapt onboard`` (single-command pack onboarding)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from openadapt_flow.onboard.engine import onboard_pack, synthesize_pack
from openadapt_flow.onboard.types import OnboardSpec


def _cmd_onboard(args: argparse.Namespace) -> int:
    try:
        spec = OnboardSpec.load(args.spec)
    except (OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: invalid onboard spec: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    if args.dry_run:
        manifest_path, digest = synthesize_pack(spec, out_dir)
        payload = {
            "pack_name": spec.name,
            "certified": False,
            "dry_run": True,
            "manifest_path": str(manifest_path),
            "pack_digest": digest,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"Synthesized pack {spec.name!r} at {manifest_path} "
                f"(digest {digest}; dry-run, not certified)"
            )
        return 0

    result = onboard_pack(spec, out_dir)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        status = "CERTIFIED" if result.certified else "REFUSED"
        print(
            f"{status}: pack={result.pack_name!r} "
            f"planted_faults={result.planted_faults_caught}/{result.planted_faults_total} "
            f"clean_writes={result.clean_writes_verified}/{result.clean_writes_total} "
            f"false_stop_rate={result.false_stop_rate:.1%} "
            f"digest={result.pack_digest}"
        )
        for reason in result.refusal_reasons:
            print(f"  refusal: {reason}", file=sys.stderr)
    return 0 if result.certified else 2


def register(subparsers: Any) -> None:
    """Register the ``onboard`` subcommand on the Flow root parser."""

    p = subparsers.add_parser(
        "onboard",
        help="Synthesize and planted-fault-qualify a verification pack for any system of record",
        allow_abbrev=False,
    )
    p.add_argument(
        "--spec",
        required=True,
        help="Path to declarative onboard spec (YAML or JSON)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output directory for synthesized pack, contracts, and QUALIFICATION.json",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured QualificationResult JSON to stdout",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Synthesize the pack without running qualification (pack is not certified)",
    )
    p.set_defaults(func=_cmd_onboard)
