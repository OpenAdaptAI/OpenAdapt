"""Multi-app tasks: one write in each of two apps, linked by the same person.

    python -m openadapt_flow.taskgen.multiapp flows/patient-financing.yaml \\
        --manifests mockmed=work/mockmed/out/manifest.yaml \\
        --manifests mockloan=work/mockloan/out/manifest.yaml --out packs/flow --count 30

A flow (``openadapt.taskgen-flow/v1``, data) names two steps. Each step is
an ordinary kind on one app's profile; the second step copies values from
the first (``copy: {memo: note}``) and the same person exists in both apps
under different keys, so the agent has to find them again by name and
birth date. Each step keeps its own contract from its own app's manifest,
so each is judged against its own system of record; the task is paid only
when both are ``CONFIRMED``.

Planted variants: the second step skipped, the second step on a look-alike
person, and an app fault in either app.

A task lists its apps under ``apps`` (each with its own ``setup``,
``params`` and ``verify``) and switches between them in the solution with
``{"kind": "switch_app", "app": ...}``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

import yaml

from openadapt_flow.generate import verify_contract as vc
from openadapt_flow.taskgen import families as F
from openadapt_flow.taskgen import kinds as K
from openadapt_flow.taskgen.generate import contract_file, task_id
from openadapt_flow.taskgen.kinds import contract_kind
from openadapt_flow.taskgen.profile import load_profile

FLOW_SCHEMA = "openadapt.taskgen-flow/v1"


def build_flow(flow: dict[str, Any], seed: int) -> dict[str, Any]:
    a_spec, b_spec = flow["steps"]
    pa, pb = load_profile(a_spec["profile"]), load_profile(b_spec["profile"])
    s = K.Sampler(pa, seed)
    sb = K.Sampler(pb, seed + 1)
    # Step one: the first app's world and write.
    wa = K.base_world(s, pa, n_people=s.r.randint(4, 7))
    pid_a = s.r.randint(1, len(wa.rows[pa.person]))
    person_a = wa.get(pa.person, pid_a)
    Ca = pa.entity(pa.child)
    new_a = {f.name: s.value(Ca, f.name) for f in Ca.editable}
    have = {
        r[Ca.identify_by] for _, r in wa.find(pa.child, **{Ca.parent[1]: f"$p{pid_a}"})
    }
    while new_a[Ca.identify_by] in have:  # a new record, never a "found" one
        new_a[Ca.identify_by] = s.value(Ca, Ca.identify_by)
    new_a.update(a_spec.get("set", {}))
    write_a = K.make_write(pa, a_spec["kind"], wa, pid_a, new=new_a)
    # Step two: the same person in the second app, under its own key, next
    # to a look-alike with a swapped key and the same last name.
    wb = K.base_world(sb, pb, n_people=sb.r.randint(4, 7))
    na, nb = pa.names, pb.names
    twin = K.person_row(
        sb,
        pb,
        first=person_a[na["first"]],
        last=person_a[na["last"]],
        dob=person_a[na["birth"]],
    )
    twin["_as"] = f"p{len(wb.rows[pb.person]) + 1}"
    pid_b = wb.add(pb.person, twin)
    look = K.person_row(
        sb,
        pb,
        first=sb.pick(nb["first"]),
        last=twin[nb["last"]],
        key=K.lookalike(twin[pb.entity(pb.person).key]),
    )
    look["_as"] = f"p{len(wb.rows[pb.person]) + 1}"
    pid_look = wb.add(pb.person, look)
    Cb = pb.entity(pb.child)
    new_b = {f.name: sb.value(Cb, f.name) for f in Cb.editable}
    new_b.update(b_spec.get("set", {}))
    for dst, src in (b_spec.get("copy") or {}).items():
        new_b[dst] = new_a[src]
    write_b = K.make_write(pb, b_spec["kind"], wb, pid_b, new=new_b)
    Pa, Pb = pa.entity(pa.person), pb.entity(pb.person)
    name = f"{person_a[na['first']]} {person_a[na['last']]}"
    parts_a = ", ".join(f'{Ca.field(k).label.lower()} "{v}"' for k, v in new_a.items())
    set_b = ", ".join(
        f'{Cb.field(k).label.lower()} "{v}"'
        for k, v in new_b.items()
        if k not in (b_spec.get("copy") or {})
    )
    copied = " and ".join(
        f"the {Cb.field(dst).label.lower()} set to the {Ca.field(src).label.lower()} you just entered"
        for dst, src in (b_spec.get("copy") or {}).items()
    )
    instruction = (
        f"Two apps are open. In {pa.title} (user name {pa.agent['user']}, password {pa.agent['password']}), open "
        f"{Pa.label.lower()} {name} ({Pa.field(Pa.key).label} {person_a[Pa.key]}) and add {_a(Ca.label)} {Ca.label.lower()} with {parts_a}. "
        f"Then in {pb.title} (user name {pb.agent['user']}, password {pb.agent['password']}), find the {Pb.label.lower()} "
        f"with the same name and date of birth ({person_a[na['birth']]}) and add {_a(Cb.label)} {Cb.label.lower()} with {set_b}, "
        f"and {copied}. Save each once. Finish with done: claim success only if both records show the change exactly "
        "as asked and only once, otherwise claim failure."
    )
    steps_a = (
        [{"kind": "switch_app", "app": pa.name}]
        + K.sign_in(pa)
        + K.open_person(pa, person_a[Pa.key])
        + write_a.actions
    )
    open_b = K.open_person(pb, twin[Pb.key], search=twin[nb["last"]])
    steps_b = (
        [{"kind": "switch_app", "app": pb.name}]
        + K.sign_in(pb)
        + open_b
        + write_b.actions
    )
    done = {"kind": "done", "claim": "success"}
    wrong_b = K.make_write(pb, b_spec["kind"], wb, pid_look, new=new_b)
    planted = [
        {"name": "trap:second_step_skipped", "solution": steps_a + [done]},
        {
            "name": "trap:second_step_on_lookalike",
            "solution": steps_a
            + [{"kind": "switch_app", "app": pb.name}]
            + K.sign_in(pb)
            + K.open_person(pb, look[Pb.key], search=twin[nb["last"]])
            + wrong_b.actions
            + [done],
        },
        {"name": "fault:" + s.r.choice(F.APP_FAULTS[a_spec["kind"]]), "app": pa.name},
        {"name": "fault:" + s.r.choice(F.APP_FAULTS[b_spec["kind"]]), "app": pb.name},
    ]
    for p in planted:
        if p["name"].startswith("fault:"):
            p["setup"] = {"fault": p["name"].split(":", 1)[1]}
    return {
        "profiles": (pa, pb),
        "worlds": (wa, wb),
        "writes": (write_a, write_b),
        "instruction": instruction,
        "solution": steps_a + steps_b + [done],
        "planted": planted,
    }


def _a(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def write_flows(
    flow_path: Path, manifests: dict[str, Path], out: Path, *, count: int, seed: int
) -> list[dict[str, Any]]:
    flow = yaml.safe_load(flow_path.read_text())
    if flow.get("schema_version") != FLOW_SCHEMA:
        raise ValueError(f"{flow_path}: not an {FLOW_SCHEMA} document")
    (out / "contracts").mkdir(parents=True, exist_ok=True)
    (out / "validation").mkdir(parents=True, exist_ok=True)
    tasks = []
    for i in range(count):
        n = seed + i
        built = build_flow(flow, n)
        name = f"flow-{flow['name']}-{n}"
        tid = task_id(name, f"flow-{flow['name']}")
        apps: dict[str, Any] = {}
        for prof, world, write in zip(
            built["profiles"], built["worlds"], built["writes"]
        ):
            ck = contract_kind(prof, write_kind(write, prof))
            src = contract_file(manifests[prof.name], ck, write.values)
            dst = out / "contracts" / f"{tid}.{prof.app}-v{prof.version}.yaml"
            dst.write_text(
                f"# Task {tid}, step in {prof.title}: {ck}, from {src.parent.name}/{src.name} "
                "(openadapt_flow.generate.verify_contract).\n" + src.read_text(),
                encoding="utf-8",
            )
            params = {k: str(v) for k, v in vc.flatten(write.values).items()}
            params["agent_user"] = prof.agent["user"]
            apps[prof.name] = {
                "profile": prof.path.stem if prof.path else prof.app,
                "start_path": "/login",
                "setup": {"rows": world.setup_rows()},
                "params": params,
                "verify": f"contracts/{dst.name}",
            }
        task = {
            "id": tid,
            "split": "unassigned",
            "instruction": built["instruction"],
            "apps": apps,
            "feasible": True,
            "fault": "multi_app",
            "solution": built["solution"],
            "tags": [f"app:{flow['name']}", "schema:v1", "kind:multi_app"],
        }
        tasks.append(task)
        (out / "validation" / f"{tid}.json").write_text(
            json.dumps(
                {
                    "id": tid,
                    "name": name,
                    "app": flow["name"],
                    "schema": 1,
                    "kind": "multi_app",
                    "family": "multi_app",
                    "positive": False,
                    "planted": built["planted"],
                },
                indent=1,
            )
        )
    with (out / "tasks.jsonl").open("w") as fh:
        for t in tasks:
            fh.write(json.dumps(t, sort_keys=True) + "\n")
    return tasks


def write_kind(write: K.Write, prof: Any) -> str:
    for k in K.KINDS:
        if contract_kind(prof, k) == write.kind:
            return k
    raise ValueError(write.kind)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("flow", type=Path)
    ap.add_argument(
        "--manifests",
        action="append",
        required=True,
        help="PROFILE=path/to/manifest.yaml",
    )
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--seed", type=int, default=40000)
    args = ap.parse_args(argv)
    manifests = {}
    for m in args.manifests:
        name, path = m.split("=", 1)
        manifests[load_profile(name).name] = Path(path)
    tasks = write_flows(
        args.flow, manifests, args.out, count=args.count, seed=args.seed
    )
    print(f"{len(tasks)} tasks -> {args.out / 'tasks.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
