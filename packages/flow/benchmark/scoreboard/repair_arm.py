"""Arm 3 with self-repair: the openIMIS batch, each item run through SelfRepair.

    python -m scoreboard.repair_arm --program-run sb-sonnet-1 --run-id <id> --tag <2-3 letters> --start 50 --n 100

Each item runs the compiled program under
:class:`openadapt_flow.repair.self_repair.SelfRepair` (#1265, with the
standard-change rules of #1294 available but no signed repair policy, so any
fix goes to a person). A healthy item records ``not_needed`` and no model
calls. When an item halts, self-repair decides whether it may repair the halt;
if it may, the Claude repair agent (``agent.ClaudeRepairAgent``) proposes one
click, the probe replays it up to the save, and one approval is asked.

The program runs on the demo profile, as arm 3 does on openIMIS (it is not
admitted), with the same read-only SQL record check. The approval, if one is
ever asked, is answered by this script standing in for the person
(``SCRIPTED_APPROVER``), like arm 2's scripted admission, and counted in
``approvals_asked``.

No fault probe is configured: openIMIS identity is read from the DOM, so a
halt that self-repair accepts stops at ``needs_fault_probes`` until a real
fault session exists for this app.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402
from scoreboard.run import SPEND_CAP_USD, add_spend, model_key, spent  # noqa: E402

REPO = HERE.parents[3]
PROGRAMS = REPO / "demo" / "cua_to_program" / "out" / "program"
SCRIPTED_APPROVER = "scoreboard:scripted-approver (stands in for a person)"


class DemoTarget:
    """A SelfRepairTarget: the demo profile's plain Replayer, SQL record check."""

    def __init__(self, app: Any, values: dict[str, str]) -> None:
        self.app = app
        self.values = values

    def open(self, fault: Optional[str] = None) -> Any:
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.repair.self_repair import AppSession

        backend, close = PlaywrightBackend.launch(
            self.app.claim_form_url(), headless=True
        )
        try:
            self.app.setup(backend.page)
        except BaseException:
            close()
            raise
        return AppSession(backend=backend, close=close)

    def execute(
        self,
        workflow: Any,
        bundle_dir: Path,
        session: Any,
        run_dir: Path,
        *,
        approval_gate: Any = None,
    ) -> Any:
        from openadapt_flow.runtime import Replayer

        return Replayer(
            session.backend,
            effect_verifier=self.app.claims_verifier(),
            approval_gate=approval_gate,
        ).run(workflow, params=self.values, bundle_dir=bundle_dir, run_dir=run_dir)


def _scripted_answer(run_dir: Path, asked: list[int]) -> Any:
    """SelfRepair's wait hook: approve each pending request, once."""

    from openadapt_flow.console.save_approvals import (
        SaveApprovalActionRequest,
        decide_save_approval,
        list_save_approvals,
        save_approval_detail,
    )

    def sleep(seconds: float) -> None:
        for row in list_save_approvals(run_dir.parent):
            detail = save_approval_detail(run_dir.parent, row.request_id)
            decide_save_approval(
                run_dir.parent,
                row.request_id,
                SaveApprovalActionRequest(
                    action="verify_and_resume",
                    task_digest=detail["task_digest"],
                    task_signature=detail["task"]["signature"],
                ),
            )
            asked.append(1)
        time.sleep(min(seconds, 0.5))

    return sleep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--program-run", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--ui-change-at", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-cost", type=float, default=0.50)
    args = parser.parse_args()

    from scoreboard.tasks import openimis as app

    bundle = PROGRAMS / args.program_run / "bundle"
    out = schema.run_dir("openimis", "program_repair", args.run_id)
    items = out / "items.csv"
    cache = schema.CACHE / "openimis" / "program_repair" / args.run_id
    schema.write_meta(
        out,
        task="openimis",
        arm="program_repair",
        program_run=args.program_run,
        profile="demo (plain Replayer, SQL record check attached); not admitted",
        repair="SelfRepair (#1265); no signed repair policy (#1294); no fault probe",
        approver=SCRIPTED_APPROVER,
        model="claude-sonnet-5-5 (repair agent, only if a halt is accepted)",
    )
    key = model_key()
    app.ui_change(False)
    state = {"ui_change": False}
    try:
        _batch(args, app, bundle, out, items, cache, key, state)
    finally:
        app.ui_change(False)
    print("ui change off:", not app.ui_change_is_on())
    return 0


def _batch(
    args: Any,
    app: Any,
    bundle: Path,
    out: Path,
    items: Path,
    cache: Path,
    key: Any,
    state: dict[str, bool],
) -> None:
    from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair
    from scoreboard.agent import ClaudeRepairAgent

    paused_at: Optional[int] = None
    for i in range(args.start, args.n + 1):
        changed = args.ui_change_at > 0 and i >= args.ui_change_at
        values = app.item_values(i, args.tag)
        if paused_at is not None:
            schema.append(
                items,
                schema.Row(
                    task="openimis",
                    arm="program_repair",
                    run_id=args.run_id,
                    item=i,
                    attempt=0,
                    fault="ui_change" if changed else "none",
                    reported_success=False,
                    readback="not_checked",
                    outcome="paused",
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                    item_s=0.0,
                    wall_s=0.0,
                    approvals_asked=0,
                    host_load_1m=-1.0,
                    timed=False,
                    detail=f"batch paused for a person at item {paused_at}",
                ),
            )
            continue
        if changed != state["ui_change"]:
            app.ui_change(changed)
            state["ui_change"] = changed
        for attempt in range(1, args.max_attempts + 1):
            if spent() + args.max_cost > SPEND_CAP_USD:
                raise SystemExit("spend cap reached")
            run_dir = cache / f"item-{i:03d}-{attempt}" / "run"
            if run_dir.parent.exists():
                shutil.rmtree(run_dir.parent)
            agent = ClaudeRepairAgent(
                ledger_path=out / "ledgers" / f"item-{i:03d}-{attempt}.json"
            )
            asked: list[int] = []
            load0 = schema.load_1m()
            t0 = time.time()
            result = SelfRepair(
                agent,
                DemoTarget(app, values),
                store=cache / "repair-store",
                screen_access=ScreenAccess(
                    enabled_by="scoreboard (synthetic data only)",
                    mode="allow_unscrubbed",
                ),
                model_key=key,
                approval_wait_s=120,
                sleep=_scripted_answer(run_dir, asked),
            ).run(bundle, run_dir)
            wall = time.time() - t0
            load1 = schema.load_1m()
            if agent.cost_usd:
                add_spend(
                    agent.cost_usd,
                    f"openimis program_repair {args.run_id} item {i} attempt {attempt}",
                )
            readback = app.check(values)
            report = result.report
            success = bool(report.success) and result.status in (
                "not_needed",
                "resumed",
            )
            ledger = result.evidence
            halted = next((r for r in report.results if not r.ok), None)
            schema.append(
                items,
                schema.Row(
                    task="openimis",
                    arm="program_repair",
                    run_id=args.run_id,
                    item=i,
                    attempt=attempt,
                    fault="ui_change" if changed else "none",
                    reported_success=success,
                    readback=readback,
                    outcome=schema.classify(success, readback, "none"),
                    model_calls=int(report.model_calls or 0) + ledger.model_calls,
                    input_tokens=sum(c.input_tokens for c in agent.calls),
                    output_tokens=sum(c.output_tokens for c in agent.calls),
                    cache_read_tokens=sum(
                        c.cache_read_input_tokens for c in agent.calls
                    ),
                    cache_write_tokens=sum(
                        c.cache_creation_input_tokens for c in agent.calls
                    ),
                    cost_usd=round(agent.cost_usd, 6),
                    item_s=round(wall, 3),
                    wall_s=round(wall, 3),
                    approvals_asked=len(asked),
                    host_load_1m=load0,
                    timed=schema.timed(load0, load1),
                    detail=(
                        f"self_repair={result.status}"
                        + (
                            f" ({getattr(ledger, 'reason', '') or ''})"
                            if result.status not in ("not_needed", "resumed")
                            else ""
                        )
                        + (
                            f"; halted {halted.step_id}: {(halted.error or '')[:160]}"
                            if halted is not None
                            else ""
                        )
                    )[:400],
                ),
            )
            print(
                f"openimis program_repair {i:3d}/{attempt} {result.status:16s} "
                f"{readback:8s} {wall:6.1f}s load {load0}",
                flush=True,
            )
            if readback != "missing":
                break
        else:
            if changed:
                paused_at = i


if __name__ == "__main__":
    raise SystemExit(main())
