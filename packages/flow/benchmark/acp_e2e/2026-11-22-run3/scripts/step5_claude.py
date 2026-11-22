"""Step 5 (run 3): one real Claude computer-use candidate through `readiness run`.

ANTHROPIC_API_KEY must come from the caller's environment (a subshell that
sourced only that line of .env); this script never reads, prints or writes it.

Run 3 uses the instruction `readiness import` writes (it now names the
task's record). The same scripted person records tasks 0, 1 and 2 (p1, p2,
p3) on a fresh app so the episodes match this app's origin; no instruction
is edited by hand. Egress is scrubbed with run pseudonyms (the proxy's
default): names and ids become [PERSON_n] / [ID_n] tokens, consistent in
text and screenshots.

Caps: `--spend-cap 0.60` per task at the proxy and OPENADAPT_CU_MAX_USD=0.60
in the adapter, 25 steps; 3 tasks, so at most $1.80 plus one call's
overshoot per task, under the $2 total cap.
Run from packages/flow of the merged tree: python step5_claude.py OUT
"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from benchmark.agent_guard.app import serve
from tests.e2e import test_readiness_e2e as rt

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY not in the environment"
PY = sys.executable; N = 3

def flow(*args, env=None):
    t = time.monotonic()
    p = subprocess.run([PY, "-m", "openadapt_flow", *args], capture_output=True, text=True, env=env)
    (OUT / "cli.log").open("a").write(f"$ openadapt-flow {' '.join(args)}\n[exit {p.returncode}, {time.monotonic()-t:.1f}s]\n{p.stdout[-6000:]}\n{p.stderr[-6000:]}\n")
    return p

app = serve(OUT / "app.sqlite")
res = {"n": N}
try:
    app_url = f"{app.base_url}/?fault=ok#tasks"
    rt._record_people(app_url, OUT / "people", N)
    res["person_writes"] = app.writes
    p = flow("readiness", "import", "--from", str(OUT / "people"), "--out", str(OUT / "episodes"), "--app-url", app_url)
    res["import_exit"] = p.returncode
    app.store.reset(); app.writes = 0; before = app.store.read_encounters()
    reset = f"curl -s -X POST -H 'Content-Type: application/json' -d '{{}}' {app.base_url}/api/reset"
    env = {**os.environ, "OPENADAPT_CU_MAX_USD": "0.60", "OPENADAPT_CU_MAX_STEPS": "25", "OPENADAPT_CU_MAX_S": "600"}
    agent = f"{PY} {Path('benchmark/readiness/claude_cu_agent.py').resolve()}"
    t = time.monotonic()
    p = flow("readiness", "run", "--episodes", str(OUT / "episodes"), "--out", str(OUT / "runs"),
             "--app-url", app_url, "--agent-cmd", agent, "--label", "claude-sonnet-5-5-computer-use",
             "--screen-selector", "#patient-banner", "--timeout", "600", "--linger", "1.0",
             "--reset-cmd", reset, "--egress", "scrubbed", "--spend-cap", "0.60", env=env)
    res["run_exit"] = p.returncode; res["run_s"] = round(time.monotonic() - t, 1)
    res["agent_writes_landed"] = app.writes
    res["store_unchanged"] = app.store.read_encounters() == before
    noenv = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    p = flow("readiness", "compare", "--episodes", str(OUT / "episodes"), "--runs", str(OUT / "runs"), "--out", str(OUT / "card"), env=noenv)
    res["compare_exit"] = p.returncode
    p = flow("readiness", "report", "--cases", str(OUT / "card" / "cases.json"), "--out", str(OUT / "card"),
             "--hourly-cost", "38", "--label", "claude-sonnet-5-5-computer-use", "--task-type", "intake", env=noenv)
    res["report_exit"] = p.returncode
finally:
    app.stop()
(OUT / "step5_result.json").write_text(json.dumps(res, indent=2))
print(json.dumps(res, indent=2))
