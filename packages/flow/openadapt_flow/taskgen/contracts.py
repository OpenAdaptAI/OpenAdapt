"""Contracts for every kind, from the schema and demonstrations.

The task generator writes no contract logic of its own. For each kind it
makes two demonstration writes through the app's screens (the same
scripted UI steps a task's solution uses), snapshots the database around
each with :mod:`openadapt_flow.generate.verify_contract`, and runs that
generator's ``propose``. Each task then gets its contract from the manifest
with :func:`~openadapt_flow.generate.verify_contract.choose`, exactly as a
deployment picks one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from openadapt_flow.generate import verify_contract as vc
from openadapt_flow.taskgen import kinds as K
from openadapt_flow.taskgen.profile import Profile
from openadapt_flow.taskgen.runner import Browser

READER_PASSWORD_ENV = "WB_READER_PASSWORD"


def read_yaml(dsn: Mapping[str, Any]) -> dict[str, Any]:
    """The read-only connection the contracts use (``read.yaml``)."""

    return {
        "sql_driver": "psycopg2",
        "sql_connect_args": {
            "host": dsn["host"],
            "port": int(dsn["port"]),
            "user": dsn["reader"],
            "dbname": dsn["dbname"],
        },
        "sql_password_env": READER_PASSWORD_ENV,
    }


def demo_plan(prof: Profile, w: K.World, s: K.Sampler) -> list[K.Write]:
    """Two demonstration writes per kind, on different people, with
    different field sets where a kind allows them."""

    P, C = prof.entity(prof.person), prof.entity(prof.child)
    out: list[K.Write] = []
    people = [i + 1 for i in range(len(w.rows[prof.person]))]
    kids = {
        pid: [cid for cid, _ in w.find(prof.child, **{C.parent[1]: f"$p{pid}"})]
        for pid in people
    }
    for n, pid in enumerate(people[:2]):
        new = {f.name: s.value(C, f.name) for f in C.editable}
        out.append(K.make_write(prof, "create", w, pid, new=new))
        person = w.get(prof.person, pid)
        changes = P.changes[:1] if n == 0 else P.changes
        out.append(
            K.make_write(
                prof,
                "update",
                w,
                pid,
                new={c: s.value(P, c, avoid=person[c], person=person) for c in changes},
            )
        )
        cid = kids[pid][0]
        child = w.get(prof.child, cid)
        cchanges = C.changes[:1] if n == 0 else C.changes
        out.append(
            K.make_write(
                prof,
                "update_child",
                w,
                pid,
                child_id=cid,
                new={c: s.value(C, c, avoid=child[c]) for c in cchanges},
            )
        )
        cid2 = kids[pid][-1] if len(kids[pid]) > 1 and n == 1 else kids[pid][0]
        out.append(
            K.make_write(
                prof,
                "cancel",
                w,
                pid,
                child_id=cid2,
                new={C.cancel["reason_field"]: s.value(C, C.cancel["reason_field"])},
            )
        )
    out.append(K.make_write(prof, "merge", w, people[2], survivor_id=people[3]))
    out.append(K.make_write(prof, "merge", w, people[4], survivor_id=people[5]))
    return out


def demonstrate_and_propose(
    prof: Profile,
    url: str,
    wb: Any,
    dsn: Mapping[str, Any],
    work: Path,
    *,
    seed: int = 7,
) -> Path:
    """Make the demonstrations, run ``propose``; returns the manifest path.

    Each demonstration starts from a fresh reset of the same world so the
    diff holds that one write.
    """

    os.environ.setdefault(READER_PASSWORD_ENV, dsn.get("password", "synthetic-reader"))
    work.mkdir(parents=True, exist_ok=True)
    read_path = work / "read.yaml"
    read_path.write_text(yaml.safe_dump(read_yaml(dsn)), encoding="utf-8")
    s = K.Sampler(prof, seed)
    w = K.base_world(s, prof, n_people=6)
    plan = demo_plan(prof, w, s)
    demos = work / "demos"
    kinds_doc = K.kinds_yaml(prof)
    examples: dict[str, list[dict[str, Any]]] = {}
    browser = Browser()
    try:
        for i, write in enumerate(plan):
            wb.reset({"rows": w.setup_rows()})
            before = work / f"before-{i}.json"
            vc.main(["snapshot", "--read", str(read_path), "--out", str(before)])
            pid = next(
                n
                for n, r in w.find(prof.person)
                if r[prof.entity(prof.person).key]
                == write.values[prof.entity(prof.person).key]
            )
            browser.open(url + "/login")
            browser.run(
                K.sign_in(prof)
                + K.open_person(prof, write.values[prof.entity(prof.person).key])
                + write.actions
            )
            vc.main(
                [
                    "record",
                    "--read",
                    str(read_path),
                    "--before",
                    str(before),
                    "--kind",
                    write.kind,
                    "--values",
                    json.dumps(write.values),
                    "--demos",
                    str(demos),
                ]
            )
            examples.setdefault(write.kind, []).append({"values": write.values})
            del pid
    finally:
        browser.close()
    for k in kinds_doc["kinds"]:
        k["examples"] = examples.get(k["kind"], [])
    kinds_path = work / "kinds.yaml"
    kinds_path.write_text(yaml.safe_dump(kinds_doc, sort_keys=False), encoding="utf-8")
    out = work / "out"
    argv = [
        "propose",
        "--read",
        str(read_path),
        "--kinds",
        str(kinds_path),
        "--demos",
        str(demos),
        "--out",
        str(out),
        "--actor-param",
        f"agent_user={prof.agent['user']}",
        "--environment-id",
        f"taskgen-workbench-{prof.app}-v{prof.version}",
    ]
    if prof.answers:
        answers = work / "answers.yaml"
        answers.write_text(yaml.safe_dump(dict(prof.answers)), encoding="utf-8")
        argv += ["--answers", str(answers)]
    vc.main(argv)
    return out / "manifest.yaml"
