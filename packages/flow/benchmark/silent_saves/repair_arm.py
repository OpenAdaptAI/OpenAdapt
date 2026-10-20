"""Arm 3r: the admitted program under self-repair, on MockMed.

``--agent self-repair[:<bundle dir>]``. Each item runs the admitted MockMed
program through :class:`openadapt_flow.repair.self_repair.SelfRepair` (#1265),
with the shipped :class:`~openadapt_flow.repair.self_repair.ReplayTarget` (run
gate, standard profile, the REST record check). A healthy item makes no model
calls. When an item halts in a way self-repair accepts, the scoreboard's
repair agent (``scoreboard.agent.ClaudeRepairAgent``) proposes one click, the
probe replays it up to the save, the fault probe must stop before the save,
and one approval is asked. The scoreboard's scripted approver answers it,
standing in for a person, and each answer is logged as a scripted human
event with 0 minutes.

The fault probe opens the item's screen with Jane Sample's referral removed
(MockMed's ``missing``); a fix must stop before the save there.

After a repair resumes and verifies, later items run the patched bundle.
Every repair agent call is in the item's ``repair-ledger.json`` and its total
in the cost ledger (:mod:`.spend`), which stops the arm before its cap.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from . import spend
from .executors import BENCHMARK_DIR, Report, Task

DEFAULT_BUNDLE = (
    Path.home()
    / ".cache"
    / "silent-saves"
    / "program-2026-10-18-a"
    / "mockmed.add_note-48f60d6be84e"
    / "program"
    / "bundle"
)
FAULT_PROBES = ("missing",)


class SelfRepairProgram:
    name = "self-repair"
    paid = True

    def __init__(
        self,
        app: Any,
        bundle: str = "",
        arm: str = "program_repair",
        arm_cap_usd: float = 0.0,
        max_cost_usd: float = 0.25,
    ) -> None:
        if str(BENCHMARK_DIR) not in sys.path:
            sys.path.append(str(BENCHMARK_DIR))
        from scoreboard import repair_arm as sb_repair
        from scoreboard import run as sb

        self._sb = sb
        self._sb_repair = sb_repair
        self.app = app
        self.original = Path(bundle).expanduser() if bundle else DEFAULT_BUNDLE
        if not (self.original / "workflow.json").is_file():
            raise SystemExit(f"no program bundle at {self.original}")
        self.bundle = self.original
        # The run gate checks the admission against the key it was signed
        # with, which lives beside the program's cache.
        keys = self.original.parents[2] / "admission-keys"
        if keys.is_dir():
            os.environ.setdefault("OPENADAPT_JIT_KEY_DIR", str(keys))
        self.arm = arm
        self.arm_cap_usd = arm_cap_usd
        self.max_cost_usd = max_cost_usd

    def _open(self, fault: Optional[str]) -> Any:
        """A fresh session on the item's screen, or on a fault probe's."""

        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.repair.self_repair import AppSession

        item_fault, drift, slow_ms, key = self._state
        if fault is None:
            self.app.prepare(item_fault, drift, slow_ms, key)
        else:
            self.app.prepare("ok", drift + (fault,), slow_ms, key)
        backend, close = PlaywrightBackend.launch(self.app.url, headless=True)
        try:
            backend.page.wait_for_load_state("networkidle")
        except BaseException:
            close()
            raise
        return AppSession(backend=backend, close=close)

    def run(self, task: Task) -> Report:
        from scoreboard.agent import ClaudeRepairAgent

        from openadapt_flow.repair.self_repair import (
            ReplayTarget,
            ScreenAccess,
            SelfRepair,
        )
        from openadapt_flow.runtime.effects import RestRecordVerifier

        a = self.app
        self._state = (a.fault, a.drift, a.slow_ms, a.idempotency_key)
        spend.check(self.arm, self.max_cost_usd, self.arm_cap_usd)
        agent = ClaudeRepairAgent(ledger_path=task.work_dir / "repair-ledger.json")
        target = ReplayTarget(
            self._open,
            effect_verifier=RestRecordVerifier(
                a.store_base, records_path="/api/db", records_key="records"
            ),
            policy="clinical-write",
            profile="standard",
            params=task.params,
            entry_url=task.url,
            execution_origin=a.base,
        )
        run_dir = task.work_dir / "run"
        asked: list[int] = []
        error: Optional[str] = None
        result = None
        try:
            result = SelfRepair(
                agent,
                target,
                store=task.work_dir.parent.parent / "repair-store",
                screen_access=ScreenAccess(
                    enabled_by="silent-saves benchmark (synthetic data only)",
                    mode="allow_unscrubbed",
                ),
                model_key=self._sb.model_key(),
                fault_probes=FAULT_PROBES,
                approval_wait_s=120,
                sleep=self._sb_repair._scripted_answer(run_dir, asked),
            ).run(self.bundle, run_dir)
        except Exception as exc:  # noqa: BLE001 - an executor error is a row
            error = f"{type(exc).__name__}: {exc}"
        finally:
            # The fault probe changed the page; the scored store is the item's.
            a.fault, a.drift, a.slow_ms, a.idempotency_key = self._state
        cost = float(getattr(agent, "cost_usd", 0.0) or 0.0)
        if cost:
            spend.add(cost, self.arm, f"item {task.item} repair agent")
        events = [
            {"kind": "repair_approval", "minutes": 0, "by": "scripted"} for _ in asked
        ]
        if result is None:
            return Report(
                False,
                error=error,
                cost_usd=round(cost, 6),
                model_calls=len(getattr(agent, "calls", [])),
                extra={"human_events": events},
            )
        if result.status == "resumed" and result.verified and result.patched_bundle:
            self.bundle = Path(result.patched_bundle)
        report = result.report
        halted = next((r for r in report.results if not r.ok), None)
        detail = f"self_repair={result.status}"
        if halted is not None:
            detail += f"; halted {halted.step_id}: {(halted.error or '')[:200]}"
        return Report(
            claimed_success=bool(result.verified),
            final_text=detail[:2000],
            model_calls=int(report.model_calls or 0) + result.evidence.model_calls,
            cost_usd=round(cost, 6),
            error=error,
            extra={"human_events": events, "self_repair": result.status},
        )
