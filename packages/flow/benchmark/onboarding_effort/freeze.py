"""Freeze workflow specs and oracles before any teaching (PROTOCOL.md "Pool").

    PYTHONPATH=. python benchmark/onboarding_effort/freeze.py --wave 1 tr-ticket-new ...

Writes ``pool/specs/<workflow>.json`` (the task, inputs, shape, policy, second
installation, the engineer's verify.yaml for both installations, the held-out
items' digests) and appends a wave to ``pool/MANIFEST.json`` with the SHA-256
of every file the wave depends on. A workflow already frozen in a live wave
is refused; ``--supersede`` re-freezes it under the current protocol version
and marks the earlier wave superseded (it stays in the manifest). Commit the
result before teaching any workflow in the wave.
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

from benchmark.onboarding_effort.apps import trac_app  # noqa: E402
from benchmark.onboarding_effort.ledger import PROTOCOL_VERSION  # noqa: E402
from benchmark.onboarding_effort.registry import WORKFLOWS, get  # noqa: E402

COMMON = ["PROTOCOL.md", "implementer.py", "heldout.py", "hooks.py", "ledger.py", "registry.py",
          "validate.py", "pool/base.py"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec(wid: str) -> dict:
    """The frozen spec. Machine-specific locations take their defaults."""
    import os

    saved = os.environ.pop(trac_app.DB_DIR_ENV, None)
    try:
        return _spec(wid)
    finally:
        if saved is not None:
            os.environ[trac_app.DB_DIR_ENV] = saved


def _spec(wid: str) -> dict:
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
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--wave", type=int, required=True)
    ap.add_argument("--validation", type=Path, help="validation results (jsonl) to attach")
    ap.add_argument("--supersede", action="store_true",
                    help="re-freeze workflows of an earlier wave under the current protocol")
    ap.add_argument("--note", default="", help="why this wave exists (recorded in the manifest)")
    ap.add_argument("workflows", nargs="+")
    a = ap.parse_args()
    manifest_path = HERE / "pool" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"waves": []}
    live = [w for w in manifest["waves"] if not w.get("superseded_by")]
    if any(w["wave"] == a.wave for w in manifest["waves"]):
        raise SystemExit(f"wave {a.wave} already exists")
    clash = {w for wave in live for w in wave["workflows"]} & set(a.workflows)
    if clash and not a.supersede:
        raise SystemExit(f"already frozen: {sorted(clash)} (use --supersede to re-freeze)")
    for wave in live:
        if set(wave["workflows"]) & clash:
            if not set(wave["workflows"]) <= set(a.workflows):
                raise SystemExit(f"wave {wave['wave']} would be split; re-freeze all of it")
            # Waves frozen before the protocol field existed are v1.
            wave.setdefault("protocol", "onboarding-effort/v1")
            wave["superseded_by"] = a.wave
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
        "protocol": PROTOCOL_VERSION,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workflows": a.workflows,
        **({"note": a.note} if a.note else {}),
        "files": {f: sha(HERE / f) for f in sorted(set(files))},
        "validation": [{k: v[k] for k in ("workflow", "installation", "ok", "idle", "performed", "keyed")}
                       for v in validation if v["workflow"] in a.workflows],
    }
    # Validated without OpenAdapt (validate.py) on the live apps before teaching.
    wave["validation_pending"] = not wave["validation"]
    manifest["waves"].append(wave)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: wave[k] for k in ("wave", "frozen_at", "workflows")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
