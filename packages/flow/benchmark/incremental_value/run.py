"""Run one arm over the stream on the live fixture.

    cd packages/flow/benchmark
    INCREMENTAL_VALUE_FAULTSET=.../faultset6.yaml \\
    INCREMENTAL_VALUE_FAULT_OPS=.../faultset6_ops.py \\
    python -m incremental_value.run --arm A --out DIR [--only iv-001,iv-007] [--limit N]

Per arm: reset the fixture to its baseline, seed the same users and charts,
read every table (the oracle's start), sign the agent in. Per item: run the
item's ``prepare`` hooks, absorb them into the oracle's state, run the arm,
wait for every delayed write, then diff every table and judge. One JSONL row
per item in ``DIR/<arm>/ledger.jsonl``; rerunning skips items already there.

Needs the fixture lease (``~/oa/scratch/fixture-lease``). The API key comes
from the environment and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from lifecycle.apps import OpenEMRApp
from lifecycle.arm import Env, OperatorDecision, OperatorRequest

from . import faults, workload
from .oracle import DbOracle, judge, load_form_defaults, root_sql

HERE = Path(__file__).resolve().parent
BOT = "openadapt_actor"
OTHER = "clinician2"
READER = "oa_iv_reader"
READER_ENV = "INCREMENTAL_VALUE_READER_PASSWORD"
WIDE_SCOPE = (
    "openid api:oemr user/patient.crus user/allergy.crus user/medical_problem.crus "
    "user/medication.crus user/message.cud user/insurance.crus user/appointment.crus "
    "user/encounter.crus"
)


def q(v: Any) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


class Recorder:
    """Records each request an arm raises; decisions don't feed back.
    People time is charged offline from the ledger (``score.py``)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def ask(self, req: OperatorRequest) -> OperatorDecision:
        self.requests.append(asdict(req))
        return OperatorDecision(action="noted", charged_s=0.0, rule="recorded")


class Fixture:
    def __init__(self) -> None:
        self.app = OpenEMRApp()
        self._tokens: dict[str, tuple[float, Any]] = {}

    def sql(self, s: str) -> str:
        return self.app.sql(s)

    def add_user(self, username: str, fname: str, lname: str) -> int:
        self.sql(
            "CREATE TEMPORARY TABLE openemr.iv_u AS SELECT * FROM openemr.users "
            f"WHERE username = {q(BOT)};"
            "UPDATE openemr.iv_u SET id = (SELECT MAX(id) + 1 FROM openemr.users), "
            f"uuid = UNHEX(REPLACE(UUID(), '-', '')), username = {q(username)}, "
            f"fname = {q(fname)}, lname = {q(lname)}, authorized = 1;"
            "INSERT INTO openemr.users SELECT * FROM openemr.iv_u;"
            "CREATE TEMPORARY TABLE openemr.iv_s AS SELECT * FROM openemr.users_secure "
            f"WHERE username = {q(BOT)};"
            f"UPDATE openemr.iv_s SET id = (SELECT id FROM openemr.users WHERE username = {q(username)}), "
            f"username = {q(username)};"
            "INSERT INTO openemr.users_secure SELECT * FROM openemr.iv_s;"
            "SET @aro = (SELECT MAX(id) + 1 FROM openemr.gacl_aro);"
            "INSERT INTO openemr.gacl_aro (id, section_value, value, order_value, name, hidden) "
            f"VALUES (@aro, 'users', {q(username)}, 10, {q(fname + ' ' + lname)}, 0);"
            "UPDATE openemr.gacl_aro_seq SET id = @aro;"
            "INSERT INTO openemr.gacl_groups_aro_map (group_id, aro_id) SELECT m.group_id, @aro "
            "FROM openemr.gacl_groups_aro_map m JOIN openemr.gacl_aro a ON a.id = m.aro_id "
            f"WHERE a.value = {q(BOT)};"
            "INSERT INTO openemr.`groups` (name, user) SELECT name, "
            f"{q(username)} FROM openemr.`groups` WHERE user = {q(BOT)};"
        )
        return int(self.sql(f"SELECT id FROM openemr.users WHERE username = {q(username)};").split()[0])

    def token(self, username: str) -> Any:
        import httpx

        got = self._tokens.get(username)
        if got and time.time() - got[0] < 2400:
            return got[1]
        fx = self.app.fx
        clients = json.loads(fx.clients_path.read_text())
        values = fx._runtime_values()
        s = httpx.Client(verify=False, timeout=60)
        r = s.post(
            f"{fx.api_base_url}/oauth2/default/token",
            data={
                "grant_type": "password",
                "client_id": clients["actor_client_id"],
                "scope": WIDE_SCOPE,
                "user_role": "users",
                "username": username,
                "password": values["OPENEMR_ACTOR_PASSWORD"],
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"token for {username}: HTTP {r.status_code}")
        s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        self._tokens[username] = (time.time(), s)
        return s

    def seed(self, stream: workload.Stream) -> dict[str, Any]:
        """The same users and charts for both arms, after the reset."""

        fx = self.app.fx
        clients = json.loads(fx.clients_path.read_text())
        self.sql(
            f"UPDATE openemr.oauth_clients SET scope = {q(WIDE_SCOPE)} "
            f"WHERE client_id = {q(clients['actor_client_id'])};"
        )
        pw = os.environ.setdefault(READER_ENV, os.urandom(12).hex())
        self.sql(
            f"DROP USER IF EXISTS {q(READER)}@'%'; CREATE USER {q(READER)}@'%' IDENTIFIED BY {q(pw)};"
            f"GRANT SELECT, SHOW VIEW, TRIGGER ON *.* TO {q(READER)}@'%'; FLUSH PRIVILEGES;"
        )
        self.sql(
            "CREATE DATABASE IF NOT EXISTS oa_audit; CREATE OR REPLACE SQL SECURITY DEFINER VIEW oa_audit.writes AS "
            + (HERE / os.environ.get("INCREMENTAL_VALUE_AUDIT_SQL", "audit_query.sql")).read_text().strip().rstrip(";")
            + ";"
        )
        targets: dict[str, Any] = {
            "clinician2_user": self.add_user(OTHER, "Casey", "Clinician"),
            "bot_user": int(self.sql(f"SELECT id FROM openemr.users WHERE username = {q(BOT)};").split()[0]),
        }
        self._tokens.clear()
        pids = []
        for person in stream.existing:
            cid = self.app.create_chart(person, who="seed")
            pid = int(self.sql(f"SELECT pid FROM openemr.patient_data WHERE id = {int(cid)};").split()[0])
            # The API path writes no history_data; the New Patient form does.
            self.sql(f"INSERT INTO openemr.history_data (pid, date) VALUES ({pid}, NOW());")
            pids.append(pid)
        targets.update(
            existing_pids=pids,
            other_patient=pids[0],
            second_other_patient=pids[1],
        )
        return targets

    def api_for(self, user: str) -> Any:
        return self.token(user)


def env_for(app: OpenEMRApp, out: Path) -> Env:
    return Env(
        app="openemr",
        ui_url=app.ui_url,
        api_url=app.api_url,
        accounts=tuple(app.accounts()[:1]),
        workdir=out,
        seed=workload.SEED,
        extra=app.extra(),
    )


def make_arm(name: str) -> Any:
    from .arms import ArmA, ArmB

    if name == "A":
        return ArmA({})
    if name == "B":
        return ArmB({"contract": os.environ.get("INCREMENTAL_VALUE_CONTRACT", str(HERE / "contract.yaml"))})
    raise SystemExit(f"unknown arm {name}")


def hooks_for(item: workload.Item, cases: dict[str, dict[str, Any]], ops: Any) -> faults.Hooks:
    if item.kind == "perturbation":
        return faults.perturbation(item.label, item.variant)
    if item.kind == "fault":
        if ops is None:
            h = faults.Hooks()
            h.not_run.append(("*", "no ops module"))
            return h
        return ops.build(cases[item.label])
    return faults.Hooks()


def load1() -> float:
    return os.getloadavg()[0]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["A", "B"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--stream", type=Path)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--run", choices=["1", "2", "slice"], default="1", help="the pre-registered stream")
    ap.add_argument("--chunk", type=int, default=0,
                    help="run 2: run only items iv-(50k-49)..iv-50k (PREREGISTRATION_2's fixed chunks)")
    a = ap.parse_args(argv)

    cases_list, sha, ops = faults.load_cases()
    cases = {c["id"]: c for c in cases_list}
    stream_path = a.stream or (a.out / "stream.json")
    if stream_path.exists():
        stream = workload.load(stream_path)
    else:
        ids = [c["id"] for c in cases_list]
        if a.run == "slice":
            stream = workload.build_slice(ids, sha)
        else:
            stream = workload.build(ids, sha, **(workload.RUN2 if a.run == "2" else {}))
        a.out.mkdir(parents=True, exist_ok=True)
        workload.save(stream, stream_path)
    if stream.faultset_sha256 != sha:
        raise SystemExit("the stream was built from another fault set")

    arm = make_arm(a.arm)
    out = a.out / arm.name
    out.mkdir(parents=True, exist_ok=True)
    ledger = out / "ledger.jsonl"
    ldir = os.environ.get("INCREMENTAL_VALUE_LEDGER_DIR")
    if a.arm == "B" and ldir and Path(ldir).is_dir() and any(Path(ldir).iterdir()) and not ledger.exists():
        # A ledger left from another run would answer AlreadyWritten for
        # the same seed, ids and values.
        raise SystemExit(f"{ldir} holds another run's write ledger; use a fresh INCREMENTAL_VALUE_LEDGER_DIR")
    # B's late rechecks land after their item's row: one line per recheck.
    arm.late_path = out / "late.jsonl"
    done = set()
    if ledger.exists():
        done = {json.loads(ln)["item_id"] for ln in ledger.read_text().splitlines() if ln.strip()}

    fx = Fixture()
    app = fx.app
    app.point_hooks_here()
    state = out / "seed.json"
    if done and state.exists() and a.no_reset is False:
        raise SystemExit("this arm has a partial ledger: move it aside and rerun from the start (the app is reset per arm)")
    if not a.no_reset:
        print(f"[{arm.name}] reset", flush=True)
        app.reset()
        targets = fx.seed(stream)
        state.write_text(json.dumps(targets))
    else:
        targets = json.loads(state.read_text())
    oracle = DbOracle(root_sql(app.db_container))
    t0 = time.time()
    oracle.start()
    print(f"[{arm.name}] oracle read {len(oracle._columns)} tables in {time.time() - t0:.0f} s", flush=True)
    env = env_for(app, out)
    rec = Recorder()
    with (out / "arm.json").open("w") as fh:
        json.dump({"describe": arm.describe(), "stream_sha256": stream.sha256(), "faultset_sha256": sha, "targets": targets,
                   "engine_commit": os.popen("git rev-parse HEAD").read().strip()}, fh, indent=1)
    # Run 2 and its slice compare unnamed new-chart columns with the defaults
    # recorded before the freeze (PREREGISTRATION_2); a missing file stops.
    default_file = "form_defaults_2.json" if a.run in ("2", "slice") else ""
    defaults_path = os.environ.get("INCREMENTAL_VALUE_FORM_DEFAULTS", "") or (str(HERE / default_file) if default_file else "")
    form_defaults = load_form_defaults(defaults_path) if defaults_path else None
    if form_defaults is None and default_file:
        raise SystemExit(f"run {a.run} needs the oracle's form defaults ({defaults_path})")
    if form_defaults is None:
        print("warning: no INCREMENTAL_VALUE_FORM_DEFAULTS; the oracle checks only the referral's fields", flush=True)
    setup = arm.setup(env, rec)
    only = set(filter(None, a.only.split(",")))
    if a.chunk:
        only |= {f"iv-{n:03d}" for n in range(50 * a.chunk - 49, 50 * a.chunk + 1)}
    n = 0
    with ledger.open("a") as fh:
        for item in stream.items:
            if item.item_id in done or (only and item.item_id not in only):
                continue
            if a.limit and n >= a.limit:
                break
            gate = float(os.environ.get("INCREMENTAL_VALUE_LOAD_GATE", "40" if a.run == "1" else "60"))
            while load1() > gate:
                print(f"[{arm.name}] load {load1():.0f}; waiting", flush=True)
                time.sleep(30)
            if shutil.disk_usage("/System/Volumes/Data").free / 2**30 < 10:
                raise SystemExit("free disk under 10 GiB; stopping between items")
            n += 1
            load_at_start = round(load1(), 1)
            hooks = hooks_for(item, cases, ops)
            ctx = faults.Ctx(item, app, oracle, targets, fx.api_for)
            prep_err = None
            for p in hooks.prepare:
                try:
                    p(ctx)
                except Exception as exc:  # noqa: BLE001 - the item is void
                    prep_err = f"{type(exc).__name__}: {exc}"[:400]
            pre = oracle.advance()
            rec.requests.clear()
            wi = item.work_item()
            if ctx.source_text != item.source_text:
                from lifecycle.arm import WorkItem

                wi = WorkItem(seq=wi.seq, item_id=wi.item_id, day=0, app="openemr", params=wi.params, source_text=ctx.source_text)
            # The harness users' API tokens, issued before the item.
            fx.api_for(BOT)
            fx.api_for(OTHER)
            arm.begin(ctx, hooks)
            infra = None
            try:
                res = arm.run_item(wi, env, rec)
            except Exception as exc:  # noqa: BLE001 - infrastructure; the arm keeps going
                infra = f"{type(exc).__name__}: {exc}"[:500]
                res = None
            quiet = ctx.wait_background(ctx.max_delay(hooks) + 120)
            # B's later recheck runs at the time its contract declares, not
            # when the harness knows the background writes are done.
            if res is not None and infra is None:
                try:
                    arm.after_item(ctx, res)
                except Exception as exc:  # noqa: BLE001 - never lose the item's row
                    print(f"[{arm.name}] after_item failed: {type(exc).__name__}: {exc}", flush=True)
                    # A recheck that could not run never passes.
                    if hasattr(arm, "recheck_failed"):
                        arm.recheck_failed(item.item_id, exc)
            time.sleep(3)
            diffs = oracle.advance()
            verdict = judge(oracle, diffs, intended=ctx.intended, person=item.person, legit=ctx.legit,
                            form_defaults=form_defaults)
            row = {
                "item_id": item.item_id,
                "seq": item.seq,
                "kind": item.kind,
                "label": item.label,
                "variant": item.variant,
                "intended": ctx.intended is not None,
                "hooks": faults.describe(hooks),
                "prepare_error": prep_err,
                "prepare_tables": sorted(pre),
                "events": ctx.events,
                "background_done": quiet,
                "legit_rows": len(ctx.legit.keys),
                "faults_landed": ctx.faults_landed,
                "infra_error": infra,
                "result": asdict(res) if res is not None else None,
                "operator_requests": list(rec.requests),
                "oracle": verdict,
                "moved_tables": sorted(diffs),
                "t": round(time.time(), 1),
                "load1_at_start": load_at_start,
                "load1_at_end": round(load1(), 1),
            }
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            claimed = res.claimed if res else "infra"
            print(f"[{arm.name}] {item.item_id} {item.kind}:{item.label} claimed={claimed} oracle={verdict['verdict']} "
                  f"{res.wall_s if res else 0:.0f}s ${res.cost_usd if res else 0:.3f}", flush=True)
    arm.close()
    print(json.dumps({"setup": setup}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
