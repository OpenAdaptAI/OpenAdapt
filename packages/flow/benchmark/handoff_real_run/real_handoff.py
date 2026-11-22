"""A real headless MockMed run, then a two-party handoff over the real CLI.

Party A (sender) runs the tutorial program in a headless browser; Flow writes
receipt.json. A hands claims about the record it entered with
`openadapt-flow handoff send`. Party B (receiver: the MockMed system of
record's owner) pins A's key with `handoff trust`, checks each handoff with
`handoff verify`, then `decide()` reads B's own record (MockMed's /api/db).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
FLOW = [sys.executable, "-m", "openadapt_flow"]
sender_env = dict(os.environ, OPENADAPT_SEAL_HOME=str(out / "sender-seal"))
receiver_env = dict(os.environ, OPENADAPT_SEAL_HOME=str(out / "receiver-seal"))
os.environ.update(OPENADAPT_SEAL_HOME=str(out / "sender-seal"))

import openadapt_flow.mockmed.fault_server as fs  # noqa: E402
from openadapt_flow import handoff as h  # noqa: E402
from openadapt_flow import tutorial  # noqa: E402

real_serve, held = fs.serve, {}
def serve(*a, **k):
    base, db, stop = real_serve(*a, **k)
    held.update(base=base, stop=stop)
    return base, db, (lambda: None)  # keep B's system up for B's own read
fs.serve = serve

evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
try:
    t0 = time.monotonic()
    tutorial.run_tutorial(out / "sender", echo=lambda m: None)
    evidence["run_seconds"] = round(time.monotonic() - t0, 1)
    run_dir = out / "sender" / "run"
    report = json.loads((run_dir / "report.json").read_text())
    evidence["run"] = {
        "execution_outcome": report.get("execution_outcome"),
        "transaction_outcome": report.get("transaction_outcome"),
        "model_calls": report.get("model_calls"),
        "receipt_written": (run_dir / "receipt.json").is_file(),
    }
    records = tutorial._records(held["base"])
    entered = records[-1]
    evidence["record_fields"] = sorted(entered)
    fields = {k: v for k, v in entered.items() if isinstance(v, str) and k != "id"}
    key_field = "id" if "id" in entered else sorted(fields)[0]
    first_field = next(k for k in sorted(fields) if k != key_field)
    claims = {
        "true": {"key": entered.get(key_field), "fields": fields},
        "differing": {"key": entered.get(key_field), "fields": {**fields, first_field: fields[first_field] + " (changed)"}},
        "fabricated": {"key": "no-such-record", "fields": fields},
    }

    # A's key, pinned by B under A's name.
    key = subprocess.run(FLOW + ["handoff", "key"], env=sender_env, capture_output=True, text=True, check=True).stdout
    (out / "sender-key.json").write_text(key)
    subprocess.run(FLOW + ["handoff", "trust", str(out / "sender-key.json"), "--org", "Sender Clinic", "--keys", str(out / "receiver-keys.json")], env=receiver_env, check=True, capture_output=True)
    registry = h.load_registry(out / "receiver-keys.json")

    def own_record(claim):
        for record in tutorial._records(held["base"]):
            if record.get(key_field) == claim["key"]:
                differ = tuple(k for k, v in claim["fields"].items() if record.get(k) != v)
                return h.ReceiverRecord("recorded_differently", differ) if differ else h.ReceiverRecord("recorded_as_intended")
        return h.ReceiverRecord("not_found")

    evidence["handoffs"] = {}
    for kind, claim in claims.items():
        path = out / f"claim-{kind}.json"
        path.write_text(json.dumps(claim, sort_keys=True))
        doc_path = out / f"claim-{kind}.handoff.json"
        send = subprocess.run(FLOW + ["handoff", "send", str(path), "--run", str(run_dir), "--out", str(doc_path), "--media-type", "application/json"], env=sender_env, capture_output=True, text=True)
        verify = subprocess.run(FLOW + ["handoff", "verify", str(doc_path), "--keys", str(out / "receiver-keys.json"), "--json"], env=receiver_env, capture_output=True, text=True)
        v = json.loads(verify.stdout)
        doc = json.loads(doc_path.read_text())
        payload_bytes = path.read_bytes()
        started = time.perf_counter()
        d = h.decide(doc, registry, own_record, payload=payload_bytes)
        evidence["handoffs"][kind] = {
            "send_exit": send.returncode,
            "verify_exit": verify.returncode,
            "integrity": v["integrity"],
            "signer_lines": v["signer_lines"],
            "payload_bound": v["payload"].get("bound"),
            "outcome": v["outcome"],
            "decision": d.decision,
            "receiver_check": d.receiver_check,
            "decide_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    # A swapped payload: the bound handoff with other bytes beside it.
    swap = h.verify_handoff(json.loads((out / "claim-true.handoff.json").read_text()), registry, payload=(out / "claim-differing.json").read_bytes())
    evidence["swapped_payload"] = {"integrity": swap.integrity, "reason": swap.reason}
finally:
    if "stop" in held:
        held["stop"]()
(out / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True))
print(json.dumps(evidence, indent=2, sort_keys=True))
