"""Arm ``human_script`` (optional): a Playwright + REST script written once by hand.

The script (``human_script_v1.py``) was written by this benchmark's session the
way a skilled engineer would write it, from the same demonstration, procedure
and REST read the other arms get, and tested on the offline mock before it ran
against the fixture. Its authoring time is logged below. That time is a coding
agent's wall time, so it understates what a person would spend; the report
shows the setup charge with this measured time and with the cost model's
engineer assumption.

No one maintains it during a run: when it breaks (an exception or an
"UNEXPECTED" stop), the item goes to the operator, and so does every later item
it breaks on. The fix an engineer writes afterwards is timed and reported as
the maintenance cost per drift event.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..arm import Env, ItemResult, Operator, OperatorRequest, WorkItem
from . import budget, scripts
from .openemr_ui import OpenEMRSession, RestReader

HERE = Path(__file__).resolve().parent

#: Wall-clock authoring log (UTC), appended as the work happens.
AUTHORING = [
    {"what": "write v1 from the demonstration and SOP", "start": "2026-10-20T16:39:05Z", "end": "2026-10-20T16:42:39Z",
     "note": "includes the first offline mock run (dry run, save, duplicate, typo date, missing date, failed save, interstitial)"},
]


def authoring_seconds() -> float:
    from datetime import datetime

    def t(s: str) -> float:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").timestamp()

    return sum(t(a["end"]) - t(a["start"]) for a in AUTHORING)


class HumanScriptArm:
    name = "human_script"

    def __init__(self, options: dict[str, Any]) -> None:
        self.version = str(options.get("version", "v1"))
        self.headless = bool(options.get("headless", True))
        self.session: Optional[OpenEMRSession] = None
        self.rest: Optional[RestReader] = None
        self.source = (HERE / f"human_script_{self.version}.py").read_text()
        self.breaks: list[dict[str, Any]] = []

    def describe(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "kind": "hand-written Playwright + REST script with read-back; not maintained during the run",
            "version": self.version,
            "authoring_log": AUTHORING,
            "authoring_s": authoring_seconds(),
            "per_item_model_calls": 0,
        }

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        self.session = OpenEMRSession(env, headless=self.headless)
        self.rest = RestReader(env)
        self.session.start()
        self.session.sign_in()
        return [
            {
                "phase": "author_script_by_hand",
                "wall_s": authoring_seconds(),
                # The engineer attends all of their own authoring time.
                "attended_s": authoring_seconds(),
                "model_calls": 0,
                "cost_usd": 0.0,
                "detail": {"authoring_log": AUTHORING, "note": "a coding agent's wall time; a person's would be longer"},
            }
        ]

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult:
        assert self.session is not None and self.rest is not None
        t0 = time.time()
        out = scripts.run(self.source, self.session, self.rest, item.params, commit=True)
        if out.broke:
            self.breaks.append({"item_id": item.item_id, "reason": out.reason[-300:]})
        claimed = {"SAVED": "done", "NOT SAVED": "stopped"}.get(out.result, "error")
        status = f"{out.result}: {out.reason}"[:500]
        if claimed != "done":
            operator.ask(OperatorRequest(kind="stop", item_id=item.item_id, reason=status[:300], surface="script result"))
        wall = time.time() - t0
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed,
            product_status=status,
            wall_s=round(wall, 2),
            evidence=f"{self.name}/",
            detail={"version": self.version, "broke": out.broke, "log": out.log[-10:], "usage": [],
                    "compute_usd": round(budget.compute_usd(wall), 6)},
        )

    def after_drift(self, event: dict[str, Any], env: Env, operator: Operator) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def factory(options: dict[str, Any]) -> HumanScriptArm:
    return HumanScriptArm(options)
