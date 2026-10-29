"""Arm ``cua_alone``: a screenshot-driven computer-use agent, alone.

The current frontier model drives the browser through the computer use toolset
(``computer_toolset_20260801``). It sees screenshots only: no DOM, no API, no
record read-back. Nothing records or checks it. Each item has a fixed budget
(model calls and dollars); an item that runs out is a stop the operator sees.

Actions go to the page through the scoreboard's :class:`PlainComputer`, the
same translator the earlier scoreboard agent arm used.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from ..arm import Env, ItemResult, Operator, OperatorRequest, WorkItem
from . import budget
from .openemr_ui import SOP, OpenEMRSession, claimed_from

DISABLED_MEMBERS = (
    "zoom",
    "triple_click",
    "middle_click",
    "left_mouse_down",
    "left_mouse_up",
    "hold_key",
)

SYSTEM = """\
You operate a web browser that shows one page of a clinic's electronic medical \
record (OpenEMR). There is no address bar and no other application. The screen \
is 1280x800 pixels. Work only on the page you are shown. Do not navigate away, \
open menus you do not need, or change any field the task does not mention.

Click a field before you type into it. To scroll, scroll at the point you last \
clicked. When the task is done, stop and reply; do not take further actions."""


class CuaArm:
    name = "cua_alone"

    def __init__(self, options: dict[str, Any]) -> None:
        self.name = str(options.get("name", self.name))
        self.model = options.get("model", "claude-fable-5-1")
        self.effort = options.get("effort", "medium")
        self.max_turns = int(options.get("max_turns", 40))
        self.item_budget_usd = float(options.get("item_budget_usd", 1.50))
        self.headless = bool(options.get("headless", True))
        self.session: OpenEMRSession | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "kind": "computer-use agent alone (screenshots only)",
            "model": self.model,
            "effort": self.effort,
            "tool": "computer_toolset_20260801",
            "disabled_members": list(DISABLED_MEMBERS),
            "max_turns_per_item": self.max_turns,
            "item_budget_usd": self.item_budget_usd,
            "prices_as_of": budget.prices()["as_of"],
            "sees": "screenshots of the OpenEMR page",
            "reads_record": False,
        }

    def setup(self, env: Env, operator: Operator) -> list[dict[str, Any]]:
        # Writing the procedure (the SOP above) is the only setup; it is shared
        # by every agent arm and charged the same to each.
        self.session = OpenEMRSession(env, headless=self.headless)
        t0 = time.time()
        self.session.start()
        self.session.sign_in()
        return [
            {
                "phase": "write_procedure",
                "wall_s": round(time.time() - t0, 2),
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "detail": {"note": "the shared SOP; its writing time is charged by the cost model's procedure rule if any"},
            }
        ]

    def after_drift(self, event: dict[str, Any], env: Env, operator: Operator) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    def run_item(self, item: WorkItem, env: Env, operator: Operator) -> ItemResult:
        try:
            from benchmark.scoreboard.agent import PlainComputer
        except ImportError:  # run from packages/flow/benchmark
            from scoreboard.agent import PlainComputer

        assert self.session is not None
        prep = self.session.blank_form()
        meter = budget.Meter(f"{self.name} {item.item_id}", self.item_budget_usd)
        evidence = Path(env.workdir) / self.name / item.item_id
        evidence.mkdir(parents=True, exist_ok=True)
        computer = PlainComputer(self.session.page)
        t0 = time.time()
        final_text, error = "", ""
        turns = 0
        try:
            final_text, turns = self._loop(item, computer, meter, evidence)
        except (budget.ItemBudgetExceeded, budget.SpendCapReached) as exc:
            if isinstance(exc, budget.SpendCapReached):
                raise
            error = f"budget: {exc}"
        except Exception as exc:  # noqa: BLE001 - an agent crash is a stop
            error = f"{type(exc).__name__}: {exc}"[:500]
        wall = time.time() - t0
        claimed, status = claimed_from(final_text) if not error else ("error", error)
        if claimed != "done":
            operator.ask(
                OperatorRequest(
                    kind="stop",
                    item_id=item.item_id,
                    reason=status,
                    surface="agent final reply",
                )
            )
        (evidence / "reply.json").write_text(
            json.dumps({"final_text": final_text, "error": error, "turns": turns}, indent=1)
        )
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed if claimed != "error" else "error",
            product_status=status,
            wall_s=round(wall, 2),
            model_calls=meter.calls,
            cost_usd=round(meter.usd, 6),
            evidence=str(evidence.relative_to(env.workdir)),
            detail={
                **budget.summarize(meter),
                **prep,
                "turns": turns,
                "compute_usd": round(budget.compute_usd(wall), 6),
            },
        )

    def _loop(self, item: WorkItem, computer: Any, meter: budget.Meter, evidence: Path) -> tuple[str, int]:
        client = budget.client(self.model)
        tools = [
            {
                "type": "computer_toolset_20260801",
                "configs": {m: {"enabled": False} for m in DISABLED_MEMBERS},
            }
        ]
        first = computer.execute({"action": "screenshot"})
        instruction = SOP + "\n\nThe referral:\n" + item.source_text
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    _image(first.png),
                ],
            }
        ]
        ledger: list[dict[str, Any]] = []
        for turn in range(1, self.max_turns + 1):
            # An upper bound for this call: history so far, uncached, plus 16k out.
            approx_in = 4000 + 1600 * turn
            meter.before_call(self.model, budget.max_call_usd(self.model, approx_in, 4000))
            t0 = time.time()
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
            usd = meter.record(self.model, response.usage)
            calls = [b for b in response.content if b.type == "tool_use"]
            ledger.append(
                {
                    "turn": turn,
                    "s": round(time.time() - t0, 2),
                    "usd": round(usd, 6),
                    "stop_reason": response.stop_reason,
                    "actions": [{"name": b.name, "input": b.input} for b in calls],
                }
            )
            (evidence / "calls.json").write_text(json.dumps(ledger, indent=1))
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                return "RESULT: NOT SAVED: the model declined the request", turn
            if not calls:
                return " ".join(b.text for b in response.content if b.type == "text"), turn
            results: list[dict[str, Any]] = []
            for block in calls:
                native = {"action": block.name, **dict(block.input)}
                if block.name == "key" and block.input.get("repeat"):
                    n = max(1, min(int(block.input["repeat"]), 100))
                    native = {"action": "key", "text": " ".join([str(block.input.get("text", ""))] * n)}
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "toolset_name": "computer",
                }
                try:
                    obs = computer.execute(native)
                except Exception as exc:  # noqa: BLE001 - tell the model, let it choose
                    result["content"] = f"Error: {exc}"[:300]
                    result["is_error"] = True
                    results.append(result)
                    continue
                if block.name == "screenshot" or block is calls[-1]:
                    content: list[dict[str, Any]] = []
                    if obs.notes:
                        content.append({"type": "text", "text": "; ".join(obs.notes)})
                    content.append(_image(obs.png))
                    result["content"] = content
                else:
                    result["content"] = [{"type": "text", "text": "OK"}]
                results.append(result)
            messages.append({"role": "user", "content": results})
        return f"RESULT: NOT SAVED: no finish within {self.max_turns} model calls", self.max_turns


def _image(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
    }


def factory(options: dict[str, Any]) -> CuaArm:
    return CuaArm(options)
