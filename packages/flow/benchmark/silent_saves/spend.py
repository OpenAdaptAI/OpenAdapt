"""The cost ledger for paid executors, with a per-arm cap and a total stop.

Every model call an executor makes is in its per-item ledger (copied to
``<run>/ledgers/``). This file keeps the per-item totals in
``results/spend.json``, committed with the results. It reads, and never
writes, the scoreboard's shared spend file, so the stop counts both.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "results" / "spend.json"
#: The scoreboard's ledger (read only). Point SILENT_SAVES_SHARED_SPEND at
#: the live copy when the scoreboard runs from another checkout.
SHARED = Path(
    os.environ.get(
        "SILENT_SAVES_SHARED_SPEND", HERE.parent / "scoreboard" / "raw" / "spend.json"
    )
)
#: Ask before anything that would take the combined total past this.
STOP_TOTAL_USD = 20.0


class OverBudget(SystemExit):
    pass


def _read(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text())
    return {"total_usd": 0.0, "entries": []}


def shared_total() -> float:
    return float(_read(SHARED).get("total_usd", 0.0))


def own() -> dict:
    return _read(LEDGER)


def arm_total(arm: str) -> float:
    return round(sum(e["usd"] for e in own()["entries"] if e.get("arm") == arm), 6)


def check(arm: str, next_max_usd: float, arm_cap_usd: float) -> None:
    """Stop before a call that could pass the arm's cap or the total stop."""

    a = arm_total(arm)
    if a + next_max_usd > arm_cap_usd:
        raise OverBudget(
            f"arm {arm}: ${a:.4f} spent + up to ${next_max_usd:.2f} next "
            f"would pass its ${arm_cap_usd:.2f} cap; stopping"
        )
    total = shared_total() + own()["total_usd"]
    if total + next_max_usd > STOP_TOTAL_USD:
        raise OverBudget(
            f"total spend ${total:.4f} + up to ${next_max_usd:.2f} would pass "
            f"${STOP_TOTAL_USD:.0f}; stopping to ask first"
        )


def add(usd: float, arm: str, note: str) -> None:
    data = own()
    data["total_usd"] = round(data["total_usd"] + usd, 6)
    data["entries"].append(
        {"usd": round(usd, 6), "arm": arm, "note": note, "at": time.time()}
    )
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(data, indent=2) + "\n")
