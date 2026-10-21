"""Freeze workflow specs and oracles before any teaching (PROTOCOL.md "Pool").

    PYTHONPATH=. python benchmark/onboarding_effort/freeze.py --wave 1 tr-ticket-new ...

Writes ``pool/specs/<workflow>.json`` (the task, inputs, shape, policy, second
installation, the engineer's verify.yaml for both installations, the held-out
items' digests) and appends a wave to ``pool/MANIFEST.json`` with the SHA-256
of every file the wave depends on. A workflow already frozen is refused.
Commit the result before teaching any workflow in the wave.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from benchmark.onboarding_effort.registry import WORKFLOWS, get  # noqa: E402

COMMON = ["PROTOCOL.md", "implementer.py", "heldout.py", "hooks.py", "ledger.py", "registry.py",
          "validate.py", "pool/base.py"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec(wid: str) -> dict:
    wf = get(wid)
    items = {inst: wf.items("heldout", 25, inst) for inst in ("first", "second")}
    return {
        "id": wid,
        "app": wf.app,
        "intent": wf.intent,
        "shape": wf.shape,
        "batch_run": wf.batch_run,
        "inputs": list(wf.inputs),
        "keys": list(wf.keys),
        "policy": wf.policy,
        "second_installation": wf.second_installation,
        "second_kind": wf.second_kind,
        "viewport": {inst: list(wf.installation_viewport(inst)) for inst in ("first", "second")},
        "device_scale": {inst: wf.device_scale(inst) for inst in ("first", "second")},
        "verify_yaml": {inst: wf.verify_yaml(inst) for inst in ("first", "second")},
        "oracle_source_sha256": hashlib.sha256(inspect.getsource(type(wf).judge).encode()).hexdigest(),
        "heldout_items_sha256": {
            inst: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
            for inst, v in items.items()
        },
        "heldout_n": 25,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=int, required=True)
    ap.add_argument("--validation", type=Path, help="validation results (jsonl) to attach")
    ap.add_argument("workflows", nargs="+")
    a = ap.parse_args()
    manifest_path = HERE / "pool" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"waves": []}
    frozen = {w for wave in manifest["waves"] for w in wave["workflows"]}
    clash = frozen & set(a.workflows)
    if clash:
        raise SystemExit(f"already frozen: {sorted(clash)}")
    (HERE / "pool" / "specs").mkdir(parents=True, exist_ok=True)
    files = list(COMMON)
    for wid in a.workflows:
        module = WORKFLOWS[wid].split(":")[0]
        s = spec(wid)
        out = HERE / "pool" / "specs" / f"{wid}.json"
        out.write_text(json.dumps(s, indent=2) + "\n")
        files += [f"pool/{module}.py", f"pool/specs/{wid}.json"]
        app_file = {"trac": "apps/trac_app.py", "netbox": "apps/netbox_app.py",
                    "openemr": "apps/openemr_app.py", "openimis": "apps/openimis_app.py"}.get(s["app"])
        if app_file:
            files.append(app_file)
    validation = []
    if a.validation and a.validation.exists():
        validation = [json.loads(x) for x in a.validation.read_text().splitlines() if x.strip()]
        bad = [v for v in validation if v["workflow"] in a.workflows and not v["ok"]]
        if bad:
            raise SystemExit(f"validation failed: {[(v['workflow'], v['installation']) for v in bad]}")
    wave = {
        "wave": a.wave,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workflows": a.workflows,
        "files": {f: sha(HERE / f) for f in sorted(set(files))},
        "validation": [{k: v[k] for k in ("workflow", "installation", "ok", "idle", "performed", "keyed")}
                       for v in validation if v["workflow"] in a.workflows],
    }
    manifest["waves"].append(wave)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: wave[k] for k in ("wave", "frozen_at", "workflows")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
