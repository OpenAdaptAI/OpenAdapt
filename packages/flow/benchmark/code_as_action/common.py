"""Shared pieces of the code-as-action measurements: the key, the spend
ledger, host load, and how a run-1 result becomes a committed row.

The key is read from an env file into this process only and never printed.
The spend ledger (``results/spend.json``) is shared by every measurement
script here, so the total cap holds across them.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
SPEND = RESULTS / "spend.json"
TOTAL_CAP_USD = 15.0
#: Timed runs count as unloaded only below this 1-minute load (12 cores).
LOAD_LIMIT = 12.0


def load_key(env_file: Path) -> None:
    for line in env_file.read_text().splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "ANTHROPIC_API_KEY":
            os.environ["ANTHROPIC_API_KEY"] = value.strip().strip('"').strip("'")
            return
    raise SystemExit(f"no ANTHROPIC_API_KEY in {env_file}")


def spent() -> float:
    if not SPEND.is_file():
        return 0.0
    return float(json.loads(SPEND.read_text()).get("total_usd", 0.0))


def may_start(run_cap: float, cap: float = TOTAL_CAP_USD) -> Optional[str]:
    """None when a run capped at ``run_cap`` can't pass the total cap."""

    total = spent()
    if total + run_cap > cap:
        return f"${total:.2f} spent; a ${run_cap:.2f} run could pass the ${cap:.2f} cap"
    return None


def add_spend(label: str, usd: float) -> float:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data = json.loads(SPEND.read_text()) if SPEND.is_file() else {"runs": []}
    data["runs"].append({"label": label, "usd": round(usd, 6), "at": now()})
    data["total_usd"] = round(sum(r["usd"] for r in data["runs"]), 6)
    data["cap_usd"] = TOTAL_CAP_USD
    SPEND.write_text(json.dumps(data, indent=2) + "\n")
    return data["total_usd"]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_1m() -> float:
    return round(os.getloadavg()[0], 1)


def commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=HERE,
    ).stdout.strip()


def append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def agent_metrics(agent: dict[str, Any], harness: dict[str, Any]) -> dict[str, Any]:
    """The comparison columns, from an agent ledger and harness stats."""

    return {
        "model_calls": agent.get("model_calls"),
        "prompt_tokens": agent.get("prompt_tokens"),
        "input_tokens": agent.get("input_tokens"),
        "cache_read_input_tokens": agent.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": agent.get("cache_creation_input_tokens"),
        "output_tokens": agent.get("output_tokens"),
        "cost_usd": agent.get("cost_usd"),
        "model_seconds": agent.get("model_seconds"),
        "agent_seconds": agent.get("wall_seconds"),
        "actions": harness.get("actions"),
        "screenshots_sent": (
            harness.get("screenshots_requested", 0)
            + harness.get("screenshots_at_gate", 0)
            + harness.get("screenshots_every_turn", 0)
        ),
        "images_in_requests": agent.get("images_in_requests"),
        "tool_errors": agent.get("tool_errors"),
    }


def median(rows: list[dict[str, Any]], key: str) -> Optional[float]:
    values = [float(r[key]) for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def summarize(
    rows: list[dict[str, Any]], arms: list[str], keys: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        unloaded = [r for r in mine if not r.get("under_load")]
        out[arm] = {
            "runs": len(mine),
            "correct": sum(1 for r in mine if r.get("correct")),
            "runs_under_load": len(mine) - len(unloaded),
            "total_cost_usd": round(
                sum(float(r.get("cost_usd") or 0) for r in mine), 4
            ),
            **{f"median_{k}": median(mine, k) for k in keys},
        }
    return out
