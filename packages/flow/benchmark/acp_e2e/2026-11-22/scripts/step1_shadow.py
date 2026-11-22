"""Step 1 (run 2): shadow mode to a readiness scorecard, through the readiness CLI.

A scripted person does N synthetic MockMed tasks with network capture
(tests/e2e/test_readiness_e2e.py _record_people, which now drains the
capture itself). The scripted stand-in for a third-party agent (agent_main
in the same file, a separate process attached over CDP, with its planted
field errors and two wrong-record tasks) does them again under the shadow
hold, with `--egress blocked`. Then the adversarial agent
(tests/e2e/test_readiness_adversarial_e2e.py) runs once on one task with the
`create_target` route (a second tab opened over CDP that POSTs at once).
Everything is driven through `openadapt-flow readiness ...`.
Run from packages/flow of the merged tree: python step1_shadow.py OUT N
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, os.getcwd())
from benchmark.agent_guard.app import serve
from tests.e2e import test_readiness_e2e as rt

OUT = Path(sys.argv[1]); N = int(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
PY = sys.executable

def flow(*args, log="cli.log"):
    t = time.monotonic()
    p = subprocess.run([PY, "-m", "openadapt_flow", *args], capture_output=True, text=True)
    (OUT / log).open("a").write(f"$ openadapt-flow {' '.join(args)}\n[exit {p.returncode}, {time.monotonic()-t:.1f}s]\n{p.stdout[-6000:]}\n{p.stderr[-6000:]}\n")
    return p

app = serve(OUT / "app.sqlite")
res = {"n": N}
try:
    app_url = f"{app.base_url}/?fault=ok#tasks"
    rt._record_people(app_url, OUT / "people", N)
    res["person_writes"] = app.writes
    p = flow("readiness", "import", "--from", str(OUT / "people"), "--out", str(OUT / "episodes"), "--app-url", app_url)
    res["import_exit"] = p.returncode
    app.store.reset(); app.writes = 0
    before = app.store.read_encounters()
    plan = OUT / "plan.json"
    plan.write_text(json.dumps({rt._episode(i)["task_id"]: rt._agent_plan(i) for i in range(N)}))
    os.environ["READINESS_E2E_PLAN"] = str(plan)
    agent = f"{PY} {Path('tests/e2e/test_readiness_e2e.py').resolve()}"
    reset = f"curl -s -X POST -H 'Content-Type: application/json' -d '{{}}' {app.base_url}/api/reset"
    t = time.monotonic()
    p = flow("readiness", "run", "--episodes", str(OUT / "episodes"), "--out", str(OUT / "runs"),
             "--app-url", app_url, "--agent-cmd", agent, "--label", "vendor-agent-standin",
             "--screen-selector", "#patient-banner", "--timeout", "90", "--linger", "1.0",
             "--reset-cmd", reset, "--egress", "blocked")
    res["run_exit"] = p.returncode; res["run_s"] = round(time.monotonic() - t, 1)
    res["agent_writes_landed"] = app.writes
    res["store_unchanged"] = app.store.read_encounters() == before
    p = flow("readiness", "compare", "--episodes", str(OUT / "episodes"), "--runs", str(OUT / "runs"), "--out", str(OUT / "card"))
    res["compare_exit"] = p.returncode
    p = flow("readiness", "report", "--cases", str(OUT / "card" / "cases.json"), "--out", str(OUT / "card"),
             "--hourly-cost", "38", "--label", "vendor-agent-standin", "--task-type", "intake")
    res["report_exit"] = p.returncode

    # The adversarial second-tab agent, once (one task).
    adv = OUT / "adversarial"; adv.mkdir(exist_ok=True)
    ep0 = sorted(p for p in (OUT / "episodes").iterdir() if p.is_dir())[0]
    shutil.copytree(ep0, adv / "episodes" / ep0.name)
    tid = json.loads(next(ep0.rglob("*.json")).read_text()).get("task_id") if False else None
    ep = rt._episode(0)
    (adv / "plan.json").write_text(json.dumps({ep["task_id"]: {**ep, "route": "create_target"}}))
    os.environ["READINESS_ADV_PLAN"] = str(adv / "plan.json")
    app.store.reset(); app.writes = 0; before = app.store.read_encounters()
    advcmd = f"{PY} {Path('tests/e2e/test_readiness_adversarial_e2e.py').resolve()}"
    p = flow("readiness", "run", "--episodes", str(adv / "episodes"), "--out", str(adv / "runs"),
             "--app-url", app_url, "--agent-cmd", advcmd, "--label", "adversarial-second-tab",
             "--screen-selector", "#patient-banner", "--timeout", "60", "--linger", "0.5",
             "--reset-cmd", reset, "--egress", "blocked", log="adversarial/cli.log")
    res["adv_run_exit"] = p.returncode
    res["adv_writes_landed"] = app.writes
    res["adv_store_unchanged"] = app.store.read_encounters() == before
    p = flow("readiness", "compare", "--episodes", str(adv / "episodes"), "--runs", str(adv / "runs"), "--out", str(adv / "card"), log="adversarial/cli.log")
    res["adv_compare_exit"] = p.returncode
    p = flow("readiness", "report", "--cases", str(adv / "card" / "cases.json"), "--out", str(adv / "card"),
             "--hourly-cost", "38", "--label", "adversarial-second-tab", "--task-type", "intake", log="adversarial/cli.log")
    res["adv_report_exit"] = p.returncode
finally:
    app.stop()
(OUT / "step1_result.json").write_text(json.dumps(res, indent=2))
print(json.dumps(res, indent=2))
