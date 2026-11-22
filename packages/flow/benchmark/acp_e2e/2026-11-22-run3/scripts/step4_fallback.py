"""Step 4 (run 3; same as run 2): a compiled run, then a forced drift with the gated fallback (P7),
through the wrap-dispatch CLI's own code path.

What is real: the `openadapt-flow wrap-dispatch` argument parser (the real
build_parser), `dispatch_cli.run` with those args, the CLI's own
item-approval gate built from `--fallback-gate item-approval` and the
`--fallback-approver-root` pin (no TTY: stdin is /dev/null), the admitted
compiled program on MockMed, the FallbackPolicy, the repair desk and the
dispatch envelope it stores. A person thread answers the gate's
item-approval request with a registered test approver key.

What is injected (and why): `dispatch_cli._setup` returns the admitted
SkillCache built in process (tests/e2e/test_wrap_fallback_e2e.py `admitted`),
because the CLI's `--task` loader needs an operator task JSON whose
admission ran elsewhere; and `agent=` is the scripted fallback agent, because
the CLI otherwise builds a real provider agent (a model call). The compiled
program runs in process on a thread (#2406 is not merged).
Run from packages/flow of the merged tree: python step4_fallback.py OUT
"""
import json, os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, os.getcwd())
from tests import _approver_keys as ak
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from benchmark.wrap_fallback.measure import Env, classify
from tests.e2e.test_wrap_fallback_e2e import NOTE, PROMPT, DRIFTED_STEPS, DriftableMockMed, admitted, saved
from openadapt_flow.compile_agent import ScriptedAgent
from openadapt_flow.__main__ import build_parser
from openadapt_flow.wrap import dispatch_cli as dc

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
assert not sys.stdin.isatty(), "run with stdin from /dev/null (no TTY)"
akd = OUT / "approver-keys"
os.environ[KEY_DIR_ENV] = str(akd); os.environ[KEY_PASSPHRASE_ENV] = ak.PASSPHRASE
_, signer = ak.provision(akd); root = ak.root_of(akd)
os.environ.pop(ROOT_ENV, None)  # the pin comes from the CLI flag only

def person(approvals: Path, decision: str, stop: threading.Event, log: dict):
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore, make_answer
    from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE
    while not stop.is_set():
        reqs = list(approvals.rglob("item-approval/request-*.json"))
        if reqs: break
        time.sleep(0.1)
    else:
        log["request"] = None; return
    store = ItemApprovalStore(reqs[0].parent.parent, approver_root=root)  # the person pins the same root the CLI flag named
    rid = reqs[0].stem.split("request-", 1)[1]
    req = store.read_request(rid)
    log["request"] = {"request_id": rid, "step_intent": req.step_intent if hasattr(req, "step_intent") else None}
    store.write_answer(make_answer(req, decision=decision, **({"decline_reason": "wrong_values"} if decision == "decline" else {}),
                                   decided_by="human", route=ITEM_APPROVAL_ROUTE, approver_id=None, auth_method="none",
                                   policy=store.context(), signer=signer))
    log["answered"] = decision

res = {}
env = Env(); app = DriftableMockMed(OUT / "app")
try:
    t = time.monotonic(); cache = admitted(OUT, app, env); res["compile_admit_s"] = round(time.monotonic() - t, 1)
    setup = SimpleNamespace(cache=cache, instruction=PROMPT, parameters=("note",))
    env.setattr(dc, "_setup", lambda *_a, **_k: setup)
    for label, drift, decision in (("happy", None, "approve"), ("drift-approved", "redesign", "approve"), ("drift-declined", "redesign", "decline")):
        app.reset()
        if drift: app.drift(drift)
        approvals = OUT / "approvals" / label
        argv = ["wrap-dispatch", "--task", str(OUT / "task.json"), "--request-id", f"dispatch-{label}",  # attempt 2 used run2-{label}; the ledger scrubber reads "run2" as an organization
                "--param", f"note={NOTE}", "--runs-dir", str(OUT / "runs" / label),
                "--fallback", "agent", "--fallback-max-cost-usd", "0.5",
                "--fallback-gate", "item-approval", "--fallback-approvals-dir", str(approvals),
                "--fallback-approval-wait-s", "30", "--fallback-approver-root", root,
                "--result", str(OUT / f"result-{label}.json")]
        args = build_parser().parse_args(argv)
        stop = threading.Event(); plog = {}
        th = threading.Thread(target=person, args=(approvals, decision, stop, plog), daemon=True); th.start()
        t = time.monotonic()
        result = dc.run(args, agent=ScriptedAgent(steps=DRIFTED_STEPS["redesign"]))
        stop.set(); th.join(timeout=10)
        dc._emit(result, args.result)
        fb = result.get("fallback") or {}
        res[label] = {"argv": argv[:1] + ["..."], "path": result.get("path"), "outcome": result.get("outcome"), "status": result.get("status"),
                      "verified": result.get("verified"), "model_calls": result.get("model_calls"), "cost_usd": result.get("cost_usd"),
                      "fallback_status": fb.get("status"), "fallback_reason": fb.get("reason"), "approvals": fb.get("approvals"),
                      "gate_answers": fb.get("gate_answers"), "repair_opened": (fb.get("repair") or {}).get("opened"),
                      "fallback_run_dir": fb.get("run_dir") or fb.get("root"), "person": plog,
                      "saved": classify(saved(app)), "saved_rows": len(saved(app)), "seconds": round(time.monotonic() - t, 2)}
        print(label, json.dumps(res[label], default=str), flush=True)
finally:
    app.stop(); env.close()
(OUT / "step4_result.json").write_text(json.dumps(res, indent=2, default=str))
