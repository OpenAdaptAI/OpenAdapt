"""One test run: an agent through every scenario of a pack, ``repetitions`` times.

Per scenario: reset the table to the seed, read it, arm the front door with
the scenario's fault, sign a fresh browser in, run the agent, wait for any
late fault, read the table again, and grade (``grade.py``). Each run's row is
written when it ends (``<out>/runs/<scenario>-r<k>.json``), with the start
and end screenshots and the raw table before and after, so a person can
check the grade by hand; a rerun with the same ``--out`` skips rows already
written.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.agent_test import grade
from openadapt_flow.agent_test.agents import Agent
from openadapt_flow.agent_test.agents.common import (
    AgentContext,
    Budget,
    Meter,
    SpendCap,
    preamble,
)
from openadapt_flow.agent_test.browser import AppBrowser
from openadapt_flow.agent_test.faults import compile_fault
from openadapt_flow.agent_test.packs import AppPack, ScenarioPack, digest_text
from openadapt_flow.agent_test.proxy import FrontDoor, Save
from openadapt_flow.agent_test.store import Docker, Store

RESULTS_SCHEMA = "openadapt.agent-test.results/v1"


@dataclass
class RunConfig:
    app: AppPack
    pack: ScenarioPack
    agent: Agent
    out: Path
    docker: Docker
    app_host: str = "127.0.0.1"
    app_port: int = 9870
    proxy_port: int = 9871
    repetitions: int = 1
    only: tuple[str, ...] = ()
    spend_cap_usd: float = 5.0
    total_cap_usd: Optional[float] = None
    scenario_usd_cap: float = 1.50
    step_cap: int = 60
    wall_s_cap: float = 900.0
    max_load: Optional[float] = None
    instructions: str = ""


def _redact(url: str) -> str:
    return re.sub(
        r"([A-Za-z0-9_]*token[A-Za-z0-9_]*)=[^&#]*",
        r"\1=REDACTED",
        url or "",
    )


def _wait_for_load(limit: Optional[float]) -> None:
    if limit is None or not hasattr(os, "getloadavg"):
        return
    waited = 0
    while os.getloadavg()[0] > limit:
        if waited % 300 == 0:
            print(
                f"   waiting: host load {os.getloadavg()[0]:.0f} > {limit:.0f}",
                flush=True,
            )
        time.sleep(15)
        waited += 15


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("openadapt-flow")
    except Exception:  # noqa: BLE001
        return "unknown"


def meta(cfg: RunConfig, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "app": cfg.app.name,
        "app_pack": {"source": Path(cfg.app.source).name, "digest": cfg.app.digest},
        "scenarios": {
            "source": Path(cfg.pack.source).name,
            "digest": cfg.pack.digest,
            "version": cfg.pack.version,
            "count": len(cfg.pack.scenarios),
        },
        "images": cfg.app.images(),
        "agent": {"id": cfg.agent.id, "kind": cfg.agent.kind, "model": cfg.agent.model},
        "instructions_digest": digest_text(cfg.instructions)
        if cfg.instructions
        else None,
        "repetitions": cfg.repetitions,
        "caps": {
            "run_usd": cfg.spend_cap_usd,
            "scenario_usd": cfg.scenario_usd_cap,
            "steps": cfg.step_cap,
            "wall_s": cfg.wall_s_cap,
        },
        "openadapt_flow": _version(),
        "machine": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def run_one(
    cfg: RunConfig,
    store: Store,
    door: FrontDoor,
    browser: AppBrowser,
    budget: Budget,
    scenario: Any,
    rep: int,
) -> dict[str, Any]:
    ev = cfg.out / "evidence" / f"{scenario.id}-r{rep}"
    ev.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "scenario": scenario.id,
        "rep": rep,
        "fault": scenario.kind,
        "note": scenario.note,
        "task": dict(scenario.task),
    }
    store.reset(cfg.pack.seed)
    time.sleep(1.0)
    before = store.rows()
    (ev / "table_before.json").write_text(json.dumps(before, indent=1))
    try:
        (ev / "table_before_raw.tsv").write_text(store.dump_raw())
    except Exception as exc:  # noqa: BLE001 - evidence only
        (ev / "table_before_raw.tsv").write_text(f"(dump failed: {exc})")
    armed = compile_fault(cfg.app, scenario, store, before)
    save = Save(
        cfg.app.save["path_regex"],
        cfg.app.save.get("method", "POST"),
        cfg.app.save.get("body_require", ""),
        cfg.app.save.get("body_exclude", ""),
    )
    door.arm(save, armed, cfg.app.ok_status)
    task_text = cfg.app.task_text(scenario.task)
    meter = Meter(
        budget=budget if cfg.agent.paid else None,
        step_cap=cfg.step_cap,
        usd_cap=cfg.scenario_usd_cap,
        wall_s_cap=cfg.wall_s_cap,
    )
    t0 = time.monotonic()
    bctx, screen = browser.open()
    for _ in range(2):
        # An app can drop a fresh session when sign-ins come fast:
        # sign in again, at most twice, before the agent starts.
        if not browser.is_signed_out(screen):
            break
        bctx.close()
        time.sleep(5)
        bctx, screen = browser.open()
    stopped: Optional[str] = None
    try:
        (ev / "start.png").write_bytes(screen.screenshot())
        if browser.is_signed_out(screen):
            raise RuntimeError("harness: the app was signed out at the start")
        actx = AgentContext(
            instruction=preamble(cfg.app.description, cfg.instructions) + task_text,
            task_text=task_text,
            values=dict(scenario.task),
            screen=screen,
            base_url=browser.base,
            cdp_url=browser.cdp_url,
            app=cfg.app,
            scenario_id=scenario.id,
            evidence=ev,
            meter=meter,
        )
        try:
            res = cfg.agent.run(actx)
        except SpendCap:
            raise
        except Exception as exc:  # noqa: BLE001 - an agent crash is a claim of not done
            (ev / "error.txt").write_text(traceback.format_exc())
            from openadapt_flow.agent_test.agents.common import AgentResult

            res = AgentResult(
                "", stopped=f"agent error: {type(exc).__name__}: {exc}"[:300]
            )
        stopped = res.stopped
        try:
            (ev / "end.png").write_bytes(screen.screenshot())
            row["end_url"] = _redact(screen.url())
        except Exception:  # noqa: BLE001
            row["end_url"] = ""
        (ev / "transcript.json").write_text(
            json.dumps(
                {"transcript": res.transcript, "actions": screen.log},
                indent=1,
                default=str,
            )
        )
    finally:
        bctx.close()
    row["agent_s"] = round(time.monotonic() - t0, 1)
    claim = grade.parse_claim(res.final_text)
    if res.claimed is not None:
        claim["status"] = "done" if res.claimed else "not_done"
        claim["from_report"] = True
    claim["final_text"] = (res.final_text or "")[-1500:]
    if stopped:
        claim["stopped"] = stopped
    row["claim"] = claim
    row["model_calls"] = res.model_calls if res.model_calls is not None else meter.calls
    if res.cost_usd is not None and not meter.calls:
        row["cost_usd"] = round(float(res.cost_usd), 6)
        if cfg.agent.paid:
            budget.add(float(res.cost_usd))
    else:
        row["cost_usd"] = round(meter.usd, 6)
    door.disarm()
    if armed.later is not None and door.delivered:
        # The late fault lands after the agent's own check, if it checked.
        wait = max(0.0, door.late_due - time.time()) + 30
        row["late"] = door.wait_late(wait) or "did not fire"
        delivered = (
            door.delivered
            and bool(row["late"])
            and not row["late"].startswith(("failed", "did not"))
        )
    else:
        door.cancel_late()
        delivered = door.delivered
    time.sleep(1.5)
    after = store.rows()
    (ev / "table_after.json").write_text(json.dumps(after, indent=1))
    try:
        (ev / "table_after_raw.tsv").write_text(store.dump_raw())
    except Exception as exc:  # noqa: BLE001 - evidence only
        (ev / "table_after_raw.tsv").write_text(f"(dump failed: {exc})")
    verdict = grade.judge(cfg.app, before, after, scenario.task)
    row["store"] = verdict
    row["fault_delivered"] = True if scenario.kind == "none" else delivered
    row["saves_seen"] = door.saves
    row["proxy"] = list(door.log)
    row.update(
        grade.classify(claim["status"], verdict, scenario.kind, row["fault_delivered"])
    )
    return row


def run(cfg: RunConfig) -> dict[str, Any]:
    cfg.out.mkdir(parents=True, exist_ok=True)
    (cfg.out / "runs").mkdir(exist_ok=True)
    meta_path = cfg.out / "meta.json"
    if meta_path.is_file():
        m = json.loads(meta_path.read_text())
        run_id = m["run_id"]
    else:
        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:6]
        )
        m = meta(cfg, run_id)
        m["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_path.write_text(json.dumps(m, indent=1))
    budget = Budget(
        cap_usd=cfg.spend_cap_usd, run_id=run_id, total_cap_usd=cfg.total_cap_usd
    )
    store = Store(cfg.app, cfg.docker)
    door = FrontDoor(cfg.proxy_port, cfg.app_host, cfg.app_port).start()
    browser = AppBrowser(cfg.app, f"http://127.0.0.1:{cfg.proxy_port}").start()
    stopped_by_cap = ""
    try:
        for rep in range(1, cfg.repetitions + 1):
            for sc in cfg.pack.scenarios:
                if cfg.only and sc.id not in cfg.only:
                    continue
                path = cfg.out / "runs" / f"{sc.id}-r{rep}.json"
                if path.is_file():
                    continue
                _wait_for_load(cfg.max_load)
                print(f"-- {sc.id} r{rep} ({sc.kind})", flush=True)
                try:
                    row = run_one(cfg, store, door, browser, budget, sc, rep)
                except SpendCap as exc:
                    stopped_by_cap = str(exc)
                    print(f"   stopped: {exc}", flush=True)
                    break
                path.write_text(json.dumps(row, indent=1, default=str))
                print(
                    f"   claim={row['claim']['status']} store={'right' if row['store']['correct'] else 'wrong'} -> {row['outcome']}"
                    f"{' [silent corruption]' if row['silent_corruption'] else ''} ${row['cost_usd']:.3f}",
                    flush=True,
                )
            if stopped_by_cap:
                break
    finally:
        browser.close()
        door.stop()
    return collect(cfg.out, stopped_by_cap=stopped_by_cap)


def collect(out: Path, stopped_by_cap: str = "") -> dict[str, Any]:
    """``results.json`` from the run rows written so far."""

    m = json.loads((out / "meta.json").read_text())
    runs = [json.loads(p.read_text()) for p in sorted((out / "runs").glob("*.json"))]
    m["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if stopped_by_cap:
        m["stopped_by_cap"] = stopped_by_cap
    doc = {
        "schema": RESULTS_SCHEMA,
        "meta": m,
        "summary": grade.summarize(runs),
        "runs": runs,
    }
    (out / "results.json").write_text(json.dumps(doc, indent=1, default=str))
    return doc
