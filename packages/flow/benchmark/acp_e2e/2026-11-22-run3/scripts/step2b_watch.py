"""Step 2b (run 3; same as run 2): P6's exception watch on a synthetic one-time-code screen.

Same flow as tests/e2e/test_exception_watch_e2e.py, with the run folder and
OPENADAPT_HOME kept so `ledger add --run-ref` can read the card and answer.
The scripted agent never asks; the watch raises a second_factor card on
its own and blocks the agent's save (409) while it is open; a person clears
the prompt and answers done_by_person with a registered test key; the next
save goes through the guard and reads back.
Run from packages/flow of the merged tree: python step2b_watch.py OUT
"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.exception_card import answer_message
from benchmark.agent_guard import run as bench
from benchmark.agent_guard.agent import ScriptedAgent
from benchmark.agent_guard.app import serve
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV, file_key_signer, init_root
from openadapt_flow.exceptions.registry import ExceptionRegistry
from openadapt_flow.execute.keys import sign_bytes
from openadapt_flow.jit.authority import public_hex
from tests.e2e.test_exception_watch_e2e import AGENT_SAVE, CODE_PROMPT, _Watch, _until, _port

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
home = Path(os.environ["OPENADAPT_HOME"]); key_dir = OUT / "keys"
key = Ed25519PrivateKey.generate()
init_root(key_dir, principal="front-desk", public_key=public_hex(key), sign=file_key_signer(key))
os.environ["OPENADAPT_JIT_KEY_DIR"] = str(key_dir); os.environ[ROOT_ENV] = public_hex(key)
RUN_REF = "watch-2fa-run3"
registry = ExceptionRegistry(home / "exceptions")
app = serve(OUT / "app.sqlite"); port = _port(); work = OUT / "run"
agent = guard = watch = None; res = {}
try:
    bench._post(app.base_url, "/api/reset", {"seed_concurrent": None, "fault": None})
    files = bench.write_setup(OUT / "setup", app.base_url, teach_dir=None)
    agent = ScriptedAgent(port=port, think_s=0.2)
    agent.open(f"{app.base_url}/?fault=ok&idempotency=demo#tasks")
    guard = bench._GuardProcess(["--cdp", f"http://127.0.0.1:{port}", "--config", str(files["guard"]), "--task", str(files["task"]),
                                 "--out", str(work), "--timeout", "120", "--no-teach"])
    guard.wait_attached()
    watch = _Watch(["--cdp", f"http://127.0.0.1:{port}", "--config", str(files["guard"]), "--run-dir", str(work), "--run-ref", RUN_REF,
                    "--task-id", "triage-p1-0001", "--poll-s", "0.2", "--max-s", "240"])
    watch.wait_attached()
    w0 = app.writes
    agent.page.evaluate(CODE_PROMPT)
    agent.page.screenshot(path=str(OUT / "code_prompt.png"))
    card = _until(lambda: next(iter(registry.open_cards(RUN_REF)), None), what="a card")
    res["card_kind"] = card.kind
    tried = agent.page.evaluate(AGENT_SAVE, bench.NOTE)
    res["save_while_open"] = tried; res["writes_while_open"] = app.writes - w0
    agent.page.evaluate("() => document.getElementById('synthetic-code-prompt').remove()")
    unsigned = registry.unsigned_answer(card, "done_by_person", route="cli")
    registry.write_answer(card, {**unsigned, "public_key": public_hex(key), "signature": sign_bytes(key, answer_message(unsigned))})
    log = work / "exceptions_watch.jsonl"
    _until(lambda: log.exists() and '"proceed": true' in log.read_text(), what="the watch to settle the card")
    agent.add_triage_note(patient="p1", note=bench.NOTE)
    result = guard.finish(wait_s=30)
    res["guard_verdict"] = result["run"]["verdict"]; res["writes_after_answer"] = app.writes - w0
    watch.proc.terminate(); done = watch.finish()
    res["watch"] = {k: done.get(k) for k in ("exit", "run_ref", "stopped", "outcomes")}
    res["events"] = [json.loads(l)["event"] for l in log.read_text().splitlines()]
    res["note_in_log"] = bench.NOTE.encode() in log.read_bytes()
    res["run_ref"] = RUN_REF; res["task_json"] = str(files["task"])
finally:
    for proc in (watch, guard):
        if proc is not None and proc.proc.poll() is None: proc.proc.kill()
    if agent is not None: agent.close()
    app.stop()
(OUT / "step2b_result.json").write_text(json.dumps(res, indent=2, default=str))
print(json.dumps(res, indent=2, default=str))
