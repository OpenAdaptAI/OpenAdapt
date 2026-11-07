"""Run one agent through the leaderboard: every case, k times, in the Medplum app.

    python -m benchmark.fhir_medplum.leaderboard.run --agent claude --port 9800

Brings up Medplum's server and web app on loopback, signs the agent's user
in to the app, and runs each case of the public case set (cases.yaml) the
way the API benchmark does (run.py's Bench): empty the project, write the
seed, snapshot, let the agent work, check, snapshot again, judge the end
state with the oracle. The only difference is the agent: it works through
the web app, and its FHIR traffic passes through the case's fault proxy.

Runs go repetition by repetition (all cases once, then all again), so a run
cut short by the spend cap leaves balanced repetitions. Each run's row is
written as soon as it ends (``runs/r<k>-<case>.json``); a rerun skips rows
already written. Rows are scored by score.py.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from benchmark.fhir_medplum.fixture import MedplumFixture
from benchmark.fhir_medplum.leaderboard.browser import MedplumBrowser
from benchmark.fhir_medplum.leaderboard.common import (
    Meter,
    SpendCap,
    parse_claim,
    spent,
)
from benchmark.fhir_medplum.public.proxy import FaultProxy
from benchmark.fhir_medplum.public.run import PROJECT, Bench, _git_commit, bring_up

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "public" / "cases.yaml"

#: The agents, pre-registered: name -> (adapter module, model).
AGENTS: dict[str, tuple[str, str]] = {
    "claude": ("claude_cu", "claude-sonnet-5-5"),
    "openai": ("openai_cu", "gpt-6.1-sol"),
    "gemini": ("gemini_cu", "gemini-3.8-flash"),
    "fara": ("fara_cu", "microsoft/Fara-7B (mlx-community/Fara-7B-8bit)"),
}


def gui_agent(
    name: str, browser: MedplumBrowser, runs_dir: Path, rep: int
) -> Callable[[dict[str, Any], str, str], dict[str, Any]]:
    if name in AGENTS:
        adapter = importlib.import_module(
            f"benchmark.fhir_medplum.leaderboard.agents.{AGENTS[name][0]}"
        )
        run_fn = adapter.run
    else:  # your own agent: py:<module>:<function>
        _, mod, fn = name.split(":", 2)
        run_fn = getattr(importlib.import_module(mod), fn)

    def agent(task: dict[str, Any], proxy_fhir_url: str, _token: str) -> dict[str, Any]:
        evidence = runs_dir / f"r{rep}-{task['id']}"
        evidence.mkdir(parents=True, exist_ok=True)
        meter = Meter(f"{name} r{rep} {task['id']}")
        browser.fresh()
        ctx, screen = browser.open(proxy_fhir_url)
        try:
            (evidence / "start.png").write_bytes(screen.screenshot())
            if "/signin" in screen.url():
                raise RuntimeError("harness: the app was signed out at the start")
            kw = {"app_origin": browser.ui_url} if name in ("gemini", "fara") else {}
            out = run_fn(task["instruction"], screen, meter, **kw)
            try:
                (evidence / "end.png").write_bytes(screen.screenshot())
                end_url = screen.url()
            except Exception:  # noqa: BLE001
                end_url = ""
        finally:
            ctx.close()
        if "/signin" in end_url:
            # The session ended under the agent: the harness's failure.
            raise RuntimeError("harness: the app was signed out during the run")
        claim = parse_claim(out.final_text)
        if out.stopped:
            claim["stopped"] = out.stopped
        claim["end_url"] = end_url
        if claim["status"] == "done" and not claim["id"]:
            # Pre-registered: a DONE line without an id names the record
            # whose page the browser ends on, if any (read-back column only).
            m = re.search(r"/(Patient|AllergyIntolerance|Appointment)/([A-Za-z0-9\-\.]+)", end_url)
            claim["id"] = f"{m.group(1)}/{m.group(2)}" if m else ""
        claim["final_text"] = out.final_text[-2000:]
        claim["model_calls"] = meter.calls
        claim["actions"] = screen.actions
        claim["tokens_in"], claim["tokens_out"] = meter.tokens_in, meter.tokens_out
        claim["cost_usd"] = round(meter.usd, 6)
        (evidence / "transcript.json").write_text(
            json.dumps({"transcript": out.transcript, "actions": screen.log, "usage": meter.usage}, indent=1, default=str)
        )
        return claim

    return agent


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--agent",
        required=True,
        help=f"{' | '.join(sorted(AGENTS))} | py:<module>:<function> (see README.md)",
    )
    ap.add_argument("--port", type=int, required=True, help="Medplum server; the app gets port+1, the proxy port+2")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--cases", default=str(CASES), help="the scored set unless you are practising")
    ap.add_argument("--only", action="append")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-up", action="store_true")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    suite = yaml.safe_load(Path(args.cases).read_text())
    cases = [c for c in suite["cases"] if not args.only or c["id"] in args.only]
    if args.agent not in AGENTS and not args.agent.startswith("py:"):
        raise SystemExit(f"unknown agent {args.agent!r}")
    label = args.agent if args.agent in AGENTS else args.agent.split(":")[-1]
    out = Path(args.out or HERE / "results" / label)
    runs_dir = out / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    fx = MedplumFixture(
        state_dir=Path.home() / ".openadapt" / "leaderboard" / str(args.port),
        server_port=args.port,
        app_port=args.port + 1,
        project=f"{PROJECT}-lb-{args.port}",
    )
    if not args.no_up:
        bring_up(fx)
        fx.compose("up", "-d", "medplum-app")
    bench = Bench(fx, suite)
    bench.setup()
    browser = MedplumBrowser(fx.ui_url, fx.base_url, headless=not args.headed).start()
    _wait_app(fx.ui_url)
    browser.sign_in(bench.actors.emails["bot"], bench.actors.passwords["bot"])
    proxy = FaultProxy(fx.fhir_url, port=args.port + 2).start()  # within the job's port block
    proxy.side_write = bench.side_write
    meta = {
        "schema": "openadapt.write-leaderboard.run/v1",
        "agent": args.agent,
        "adapter": AGENTS.get(args.agent, (args.agent, ""))[0],
        "model": AGENTS.get(args.agent, ("", "yours"))[1],
        "commit": _git_commit(),
        "medplum": fx.lock["services"],
        "host": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out / f"meta-{int(time.time())}.json").write_text(json.dumps(meta, indent=1))
    try:
        for rep in range(1, args.reps + 1):
            agent = gui_agent(args.agent, browser, runs_dir, rep)
            for case in cases:
                path = runs_dir / f"r{rep}-{case['id']}.json"
                if path.exists() and not json.loads(path.read_text()).get("infra_error"):
                    continue
                waited = _wait_for_load()
                load_start = os.getloadavg()[0]
                row = _one(bench, case, agent, proxy, path)
                row["rep"] = rep
                # Host load, for timing (machine rule: pause above 35).
                row["host_load"] = {
                    "start": round(load_start, 1),
                    "end": round(os.getloadavg()[0], 1),
                    "waited_s": waited,
                }
                path.write_text(json.dumps(row, indent=1, default=str) + "\n")
                o = (row.get("oracle") or {}).get("verdict", "error")
                c = (row.get("claim") or {}).get("status", "-")
                v = (row.get("verifier") or {}).get("outcome", "-")
                print(
                    f"[r{rep}] {case['id']:5} {case['class']:16} end {o:12} claim {c:9} "
                    f"verifier {v:13} ${row.get('cost_usd', 0):.3f}  total ${spent():.2f}",
                    flush=True,
                )
    except SpendCap as exc:
        print(f"Stopped: {exc}", flush=True)
    finally:
        proxy.stop()
        browser.close()
    return 0


def _one(bench: Bench, case: dict[str, Any], agent: Any, proxy: FaultProxy, path: Path) -> dict[str, Any]:
    """One run; a harness failure is retried up to twice (pre-registered)."""

    last = ""
    for attempt in range(3):
        try:
            row = bench.run_case(case, agent, proxy)
            note = str((row.get("claim") or {}).get("note", ""))
            if note.startswith("agent raised SpendCap"):
                raise SpendCap(note)
            if note.startswith("agent raised"):
                # The adapter or browser crashed: the harness's failure, not a
                # claim. Retried; the third failure is scored as no claim.
                last = note
                if attempt < 2:
                    time.sleep(10)
                    continue
                row["infra_note"] = last
            row["attempts"] = attempt + 1
            return row
        except SpendCap:
            raise
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
            time.sleep(10)
    return {"id": case["id"], "class": case["class"], "type": case["type"], "infra_error": last[:3000]}


LOAD_LIMIT = float(os.environ.get("LEADERBOARD_LOAD_LIMIT", 35))


def _wait_for_load() -> int:
    """Hold the next run while the host's 1-minute load is over the limit."""

    waited = 0
    while os.getloadavg()[0] > LOAD_LIMIT:
        if waited % 600 == 0:
            print(f"   waiting: host load {os.getloadavg()[0]:.0f} > {LOAD_LIMIT:.0f}", flush=True)
        time.sleep(30)
        waited += 30
    return waited


def _wait_app(url: str, wait_s: float = 300) -> None:
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
    raise SystemExit(f"Medplum's app didn't answer {url}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
