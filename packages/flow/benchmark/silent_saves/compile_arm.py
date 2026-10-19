"""Arm 2: the agent records once under OpenAdapt, then the program runs.

``--agent compile-agent[:<cache root>]``. The scoreboard's agent (Claude
Sonnet 5.5 computer use) does the first item under
:func:`openadapt_flow.jit.compile_agent`, which records, reads back and
compiles it. Admission follows: confirm parameters, review, an attended
canary and admit, each done by this script standing in for a person and
logged as a scripted human event with 0 minutes, as on the scoreboard. From
then on the admitted program runs each item, with no model calls.

When the program halts, the agent records again only if the halt is
allowed: nothing was written, and the halt was not a record check refusing a
save. A new recording goes to a fresh cache, so it compiles and is admitted
like the first. Every recording's spend goes to the cost ledger
(:mod:`.spend`), which stops the arm before its cap.

Admission runs at the start of the next item, before that item's program
run resets the store, so the canary's save never reaches a scored store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import spend
from .executors import BENCHMARK_DIR, Report, Task

TASK_ID = "mockmed.add_note_by_name"
SCRIPTED = "scripted"
CANARY_NOTE = "Synthetic canary note: recheck in 30 days"


class CompileAgent:
    name = "compile-agent"
    paid = True

    def __init__(
        self,
        app: Any,
        cache_root: str = "",
        arm: str = "compile_agent",
        arm_cap_usd: float = 0.0,
        max_cost_usd: float = 0.25,
    ) -> None:
        if str(BENCHMARK_DIR) not in sys.path:
            sys.path.append(str(BENCHMARK_DIR))
        from scoreboard import run as sb

        self._sb = sb
        self.app = app
        self.root = Path(
            cache_root or Path.home() / ".cache" / "silent-saves" / "compile-agent"
        ).expanduser()
        self.arm = arm
        self.arm_cap_usd = arm_cap_usd
        self.max_cost_usd = max_cost_usd
        self.generation = self._last_generation()
        self.pending_admission = False
        self.recordings = 0

    # -- caches ---------------------------------------------------------------

    def _last_generation(self) -> int:
        gens = sorted(
            int(p.name.split("-")[1]) for p in self.root.glob("gen-*") if p.is_dir()
        )
        return gens[-1] if gens else 0

    @property
    def cache(self) -> Path:
        return self.root / f"gen-{self.generation}"

    def _state(self) -> tuple[Any, ...]:
        a = self.app
        return (a.fault, a.drift, a.slow_ms, a.idempotency_key)

    def _jit(self, task: Task, agent: Any = None) -> Any:
        from openadapt_flow.jit import TaskSignature, WebApp, compile_agent
        from openadapt_flow.runtime.effects import RestRecordVerifier

        state = self._state()
        template = task.instruction.replace(task.params["note"], "{note}")
        return compile_agent(
            agent,
            task=TaskSignature(TASK_ID, template, {"note": str}),
            app=WebApp(
                url=task.url,
                record_reader=self.app.records,
                reset=lambda: self.app.prepare(*state),
            ),
            verifier=RestRecordVerifier(
                self.app.store_base, records_path="/api/db", records_key="records"
            ),
            policy="clinical-write",
            cache_dir=self.cache,
            key_dir=self.cache / "admission-keys",
            model_key=self._sb.model_key() if agent is not None else None,
            dialect="anthropic",
        )

    # -- admission, scripted --------------------------------------------------

    def _admit(self, task: Task) -> list[dict[str, Any]]:
        sb = self._sb
        jit = self._jit(task)
        # The canary runs on this item's screen, with the store reset to it.
        self.app.prepare(
            "ok", self.app.drift, self.app.slow_ms, self.app.idempotency_key
        )
        result = sb._admission(jit, ["note"], {"note": CANARY_NOTE}, self.cache)
        events = [
            {"kind": k, "minutes": 0, "by": SCRIPTED}
            for k in ("confirm_params", "review", "canary")
        ]
        if result.get("admitted"):
            events.append({"kind": "admit", "minutes": 0, "by": SCRIPTED})
        (task.work_dir / "admission.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
        return events

    # -- one item -------------------------------------------------------------

    def _record(self, task: Task, why: str, events: list[dict[str, Any]]) -> Report:
        sb = self._sb
        spend.check(self.arm, self.max_cost_usd, self.arm_cap_usd)
        if self.cache.exists():
            self.generation += 1  # a fresh cache, so this recording compiles anew
        self.recordings += 1
        agent = sb.ClaudeComputerUseAgent(
            ledger_path=task.work_dir / "ledger.json",
            model=sb.AGENT_MODEL,
            system=sb.SYSTEM_TEMPLATE.format(
                app="a clinic's electronic medical record (MockMed)"
            ),
            max_cost_usd=self.max_cost_usd,
        )
        error: Optional[str] = None
        status = ""
        try:
            result = self._jit(task, agent).run(**task.params)
            status = result.status
            self.pending_admission = result.candidate is not None
        except Exception as exc:  # noqa: BLE001 - an executor error is a row
            error = f"{type(exc).__name__}: {exc}"
        spend.add(agent.cost_usd, self.arm, f"item {task.item} recording ({why})")
        return Report(
            claimed_success=sb.reported_success(agent, error),
            final_text=f"[recorded by the agent: {why}; {status}] {agent.final_text}"[
                :2000
            ],
            model_calls=len(agent.calls),
            cost_usd=round(agent.cost_usd, 6),
            error=error,
            extra={"human_events": events, "path": "recording"},
        )

    def run(self, task: Task) -> Report:
        events: list[dict[str, Any]] = []
        if self.pending_admission:
            self.pending_admission = False
            events = self._admit(task)
            self.app.prepare(*self._state())  # the item's own state again
        jit = self._jit(task)
        if not jit.admitted:
            why = "first run" if not self.recordings else "no admitted program"
            return self._record(task, why, events)
        before = self.app.records()
        try:
            result = jit.run(**task.params)
        except Exception as exc:  # noqa: BLE001 - an executor error is a row
            return Report(
                False,
                error=f"{type(exc).__name__}: {exc}",
                extra={"human_events": events},
            )
        if result.verified:
            return Report(
                True,
                final_text=str(result.status),
                extra={"human_events": events, "path": "program"},
            )
        halt = str(result.halt_reason or result.detail or result.status)
        unchanged = self.app.records() == before
        allowed = unchanged and "effect verification" not in halt
        if not allowed:
            return Report(
                False,
                final_text=f"[program halted; not re-recorded] {halt}"[:2000],
                extra={"human_events": events, "path": "program"},
            )
        return self._record(task, f"program halted: {halt[:120]}", events)
