"""Re-score the frozen write leaderboard with the verifier, on the runs' own server state.

    python -m benchmark.fhir_medplum.leaderboard_rescore.rescore --contracts main
    python -m benchmark.fhir_medplum.leaderboard_rescore.rescore --contracts operations

No agent runs again. For each scored row of the leaderboard (results of
PR #1737, extracted to ``inputs/rows-<agent>.jsonl.gz``), this rebuilds what
the harness had at the moment it checked the write, and calls
``ensure_record.capture_baseline`` and ``verify_ensure`` unchanged, through a
``FhirReader`` whose ``get`` is :class:`frozen.FrozenMedplum`:

- **When.** The harness wrote ``start.png`` just after the baseline and the
  browser opened, and ``transcript.json`` just before the verifier ran; their
  file times (``inputs/times.json``, captured from the run folders) bound the
  run. The baseline is taken 2.6 s after the case's last setup write (the
  harness sleeps 2.5 s, then reads), and the check at the transcript's time.
- **The task.** The case (``public/cases.yaml``) gives the parameters; the
  seed's record ids are the setup user's creates between the last row and
  this one, in seed order, exactly as ``Bench.create`` named them.
- **Who.** The agent, the setup user and the verifier's client are the
  actors the dump's audit events name.

A row whose window or seed can't be rebuilt that way is not judged: it is
listed with the reason and counted as flagged (fail closed), never dropped.

``--contracts main`` uses ``public/contracts/*.yaml`` as they are on main:
the verifier of the leaderboard, with a working read token. ``--contracts
operations`` uses the same contracts with this branch's ``operations:``
section (``contracts_operations/``). PREREGISTRATION.md fixes the scoring.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import yaml

from benchmark.fhir_medplum.leaderboard_rescore.frozen import FrozenMedplum
from benchmark.fhir_medplum.public.model import WIPED
from benchmark.fhir_medplum.public.run import CONTRACTS, PRACTITIONER_ID, Bench
from openadapt_flow.ensure_record import (
    FhirReader,
    Unreadable,
    capture_baseline,
    load_contract,
    verify_ensure,
)

HERE = Path(__file__).resolve().parent
DATA = HERE / "inputs"
CASES = HERE.parent / "public" / "cases.yaml"
OPERATIONS = HERE / "contracts_operations"
#: Agent -> the Medplum stack its runs used (leaderboard README).
STACKS = {"claude": 9800, "openai": 9810, "fara": 9820}
#: Agent -> the time its live checks got a fresh token. A row checked at or
#: after it has a live verdict and is not replayed (``PREREGISTRATION-FARA.md``).
CHECKED_BEFORE = {"fara": datetime(2026, 11, 4, 11, 55, tzinfo=timezone.utc)}
#: The harness sleeps 2.5 s after the seed, then snapshots and reads.
BASELINE_AFTER_SEED_S = 2.6
#: Deviation 1: cap what the check sees at the harness's next step.
#: ``--as-registered`` turns it off, for the numbers as first computed.
HORIZON = True
#: Deviation 2: a retried run's seed is the creates after its last wipe.
RETRY_SEED = True
#: Actor display names Medplum stamps on audit events.
AGENT, SETUP, CHECK = "OpenAdapt Bot", "Medplum Admin", "openadapt-record-check"


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def load_rows(agent: str) -> list[dict[str, Any]]:
    with gzip.open(DATA / f"rows-{agent}.jsonl.gz", "rt") as fh:
        return [json.loads(line) for line in fh]


class Stack:
    """One agent's frozen server, its actors and its run timeline."""

    def __init__(self, agent: str, times: dict[str, dict[str, float]]) -> None:
        port = STACKS[agent]
        self.base_url = f"http://localhost:{port}/fhir/R4"
        dump = DATA / f"frozen-{port}.jsonl.gz"
        events = []
        with gzip.open(dump, "rt") as fh:
            for line in fh:
                res = json.loads(line)
                if res["resourceType"] == "AuditEvent":
                    events.append(res)
        actors: dict[str, str] = {}
        for e in events:
            who = e["agent"][0]["who"]
            if who.get("display") in (AGENT, SETUP, CHECK):
                actors[who["display"]] = who["reference"]
        self.agent_ref, self.setup_ref = actors[AGENT], actors[SETUP]
        self.server = FrozenMedplum(dump, self.base_url, reader_ref=actors[CHECK])
        #: Every audit event by the setup user, oldest first: the harness's
        #: own steps (wipe, seed, read-back after each check).
        self.setup_times = sorted(
            datetime.fromisoformat(e["recorded"].replace("Z", "+00:00"))
            for e in events
            if e["agent"][0]["who"]["reference"] == self.setup_ref
        )
        #: Setup writes of the wiped types, oldest first: (time, action, ref).
        self.setup_writes = sorted(
            (
                datetime.fromisoformat(e["recorded"].replace("Z", "+00:00")),
                e["action"],
                e["entity"][0]["what"]["reference"],
            )
            for e in events
            if e["agent"][0]["who"]["reference"] == self.setup_ref
            and e["action"] in ("C", "U", "D")
            and e["entity"][0]["what"]["reference"].split("/")[0] in WIPED
        )
        self.practitioners = {}
        for ref, versions in self.server.versions.items():
            if not ref.startswith("Practitioner/"):
                continue
            for ident in versions[-1][1].get("identifier") or []:
                if ident.get("system") == PRACTITIONER_ID:
                    self.practitioners[ident["value"]] = ref
        #: Every run on this stack, scored or excluded, and each start of the
        #: harness (``<agent>/meta/<time>``), in the order they happened.
        self.timeline = sorted(
            (t.get("start.png", t["row"]), t["row"], key)
            for key, t in times.items()
            if key.startswith(f"{agent}/")
        )


def rebuild(
    stack: Stack, key: str, times: dict[str, float], case: dict[str, Any]
) -> tuple[datetime, datetime, dict[str, str]]:
    """(baseline time, check time, refs) for one row, or raise ValueError."""

    if "start.png" not in times or "transcript.json" not in times:
        raise ValueError("the run folder has no start or transcript time")
    start, check = _utc(times["start.png"]), _utc(times["transcript.json"])
    keys = [k for _, _, k in stack.timeline]
    i = keys.index(key)
    prev_end = (
        _utc(stack.timeline[i - 1][1])
        if i
        else datetime.min.replace(tzinfo=timezone.utc)
    )
    setup = [w for w in stack.setup_writes if prev_end < w[0] < start]
    last_setup = max([w[0] for w in setup], default=prev_end)
    baseline_at = max(last_setup, prev_end) + timedelta(seconds=BASELINE_AFTER_SEED_S)
    if not baseline_at < start:
        raise ValueError("the baseline time falls after the run started")
    # Deviation 2: a retried run (``attempts`` > 1) wiped and seeded again
    # in the same gap; its seed is the creates after the last wipe.
    deletes = [i for i, (_, action, _) in enumerate(setup) if action == "D"]
    after_wipe = setup[deletes[-1] + 1 :] if deletes and RETRY_SEED else setup
    creates = [ref for _, action, ref in after_wipe if action == "C"]
    seeds = case.get("seed") or []
    if len(creates) != len(seeds):
        raise ValueError(f"{len(creates)} setup creates for {len(seeds)} seed records")
    refs = dict(stack.practitioners)
    for spec, ref in zip(seeds, creates):
        ((_, v),) = spec.items()
        if v.get("as"):
            refs[v["as"]] = ref
    return baseline_at, check, refs


def contract_path(kind: str, which: str) -> Path:
    if which == "main":
        return CONTRACTS[kind]
    if which == "operations-v2":  # exploratory (deviation 3)
        return HERE / "contracts_operations_v2" / f"{kind}.yaml"
    return OPERATIONS / f"{kind}.yaml"


def judge_row(
    stack: Stack,
    row: dict[str, Any],
    times: dict[str, float],
    case: dict[str, Any],
    suite: dict[str, Any],
    which: str,
) -> dict[str, Any]:
    try:
        baseline_at, check_at, refs = rebuild(stack, row["_key"], times, case)
    except ValueError as exc:
        return {"outcome": "not_rebuilt", "reasons": [str(exc)]}
    _, _, params = Bench.task(SimpleNamespace(suite=suite), case, refs)
    os.environ["BENCH_FHIR_URL"] = stack.base_url
    os.environ["BENCH_AGENT_REF"] = stack.agent_ref
    os.environ["BENCH_RECORD_TOKEN"] = "frozen"  # never sent: the reader is the dump
    contract = load_contract(contract_path(row["type"], which))
    reader = FhirReader(contract, get=stack.server.get)
    stack.server.at(baseline_at)
    try:
        baseline = capture_baseline(contract, params, reader)
        served = stack.server.served
    except Unreadable as exc:
        return {
            "outcome": "needs_review",
            "reasons": [f"unreadable: {exc}"],
            "unreadable": True,
        }
    # Deviation 1 (PREREGISTRATION.md): the store shows only what happened
    # before the harness's next step after the check. A healthy check finished
    # before that step; with the token expired it failed at once, and the
    # harness's read-back and after-check writes followed within a second.
    nxt = [t for t in stack.setup_times if t > check_at]
    # Exclusive: the step itself (the read-back) came after a healthy check.
    cap = nxt[0] - timedelta(microseconds=1) if nxt and HORIZON else None
    stack.server.at(check_at, horizon=cap)
    stack.server.served = served
    result = verify_ensure(
        contract, params, baseline, reader=reader, sleep=stack.server.sleep
    )
    return {
        "outcome": result.outcome,
        "id": result.record_id,
        "reasons": result.reasons,
        "evidence": result.evidence,
        "unreadable": bool(result.evidence.get("unreadable")),
        "baseline_at": baseline_at.isoformat(),
        "check_at": check_at.isoformat(),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--contracts", choices=("main", "operations", "operations-v2"), required=True
    )
    ap.add_argument("--agent", action="append", choices=sorted(STACKS))
    ap.add_argument(
        "--only-live",
        action="store_true",
        help="only rows the live verifier judged (the fidelity check)",
    )
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--as-registered",
        action="store_true",
        help="without deviation 1 (the store is not capped at the harness's next step)",
    )
    args = ap.parse_args(argv)
    global HORIZON, RETRY_SEED
    HORIZON = RETRY_SEED = not args.as_registered
    suite = yaml.safe_load(CASES.read_text())
    cases = {c["id"]: c for c in suite["cases"]}
    times = json.loads((DATA / "times.json").read_text())
    out: dict[str, Any] = {
        "schema": "openadapt.leaderboard-rescore/v1",
        "contracts": args.contracts + ("-as-registered" if args.as_registered else ""),
        "inputs": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(DATA.iterdir())
            if p.is_file()
        },
        "rows": {},
    }
    for agent in args.agent or ("claude", "openai"):
        stack = Stack(agent, times)
        for row in load_rows(agent):
            live = row.get("verifier") or {}
            if args.only_live and any("401" in r for r in live.get("reasons") or []):
                continue
            key = f"{agent}/runs/{row['_name']}"
            row["_key"] = key
            cutoff = CHECKED_BEFORE.get(agent)
            if cutoff and _utc(times[key]["transcript.json"]) >= cutoff:
                continue
            res = judge_row(
                stack, row, times[key], cases[row["id"]], suite, args.contracts
            )
            out["rows"][key] = res
            print(
                f"{key:28} live {live.get('outcome', '-'):16} now {res['outcome']:16} "
                f"{'; '.join(res['reasons'])[:110]}",
                flush=True,
            )
    text = json.dumps(out, indent=1, sort_keys=True, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
