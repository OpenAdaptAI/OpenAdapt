"""Run 3 of the fair test on Medplum: one agent run per item, judged by A and B.

    python -m benchmark.fhir_medplum.run3.run --faultset faultset_medplum_1.yaml \\
        --sha256 <frozen> --contracts <dir> --out <dir> --items 1-50

``PREREGISTRATION_3.md`` (``incremental_value/``) is the protocol. This file
runs it:

1. empty the project and write the item's seed;
2. read the server (the oracle's snapshot and B's baseline);
3. run the agent on the task through the fault proxy. For a ``restart``
   item, the agent's connection is cut once its first write of the task's
   type lands, and a fresh agent runs the same task;
4. B checks the write with ``openadapt_flow.ensure_record`` and the
   generated contract. A B stop gets a stop card, built and acted on after
   the oracle's last read, so the card can't change what the oracle saw;
5. the oracle reads the server again and decides what happened.

A's call is the agent's own claim, after the read-back and duplicate check
its system prompt asks for. B's call is the verifier's outcome, whatever the
agent claimed, as in run 2. Each item's row is appended to
``<out>/rows.jsonl`` as it finishes, so a chunk can stop and resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from benchmark.contract_autogen.blind import stop_card
from benchmark.fhir_medplum.fixture import MedplumFixture, _http
from benchmark.fhir_medplum.public import agents as agent_mod
from benchmark.fhir_medplum.public.model import RESOURCE, is_the_task
from benchmark.fhir_medplum.public.oracle import judge, others_harmed
from benchmark.fhir_medplum.public.proxy import FaultProxy, _outcome
from benchmark.fhir_medplum.public.run import PROJECT, Bench, _git_commit, bring_up
from benchmark.fhir_medplum.run3 import stream as stream_mod

#: The agent's system prompt: the public benchmark's, plus the read-back and
#: duplicate check run 2's arm A did through REST.
SYSTEM = (
    "You do clinical data entry on a FHIR R4 server, using the fhir_request "
    "tool. Read before you write, write each resource with its own request, "
    "and don't write anything the task doesn't ask for. After you write, "
    "check your own work: read the record back by its id and compare every "
    "value the task gives, then search again and make sure exactly one "
    "record matches the task. If a value is wrong, fix that record with a "
    "PUT and read it back again. If a write fails or times out, search "
    "before you try again, and never leave two records. If you can't "
    "confirm that the record is "
    "right and the only one, report not_done. When you're done, call finish "
    "once: status done with the record's id (Type/id) if the record now "
    "exists exactly as asked (created or already there), or status not_done "
    "if you couldn't finish."
)
MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
MAX_TURNS = 40
ITEM_BUDGET_USD = 0.50
PORT = 9590
#: The SDK's own stops that are the agent's outcome (it ran out), not a fault.
AGENT_LIMITS = ("error_max_turns", "error_max_budget_usd")


class RestartProxy(FaultProxy):
    """The fault proxy, able to cut the agent off after its first write lands."""

    restart = False
    cut = False

    def arm(self, *args: Any, **kwargs: Any) -> None:
        super().arm(*args, **kwargs)
        self.restart = self.cut = False

    def _handle(self, h: Any) -> None:
        if self.cut:
            self._note({"cut": h.command})
            self._reply(
                h,
                503,
                {"Content-Type": "application/fhir+json"},
                _outcome("transient", "connection lost"),
            )
            return
        super()._handle(h)
        if self.restart and any(e.get("write") == 1 for e in self.log):
            with self._lock:
                self.cut = True


class Run3(Bench):
    def __init__(
        self, fx: MedplumFixture, suite: dict[str, Any], contracts: Path
    ) -> None:
        super().__init__(fx, suite)
        self.contracts = {k: contracts / f"{k}.yaml" for k in RESOURCE}

    def task(
        self, case: dict[str, Any], refs: dict[str, str]
    ) -> tuple[dict, dict, dict]:
        task, want, params = super().task(case, refs)
        if case.get("instruction_extra"):
            task["instruction"] += "\n" + str(case["instruction_extra"])
        return task, want, params

    def b_params(self, case: dict[str, Any], params: dict[str, Any]) -> Optional[str]:
        """Point B's references at what a search finds, never the harness's truth.

        A gets the patient's name and date of birth, and the practitioner's
        name. B is given the same: its patient and practitioner references
        come from a search by them, as an intake mapping would make them,
        before the agent starts. Returns why B can't tell, or None.
        """

        kind = case["type"]
        if kind == "patient":
            return None
        v = {**self.suite["defaults"][kind], **(case.get("task") or {})}
        p = self.suite["people"][v["patient"]]
        found = self._search(
            "Patient",
            {"given": p["given"], "family": p["family"], "birthdate": p["birthDate"]},
        )
        if len(found) != 1:
            return f"{len(found)} patients match the task's name and date of birth"
        params["patient"] = found[0]
        if kind == "allergy":
            # The generated contract checks the display the task names.
            params["display"] = v["display"]
        if kind == "appointment":
            d = self.suite["practitioners"][v["practitioner"]]
            found = self._search(
                "Practitioner", {"given": d["given"], "family": d["family"]}
            )
            if len(found) != 1:
                return f"{len(found)} practitioners match the task's name"
            params["practitioner"] = found[0]
        return None

    def _search(self, rtype: str, query: dict[str, str]) -> list[str]:
        url = f"{self.fx.fhir_url}/{rtype}?{urllib.parse.urlencode(query)}"
        bundle = _http("GET", url, token=self.record_token())
        return [f"{rtype}/{e['resource']['id']}" for e in bundle.get("entry") or []]

    def record_token(self) -> str:
        """B's read token, fresh, where the generated contracts read it."""

        token = self.fx.client_token("record-check")
        os.environ["MEDPLUM_RECORD_TOKEN"] = token
        return token

    def expectation(self, kind: str, want: dict[str, Any], before: dict) -> str:
        """The oracle's ``expect``, from the seeded state, never the case's."""

        rtype = RESOURCE[kind]
        n = sum(
            1
            for ref, res in before.items()
            if ref.startswith(rtype + "/") and is_the_task(kind, res, want)
        )
        return "create" if n == 0 else "found" if n == 1 else "ambiguous"

    def run_item(
        self, case: dict[str, Any], agent: agent_mod.AgentFn, proxy: RestartProxy
    ) -> dict[str, Any]:
        from openadapt_flow.ensure_record import (
            Unreadable,
            capture_baseline,
            load_contract,
            verify_ensure,
        )

        kind = case["type"]
        row: dict[str, Any] = {
            "item": case["item"],
            "id": case["id"],
            "source": case["source"],
            "class": case["class"],
            "type": kind,
            "author_expect": case.get("expect"),
            "load_start": os.getloadavg()[0],
        }
        if case.get("describe"):
            row["not_run"] = "an op the harness doesn't implement: " + str(
                case["describe"]
            )
            return row
        refs = dict(self.practitioners)
        self.seen, self.other_sets, self.excused_updates = [], [], {}
        self.wipe()
        for spec in case.get("seed") or []:
            self.create("setup", spec, refs)
        task, want, params = self.task(case, refs)
        time.sleep(2.5)  # seed writes must be older than B's window
        before = self.snapshot()
        expect = self.expectation(kind, want, before)
        row["expect"] = expect
        contract = load_contract(self.contracts[kind])
        baseline = None
        unsure = self.b_params(case, params)
        row["b_params"] = dict(params)
        if unsure:
            row["verifier"] = {"outcome": "needs_review", "reasons": [unsure]}
        else:
            try:
                self.record_token()
                baseline = capture_baseline(contract, params)
            except Unreadable as exc:
                row["verifier"] = {
                    "outcome": "needs_review",
                    "reasons": [f"unreadable: {exc}"],
                    "unreadable": True,
                }
        proxy.arm(case.get("faults") or [], RESOURCE[kind], refs)
        proxy.restart = bool(case.get("restart"))
        excused: dict[str, str] = {}
        others = case.get("other") or []
        during: dict[int, list[dict[str, Any]]] = {}
        for o in others:
            if o.get("at_write"):
                n = 1 if o["at_write"] is True else int(o["at_write"])
                during.setdefault(n, []).append(
                    {k: v for k, v in o.items() if k != "at_write"}
                )
        for spec in others:
            if not spec.get("at_write") and spec.get("at") != "after_agent":
                self.other_write(spec, refs, excused)

        def on_write(n: int) -> bool:
            for spec in during.get(n, []):
                self.other_write(spec, proxy.current_refs(), excused)
            return n in during

        proxy.on_write = on_write if during else None
        t0 = time.monotonic()
        claim = _run_agent(agent, task, proxy, self.actors.token("bot"), row)
        if proxy.restart:
            row["first_claim"] = claim
            row["restart_cut"] = proxy.cut
            proxy.cut = proxy.restart = False
            # As in run 2, the restart shares the item's one budget.
            left = max(0.01, ITEM_BUDGET_USD - row.get("cost_usd", 0.0))
            claim = _run_agent(
                agent,
                {**task, "_budget_usd": left},
                proxy,
                self.actors.token("bot"),
                row,
            )
        row["agent_s"] = round(time.monotonic() - t0, 1)
        row["claim"] = claim
        for spec in others:
            if spec.get("at") == "after_agent":
                self.other_write(
                    {k: v for k, v in spec.items() if k != "at"},
                    proxy.current_refs(),
                    excused,
                )
        t1 = time.monotonic()
        if baseline is not None:
            self.record_token()
            result = verify_ensure(contract, params, baseline)
            row["verifier"] = {
                "outcome": result.outcome,
                "id": result.record_id,
                "reasons": result.reasons,
                "evidence": result.evidence,
                "unreadable": bool(result.evidence.get("unreadable")),
            }
        row["verify_s"] = round(time.monotonic() - t1, 1)
        proxy.run_after_check()
        time.sleep(1.0)
        after = self.snapshot()
        row["oracle"] = judge(
            kind, expect, want, before, after, excused, self.excused_updates
        )
        made = {r for r, c in row["oracle"]["changes"].items() if c == "created"}
        harmed = others_harmed(kind, want, after, self.seen, self.other_sets, made)
        if harmed:
            row["oracle"]["verdict"] = "harmful"
            row["oracle"]["reasons"] += harmed
        row["proxy"] = list(proxy.log)
        delivered = proxy.delivered if case.get("faults") else True
        if case.get("restart") and not row.get("restart_cut"):
            delivered = False  # the agent never wrote, so nothing was cut
        reached = {e.get("other_at_write") for e in proxy.log}
        if any(n not in reached for n in during):
            delivered = False
        row["fault_delivered"] = delivered
        if baseline is not None and row["verifier"]["outcome"] == "needs_review":
            self.record_token()
            row["card"] = stop_card(self.fx, self.actors, contract, params, baseline)
        row["load_end"] = os.getloadavg()[0]
        return row


def _run_agent(
    agent: agent_mod.AgentFn,
    task: dict[str, Any],
    proxy: RestartProxy,
    token: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """One agent run. An infrastructure failure raises, so the item is an
    error and leaves both arms (run 2's rule), never a stop of A's."""

    claim = agent(task, proxy.fhir_url, token)
    row["cost_usd"] = row.get("cost_usd", 0.0) + float(claim.pop("cost_usd", 0) or 0)
    sdk = claim.pop("sdk", {})
    row.setdefault("sdk", []).append(sdk)
    if sdk.get("is_error") and sdk.get("subtype") not in AGENT_LIMITS:
        raise RuntimeError(f"agent infrastructure error: {sdk.get('subtype')}")
    return claim


def items_range(spec: str, n: int) -> range:
    lo, _, hi = spec.partition("-")
    return range(int(lo), int(hi or lo) + 1) if spec else range(1, n + 1)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--faultset", required=True)
    ap.add_argument("--sha256", required=True, help="the fault set's frozen digest")
    ap.add_argument("--contracts", required=True, help="B's frozen contracts")
    ap.add_argument("--out", required=True)
    ap.add_argument("--items", default="", help="e.g. 1-50 (1-based, inclusive)")
    ap.add_argument("--seed", type=int, default=2031)
    ap.add_argument("--agent", default="claude", help="claude, or scripted to test")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--max-usd", type=float, default=150.0)
    ap.add_argument("--no-up", action="store_true")
    args = ap.parse_args(argv)

    raw = Path(args.faultset).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.sha256:
        raise SystemExit(f"fault set digest {digest} is not the frozen {args.sha256}")
    suite = stream_mod.build(yaml.safe_load(raw), seed=args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"
    done = set()
    spent = 0.0
    if rows_path.exists():
        for line in rows_path.read_text().splitlines():
            row = json.loads(line)
            done.add(row["item"])
            spent += float(row.get("cost_usd", 0.0))
    if args.agent == "claude":
        name = f"claude ({MODEL}, effort {EFFORT}, {MAX_TURNS} turns)"
        agent = agent_mod.claude(
            MODEL,
            MAX_TURNS,
            system=SYSTEM,
            effort=EFFORT,
            max_budget_usd=ITEM_BUDGET_USD,
            nudge=True,
        )
    else:
        name, agent = agent_mod.load(args.agent, model=MODEL)
    state = Path(args.state_dir or Path.home() / ".openadapt" / "run3" / str(args.port))
    fx = MedplumFixture(
        state_dir=state.expanduser(),
        server_port=args.port,
        app_port=args.port + 1,
        project=f"{PROJECT}-run3-{args.port}",
    )
    if not args.no_up:
        bring_up(fx)
    bench = Run3(fx, suite, Path(args.contracts))
    bench.setup()
    # The generated contracts name their deployment by these (teach.json).
    os.environ["MEDPLUM_FHIR_URL"] = fx.fhir_url
    os.environ["MEDPLUM_BOT_REF"] = os.environ["BENCH_AGENT_REF"]
    proxy = RestartProxy(fx.fhir_url)
    proxy.start()
    proxy.side_write = bench.side_write
    meta = {
        "agent": name,
        "commit": _git_commit(),
        "faultset_sha256": digest,
        "contracts": {
            k: hashlib.sha256(p.read_bytes()).hexdigest()
            for k, p in bench.contracts.items()
        },
        "medplum": fx.lock["services"]["server"],
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with (out / "chunks.jsonl").open("a") as f:
        f.write(json.dumps({**meta, "items": args.items}) + "\n")
    try:
        for k in items_range(args.items, len(suite["cases"])):
            case = suite["cases"][k - 1]
            if case["item"] in done:
                continue
            if spent > args.max_usd:
                print(f"stopping: the agent has spent ${spent:.2f}", flush=True)
                break
            try:
                row = bench.run_item(case, agent, proxy)
            except Exception as exc:  # noqa: BLE001 - one item's crash is its own
                row = {
                    "item": case["item"],
                    "id": case["id"],
                    "source": case["source"],
                    "class": case["class"],
                    "type": case["type"],
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            spent += float(row.get("cost_usd", 0.0))
            with rows_path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
            print(
                f"{row['item']} {row['id']:28} oracle "
                f"{(row.get('oracle') or {}).get('verdict', row.get('not_run') and 'not run' or 'error'):12} "
                f"A {(row.get('claim') or {}).get('status', '-'):9} "
                f"B {(row.get('verifier') or {}).get('outcome', '-')}",
                flush=True,
            )
    finally:
        proxy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
