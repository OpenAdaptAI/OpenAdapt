"""Check: a planted pseudonyms.json and a stray file in a run folder reach
neither the ledger's push payload nor the evidence copy.

Usage: check_no_leak.py REAL_RUN_DIR   (one real readiness task folder, e.g.
run3/1-shadow/runs/readiness-mockmed-0002, and its episode's guard_task.json
next to it under ../../episodes/)

It copies that folder into a temporary run root, plants
``pseudonyms.json`` and ``egress/pseudonyms.json`` (a token map holding a
canary value) and ``stray-notes.txt`` and ``stray.json`` (the canary in
plain text and in JSON), then:

1. `openadapt-flow ledger add` + `ledger push --dry-run` in a temporary
   OPENADAPT_HOME, and searches the payload for the canary;
2. copy_evidence.copy() on the temporary root, and searches every copied
   file (and the copied file names) for the canary and the planted names.

Exit 0 only when the canary appears nowhere. Synthetic; no network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import copy_evidence  # noqa: E402

CANARY = "Quillon Canaryfield"
PLANTED = ("pseudonyms.json", "stray-notes.txt", "stray.json")


def main() -> int:
    real = Path(sys.argv[1]).resolve()
    episode = real.parent.parent / "episodes" / real.name / "guard_task.json"
    tmp = Path(tempfile.mkdtemp(prefix="acp-leak-check-"))
    run_root = tmp / "run"
    task = run_root / "1-shadow" / "runs" / real.name
    shutil.copytree(real, task)
    (task / "egress").mkdir(exist_ok=True)
    token_map = {"schema": "openadapt.flight-pseudonyms/v1",
                 "tokens": {"[PERSON_1]": CANARY, "[ID_1]": "QX-778812"}}
    for p in (task / "pseudonyms.json", task / "egress" / "pseudonyms.json"):
        p.write_text(json.dumps(token_map))
    (task / "stray-notes.txt").write_text(f"patient {CANARY} said hello\n")
    (task / "stray.json").write_text(json.dumps({"name": CANARY}))
    results: dict[str, object] = {"canary_len": len(CANARY)}

    # 1. ledger add + push --dry-run
    home = tmp / "home"
    env = {**os.environ, "OPENADAPT_HOME": str(home)}
    env.pop("ANTHROPIC_API_KEY", None)
    py = sys.executable
    add = subprocess.run([py, "-m", "openadapt_flow", "ledger", "add", str(task),
                          "--task-type", "intake", "--config", str(episode)],
                         capture_output=True, text=True, env=env)
    push = subprocess.run([py, "-m", "openadapt_flow", "ledger", "push", "--dry-run",
                           "--host", "http://127.0.0.1:9"], capture_output=True, text=True, env=env)
    payload = push.stdout
    store_text = "".join(
        f.read_text(errors="replace") for f in home.rglob("*") if f.is_file() and f.suffix != ".sqlite"
    )
    results["ledger_add_exit"] = add.returncode
    results["push_dry_run_exit"] = push.returncode
    results["payload_rows"] = len([line for line in payload.splitlines() if line.strip()])
    results["canary_in_payload"] = CANARY.casefold() in payload.casefold()
    results["canary_in_ledger_text_files"] = CANARY.casefold() in store_text.casefold()
    results["planted_names_in_payload"] = [n for n in PLANTED if n in payload]

    # 2. evidence copy
    dest = tmp / "evidence"
    dest.mkdir()
    report = copy_evidence.copy(run_root, dest)
    copied_text = "".join(f.read_text(errors="replace") for f in dest.rglob("*") if f.is_file())
    copied_names = [str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file()]
    results["evidence_copied"] = copied_names
    results["evidence_left"] = report["left"]
    results["canary_in_evidence"] = CANARY.casefold() in copied_text.casefold()
    results["planted_files_in_evidence"] = [n for n in copied_names if Path(n).name in PLANTED]

    ok = (
        add.returncode == 0 and push.returncode == 0 and results["payload_rows"] == 1
        and not results["canary_in_payload"] and not results["planted_names_in_payload"]
        and not results["canary_in_evidence"] and not results["planted_files_in_evidence"]
    )
    results["ok"] = ok
    print(json.dumps(results, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
