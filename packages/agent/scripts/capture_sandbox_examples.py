"""Record one real sandbox result per case, for the README and docs.

Drives the zero-flag sandbox with the flow engine (real openadapt-flow, a
hidden browser, and the synthetic MockMed app) and writes every result to
docs/examples/sandbox-results.json. Synthetic data only. Usage:

    pip install -e '.[tutorial]'
    python scripts/capture_sandbox_examples.py [--out docs/examples/sandbox-results.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from openadapt_agent.bridge import AgentBridge
from openadapt_agent.runner import RunnerConfig
from openadapt_agent.sandbox import CASES, SANDBOX_WORKFLOW, SandboxEngine

NOTE = "Synthetic: recheck blood pressure in 2 weeks"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/examples/sandbox-results.json")
    args = parser.parse_args()
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("OPENADAPT_TELEMETRY", "0")
    with tempfile.TemporaryDirectory(prefix="openadapt-sandbox-") as tmp:
        runs = Path(tmp)
        engine = SandboxEngine(runs, engine="flow")
        bridge = AgentBridge(None, RunnerConfig(runs_dir=runs), sandbox=engine, mode="sandbox")
        results: dict[str, object] = {}
        try:
            results["list_workflows"] = bridge.dispatch("list_workflows", {})
            for case in CASES:
                results[case] = bridge.dispatch(
                    "run_workflow",
                    {
                        "workflow": SANDBOX_WORKFLOW,
                        "inputs": {"note": NOTE, "sandbox_case": case},
                        "request_id": f"demo-{case.replace('_', '-')}",
                        "wait_seconds": 600,
                    },
                )
            results["normal_again_same_request_id"] = bridge.dispatch(
                "run_workflow",
                {
                    "workflow": SANDBOX_WORKFLOW,
                    "inputs": {"note": NOTE, "sandbox_case": "normal"},
                    "request_id": "demo-normal",
                    "wait_seconds": 600,
                },
            )
            results["invalid_input"] = bridge.dispatch(
                "run_workflow",
                {
                    "workflow": SANDBOX_WORKFLOW,
                    "inputs": {"note": ""},
                    "request_id": "demo-invalid",
                },
            )
        finally:
            engine.close()
    payload = {
        "provenance": (
            "Measured on synthetic data: openadapt-agent sandbox, flow engine, "
            "MockMed synthetic clinic app. No real records."
        ),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "versions": {
            "openadapt-agent": version("openadapt-agent"),
            "openadapt-flow": version("openadapt-flow"),
            "python": sys.version.split()[0],
        },
        "engine": engine.engine,
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} (engine {engine.engine})")
    return 0 if engine.engine == "flow" else 1


if __name__ == "__main__":
    raise SystemExit(main())
