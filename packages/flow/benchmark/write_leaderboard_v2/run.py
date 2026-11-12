"""Run one agent through write leaderboard v2 on one app: every case, k times.

    python -m benchmark.write_leaderboard_v2.run --agent claude --agent openai \\
        --app dolibarr --slot lb --port 9850 --cases cases.yaml [--cases <held-out file>]

The app runs in local containers; the agent's browser reaches it only
through the front door (proxy.py), where the case's fault waits. Per run:
reset the app's records to the case's seed, read the record table, run the
agent, read the table again, and judge the end state (apps/base.py). Medplum
runs through version 1's harness (``fhir_medplum/public/run.py``'s Bench,
proxy and oracle), unchanged.

Runs go repetition by repetition; within a repetition, every cases file in
the order given, each in file order; for each case the agents take turns
on the same app instance, in the order given. Each run's row is written when it ends
(``<out>/<agent>/<app>/r<k>-<case>.json``); a rerun skips rows already
written. ``--out`` for a held-out file must be outside the public repository.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from benchmark.write_leaderboard_v2 import common
from benchmark.write_leaderboard_v2.common import (
    Meter,
    SpendCap,
    check_spend,
    ledger,
    parse_claim,
    preamble,
)

HERE = Path(__file__).resolve().parent
V1_AGENTS = "benchmark.fhir_medplum.leaderboard.agents"

#: The agents, pre-registered: name -> (version 1 adapter module, exact model id).
AGENTS: dict[str, tuple[str, str]] = {
    "claude": ("claude_cu", "claude-sonnet-5-5"),
    "openai": ("openai_cu", "gpt-6.1-sol"),
    "gemini": ("gemini_cu", "gemini-3.8-flash"),
}

APPS = {
    "dolibarr": "benchmark.write_leaderboard_v2.apps.dolibarr:Dolibarr",
    "espocrm": "benchmark.write_leaderboard_v2.apps.espocrm:EspoCRM",
    "redmine": "benchmark.write_leaderboard_v2.apps.redmine:Redmine",
    "openemr": "benchmark.write_leaderboard_v2.apps.openemr:OpenEMR",
}

LOAD_LIMIT = 35.0


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=HERE, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def redact_url(url: str) -> str:
    """A URL without session tokens in its query (OpenEMR's ``token_main``)."""

    return re.sub(r"(token_main|token|csrf_token_form|site_token)=[^&#]*", r"\1=REDACTED", url or "")


def wait_for_load() -> int:
    waited = 0
    while os.getloadavg()[0] > LOAD_LIMIT:
        if waited % 600 == 0:
            print(f"   waiting: host load {os.getloadavg()[0]:.0f} > {LOAD_LIMIT:.0f}", flush=True)
        time.sleep(30)
        waited += 30
    return waited


def make_runner(agent: str, phase: str) -> Callable[..., Any]:
    """The adapter's ``run``, or a ``py:<module>:<function>`` of your own."""

    if agent in AGENTS:
        mod = importlib.import_module(f"{V1_AGENTS}.{AGENTS[agent][0]}")
        return mod
    _, m, fn = agent.split(":", 2)
    return getattr(importlib.import_module(m), fn)


def drive(agent: str, phase: str, label: str, instruction: str, screen: Any, app_name: str,
          description: str, app_origin: str, evidence: Optional[Path] = None) -> tuple[Any, Meter]:
    """One agent run on an open screen, with the app's preamble; a crash is written to ``error.txt``."""

    try:
        return _drive(agent, phase, label, instruction, screen, app_name, description, app_origin)
    except Exception as exc:
        if evidence is not None:
            chain, c = [], exc
            while c is not None and len(chain) < 6:
                chain.append(f"{type(c).__name__}: {c}")
                c = c.__cause__ or c.__context__
            (evidence / "error.txt").write_text("\n".join(chain) + "\n\n" + traceback.format_exc())
        raise


def _drive(agent: str, phase: str, label: str, instruction: str, screen: Any, app_name: str,
           description: str, app_origin: str) -> tuple[Any, Meter]:

    meter = Meter(label, agent=agent.split(":")[0] if agent in AGENTS else "custom", phase=phase)
    runner = make_runner(agent, phase)
    if agent in AGENTS:
        runner.PREAMBLE = preamble(app_name, description)  # the adapter prepends it
        kw: dict[str, Any] = {"model": AGENTS[agent][1]}
        if agent == "gemini":
            kw["app_origin"] = app_origin
        out = runner.run(instruction, screen, meter, **kw)
    else:
        out = runner(preamble(app_name, description) + instruction, screen, meter)
    return out, meter


def _finish(claim_src: Any, meter: Meter, screen: Any, evidence: Path) -> dict[str, Any]:
    claim = parse_claim(claim_src.final_text)
    if claim_src.stopped:
        claim["stopped"] = claim_src.stopped
    claim["final_text"] = (claim_src.final_text or "")[-2000:]
    claim["model_calls"] = meter.calls
    claim["actions"] = screen.actions
    claim["tokens_in"], claim["tokens_out"] = meter.tokens_in, meter.tokens_out
    claim["cost_usd"] = round(meter.usd, 6)
    (evidence / "transcript.json").write_text(
        json.dumps({"transcript": claim_src.transcript, "actions": screen.log, "usage": meter.usage}, indent=1, default=str)
    )
    return claim


# -- the four apps with the front-door proxy ---------------------------------------------


def run_app_case(app: Any, door: Any, browser: Any, agent: str, phase: str, case: dict[str, Any],
                 seed: list[dict[str, Any]], evidence: Path, rep: int) -> dict[str, Any]:
    from benchmark.write_leaderboard_v2.proxy import Save  # noqa: F401

    task = case["task"]
    row: dict[str, Any] = {"id": case["id"], "app": app.name, "class": case["class"], "note": case.get("note", "")}
    app.reset(task, seed)
    faults: list[dict[str, Any]] = []
    door.side_write = None
    door.accept = app.save_accepted
    for f in case.get("faults") or []:
        if "set" in f:
            faults.append({"set": app.body_sets(f["set"])})
        elif "side_write" in f:
            sw = f["side_write"]
            door.side_write = lambda sw=sw: app.side_write(sw["target"], sw["set"])
            faults.append({"side_write": True})
        else:
            faults.append(dict(f))
    time.sleep(1.0)
    before = app.rows()
    door.arm(app.save, faults)
    evidence.mkdir(parents=True, exist_ok=True)
    browser.fresh()
    ctx, screen = browser.open()
    t0 = time.monotonic()
    try:
        (evidence / "start.png").write_bytes(screen.screenshot())
        if browser.is_signed_out(screen):
            raise RuntimeError("harness: the app was signed out at the start")
        if agent == "scripted":  # model-free harness check: the app's own form filled by selectors
            from benchmark.fhir_medplum.leaderboard.common import AgentRun

            meter = Meter(f"scripted {case['id']}", agent="scripted", phase=phase)
            out = AgentRun(app.scripted(screen.page, task), meter)
        else:
            out, meter = drive(agent, phase, f"{agent} r{rep} {case['id']}", app.task_text(task), screen, app.name,
                               app.description, app.base, evidence)
        try:
            (evidence / "end.png").write_bytes(screen.screenshot())
            end_url = screen.url()
            signed_out = browser.is_signed_out(screen)
        except Exception:  # noqa: BLE001
            end_url, signed_out = "", False
        claim = _finish(out, meter, screen, evidence)
    finally:
        ctx.close()
    if signed_out:
        raise RuntimeError("harness: the app was signed out during the run")
    claim["end_url"] = redact_url(end_url)
    row["agent_s"] = round(time.monotonic() - t0, 1)
    row["cost_usd"] = claim.pop("cost_usd")
    row["claim"] = claim
    door.disarm()
    time.sleep(1.5)
    after = app.rows()
    row["oracle"] = app.judge(before, after, task)
    row["proxy"] = list(door.log)
    row["saves_seen"] = door.saves
    row["fault_delivered"] = door.delivered if case.get("faults") else True
    return row


# -- Medplum, through version 1's harness ----------------------------------------------------


class MedplumRig:
    """Version 1's Medplum stack, Bench, browser and proxy, for one slot."""

    name = "medplum"
    description = "Medplum, the clinic's electronic health record"

    def __init__(self, port: int, suite: dict[str, Any], up: bool = True) -> None:
        from benchmark.fhir_medplum.fixture import MedplumFixture
        from benchmark.fhir_medplum.leaderboard.browser import MedplumBrowser
        from benchmark.fhir_medplum.public.proxy import FaultProxy
        from benchmark.fhir_medplum.public.run import PROJECT, Bench, bring_up
        from benchmark.write_leaderboard_v2.browser import launch

        class Browser(MedplumBrowser):
            def start(self) -> "Browser":  # chrome-headless-shell only
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = launch(self._pw)
                return self

        self.fx = MedplumFixture(
            state_dir=Path.home() / ".openadapt" / "leaderboard-v2" / f"medplum-{port}",
            server_port=port, app_port=port + 1, project=f"{PROJECT}-lbv2-{port}",
        )
        if up:
            bring_up(self.fx)
            self.fx.compose("up", "-d", "medplum-app")
        self.bench = Bench(self.fx, suite)
        self.bench.setup()
        self.browser = Browser(self.fx.ui_url, self.fx.base_url).start()
        _wait_url(self.fx.ui_url)
        self.browser.sign_in(self.bench.actors.emails["bot"], self.bench.actors.passwords["bot"])
        self.proxy = FaultProxy(self.fx.fhir_url, port=port + 2).start()
        self.proxy.side_write = self.bench.side_write

    def run_case(self, agent: str, phase: str, case: dict[str, Any], evidence: Path, rep: int) -> dict[str, Any]:
        browser = self.browser

        def gui(task: dict[str, Any], proxy_fhir_url: str, _token: str) -> dict[str, Any]:
            evidence.mkdir(parents=True, exist_ok=True)
            browser.fresh()
            ctx, screen = browser.open(proxy_fhir_url)
            try:
                (evidence / "start.png").write_bytes(screen.screenshot())
                if "/signin" in screen.url():
                    raise RuntimeError("harness: the app was signed out at the start")
                out, meter = drive(agent, phase, f"{agent} r{rep} {case['id']}", task["instruction"], screen,
                                   "medplum", self.description, browser.ui_url, evidence)
                try:
                    (evidence / "end.png").write_bytes(screen.screenshot())
                    end_url = screen.url()
                except Exception:  # noqa: BLE001
                    end_url = ""
                claim = _finish(out, meter, screen, evidence)
            finally:
                ctx.close()
            if "/signin" in end_url:
                raise RuntimeError("harness: the app was signed out during the run")
            claim["end_url"] = redact_url(end_url)
            return claim

        row = self.bench.run_case(case, gui, self.proxy)
        row["app"] = "medplum"
        note = str((row.get("claim") or {}).get("note", ""))
        if note.startswith("agent raised SpendCap"):
            raise SpendCap(note)
        if note.startswith("agent raised"):
            raise RuntimeError(note)
        # Version 1's oracle verdicts: ``correct`` is right; everything else is not.
        o = row.get("oracle") or {}
        o["collateral"] = bool(set(o.get("changes", {}).values()) - {"created"}) or sum(
            1 for c in (o.get("changes") or {}).values() if c == "created") > 1
        return row

    def close(self) -> None:
        self.proxy.stop()
        self.browser.close()


def _wait_url(url: str, wait_s: float = 300) -> None:
    import urllib.request

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(3)
    raise SystemExit(f"no answer from {url}")


def load_cases(paths: list[str], app: str) -> list[tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]]:
    """[(set name, case, app seed, medplum suite)] for this app, in order."""

    out = []
    public_suite = yaml.safe_load((HERE.parent / "fhir_medplum" / "public" / "cases.yaml").read_text())
    for p in paths:
        doc = yaml.safe_load(Path(p).read_text())
        spec = (doc.get("apps") or {}).get(app) or {}
        for c in spec.get("cases") or []:
            if app == "medplum":
                case = next((x for x in public_suite["cases"] if x["id"] == c), None) if isinstance(c, str) else c
                if case is None:
                    raise SystemExit(f"no Medplum case {c}")
                out.append((doc["set"], case, [], public_suite))
            else:
                out.append((doc["set"], c, spec.get("seed") or [], {}))
    return out


def one(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A harness failure is retried up to twice; the third is excluded (pre-registered)."""

    last = ""
    for attempt in range(3):
        try:
            row = fn()
            row["attempts"] = attempt + 1
            return row
        except SpendCap:
            raise
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
            print(f"   harness failure (attempt {attempt + 1}): {str(exc)[:200]}", flush=True)
            time.sleep(10)
    return {"infra_error": last[:3000]}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--agent", action="append", required=True, help="repeat: the agents take turns, case by case")
    ap.add_argument("--app", required=True, choices=["medplum", *APPS])
    ap.add_argument("--slot", required=True, help="names this app instance's containers")
    ap.add_argument("--port", type=int, required=True, help="upstream port; the front door is port+1 (Medplum: +1 app, +2 proxy)")
    ap.add_argument("--cases", action="append", required=True)
    ap.add_argument("--out", action="append", help="one per --cases (default results/runs)")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--phase", default="scored", help="practice | validation | scored (spend ledger label)")
    ap.add_argument("--only", action="append")
    ap.add_argument("--no-up", action="store_true")
    args = ap.parse_args(argv)

    outs = args.out or [str(HERE / "results" / "runs")] * len(args.cases)
    if len(outs) != len(args.cases):
        raise SystemExit("give one --out per --cases")
    set_out = {}
    for p, o in zip(args.cases, outs):
        set_out[yaml.safe_load(Path(p).read_text())["set"]] = Path(o)
    cases = [x for x in load_cases(args.cases, args.app) if not args.only or x[1]["id"] in args.only]

    if args.app == "medplum":
        suite = dict(cases[0][3]) if cases else {}
        suite["cases"] = [c for _, c, _, _ in cases]
        rig = MedplumRig(args.port, suite, up=not args.no_up)
        app_name = "medplum"
    else:
        from benchmark.write_leaderboard_v2.browser import AppBrowser
        from benchmark.write_leaderboard_v2.proxy import FrontDoor

        mod, cls = APPS[args.app].split(":")
        app = getattr(importlib.import_module(mod), cls)(args.slot, args.port, args.port + 1)
        if not args.no_up:
            app.up()
        door = FrontDoor(args.port + 1, args.port).start()
        browser = AppBrowser(app).start()
        app_name = app.name

    def lab(agent: str) -> str:
        return agent if agent in AGENTS else agent.split(":")[-1]

    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for agent in args.agent:
        meta = {
            "schema": "openadapt.write-leaderboard-v2.run/v1",
            "agent": agent, "model": AGENTS.get(agent, ("", "yours"))[1], "app": app_name,
            "phase": args.phase, "commit": _git("rev-parse", "HEAD"), "dirty": bool(_git("status", "--porcelain", "--", ".")),
            "host": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
            "started": started, "agents_in_turn": args.agent,
            "limits": {"step_cap": common.STEP_CAP, "run_usd_cap": common.RUN_USD_CAP, "wall_s_cap": common.WALL_S_CAP},
        }
        for o in set(set_out.values()):
            d = o / lab(agent) / app_name
            d.mkdir(parents=True, exist_ok=True)
            (d / f"meta-{int(time.time())}.json").write_text(json.dumps(meta, indent=1))
    stopped: dict[str, str] = {}
    try:
        for rep in range(1, args.reps + 1):
            for set_name, case, seed, _ in cases:
                # The agents take turns on the same app instance, case by case.
                for agent in args.agent:
                    if agent in stopped:
                        continue
                    label = lab(agent)
                    d = set_out[set_name] / label / app_name
                    path = d / f"r{rep}-{case['id']}.json"
                    if path.exists() and not json.loads(path.read_text()).get("infra_error"):
                        continue
                    try:
                        if agent in AGENTS:
                            check_spend(agent)
                        waited = wait_for_load()
                        load0 = os.getloadavg()[0]
                        ev = d / f"r{rep}-{case['id']}"
                        if args.app == "medplum":
                            row = one(lambda: rig.run_case(agent, args.phase, case, ev, rep))
                        else:
                            row = one(lambda: run_app_case(app, door, browser, agent, args.phase, case, seed, ev, rep))
                    except SpendCap as exc:
                        stopped[agent] = str(exc)
                        print(f"Stopped {label}: {exc}", flush=True)
                        continue
                    row.update({"id": case["id"], "app": app_name, "class": case["class"], "set": set_name, "rep": rep,
                                "agent": label, "model": AGENTS.get(agent, ("", "yours"))[1]})
                    row["host_load"] = {"start": round(load0, 1), "end": round(os.getloadavg()[0], 1), "waited_s": waited}
                    path.write_text(json.dumps(row, indent=1, default=str) + "\n")
                    o = (row.get("oracle") or {}).get("verdict", "error")
                    c = (row.get("claim") or {}).get("status", "-")
                    print(f"[r{rep}] {app_name:8} {case['id']:7} {case['class']:15} {label:7} end {o:9} claim {c:8} "
                          f"${row.get('cost_usd', 0):.3f}  {label} total ${ledger()['by_agent'].get(label, 0):.2f}", flush=True)
    finally:
        if args.app == "medplum":
            rig.close()
        else:
            door.stop()
            browser.close()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
