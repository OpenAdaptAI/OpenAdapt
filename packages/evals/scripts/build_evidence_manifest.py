#!/usr/bin/env python3
"""Build the complete governed-evidence inventory from retained campaign files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = Path(__file__).resolve().with_name("check_published_evidence_freshness.py")
SPEC = importlib.util.spec_from_file_location("_evidence_checker", CHECKER)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)

VERIFIERS = (
    "scripts/run_current_flow_local_benchmark.py",
    "scripts/run_flow_transaction_probe.py",
    "scripts/probe_remote_lease_safety.py",
    "scripts/extract_over_halt_regression.py",
    "scripts/evidence_runtime.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _campaign_name(evidence: Path, result: Path) -> str:
    relative = result.relative_to(evidence)
    return "comparison" if relative.as_posix() == "results.json" else relative.parent.as_posix()


def _verifier_for(name: str) -> str:
    if name in {"comparison", "replication"}:
        return "scripts/run_current_flow_local_benchmark.py"
    if name == "transaction_probe":
        return "scripts/run_flow_transaction_probe.py"
    if name == "remote_lease_safety":
        return "scripts/probe_remote_lease_safety.py"
    raise ValueError(f"unknown campaign directory: {name}")


def _evidence_scope(name: str) -> dict[str, Any]:
    """Return the bounded maturity scope for one public campaign."""

    if name == "remote_lease_safety":
        evidence_class = "contract_fixture"
    else:
        evidence_class = "local_synthetic"
    return {
        "class": evidence_class,
        "production_acceptance": False,
        "customer_workflow": False,
        "hosted_execution": False,
        "real_remote_session": False,
    }


def build(evidence: Path, *, sdist_sha256: str) -> dict[str, Any]:
    evidence = evidence.resolve()
    results = sorted(evidence.rglob("results.json"))
    if not results or evidence / "results.json" not in results:
        raise ValueError("evidence directory must contain a root results.json")
    root = json.loads((evidence / "results.json").read_text(encoding="utf-8"))
    flow = root["source"]["flow"]
    campaigns: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for path in results:
        document = json.loads(path.read_text(encoding="utf-8"))
        name = _campaign_name(evidence, path)
        campaigns.append(
            {
                "name": name,
                "results_path": _relative(path),
                "verifier_path": _verifier_for(name),
                "environment": document["environment"],
                "runtime": document["runtime"],
                "evidence_scope": _evidence_scope(name),
                "metric_coverage": CHECK._metric_coverage(document),
            }
        )
        contract = CHECK._task_contract(document)
        contracts.append(
            {
                "results_path": _relative(path),
                "value": contract,
                "sha256": CHECK._canonical_sha256(contract),
            }
        )
    reports = sorted(path for path in evidence.rglob("*.md") if path.is_file())
    artifacts = sorted(
        path
        for path in evidence.rglob("*")
        if path.is_file() and path.suffix != ".md" and path.name != "EVIDENCE_MANIFEST.json"
    )
    return {
        "schema_version": 3,
        "evals_commit": root["source"]["evals"]["commit"],
        "flow": {
            "version": flow["version"],
            "source_commit": flow["commit"],
            "release_tag": flow["release_tag"],
            "wheel_sha256": flow["artifact"]["sha256"],
            "sdist_sha256": sdist_sha256,
        },
        "verifiers": [{"path": path, "sha256": _sha256(REPO_ROOT / path)} for path in VERIFIERS],
        "campaigns": campaigns,
        "task_contracts": contracts,
        "artifacts": [{"path": _relative(path), "sha256": _sha256(path)} for path in artifacts],
        "public_reports": [{"path": _relative(path), "sha256": _sha256(path)} for path in reports],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--sdist-sha256", required=True)
    args = parser.parse_args()
    manifest = build(args.evidence, sdist_sha256=args.sdist_sha256)
    output = args.evidence / "EVIDENCE_MANIFEST.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
