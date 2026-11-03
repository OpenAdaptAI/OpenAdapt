"""Time a vendor's shadow-mode setup on a running Medplum, step by step.

    python -m benchmark.fhir_medplum.public.shadow_setup_timing --port 9740 --out DIR

Each step is what a vendor does, done for real and timed:

1. a read credential for the shadow (a new client application; in Medplum's
   admin app this is "Project > Clients > New");
2. the agent's own actor reference (``/auth/me`` with the agent's sign-in);
3. ``shadow.yaml`` and the three reference contracts;
4. ``openadapt shadow --config shadow.yaml`` started, the agent pointed at it;
5. the agent's first task, then the report.

The time a person spends deciding is not something a script can measure;
the decisions are listed, and the result says which parts were timed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from benchmark.fhir_medplum.fixture import MedplumFixture
from benchmark.fhir_medplum.public import agents as agent_mod
from benchmark.fhir_medplum.public.run import CONTRACTS, DEFAULT_STATE, PROJECT
from benchmark.fhir_medplum.readpath.suite import Actors, profile_ref

DECISIONS = [
    "which server the agent writes to (the upstream URL)",
    "which contracts: the reference Patient, AllergyIntolerance and "
    "Appointment contracts, used as they are",
    "the shadow's read credential and the agent's actor reference",
]

CONFIG = """\
schema_version: openadapt.shadow/v1
mode: fhir
upstream: {upstream}
listen: {listen}
contracts: [contracts/patient.yaml, contracts/allergy.yaml, contracts/appointment.yaml]
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9740)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fx = MedplumFixture(
        state_dir=(DEFAULT_STATE / str(args.port)).expanduser(),
        server_port=args.port,
        app_port=args.port + 1,
        project=f"{PROJECT}-{args.port}",
    )
    steps: dict[str, float] = {}
    t0 = last = time.monotonic()

    def mark(name: str) -> None:
        nonlocal last
        now = time.monotonic()
        steps[name] = round(now - last, 2)
        last = now

    reader = f"shadow-reader-{int(time.time())}"
    os.environ["BENCH_RECORD_TOKEN"] = fx.client_token(reader)
    mark("1_read_credential")
    actors = Actors(fx)
    bot = actors.token("bot")
    os.environ["BENCH_AGENT_REF"] = profile_ref(fx, bot)
    os.environ["BENCH_FHIR_URL"] = fx.fhir_url
    mark("2_agent_actor_reference")
    work = out / "vendor"
    (work / "contracts").mkdir(parents=True, exist_ok=True)
    for path in CONTRACTS.values():
        shutil.copy(path, work / "contracts" / path.name)
    listen = args.port + 4
    (work / "shadow.yaml").write_text(
        CONFIG.format(upstream=f"http://127.0.0.1:{args.port}", listen=listen)
    )
    mark("3_config_and_contracts")
    proc = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "openadapt"),
            "shadow",
            "--config",
            str(work / "shadow.yaml"),
            "--out",
            str(out / "report"),
            "--until-idle",
            "5",
            "--recheck-s",
            "3",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    first = proc.stdout.readline()
    mark("4_shadow_started")
    n = int(time.time()) % 10000
    task = {
        "id": "SETUP",
        "type": "patient",
        "resource_type": "Patient",
        "instruction": "Make sure this patient is registered, exactly once.",
        "values": {
            "given": "Imani",
            # A person no earlier run registered (or the shadow, rightly,
            # reports the agent's create as a duplicate).
            "family": f"Setup{n}",
            "birthDate": f"19{50 + n % 40}-0{1 + n % 9}-1{n % 10}",
            "gender": "female",
            "line": "9 Elm Street",
            "city": "Springfield",
            "state": "MA",
            "postalCode": "01103",
            "phone": f"413-555-{n:04d}",
            "email": f"imani.setup{n}@example.org",
        },
    }
    claim = agent_mod.scripted(task, f"http://127.0.0.1:{listen}/fhir/R4", bot)
    mark("5a_agent_first_task")
    rest, _ = proc.communicate(timeout=600)
    mark("5b_check_recheck_and_report")
    report = json.loads((out / "report" / "report.json").read_text())
    total = round(time.monotonic() - t0, 1)
    result = {
        "schema": "openadapt.shadow-setup-timing/v1",
        "steps_s": steps,
        "machine_total_s": total,
        "decisions": DECISIONS,
        "claim": claim,
        "counts": report["counts"],
        "late": [i.get("late") for i in report["items"]],
        "shadow_output": (first + rest).strip().splitlines()[-2:],
        "timed": "every step's commands; not the person's reading or deciding",
    }
    (out / "setup-timing.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
