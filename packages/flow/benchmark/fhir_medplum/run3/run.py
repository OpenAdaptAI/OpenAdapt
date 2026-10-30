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
import threading
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
#: B's contracts, as frozen in openadapt-internal CONTRACT_FREEZE.md.
CONTRACT_SHA256 = {
    "patient": "b7bf94edc09e7594ab0e328e72b3e432398823441d31a14360e9f453763bec7c",
    "allergy": "b78b67e954cb4088b09895bda45a3ff6c56c442d4d0c967254ea7ec13affe77f",
    "appointment": "b51ccc2a7df5d7218175600b8662ee1abfae827268fcf29c124322f22a4a2828",
}
MAX_LOAD = 60.0
#: The SDK's own stops that are the agent's outcome (it ran out), not a fault.
AGENT_LIMITS = ("error_max_turns", "error_max_budget_usd")


class RestartProxy(FaultProxy):
    """The fault proxy, able to cut the agent off after its first write lands."""

    restart = False
    cut = False
    #: When the agent's first write of the task's type got its reply
    #: (monotonic): arm C's detection time runs from here.
    first_write_at: Optional[float] = None

    def arm(self, *args: Any, **kwargs: Any) -> None:
        super().arm(*args, **kwargs)
        self.restart = self.cut = False
        self.first_write_at = None

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
        if self.first_write_at is None and any(e.get("write") for e in self.log):
            self.first_write_at = time.monotonic()
        if self.restart and any(e.get("write") == 1 for e in self.log):
            with self._lock:
                self.cut = True


class Run3(Bench):
    def __init__(
        self,
        fx: MedplumFixture,
        suite: dict[str, Any],
        contracts: Path,
        described: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(fx, suite)
        self.contracts = {k: contracts / f"{k}.yaml" for k in RESOURCE}
        #: The fault set's ``describe:`` cases, implemented with existing ops
        #: (a private file, written after B's contracts were frozen).
        self.described = described or {}
        self._timers: list[threading.Timer] = []

    def side_write(self, op: dict[str, Any], refs: dict[str, str], token: str) -> None:
        """The public bench's side write, plus ``delay_s`` and ``set_path``."""

        if op.get("delay_s"):
            later = {k: v for k, v in op.items() if k != "delay_s"}
            timer = threading.Timer(
                float(op["delay_s"]), self.side_write, (later, dict(refs), token)
            )
            self._timers.append(timer)
            timer.start()
            return
        if op.get("set_path"):
            url = f"{self.fx.fhir_url}/{refs[op['target']]}"
            current = _http("GET", url, token=token)
            current.pop("meta", None)
            for path, value in op["set_path"].items():
                *head, last = path.split(".")
                node: Any = current
                for part in head:
                    node = node[int(part)] if part.isdigit() else node[part]
                node[int(last) if last.isdigit() else last] = value
            _http("PUT", url, body=current, token=token)
            return
        super().side_write(op, refs, token)

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
        if kind != "patient":
            # Two patients with the task's name and date of birth: no one
            # can tell which is meant without a person.
            p = self.suite["people"][want["patient"]]
            twins = sum(
                1
                for ref, res in before.items()
                if ref.startswith("Patient/") and is_the_task("patient", res, p)
            )
            if twins > 1:
                return "ambiguous"
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
            ops = self.described.get(case["id"].removeprefix("blind_"))
            if ops is None:
                row["not_run"] = "an op the harness doesn't implement: " + str(
                    case["describe"]
                )
                return row
            case = {**case, "faults": [*(case.get("faults") or []), *ops]}
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
        for timer in self._timers:
            timer.join()
        self._timers.clear()
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
        if proxy.first_write_at is not None:
            # Arm C: from the write's reply to the verifier's verdict. An upper
            # bound: here the check starts once the agent is done, while a
            # background check would start at the write.
            row["detect_s"] = round(time.monotonic() - proxy.first_write_at, 1)
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
    ap.add_argument(
        "--described", help="the private file implementing the describe: cases"
    )
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
    for kind, want in CONTRACT_SHA256.items():
        got = hashlib.sha256((Path(args.contracts) / f"{kind}.yaml").read_bytes())
        if got.hexdigest() != want and args.agent == "claude":
            raise SystemExit(f"{kind}.yaml is not the frozen contract")
    if os.getloadavg()[0] >= MAX_LOAD:
        raise SystemExit(f"load {os.getloadavg()[0]:.0f} is {MAX_LOAD:.0f} or more")
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
    described = (
        yaml.safe_load(Path(args.described).read_text()) if args.described else {}
    )
    described.pop("schema", None)
    bench = Run3(fx, suite, Path(args.contracts), described)
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
        "described_sha256": hashlib.sha256(
            Path(args.described).read_bytes()
        ).hexdigest()
        if args.described
        else None,
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
