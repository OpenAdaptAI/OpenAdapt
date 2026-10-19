"""The fixed public fault sets and their item plans.

A fault set is data, not code (``fault_set.json`` for 1.0.0,
``fault_set_1.1.0.json`` for 1.1.0), so a run's meta records exactly which
version it used. Every server fault here is already published in
``benchmark/effect_e2e/EFFECT_E2E.md`` and the scoreboard's claims run; every
UI change is one of MockMed's existing drift modes.

* 1.0.0: one fault kind per item, round robin, no UI change.
* 1.1.0: repeated execution under change. 100 items of one task; each block
  of items has one UI change switched on, and five items get a server fault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
SETS = {
    "1.0.0": HERE / "fault_set.json",
    "1.1.0": HERE / "fault_set_1.1.0.json",
}
DEFAULT = "1.0.0"


def load(version: str = DEFAULT) -> dict[str, Any]:
    if version not in SETS:
        raise ValueError(f"unknown fault set {version!r}; have {sorted(SETS)}")
    return json.loads(SETS[version].read_text(encoding="utf-8"))


# 1.0.0, kept as module constants for callers and baselines.
FAULT_SET_PATH = SETS[DEFAULT]
FAULT_SET: dict[str, Any] = load(DEFAULT)
VERSION: str = FAULT_SET["version"]
PROTOCOL: str = FAULT_SET["protocol"]
FAULTS: tuple[str, ...] = tuple(f["kind"] for f in FAULT_SET["faults"])
TARGET_PATIENT: str = FAULT_SET["task"]["target"]["patient_id"]
TARGET_TYPE: str = FAULT_SET["task"]["target"]["type"]
INSTRUCTION: str = FAULT_SET["task"]["instruction"]


@dataclass(frozen=True)
class Item:
    """One benchmark item. Everything but ``params`` is the runner's.

    Executors never see ``fault``, ``drift`` or ``kind``; they see the page.

    ``kind`` is ``control`` (no change, no fault), ``change`` (a benign UI
    change is on), ``trap`` (a change that makes acting on the old screen
    write the wrong record) or ``fault`` (the server breaks the save).
    ``expect`` is ``save``, or ``stop`` when no correct save exists.
    """

    item: int
    fault: str
    params: dict[str, str] = field(default_factory=dict)
    drift: tuple[str, ...] = ()
    kind: str = "fault"
    expect: str = "save"
    release: str = ""
    template: str = INSTRUCTION
    slow_ms: int = 0
    idempotency_key: bool = False

    @property
    def instruction(self) -> str:
        return self.template.format(**self.params)


def note_for(item: int) -> str:
    """The note item ``item`` asks for (the scoreboard's MockMed notes)."""

    return f"Synthetic follow-up note {item}: recheck in {7 + item % 14} days"


def plan(
    per_fault: int, faults: tuple[str, ...] = FAULTS, first: int = 1
) -> list[Item]:
    """1.0.0: round robin over the faults, so drift during a run spreads evenly."""

    unknown = [f for f in faults if f not in FAULTS]
    if unknown:
        raise ValueError(f"not in fault set {VERSION}: {unknown}")
    n = per_fault * len(faults)
    return [
        Item(
            item=i,
            fault=faults[(i - first) % len(faults)],
            params={"note": note_for(i)},
            kind="control" if faults[(i - first) % len(faults)] == "ok" else "fault",
        )
        for i in range(first, first + n)
    ]


def plan_change(fs: dict[str, Any], limit: Optional[int] = None) -> list[Item]:
    """1.1.0: each block switches on its own change; faults on fixed items."""

    blocks = sorted(fs["blocks"], key=lambda b: b["from"])
    changes = fs["changes"]
    faults = {int(k): v for k, v in fs["faults"].items()}
    n = fs["items"] if limit is None else min(limit, fs["items"])
    items = []
    for i in range(1, n + 1):
        block = [b for b in blocks if b["from"] <= i][-1]
        drift = tuple(block["on"])
        fault = faults.get(i, "ok")
        trap = any(changes[d].get("trap") for d in drift)
        kind = (
            "fault"
            if fault != "ok"
            else "trap"
            if trap
            else "change"
            if drift
            else "control"
        )
        stop = any(changes[d].get("expect") == "stop" for d in drift)
        items.append(
            Item(
                item=i,
                fault=fault,
                params={"note": note_for(i)},
                drift=drift,
                kind=kind,
                expect="stop" if stop else "save",
                release=block["name"],
                template=fs["task"]["instruction"],
                slow_ms=int(fs.get("slow_ms", 0)) if "slow" in drift else 0,
                idempotency_key=bool(fs.get("page", {}).get("idempotency_key")),
            )
        )
    return items
