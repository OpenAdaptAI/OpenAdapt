"""CLI for the integrated OpenEMR end-to-end proof harness.

    python -m benchmark.openemr_e2e --out /tmp/openemr-e2e

Runs the full compiled pipeline (compile -> replay -> effect-verify ->
silent-wrong-write catch -> drift HALT -> teach -> re-run clean) against the
CI-reproducible fixture and writes ``result.json`` + ``SUMMARY.md``. Model-free,
$0. The paid computer-use agent arm is gated OFF (see ``--agent-arm``); this
harness never spends money.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from benchmark.openemr_e2e.harness import AgentArmRefused, run_openemr_e2e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmark.openemr_e2e",
        description=(
            "Integrated OpenEMR add-patient-note end-to-end proof "
            "(compiled arm, $0; paid agent arm gated off and never invoked)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("benchmark/openemr_e2e/out"),
        help="Output directory for result.json + SUMMARY.md (default: %(default)s).",
    )
    parser.add_argument(
        "--agent-arm",
        action="store_true",
        help=(
            "Opt in to the PAID computer-use agent comparison arm. Requires "
            "--max-cost-usd. Even so, this harness REFUSES to invoke it (no "
            "spend); use `python scripts/openemr_demo.py benchmark` for the "
            "audited paid run."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Hard per-run USD cap; mandatory when --agent-arm is set.",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help=(
            "Fail if a live OpenEMR FHIR SoR (OPENEMR_FHIR_BASE_URL) is "
            "requested but unreachable. The fixture loop runs regardless."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = run_openemr_e2e(
            args.out,
            enable_agent_arm=args.agent_arm,
            max_cost_usd=args.max_cost_usd,
            require_live=args.require_live,
        )
    except AgentArmRefused as exc:
        print(f"agent arm refused (by design, no spend): {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
