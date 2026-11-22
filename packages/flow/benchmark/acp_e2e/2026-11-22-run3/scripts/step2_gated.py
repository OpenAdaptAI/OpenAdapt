"""Step 2 (run 3; same as run 2): gated production runs in real headless Chromium.

The scripted agent (benchmark/agent_guard/agent.py) drives its own browser;
`openadapt-flow guard` runs in another process attached over CDP, with
`approval: each_item`. A person thread waits for the held save, reads the
card the way the phone does (console save_approval_detail), renders it in
Desktop's real phone portal (card_shot.py), then signs approve or decline
with a registered test approver key. Each run writes guard_ledger_row.json.

Cases: approve-correct (MockMed write adapter), approve-wrong-value with no
adapter (the card must not say "exactly"), the same wrong value with the
adapter (blocked before a card), decline, wrong patient, not saved
(2xx, nothing persisted), plus a few clean runs.
Run from packages/flow of the merged tree: python step2_gated.py OUT CARD_SHOT
"""
import json, os, socket, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from tests import _approver_keys as ak
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from benchmark.agent_guard import run as bench
from benchmark.agent_guard.app import serve

OUT = Path(sys.argv[1]); CARD = sys.argv[2]; OUT.mkdir(parents=True, exist_ok=True)
ADAPTER = "tests._teach_scope:EncounterWrites"
kd = OUT / "approver-keys"; os.environ[KEY_DIR_ENV] = str(kd); os.environ[KEY_PASSPHRASE_ENV] = ak.PASSPHRASE
_, signer = ak.provision(kd); os.environ[ROOT_ENV] = ak.root_of(kd)

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def scen(name): return next(s for s in bench.SCENARIOS if s.name == name)

def files_for(base, setup, task_id, adapter=False):
    f = bench.write_setup(setup, base, teach_dir=None)
    t = json.loads(f["task"].read_text()); t["task_id"] = task_id
    extra = "approval: each_item\n"
    if adapter:
        extra += f"write_adapter: {ADAPTER}\n"
        t["expected_record"] = {"patient_id": "p1"}  # the adapter attributes a write to patient_id only
    f["task"].write_text(json.dumps(t))
    f["guard"].write_text(f["guard"].read_text() + extra)
    return f

def person(work: Path, decision: str, shot: Path, stop: threading.Event, log: dict):
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore, make_answer
    from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE
    from openadapt_flow.console.save_approvals import save_approval_detail
    reqs = []
    while not stop.is_set():
        reqs = list(work.rglob("item-approval/request-*.json"))
        if reqs: break
        time.sleep(0.1)
    if not reqs:
        log["card"] = None; return
    rdir = reqs[0].parent.parent; rid = reqs[0].stem.split("request-", 1)[1]
    try:
        shown = save_approval_detail(rdir.parent, rid)["presentation"]
        log["card"] = {k: shown.get(k) for k in ("values_checked", "explanation", "step_intent", "fields", "sources")}
        log["card_says_exactly"] = "exactly" in json.dumps(shown).lower()
        p = subprocess.run([sys.executable, CARD, str(work), str(shot)], capture_output=True, text=True, timeout=120)
        log["card_render_exit"] = p.returncode; log["card_render_err"] = p.stderr[-800:]
        try: log["portal_says_exactly"] = "exactly" in Path(str(shot) + ".dom.txt").read_text().lower()
        except OSError: pass
        store = ItemApprovalStore(rdir)
        req = store.read_request(rid)
        store.write_answer(make_answer(req, decision=decision, **({"decline_reason": "wrong_values"} if decision == "decline" else {}),
                                       decided_by="human", route=ITEM_APPROVAL_ROUTE, approver_id=None, auth_method="none",
                                       policy=store.context(), signer=signer))
        log["answered"] = decision
    except Exception as exc:  # noqa: BLE001
        log["error"] = repr(exc)

CASES = [  # (task id, scenario, adapter, person decision)
    ("prod-approve-correct", "ok", True, "approve"),
    ("prod-approve-wrong-value-no-adapter", "agent_wrong_value", False, "approve"),
    ("prod-wrong-value-with-adapter", "agent_wrong_value", True, "approve"),
    ("prod-decline", "ok", True, "decline"),
    ("prod-wrong-patient", "agent_wrong_patient", True, "approve"),
    ("prod-not-saved", "no_persist", True, "approve"),
    ("prod-clean-01", "ok", True, "approve"),
    ("prod-clean-02", "ok", True, "approve"),
]
app = serve(OUT / "app.sqlite")
rows = []
try:
    for tid, name, adapter, decision in CASES:
        work = OUT / "runs" / tid
        f = files_for(app.base_url, OUT / "setup" / tid, tid, adapter=adapter)
        stop = threading.Event(); plog = {}
        th = threading.Thread(target=person, args=(work, decision, OUT / f"card-{tid}.png", stop, plog), daemon=True); th.start()
        w0 = app.writes
        r = bench.run_one(app, scen(name), "guard", work, port(), think_s=0.3, files=f, teach=False)
        stop.set(); th.join(timeout=150)
        r.update(task_id=tid, adapter=adapter, decision=decision, person=plog, app_writes_delta=app.writes - w0,
                 ledger_row=(json.loads((work / "guard_ledger_row.json").read_text()) if (work / "guard_ledger_row.json").exists() else None))
        rows.append(r)
        print(tid, r["verdict"], r["outcome"], r["truth"], r["blocked"], "card" if plog.get("card") else "no-card",
              plog.get("answered"), "exactly!" if plog.get("card_says_exactly") else "", flush=True)
finally:
    app.stop()
(OUT / "step2_results.json").write_text(json.dumps(rows, indent=2, default=str))
