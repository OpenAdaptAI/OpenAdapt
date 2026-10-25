"""An in-memory app and two toy arms, for tests and for checking a new arm.

``FakeApp`` keeps a patient table in memory and honours the same injected
events as the OpenEMR adapter (a failed save raises, a forced logout signs the
arm out, a UI change is just a flag). ``naive`` types whatever the referral
says and always claims done; ``careful`` asks the operator about anything odd
and stops on a failed save. Neither is a model of a product; they exist so the
harness's bookkeeping can be checked end to end in a second.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .arm import Env, ItemResult, OperatorRequest, WorkItem


class FakeApp:
    ui_url = "fake://app"
    api_url = None

    def __init__(self) -> None:
        self.rows_: list[dict[str, str]] = []
        self.next_id = 1
        self.fail_saves: dict[str, int] = {}
        self.signed_in = True
        self.drift_on: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"app": "fake", "version": "0"}

    def extra(self) -> dict[str, Any]:
        return {"app": self}

    def accounts(self) -> list[tuple[str, str]]:
        return [("clerk", "synthetic")]

    def reset(self) -> str:
        self.rows_, self.next_id, self.fail_saves = [], 1, {}
        return "fake-baseline"

    def rows(self) -> list[dict[str, str]]:
        return [dict(r) for r in self.rows_]

    def _insert(self, fields: dict[str, str]) -> str:
        email = fields.get("email", "")
        if self.fail_saves.get(email, 0) > 0:
            self.fail_saves[email] -= 1
            raise RuntimeError("save failed")
        rid = str(self.next_id)
        self.next_id += 1
        row = {"id": rid, "pid": rid, "state": "MA"}
        row.update({k: v for k, v in fields.items() if k != "state_label"})
        self.rows_.append(row)
        return rid

    def seed_existing(self, people: list[dict[str, str]]) -> list[str]:
        return [self._insert(p) for p in people]

    def create_chart(self, fields: dict[str, str], *, who: str) -> str:
        return self._insert(
            {
                **fields,
                "fname": fields["fname"].strip(),
                "lname": fields["lname"].strip(),
            }
        )

    def inject(
        self,
        inj: dict[str, Any],
        truth: Any,
        *,
        harness: set[str],
        stop: Optional[threading.Event] = None,
    ) -> dict[str, Any]:
        ev = inj["event"]
        if ev == "other_clerk_creates":
            return {
                "event": ev,
                "chart_id": self.create_chart(inj["person"], who="clerk"),
            }
        if ev == "force_logout":
            self.signed_in = False
            return {"event": ev, "sessions_ended": 1}
        if ev == "other_clerk_edits_unrelated":
            target = next((r for r in self.rows_ if r["id"] not in harness), None)
            if target:
                target["phone_home"] = "555-0199"
            return {"event": ev, "touched_ids": [target["id"]] if target else []}
        if ev == "failed_save":
            self.fail_saves[truth.person["email"]] = int(inj.get("times", 1))
            return {"event": ev}
        if ev == "slow_save":
            return {"event": ev}
        raise ValueError(ev)

    def clear_item_events(self) -> list[str]:
        self.fail_saves.clear()
        return []

    def apply_drift(self, kind: str) -> dict[str, Any]:
        self.drift_on.append(kind)
        return {"kind": kind}

    def clear_all_events(self) -> None:
        self.drift_on = []
        self.fail_saves.clear()
        self.signed_in = True

    # what an arm sees
    def find(self, fname: str, lname: str) -> list[dict[str, str]]:
        return [
            r
            for r in self.rows_
            if r["fname"].lower() == fname.strip().lower()
            and r["lname"].lower() == lname.strip().lower()
        ]


class _ToyArm:
    name = "toy"
    careful = False

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def describe(self) -> dict[str, Any]:
        return {"kind": self.name, "toy": True, "refused_features": []}

    def setup(self, env: Env, operator: Any) -> list[dict[str, Any]]:
        return [
            {
                "phase": "teach",
                "wall_s": 60.0,
                "attended_s": 60.0,
                "model_calls": 0,
                "cost_usd": 0.0,
            }
        ]

    def after_drift(
        self, event: dict[str, Any], env: Env, operator: Any
    ) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        pass

    def run_item(self, item: WorkItem, env: Env, operator: Any) -> ItemResult:
        app: FakeApp = env.extra["app"]
        t0 = time.time()
        p = item.params
        if not app.signed_in:
            app.signed_in = True  # signs in again
        if self.careful:
            odd = (
                "DOB" not in p
                or "/" in p.get("DOB", "")
                or p["fname"] != p["fname"].strip()
                or app.find(p["fname"], p["lname"])
            )
            if odd:
                d = operator.ask(
                    OperatorRequest(
                        kind="stop",
                        item_id=item.item_id,
                        reason="looks odd",
                        surface="toy",
                        detail={"resumable": False},
                    )
                )
                return ItemResult(
                    item.item_id, "stopped", f"stopped: {d.action}", time.time() - t0
                )
        try:
            app._insert(dict(p))
        except RuntimeError as exc:
            if self.careful:
                d = operator.ask(
                    OperatorRequest(
                        kind="stop",
                        item_id=item.item_id,
                        reason=str(exc),
                        surface="toy",
                        detail={"resumable": True},
                    )
                )
                if d.action == "resume":
                    app._insert(dict(p))
                    return ItemResult(
                        item.item_id, "done", "saved after resume", time.time() - t0
                    )
                return ItemResult(item.item_id, "stopped", str(exc), time.time() - t0)
            return ItemResult(
                item.item_id, "done", "saved (error ignored)", time.time() - t0
            )
        return ItemResult(item.item_id, "done", "saved", time.time() - t0)


class NaiveArm(_ToyArm):
    name = "naive"


class CarefulArm(_ToyArm):
    name = "careful"
    careful = True


def naive(options: dict[str, Any]) -> NaiveArm:
    return NaiveArm(options)


def careful(options: dict[str, Any]) -> CarefulArm:
    return CarefulArm(options)


class StallingArm(CarefulArm):
    """The careful arm, except one item hangs (for the runner's watchdog)."""

    name = "stalling"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.stall_seq = int(options.get("stall_seq", 2))
        self.recovered = 0

    def run_item(self, item: Any, env: Any, operator: Any) -> ItemResult:
        if item.seq == self.stall_seq:
            time.sleep(30)
        return super().run_item(item, env, operator)

    def recover_after_timeout(self) -> None:
        self.recovered += 1


def stalling(options: dict[str, Any]) -> StallingArm:
    return StallingArm(options)
