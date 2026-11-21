"""Lab proof for ``openadapt plan`` on real apps: Vikunja (REST), Mailpit
(email sink) and Dolibarr's MariaDB (a database-backed app).

Run inside the zenbook's WSL, from packages/flow::

    ~/oa/scratch/winlap/remote-run.sh <worktree> uv run --with pymysql \\
        python benchmark/ext_tx/run.py --phase measured --out benchmark/ext_tx/results/x.json

The app-specific declarations (server-set and derived fields, ignored
tables) are in ``SPEC`` below and are fixed before a measured run. The
results file holds counts and digests, never record values. Synthetic data
only. Kill criteria (any one stops the work):

* a verified undo fails on more than 5% of supported writes;
* the dry-run diff misses an effect the later read-back finds;
* a double effect gets through the idempotency test.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from openadapt_flow.txplan import engine
from openadapt_flow.txplan.adapters import MailAdapter, RestAdapter, SqlAdapter
from openadapt_flow.txplan.engine import Ctx, TxRefused
from openadapt_flow.txplan.spec import load_spec
from openadapt_flow.txplan.state import State

VIKUNJA = os.environ.get("LAB_VIKUNJA", "http://127.0.0.1:9670")
MAILPIT = os.environ.get("LAB_MAILPIT", "http://127.0.0.1:9672")
SMTP_PORT = int(os.environ.get("LAB_SMTP_PORT", "9671"))
DOLI_COMPOSE = Path.home() / "fixtures" / "dolibarr" / "compose.yml"

#: Frozen before the measured run. Everything app-specific is here.
SPEC: dict[str, Any] = {
    "schema_version": "openadapt.tx/v1",
    "holdback_s": 1,
    "anomalies": [
        {"field": "amount", "above": 10000, "label": "large amount"},
        {"op": "delete", "label": "delete"},
        {"max_writes": 500, "label": "large batch"},
    ],
    "systems": [
        {
            "name": "vikunja",
            "kind": "rest",
            "base_url": VIKUNJA,
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "value_env": "LAB_VIKUNJA_TOKEN",
            },
            "collections": [
                {
                    "name": "task",
                    "id_field": "id",
                    "list": {
                        "path": "/api/v1/tasks",
                        "page_param": "page",
                        "per_page": 50,
                    },
                    "read": {"path": "/api/v1/tasks/{id}"},
                    "create": {
                        "method": "PUT",
                        "path": "/api/v1/projects/{project_id}/tasks",
                    },
                    "update": {
                        "method": "POST",
                        "path": "/api/v1/tasks/{id}",
                        "replace": True,
                    },
                    "delete": {"method": "DELETE", "path": "/api/v1/tasks/{id}"},
                    "server_fields": ["updated"],
                    "derived": {"done": ["done_at"]},
                }
            ],
        },
        {
            "name": "outbox",
            "kind": "mail",
            "smtp": {"host": "127.0.0.1", "port": SMTP_PORT},
            "mailbox": {"kind": "mailpit", "base_url": MAILPIT},
        },
        {
            "name": "dolibarr",
            "kind": "sql",
            "dialect": "mysql",
            "driver": "pymysql",
            "connect_args": {
                "host": "127.0.0.1",
                "port": 9636,
                "user": "dolidbuser",
                "database": "dolidb",
                "charset": "utf8mb4",
            },
            "password_env": "LAB_DOLI_DB_PASSWORD",
            "tables": {"llx_societe": "rowid"},
        },
    ],
}

#: Per-app overrides learned in the pilot, applied in both phases once fixed.
PILOT_DECLARATIONS: dict[str, Any] = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# -- setup ---------------------------------------------------------------------


def doli_password() -> str:
    text = DOLI_COMPOSE.read_text()
    m = re.search(r"DOLI_DB_PASSWORD:\s*\"?([^\"\n]+)\"?", text)
    if not m:
        raise SystemExit("cannot find the fixture's synthetic DB password")
    return m.group(1).strip()


def vikunja_setup() -> tuple[str, int]:
    user = f"lab{int(time.time())}"
    pw = "synthetic-Lab-pass-1"
    c = httpx.Client(base_url=VIKUNJA, timeout=20)
    r = c.post(
        "/api/v1/register",
        json={"username": user, "email": f"{user}@example.test", "password": pw},
    )
    r.raise_for_status()
    token = c.post("/api/v1/login", json={"username": user, "password": pw}).json()[
        "token"
    ]
    c.headers["Authorization"] = f"Bearer {token}"
    project = c.put("/api/v1/projects", json={"title": "lab project"}).json()["id"]
    for i in range(40):
        c.put(
            f"/api/v1/projects/{project}/tasks",
            json={
                "title": f"seed task {i}",
                "description": f"synthetic {i}",
                "priority": i % 5,
            },
        ).raise_for_status()
    return token, int(project)


def doli_seed(adapter: SqlAdapter, n: int) -> list[int]:
    con = adapter.connect()
    ids = []
    try:
        cur = con.cursor()
        tag = int(time.time())
        for i in range(n):
            cur.execute(
                "INSERT INTO llx_societe (nom, entity, datec, status, client, note_public, phone, town) "
                "VALUES (%s, 1, NOW(), 1, 1, %s, %s, %s)",
                (f"Lab seed {tag}-{i}", "synthetic", f"555-01{i:02d}", "Labtown"),
            )
            ids.append(int(cur.lastrowid))
        con.commit()
    finally:
        con.close()
    return ids


def doli_cleanup(adapter: SqlAdapter, prefix: str = "Lab ") -> int:
    con = adapter.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM llx_societe WHERE nom LIKE %s", (prefix + "%",))
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


# -- batches -------------------------------------------------------------------


def rest_batch(
    rng: random.Random, ids: list[int], project: int, b: int
) -> list[dict[str, Any]]:
    out = []
    for tid in rng.sample(ids, 6):
        field = rng.choice(["title", "description", "priority", "done", "percent_done"])
        value: Any = {
            "title": f"edited {b}-{tid}",
            "description": f"synthetic edit {b}",
            "priority": rng.randint(0, 5),
            "done": rng.choice([True, False]),
            "percent_done": rng.choice([0.1, 0.5, 0.9]),
        }[field]
        out.append(
            {
                "system": "vikunja",
                "collection": "task",
                "op": "update",
                "id": tid,
                "fields": {field: value},
            }
        )
    for i in range(4):
        out.append(
            {
                "system": "vikunja",
                "collection": "task",
                "op": "create",
                "fields": {
                    "title": f"new {b}-{i}",
                    "project_id": project,
                    "description": "synthetic",
                },
            }
        )
    return out


def sql_batch(
    rng: random.Random, ids: list[int], b: int, tag: int
) -> list[dict[str, Any]]:
    picks = rng.sample(ids, 7)
    out = []
    for rid in picks[:5]:
        field = rng.choice(["note_public", "phone", "town", "email"])
        value = {
            "note_public": f"synthetic note {b}",
            "phone": f"555-9{b:03d}",
            "town": f"Town {b}",
            "email": f"lab{b}@example.test",
        }[field]
        out.append(
            {
                "system": "dolibarr",
                "collection": "llx_societe",
                "op": "update",
                "id": rid,
                "fields": {field: value},
            }
        )
    for rid in picks[5:]:
        out.append(
            {
                "system": "dolibarr",
                "collection": "llx_societe",
                "op": "delete",
                "id": rid,
            }
        )
    for i in range(3):
        out.append(
            {
                "system": "dolibarr",
                "collection": "llx_societe",
                "op": "create",
                "fields": {
                    "nom": f"Lab new {tag}-{b}-{i}",
                    "entity": 1,
                    "datec": "2026-11-21 12:00:00",
                    "status": 1,
                    "client": 1,
                },
            }
        )
    return out


def mail_batch(b: int) -> list[dict[str, Any]]:
    return [
        {
            "system": "outbox",
            "op": "send",
            "fields": {
                "to": f"lab{b}-{i}@example.test",
                "subject": f"Synthetic notice {b}-{i}",
                "body": "synthetic body",
            },
        }
        for i in range(10)
    ]


# -- one round -----------------------------------------------------------------


def round_trip(
    ctx: Ctx, task: str, writes: list[dict[str, Any]], *, release_first: int = 0
) -> dict[str, Any]:
    plan = engine.make_plan(ctx, task, writes)
    engine.approve_plan(
        ctx.state, plan["digest"], approver="lab", decided_by="automation"
    )
    applied = engine.apply_plan(ctx, plan["digest"])
    released = None
    if release_first:
        time.sleep(1.2)
        # Release only the first N held writes: cancel the rest first.
        held = [h for h in ctx.state.held() if h["plan"] == plan["digest"]]
        for h in held[release_first:]:
            ctx.state.unhold(h["key"])
            e = ctx.state.entry(h["key"])
            ctx.state.record(
                h["key"],
                "cancelled",
                plan=plan["digest"],
                payload=e.payload if e else "",
            )
        released = engine.release(ctx, plan["digest"])
    undone = engine.undo_plan(ctx, plan["digest"])
    mailbox = None
    if release_first:
        mail = ctx.adapter("outbox")
        per = {
            "cancelled_never_sent": 0,
            "cancelled_sent": 0,
            "released_exactly_once": 0,
            "released_other": 0,
        }
        for w in plan["writes"]:
            n = mail.count(w["key"])
            status = ctx.state.entry(w["key"]).status
            if status == "cancelled":
                per["cancelled_never_sent" if n == 0 else "cancelled_sent"] += 1
            else:
                per["released_exactly_once" if n == 1 else "released_other"] += 1
        mailbox = per
    return {
        "mailbox": mailbox,
        "digest": plan["digest"][:16],
        "dry_run": plan["dry_run"],
        "anomalies": len(plan["anomalies"]),
        "apply": applied["tx"],
        "apply_outcome": applied["outcome"],
        "missed": applied["missed"],
        "false": applied["false"],
        "release": released["tx"] if released else None,
        "undo": undone["tx"],
        "undo_outcome": undone["outcome"],
        "collateral": undone["collateral"][:10],
    }


def acrfence(
    ctx: Ctx,
    kind: str,
    trials: int,
    count: Callable[[], int],
    make: Callable[[str, int], dict[str, Any]],
    *,
    release: bool = False,
) -> dict[str, Any]:
    """Control arm (no guard) and guarded arm, per trial."""
    raw = ctx.adapter(make("x", 0)["system"])
    ctrl_dups = 0
    guarded_extra = 0
    blocked = 0
    replays_not_sent = 0
    for t in range(trials):
        # Control: the paper's UUID-style request check sees two different requests.
        n0 = count()
        a, b = make(f"ctrl-a-{t}", t), make(f"ctrl-b-{t}", t)
        if isinstance(raw, MailAdapter):
            raw.send(a, f"ik-ctrla{t:035d}")
            raw.send(b, f"ik-ctrlb{t:035d}")
            time.sleep(0.5)
        else:
            raw.apply(a, None, f"req-a-{t}")
            raw.apply(b, None, f"req-b-{t}")
        ctrl_dups += count() - n0 - 1
        # Guarded.
        task = f"acr-{kind}-{t}-{int(time.time())}"
        n0 = count()
        p = engine.make_plan(ctx, task, [make(f"uuid-a-{t}", t)])
        engine.approve_plan(
            ctx.state, p["digest"], approver="lab", decided_by="automation"
        )
        engine.apply_plan(ctx, p["digest"])
        if release:
            time.sleep(1.2)
            engine.release(ctx, p["digest"])
        # Restore: drop the agent's in-memory context and regenerate the payload.
        ctx2 = Ctx(spec=ctx.spec, state=State(ctx.state.root))
        q = engine.make_plan(ctx2, task, [make(f"uuid-b-{t}", t)])
        engine.approve_plan(
            ctx2.state, q["digest"], approver="lab", decided_by="automation"
        )
        try:
            engine.apply_plan(ctx2, q["digest"])
        except TxRefused as exc:
            blocked += int(exc.code == "replay_mismatch")
        r = engine.make_plan(ctx2, task, [make(f"uuid-a-{t}", t)])
        engine.approve_plan(
            ctx2.state, r["digest"], approver="lab", decided_by="automation"
        )
        out = engine.apply_plan(ctx2, r["digest"])
        if release:
            time.sleep(1.2)
            engine.release(ctx2, r["digest"])
        replays_not_sent += int(
            out["tx"].get("replays", 0) == 1 and out["tx"]["sent"] == 0
        )
        time.sleep(0.3 if release else 0)
        guarded_extra += max(0, count() - n0 - 1)
    return {
        "trials": trials,
        "control_duplicates": ctrl_dups,
        "blocked": blocked,
        "identical_replays_not_sent": replays_not_sent,
        "double_effects_through": guarded_extra,
    }


def crash_arm(
    spec_path: Path,
    state_root: Path,
    trials: int,
    count: Callable[[], int],
    make: Callable[[str, int], dict[str, Any]],
) -> dict[str, Any]:
    extra = 0
    crashed = 0
    for t in range(trials):
        ctx = Ctx(spec=load_spec(spec_path), state=State(state_root))
        task = f"crash-{t}-{int(time.time())}"
        n0 = count()
        p = engine.make_plan(ctx, task, [make(f"uuid-c-{t}", t)])
        engine.approve_plan(
            ctx.state, p["digest"], approver="lab", decided_by="automation"
        )
        code = (
            "from pathlib import Path; from openadapt_flow.txplan import engine;"
            "from openadapt_flow.txplan.spec import load_spec; from openadapt_flow.txplan.state import State;"
            f"engine.apply_plan(engine.Ctx(load_spec(Path({str(spec_path)!r})), State(Path({str(state_root)!r}))), {p['digest']!r})"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, engine.CRASH_ENV: "1"},
            capture_output=True,
            timeout=300,
        )
        crashed += int(proc.returncode == 70)
        for ref in (f"uuid-d-{t}", f"uuid-c-{t}"):  # regenerated, then identical
            q = engine.make_plan(ctx, task, [make(ref, t)])
            engine.approve_plan(
                ctx.state, q["digest"], approver="lab", decided_by="automation"
            )
            try:
                engine.apply_plan(ctx, q["digest"])
            except TxRefused:
                pass
        extra += max(0, count() - n0 - 1)
    return {
        "trials": trials,
        "crashed_after_send": crashed,
        "double_effects_through": extra,
    }


def seeded_faults(
    ctx: Ctx,
    writes_fn: Callable[[int], list[dict[str, Any]]],
    poke: Callable[[], None],
    adapter_cls: Any,
    trials: int,
) -> dict[str, Any]:
    """A change the plan didn't predict, during apply and before undo: both must be caught."""
    caught_apply = caught_undo = 0
    detail: list[dict[str, Any]] = []
    real = adapter_cls.apply
    for t in range(trials):

        def apply_and_poke(self, w, base, key, _real=real):  # noqa: ANN001
            out = _real(self, w, base, key)
            poke()
            return out

        plan = engine.make_plan(ctx, f"fault-a-{t}-{int(time.time())}", writes_fn(t))
        engine.approve_plan(
            ctx.state, plan["digest"], approver="lab", decided_by="automation"
        )
        adapter_cls.apply = apply_and_poke
        try:
            applied = engine.apply_plan(ctx, plan["digest"])
        finally:
            adapter_cls.apply = real
        caught_apply += int(applied["tx"]["missed_effects"] > 0)
        engine.undo_plan(ctx, plan["digest"])
        plan = engine.make_plan(
            ctx, f"fault-u-{t}-{int(time.time())}", writes_fn(1000 + t)
        )
        engine.approve_plan(
            ctx.state, plan["digest"], approver="lab", decided_by="automation"
        )
        engine.apply_plan(ctx, plan["digest"])
        poke()
        undone = engine.undo_plan(ctx, plan["digest"])
        hit = (
            undone["outcome"] != "ROLLED_BACK"
            and undone["tx"]["collateral_changes"] > 0
        )
        caught_undo += int(hit)
        if not hit:
            detail.append(
                {
                    "trial": t,
                    "undo": undone["outcome"],
                    "codes": undone["tx"]["codes"],
                    "collateral": undone["tx"]["collateral_changes"],
                    "planned": plan["predicted"],
                }
            )
    return {
        "trials": trials,
        "missed_effect_caught": caught_apply,
        "collateral_caught": caught_undo,
        "not_caught": detail,
    }


# -- main ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pilot", "measured"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--acr-trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261121)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    work = Path(tempfile.mkdtemp(prefix="ext-tx-"))
    os.environ["OPENADAPT_TX_STATE"] = str(work / "state")
    os.environ.setdefault("OPENADAPT_SEAL_HOME", str(work / "seal"))
    os.environ["LAB_DOLI_DB_PASSWORD"] = doli_password()
    token, project = vikunja_setup()
    os.environ["LAB_VIKUNJA_TOKEN"] = token
    spec_path = work / "openadapt.tx.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    ctx = Ctx(spec=load_spec(spec_path), state=State())
    vik: RestAdapter = ctx.adapter("vikunja")
    sql: SqlAdapter = ctx.adapter("dolibarr")
    mail: MailAdapter = ctx.adapter("outbox")
    ids = sorted(int(r.split("/")[1]) for r in vik.frame("task"))
    log(f"vikunja: {len(ids)} tasks; project {project}")
    tag = int(time.time())
    doli_ids = doli_seed(sql, 60)
    log(f"dolibarr: seeded {len(doli_ids)} thirdparties")
    results: dict[str, Any] = {
        "schema": "openadapt.ext-tx-lab/v1",
        "phase": args.phase,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": {
            "rest": "Vikunja 2.7.1",
            "email": "Mailpit v1.27.10",
            "database": "Dolibarr 21.0.4 on MariaDB 11.4.13",
        },
        "spec_sha256": __import__("hashlib").sha256(spec_path.read_bytes()).hexdigest(),
        "rounds": {"rest": [], "sql": [], "mail": []},
    }
    try:
        for b in range(args.batches):
            r = round_trip(ctx, f"lab-rest-{tag}-{b}", rest_batch(rng, ids, project, b))
            results["rounds"]["rest"].append(r)
            log(
                f"rest {b}: apply {r['apply_outcome']} missed {r['missed']} false {r['false']} undo {r['undo_outcome']} {r['undo']['codes']} {r['collateral']}"
            )
        for b in range(args.batches):
            r = round_trip(ctx, f"lab-sql-{tag}-{b}", sql_batch(rng, doli_ids, b, tag))
            results["rounds"]["sql"].append(r)
            log(
                f"sql {b}: apply {r['apply_outcome']} missed {r['missed']} false {r['false']} undo {r['undo_outcome']} {r['undo']['codes']} {r['collateral']}"
            )
        for b in range(args.batches):
            r = round_trip(ctx, f"lab-mail-{tag}-{b}", mail_batch(b), release_first=5)
            results["rounds"]["mail"].append(r)
            log(
                f"mail {b}: apply {r['apply_outcome']} release {r['release']} undo {r['undo']['codes']}"
            )

        def vik_count() -> int:
            return len(vik.frame("task"))

        def vik_make(ref: str, t: int) -> dict[str, Any]:
            return {
                "system": "vikunja",
                "collection": "task",
                "op": "create",
                "fields": {
                    "title": f"refund {ref}",
                    "project_id": project,
                    "description": f"reference {ref}",
                },
            }

        def sql_count() -> int:
            return sum(1 for r in sql.frame() if r.startswith("llx_societe/"))

        def sql_make(ref: str, t: int) -> dict[str, Any]:
            return {
                "system": "dolibarr",
                "collection": "llx_societe",
                "op": "create",
                "fields": {
                    "nom": f"Lab refund {tag}-{ref}",
                    "entity": 1,
                    "datec": "2026-11-21 12:00:00",
                    "status": 1,
                    "client": 1,
                },
            }

        def mail_count() -> int:
            return len(mail.frame())

        def mail_make(ref: str, t: int) -> dict[str, Any]:
            return {
                "system": "outbox",
                "op": "send",
                "fields": {
                    "to": "billing@example.test",
                    "subject": f"Refund {ref}",
                    "body": f"reference {ref}",
                },
            }

        results["acrfence"] = {
            "rest": acrfence(ctx, "rest", args.acr_trials, vik_count, vik_make),
            "sql": acrfence(ctx, "sql", args.acr_trials, sql_count, sql_make),
            "mail": acrfence(
                ctx, "mail", args.acr_trials, mail_count, mail_make, release=True
            ),
        }
        log(f"acrfence {results['acrfence']}")
        results["crash"] = {
            "rest": crash_arm(
                spec_path,
                ctx.state.root,
                max(5, args.acr_trials // 4),
                vik_count,
                vik_make,
            ),
            "sql": crash_arm(
                spec_path,
                ctx.state.root,
                max(5, args.acr_trials // 4),
                sql_count,
                sql_make,
            ),
        }
        log(f"crash {results['crash']}")

        other = [i for i in ids if i % 7 == 0][:1]

        def vik_poke() -> None:
            got = vik.read("task", str(other[0])) or {}
            body = {k: v for k, v in got.items() if k != "updated"}
            body["description"] = f"poked {time.time()}"
            vik.http.post(f"/api/v1/tasks/{other[0]}", json=body).raise_for_status()

        def sql_poke() -> None:
            con = sql.connect()
            try:
                cur = con.cursor()
                cur.execute(
                    "UPDATE llx_societe SET town = %s WHERE rowid = %s",
                    (f"poked {time.time()}", doli_ids[-1]),
                )
                con.commit()
            finally:
                con.close()

        results["seeded_faults"] = {
            "rest": seeded_faults(
                ctx,
                lambda t: [
                    {
                        "system": "vikunja",
                        "collection": "task",
                        "op": "update",
                        "id": ids[t % 5],
                        "fields": {"priority": (t % 4) + 1},
                    }
                ],
                vik_poke,
                RestAdapter,
                5,
            ),
            "sql": seeded_faults(
                ctx,
                lambda t: [
                    {
                        "system": "dolibarr",
                        "collection": "llx_societe",
                        "op": "update",
                        "id": doli_ids[t % 5],
                        "fields": {"phone": f"555-7{t:03d}"},
                    }
                ],
                sql_poke,
                SqlAdapter,
                5,
            ),
        }
        log(f"faults {results['seeded_faults']}")
    finally:
        removed = doli_cleanup(sql)
        log(f"dolibarr cleanup: removed {removed} lab rows")
        results["dolibarr_lab_rows_left"] = doli_cleanup(sql)
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from receipts_tally import tally

            results["receipts"] = tally(ctx.state.root)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            results["receipts"] = {"error": f"{type(exc).__name__}: {exc}"}
        results["code_sha"] = os.environ.get("LAB_CODE_SHA", "")
        results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results["summary"] = summarize(results)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
        log(json.dumps(results["summary"], indent=2, sort_keys=True))
    return 0


def summarize(res: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, rounds in res["rounds"].items():
        if not rounds:
            continue
        sup = sum(r["undo"]["undo_supported"] for r in rounds)
        ver = sum(r["undo"]["undo_verified"] for r in rounds)
        out[kind] = {
            "rounds": len(rounds),
            "writes": sum(r["apply"]["writes"] for r in rounds),
            "apply_verified_rounds": sum(
                1
                for r in rounds
                if r["apply_outcome"] in ("VERIFIED", "COMPLETED_UNVERIFIED")
            ),
            "missed_effects": sum(r["apply"].get("missed_effects", 0) for r in rounds),
            "false_diffs": sum(r["apply"].get("false_diffs", 0) for r in rounds),
            "undo_supported": sup,
            "undo_verified": ver,
            "undo_success_rate": round(ver / sup, 4) if sup else None,
            "cancelled_in_holdback": sum(r["undo"].get("cancelled", 0) for r in rounds),
            "irreversible_released": sum(
                r["undo"].get("irreversible_released", 0) for r in rounds
            ),
            "release_double_sends": sum(
                (r["release"] or {}).get("double", 0) for r in rounds
            ),
            **{
                k: sum((r.get("mailbox") or {}).get(k, 0) for r in rounds)
                for k in (
                    "cancelled_never_sent",
                    "cancelled_sent",
                    "released_exactly_once",
                    "released_other",
                )
            },
        }
    acr = res.get("acrfence", {})
    crash = res.get("crash", {})
    out["double_effects_through"] = sum(
        v["double_effects_through"] for v in acr.values()
    ) + sum(v["double_effects_through"] for v in crash.values())
    out["blocked_double_effects"] = sum(v["blocked"] for v in acr.values())
    out["control_duplicates"] = sum(v["control_duplicates"] for v in acr.values())
    kills = []
    m = out.get("mail") or {}
    if m.get("cancelled_sent") or m.get("released_other"):
        kills.append(
            "mail: a cancelled email was sent or a released one was not sent exactly once"
        )
    for kind in ("rest", "sql"):
        k = out.get(kind) or {}
        if k.get("undo_success_rate") is not None and k["undo_success_rate"] < 0.95:
            kills.append(f"{kind}: undo success {k['undo_success_rate']}")
        if k.get("missed_effects"):
            kills.append(f"{kind}: {k['missed_effects']} missed effects")
    if out["double_effects_through"]:
        kills.append(f"{out['double_effects_through']} double effects through")
    out["kill_criteria_tripped"] = kills
    return out


if __name__ == "__main__":
    sys.exit(main())
