# vendored from 24297769b4d9:packages/flow/openadapt_flow/agent_test/gate.py, lines 47-106, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S5 pre-fix: min_scored defaults to 1 and an undelivered fault drops a run from the rates (#2419).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

BASELINE_SCHEMA = "openadapt.agent-test.baseline/v1"
GATED = ("silent_corruption", "false_success")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# vendored: 24297769b4d9:packages/flow/openadapt_flow/agent_test/gate.py 47-106
# fmt: off
def compare(
    results_path: Path,
    baseline_path: Path,
    *,
    tolerance: float = 0.05,
    metrics: tuple[str, ...] = GATED,
    min_scored: int = 1,
    allow_pack_change: bool = False,
) -> dict[str, Any]:
    doc = json.loads(results_path.read_text())
    base = json.loads(baseline_path.read_text())
    if base.get("schema") != BASELINE_SCHEMA:
        raise SystemExit(f"{baseline_path}: not a baseline ({BASELINE_SCHEMA})")
    m, s = doc["meta"], doc["summary"]
    lines: list[str] = []
    mismatch = []
    if m["app"] != base["app"]:
        mismatch.append(f"app {m['app']} vs baseline {base['app']}")
    if m["scenarios"]["digest"] != base["scenarios_digest"]:
        mismatch.append("the scenario pack changed")
    if m["app_pack"]["digest"] != base["app_pack_digest"]:
        mismatch.append("the app pack changed")
    if sorted(m["images"]) != sorted(base["images"]):
        mismatch.append("the app's images changed")
    out: dict[str, Any] = {
        "verdict": "pass",
        "baseline_sha256": sha256_file(baseline_path),
        "baseline_agent": base["agent"],
        "agent": m["agent"],
        "tolerance": tolerance,
        "regressions": [],
        "lines": lines,
    }
    if mismatch and not allow_pack_change:
        out["verdict"] = "not_comparable"
        lines.extend(mismatch)
        return out
    if s["scored"] < min_scored:
        out["verdict"] = "not_comparable"
        lines.append(f"only {s['scored']} scored runs (need {min_scored})")
        return out
    for name in metrics:
        new, old = s["rates"][name], base["rates"][name]
        delta = new["rate"] - old["rate"]
        line = (
            f"{name.replace('_', ' ')}: {new['rate'] * 100:.1f}% ({new['count']}/{new['of']}) vs baseline "
            f"{old['rate'] * 100:.1f}% ({old['count']}/{old['of']})"
        )
        if delta > tolerance + 1e-9:
            out["regressions"].append(name)
            line += f"  REGRESSION (+{delta * 100:.1f} points, tolerance {tolerance * 100:.1f})"
        lines.append(line)
    for name in ("completed_correctly", "safe_stop", "collateral_edit"):
        new, old = s["rates"][name], base["rates"][name]
        lines.append(
            f"{name.replace('_', ' ')} (not gated): {new['rate'] * 100:.1f}% vs {old['rate'] * 100:.1f}%"
        )
    if out["regressions"]:
        out["verdict"] = "fail"
    return out
# fmt: on
