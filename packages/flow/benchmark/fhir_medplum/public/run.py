"""Run the public Medplum benchmark: your agent writes, OpenAdapt checks.

    openadapt bench medplum --agent claude
    # or, from packages/flow:
    python -m benchmark.fhir_medplum.public.run --agent scripted

Brings up Medplum (Apache-2.0) in Docker on loopback, sets up a project with
a plain member for the agent, then runs each case of ``cases.yaml``:

1. empty the project and write the case's seed;
2. read the server (the oracle's snapshot and the verifier's baseline);
3. run the agent on the task, through the fault proxy (proxy.py);
4. check the write with OpenAdapt's verifier, read back the record the agent
   names (the baseline arm), let any late write land, and read the server
   again for the oracle;
5. score all three arms against the oracle (score.py).

Writes ``results.json`` and ``SCORECARD.txt`` to ``--out``. Synthetic data
only; nothing leaves the machine except the agent's own model calls.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from benchmark.fhir_medplum.fixture import FixtureError, MedplumFixture, _http
from benchmark.fhir_medplum.public import agents as agent_mod
from benchmark.fhir_medplum.public.model import (
    RESOURCE,
    SNOMED,
    WIPED,
    allergy_resource,
    appointment_resource,
    patient_resource,
    task_text,
    wrong_values,
)
from benchmark.fhir_medplum.public.oracle import judge
from benchmark.fhir_medplum.public.proxy import FaultProxy
from benchmark.fhir_medplum.public.score import render, scorecard
from benchmark.fhir_medplum.readpath.suite import Actors, audit_is_read_only

HERE = Path(__file__).resolve().parent
CONTRACTS = {k: HERE / "contracts" / f"{k}.yaml" for k in RESOURCE}
SNAPSHOT_TYPES = (*WIPED, "Practitioner")
PRACTITIONER_ID = "urn:openadapt:bench:practitioner"
PROJECT = "openadapt-bench-medplum"
DEFAULT_PORT = 9590
DEFAULT_STATE = Path.home() / ".openadapt" / "bench-medplum"


class Bench:
    def __init__(self, fx: MedplumFixture, suite: dict[str, Any]) -> None:
        self.fx = fx
        self.suite = suite
        self.actors = Actors(fx)
        self.practitioners: dict[str, str] = {}

    # -- setup -------------------------------------------------------------------

    def setup(self) -> str:
        """Make the agent a plain member who can't touch the audit trail."""

        bot_ref = self.actors.ref("bot")
        me = _http("GET", f"{self.fx.base_url}/auth/me", token=self.actors.token("bot"))
        if me["project"].get("superAdmin") or me["membership"].get("admin"):
            raise SystemExit("the agent's user is an administrator; refusing to run")
        self.fx.restrict_agent(bot_ref)
        self.actors._tokens.pop("bot", None)  # sign in again under the policy
        if not audit_is_read_only(self.fx, self.actors, "bot"):
            raise SystemExit("the agent's user can write AuditEvents; refusing to run")
        os.environ["BENCH_FHIR_URL"] = self.fx.fhir_url
        os.environ["BENCH_AGENT_REF"] = bot_ref
        os.environ["BENCH_RECORD_TOKEN"] = self.fx.client_token("record-check")
        for key, p in self.suite["practitioners"].items():
            self.practitioners[key] = self._practitioner(key, p)
        return bot_ref

    def _practitioner(self, key: str, p: dict[str, str]) -> str:
        token = self.actors.token("setup")
        query = urllib.parse.urlencode({"identifier": f"{PRACTITIONER_ID}|{key}"})
        found = _http("GET", f"{self.fx.fhir_url}/Practitioner?{query}", token=token)
        entries = found.get("entry") or []
        if entries:
            return f"Practitioner/{entries[0]['resource']['id']}"
        made = _http(
            "POST",
            f"{self.fx.fhir_url}/Practitioner",
            token=token,
            body={
                "resourceType": "Practitioner",
                "identifier": [{"system": PRACTITIONER_ID, "value": key}],
                "name": [
                    {"given": [p["given"]], "family": p["family"], "prefix": ["Dr"]}
                ],
            },
        )
        return f"Practitioner/{made['id']}"

    # -- writes the harness makes ----------------------------------------------------

    def resource(self, spec: dict[str, Any], refs: dict[str, str]) -> dict[str, Any]:
        ((kind, v),) = spec.items()
        v = dict(v)
        if kind == "patient":
            person = self.suite["people"].get(v.pop("person", ""), {})
            return patient_resource({**person, **v})
        if kind == "allergy":
            return allergy_resource(v, refs[v["patient"]])
        if kind == "appointment":
            return appointment_resource(v, refs[v["patient"]], refs[v["practitioner"]])
        raise ValueError(f"unknown seed kind {kind!r}")

    def create(
        self, who: str, spec: dict[str, Any], refs: dict[str, str]
    ) -> tuple[str, str]:
        ((_, v),) = spec.items()
        body = self.resource(spec, refs)
        made = _http(
            "POST",
            f"{self.fx.fhir_url}/{body['resourceType']}",
            body=body,
            token=self.actors.token(who),
        )
        ref = f"{made['resourceType']}/{made['id']}"
        if v.get("as"):
            refs[v["as"]] = ref
        return ref, str(made["meta"]["versionId"])

    def other_write(
        self, spec: dict[str, Any], refs: dict[str, str], excused: dict[str, str]
    ) -> None:
        """Another user's write; the oracle excuses the version it leaves."""

        if "update" in spec:
            op = spec["update"]
            url = f"{self.fx.fhir_url}/{refs[op['target']]}"
            token = self.actors.token("other")
            current = _http("GET", url, token=token)
            current.pop("meta", None)
            current.update(op["set"])
            made = _http("PUT", url, body=current, token=token)
            ref = f"{made['resourceType']}/{made['id']}"
            version = str(made["meta"]["versionId"])
        else:
            ref, version = self.create("other", spec, refs)
        excused[ref] = version

    def side_write(self, op: dict[str, Any], refs: dict[str, str], token: str) -> None:
        """A write a fault adds, made with the agent's own token."""

        kind = op["op"]
        if kind == "create":
            spec = {k: v for k, v in op.items() if k not in ("op", "at")}
            body = self.resource(spec, refs)
            _http(
                "POST",
                f"{self.fx.fhir_url}/{body['resourceType']}",
                body=body,
                token=token,
            )
            return
        url = f"{self.fx.fhir_url}/{refs[op['target']]}"
        if kind == "delete":
            _http("DELETE", url, token=token)
        elif kind == "update":
            current = _http("GET", url, token=token)
            current.pop("meta", None)
            current.update(op["set"])
            _http("PUT", url, body=current, token=token)
        else:
            raise ValueError(f"unknown side write {kind!r}")

    def wipe(self) -> None:
        token = self.actors.token("setup")
        for rtype in WIPED:
            for _ in range(50):
                bundle = _http(
                    "GET",
                    f"{self.fx.fhir_url}/{rtype}?_count=200&_elements=id",
                    token=token,
                )
                ids = [e["resource"]["id"] for e in bundle.get("entry") or []]
                if not ids:
                    break
                for rid in ids:
                    _http("DELETE", f"{self.fx.fhir_url}/{rtype}/{rid}", token=token)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        token = self.actors.token("setup")
        out: dict[str, dict[str, Any]] = {}
        for rtype in SNAPSHOT_TYPES:
            url: Optional[str] = f"{self.fx.fhir_url}/{rtype}?_count=1000"
            while url:
                bundle = _http("GET", url, token=token)
                for e in bundle.get("entry") or []:
                    res = e["resource"]
                    out[f"{rtype}/{res['id']}"] = res
                url = next(
                    (
                        ln["url"]
                        for ln in bundle.get("link") or []
                        if ln.get("relation") == "next"
                    ),
                    None,
                )
        return out

    # -- one case --------------------------------------------------------------------

    def task(
        self, case: dict[str, Any], refs: dict[str, str]
    ) -> tuple[dict, dict, dict]:
        """(task for the agent, values for the oracle, params for the verifier)."""

        kind = case["type"]
        v = {**self.suite["defaults"][kind], **(case.get("task") or {})}
        people, docs = self.suite["people"], self.suite["practitioners"]
        text = task_text(kind, v, people, docs, plain=bool(case.get("plain")))
        if kind == "patient":
            values, want, params = dict(v), dict(v), dict(v)
        elif kind == "allergy":
            values = {**v, "person": people[v["patient"]]}
            want = {**v, "patient_ref": refs[v["patient"]]}
            params = {
                "patient": refs[v["patient"]],
                "code_system": SNOMED,
                "code": v["code"],
                "criticality": v["criticality"],
                "clinical_status": "active",
                "verification_status": "confirmed",
            }
        else:
            values = {
                **v,
                "person": people[v["patient"]],
                "doctor": docs[v["practitioner"]],
            }
            want = {
                **v,
                "patient_ref": refs[v["patient"]],
                "practitioner_ref": refs[v["practitioner"]],
            }
            params = {
                "status": "booked",
                "start": v["start"],
                "end": v["end"],
                "patient": refs[v["patient"]],
                "practitioner": refs[v["practitioner"]],
            }
        task = {
            "id": case["id"],
            "type": kind,
            "resource_type": RESOURCE[kind],
            "instruction": text,
            "values": values,
        }
        return task, want, params

    def readback(
        self, kind: str, claim: dict[str, Any], want: dict[str, Any]
    ) -> dict[str, Any]:
        """The baseline: read the record the agent names and compare the task's values."""

        rid = str(claim.get("id") or "").strip()
        if claim.get("status") != "done" or not rid:
            return {"holds": False, "why": "no record named"}
        parts = rid.rstrip("/").split("/")
        if len(parts) == 1:
            ref = f"{RESOURCE[kind]}/{parts[0]}"
        elif RESOURCE[kind] in parts[:-1]:
            # Type/id, an absolute URL, or Type/id/_history/n.
            at = parts.index(RESOURCE[kind])
            ref = f"{RESOURCE[kind]}/{parts[at + 1]}"
        else:
            return {"holds": False, "why": "names another type", "ref": rid}
        try:
            res = _http(
                "GET", f"{self.fx.fhir_url}/{ref}", token=self.actors.token("setup")
            )
        except FixtureError as exc:
            return {"holds": False, "why": str(exc)[:120], "ref": ref}
        bad = wrong_values(kind, res, want)
        return {"holds": not bad, "wrong": bad, "ref": ref}

    def run_case(
        self, case: dict[str, Any], agent: agent_mod.AgentFn, proxy: FaultProxy
    ) -> dict[str, Any]:
        from openadapt_flow.ensure_record import (
            Unreadable,
            capture_baseline,
            load_contract,
            verify_ensure,
        )

        kind = case["type"]
        row: dict[str, Any] = {
            "id": case["id"],
            "class": case["class"],
            "type": kind,
            "expect": case["expect"],
            "note": case.get("note", ""),
        }
        refs = dict(self.practitioners)
        self.wipe()
        for spec in case.get("seed") or []:
            self.create("setup", spec, refs)
        task, want, params = self.task(case, refs)
        # The verifier's window opens a second before the server's clock, to
        # the second: the seed must be older than that.
        time.sleep(2.5)
        before = self.snapshot()
        contract = load_contract(CONTRACTS[kind])
        try:
            baseline = capture_baseline(contract, params)
        except Unreadable as exc:
            baseline = None
            row["verifier"] = {
                "outcome": "needs_review",
                "reasons": [f"unreadable: {exc}"],
                "unreadable": True,
            }
        proxy.arm(case.get("faults") or [], RESOURCE[kind], refs)
        excused: dict[str, str] = {}
        now = [o for o in case.get("other") or [] if not o.get("at_write")]
        during = [
            {k: v for k, v in o.items() if k != "at_write"}
            for o in case.get("other") or []
            if o.get("at_write")
        ]
        for spec in now:
            self.other_write(spec, refs, excused)
        # Another user's writes that land just as the agent writes.
        proxy.on_first_write = (
            (lambda: [self.other_write(o, refs, excused) for o in during])
            if during
            else None
        )
        t0 = time.monotonic()
        try:
            claim = agent(task, proxy.fhir_url, self.actors.token("bot"))
        except Exception as exc:  # noqa: BLE001 - an agent crash is a stop
            claim = {
                "status": "not_done",
                "note": f"agent raised {type(exc).__name__}: {exc}",
            }
        row["agent_s"] = round(time.monotonic() - t0, 1)
        row["cost_usd"] = float(claim.pop("cost_usd", 0.0) or 0.0)
        row["claim"] = claim
        t1 = time.monotonic()
        if baseline is not None:
            result = verify_ensure(contract, params, baseline)
            row["verifier"] = {
                "outcome": result.outcome,
                "id": result.record_id,
                "reasons": result.reasons,
                "evidence": result.evidence,
                "unreadable": bool(result.evidence.get("unreadable")),
            }
        row["verify_s"] = round(time.monotonic() - t1, 1)
        row["readback"] = self.readback(kind, claim, want)
        proxy.run_after_check()
        time.sleep(1.0)
        after = self.snapshot()
        row["oracle"] = judge(kind, case["expect"], want, before, after, excused)
        row["proxy"] = list(proxy.log)
        row["fault_delivered"] = proxy.delivered if case.get("faults") else True
        if during and not any(e.get("other_at_write") for e in proxy.log):
            row["fault_delivered"] = False  # the agent never wrote
        return row


# -- the command --------------------------------------------------------------------


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=HERE,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def bring_up(fx: MedplumFixture, wait_s: float = 900.0) -> float:
    """Start Medplum's server (not its web app) and wait for its health check."""

    import urllib.request

    t0 = time.monotonic()
    fx.compose("up", "-d", "medplum-server")
    deadline = t0 + wait_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{fx.base_url}/healthcheck", timeout=5) as r:  # noqa: S310
                if r.status == 200:
                    return time.monotonic() - t0
        except OSError:
            pass
        time.sleep(3)
    raise SystemExit(
        f"Medplum didn't answer {fx.base_url}/healthcheck in {wait_s:.0f} s"
    )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="openadapt bench medplum", description=__doc__.splitlines()[0]
    )
    ap.add_argument(
        "--agent",
        default="scripted",
        help="scripted | claude | cmd:<command> | py:<module>:<function>",
    )
    ap.add_argument("--model", default="claude-sonnet-5-5", help="for --agent claude")
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument(
        "--only", action="append", help="run only this case id (repeatable)"
    )
    ap.add_argument("--out", default="bench-medplum-results")
    ap.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Medplum's port on 127.0.0.1"
    )
    ap.add_argument(
        "--state-dir",
        default=None,
        help="generated passwords and client ids (default ~/.openadapt/bench-medplum/<port>)",
    )
    ap.add_argument(
        "--max-usd",
        type=float,
        default=30.0,
        help="stop when the agent's reported spend passes this",
    )
    ap.add_argument("--no-up", action="store_true", help="Medplum is already running")
    ap.add_argument(
        "--down",
        action="store_true",
        help="stop Medplum and delete its data at the end",
    )
    args = ap.parse_args(argv)

    suite = yaml.safe_load(Path(args.cases).read_text())
    if suite.get("schema") != "openadapt.medplum-public-bench/v1":
        raise SystemExit(
            f"{args.cases} is not an openadapt.medplum-public-bench/v1 case set"
        )
    cases = [c for c in suite["cases"] if not args.only or c["id"] in args.only]
    name, agent = agent_mod.load(args.agent, model=args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    fx = MedplumFixture(
        state_dir=Path(args.state_dir or DEFAULT_STATE / str(args.port)).expanduser(),
        server_port=args.port,
        app_port=args.port + 1,
        project=f"{PROJECT}-{args.port}",
    )
    started = time.monotonic()
    up_s = 0.0 if args.no_up else bring_up(fx)
    print(f"Medplum is up at {fx.base_url} ({up_s:.0f} s).", flush=True)
    bench = Bench(fx, suite)
    bench.setup()
    proxy = FaultProxy(fx.fhir_url).start()
    proxy.side_write = bench.side_write
    rows: list[dict[str, Any]] = []
    spent = 0.0
    stopped_early = False
    try:
        for i, case in enumerate(cases, 1):
            if spent > args.max_usd:
                stopped_early = True
                print(f"Stopping: the agent has spent ${spent:.2f}.", flush=True)
                break
            try:
                row = bench.run_case(case, agent, proxy)
            except Exception as exc:  # noqa: BLE001 - one case's crash is its own error
                row = {
                    "id": case["id"],
                    "class": case["class"],
                    "type": case["type"],
                    "error": str(exc)[:300],
                }
            spent += row.get("cost_usd", 0.0)
            rows.append(row)
            v = (row.get("verifier") or {}).get("outcome", "-")
            o = (row.get("oracle") or {}).get("verdict", "error")
            claim = (row.get("claim") or {}).get("status", "-")
            print(
                f"[{i:2}/{len(cases)}] {case['id']:5} {case['class']:16} end state {o:8} "
                f"agent {claim:9} OpenAdapt {v}",
                flush=True,
            )
    finally:
        proxy.stop()
        if args.down:
            fx.down(volumes=True)
            # The users and clients it names are gone with the database.
            for name in ("secrets.json", "clients.json"):
                (fx.state_dir / name).unlink(missing_ok=True)
    card = scorecard(rows)
    doc = {
        "schema": "openadapt.medplum-public-bench.results/v1",
        "agent": name,
        "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git_commit(),
        "medplum": fx.lock["services"]["server"],
        "host": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "medplum_up_s": round(up_s, 1),
        "wall_s": round(time.monotonic() - started, 1),
        "agent_usd": round(spent, 4),
        "stopped_early": stopped_early,
        "scorecard": card,
        "cases": rows,
    }
    (out / "results.json").write_text(json.dumps(doc, indent=2, default=str) + "\n")
    text = render(card, rows, agent=name, stopped_early=stopped_early)
    (out / "SCORECARD.txt").write_text(text + "\n")
    print()
    print(text)
    print(
        f"\nAgent spend ${spent:.2f}; wall time {doc['wall_s'] / 60:.1f} min. "
        f"Results in {out}/",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
