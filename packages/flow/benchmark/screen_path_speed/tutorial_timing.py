"""Time ``openadapt-flow tutorial`` (MockMed) and count scrypt key derivations.

Runs the tutorial in this process with the engine importable from the
working directory, wraps ``cryptography``'s Scrypt.derive to count every
key derivation (so the count is comparable across engine versions), and
appends one JSON row: wall seconds for the whole command, the engine's own
run time from its report when present, the derivation count and the exit
code. Encryption at rest is whatever the engine defaults to.

    cd <checkout>/packages/flow && python -m \\
        benchmark.screen_path_speed.tutorial_timing --arm A --out DIR --results R
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--results", required=True)
    a = ap.parse_args()

    from cryptography.hazmat.primitives.kdf import scrypt

    count = {"n": 0, "s": 0.0}
    original = scrypt.Scrypt.derive

    def counted(self, key_material):  # type: ignore[no-untyped-def]
        t = time.perf_counter()
        try:
            return original(self, key_material)
        finally:
            count["n"] += 1
            count["s"] += time.perf_counter() - t

    scrypt.Scrypt.derive = counted  # type: ignore[method-assign]
    import openadapt_flow
    from openadapt_flow.__main__ import main as flow_main

    root = Path(openadapt_flow.__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    load0 = os.getloadavg()[0]
    t0 = time.time()
    try:
        code = flow_main(["tutorial", "--out", a.out, "--no-receipt"])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    wall = time.time() - t0
    run_s = None
    for report in sorted(Path(a.out).rglob("report.json")):
        try:
            data = json.loads(report.read_text())
        except Exception:
            continue
        steps = data.get("results") or []
        if steps:
            run_s = round(sum(float(s.get("elapsed_ms") or 0) for s in steps) / 1000, 3)
    row = {
        "arm": a.arm,
        "engine_head": head,
        "exit": code,
        "wall_s": round(wall, 3),
        "steps_s": run_s,
        "scrypt_derivations": count["n"],
        "scrypt_s": round(count["s"], 3),
        "load_start": round(load0, 2),
        "load_end": round(os.getloadavg()[0], 2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(a.results, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
