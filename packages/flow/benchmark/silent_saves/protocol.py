"""The fixed public fault set and item plan (``fault_set.json``).

The fault set is data, not code, so a run's meta records exactly which
version it used. Every fault kind here is already published in
``benchmark/effect_e2e/EFFECT_E2E.md`` and the scoreboard's claims run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FAULT_SET_PATH = HERE / "fault_set.json"
FAULT_SET: dict[str, Any] = json.loads(FAULT_SET_PATH.read_text(encoding="utf-8"))
VERSION: str = FAULT_SET["version"]
PROTOCOL: str = FAULT_SET["protocol"]
FAULTS: tuple[str, ...] = tuple(f["kind"] for f in FAULT_SET["faults"])
TARGET_PATIENT: str = FAULT_SET["task"]["target"]["patient_id"]
TARGET_TYPE: str = FAULT_SET["task"]["target"]["type"]
INSTRUCTION: str = FAULT_SET["task"]["instruction"]


@dataclass(frozen=True)
class Item:
    """One benchmark item. ``fault`` is the runner's; executors never see it."""

    item: int
    fault: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def instruction(self) -> str:
        return INSTRUCTION.format(**self.params)


def note_for(item: int) -> str:
    """The note item ``item`` asks for (the scoreboard's MockMed notes)."""

    return f"Synthetic follow-up note {item}: recheck in {7 + item % 14} days"


def plan(
    per_fault: int, faults: tuple[str, ...] = FAULTS, first: int = 1
) -> list[Item]:
    """Round robin over the faults, so drift during a run spreads evenly."""

    unknown = [f for f in faults if f not in FAULTS]
    if unknown:
        raise ValueError(f"not in fault set {VERSION}: {unknown}")
    n = per_fault * len(faults)
    return [
        Item(
            item=i,
            fault=faults[(i - first) % len(faults)],
            params={"note": note_for(i)},
        )
        for i in range(first, first + n)
    ]
