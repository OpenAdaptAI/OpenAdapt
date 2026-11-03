"""Write task packs: OpenEnv tasks, each with its own effect contract.

    python -m openadapt_flow.taskgen.generate mockmed --manifest work/out/manifest.yaml \\
        --out packs/mockmed --per-cell 6 --seed 1000

For every kind and every family that applies to it (and both forms of the
families that have a positive form), ``--per-cell`` tasks with consecutive
seeds. Each task's contract is the one
:mod:`~openadapt_flow.generate.verify_contract` wrote for its field set; the
generator only adds checks to it (never removes one):

- ``concurrent_edit``: the second user's change must still be there after
  the save (a lost update reads as unchanged against the baseline, so the
  frame alone can't see it), and the app's audit log attributes that user's
  write to them. This is the one place a contract is relaxed: the audit log
  itself becomes append-only, because the second user's audit row names no
  record an entry could attribute; the agent's own audit row stays a
  declared effect.

Output, in ``--out``:

- ``tasks.jsonl``: one ``openadapt.rl-env`` task per line;
- ``contracts/<task id>.yaml``: the task's contract;
- ``validation/<task id>.json``: the planted variants the validator runs
  (never shipped to an agent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import yaml

from openadapt_flow.generate import verify_contract as vc
from openadapt_flow.taskgen import families as F
from openadapt_flow.taskgen.kinds import KINDS, contract_kind
from openadapt_flow.taskgen.profile import Profile, load_profile

TASK_SCHEMA = "openadapt.taskgen-task/v1"


def contract_file(manifest_path: Path, kind: str, values: dict[str, Any]) -> Path:
    """The generated (non-``found``) contract for a task's field set, as
    :func:`~openadapt_flow.generate.verify_contract.choose` picks it."""

    manifest = vc.load_yaml(manifest_path) or {}
    entry = (manifest.get("kinds") or {}).get(kind)
    if not entry:
        raise vc.GenerateError(f"no contract for kind {kind!r}")
    given = {k for k, v in values.items() if v is not None and not isinstance(v, list)}
    for var in entry["variants"]:
        if set(var["fields"]) == given and not var.get("lists"):
            return manifest_path.parent / var[entry["mode"]]
    files = vc._field_set(manifest_path, manifest, kind, entry, given, {})
    if files is None:
        raise vc.GenerateError(f"no contract for {kind} with fields {sorted(given)}")
    return manifest_path.parent / files[entry["mode"]]


def attribution(prof: Profile, read: dict[str, Any]) -> dict[str, Any]:
    """``frame.attribution`` over the app's audit log (profile data)."""

    a = prof.audit
    return {
        "kind": "audit_read",
        "actor_field": a["actor"],
        "self": [{"param": "agent_user"}],
        "record_field": a["record"],
        "record_key": a.get("record_key", "id"),
        "event_field": a["event"],
        "write_events": list(a["write_events"]),
        "insert_events": list(a.get("insert_events", [])),
        "delete_events": list(a.get("delete_events", [])),
        "table_field": a["table_field"],
        "read": {**read, "kind": "sql", "sql_query": a["query"]},
    }


def finish_contract(
    prof: Profile, doc: dict[str, Any], draft: F.Draft
) -> dict[str, Any]:
    """The generated contract with the family's added checks."""

    doc = json.loads(json.dumps(doc))
    if draft.pins:
        main = doc["effects"][0]
        assert main["kind"] == "record_written" and "surface=record" in main.get(
            "probe", ""
        )
        main["match"][draft.pins["field"]] = {"literal": str(draft.pins["value"])}
    if draft.attribution:
        read = dict(doc["frame"]["database"]["read"])
        doc["frame"]["identity_field"] = "id"
        doc["frame"]["attribution"] = attribution(prof, read)
        # The second user's own audit row names no record, so no entry can
        # attribute it: the log is append-only here (existing rows must not
        # change). The agent's one audit row is still its own effect.
        allow = doc["frame"]["database"].setdefault("allow_tables", [])
        if prof.audit["table"] not in allow:
            allow.append(prof.audit["table"])
    return doc


def task_id(prof: Profile, draft: F.Draft, seed: int) -> str:
    form = "pos" if draft.positive else "adv"
    return f"{prof.app}-v{prof.version}-{draft.kind}-{draft.family}-{form}-{seed}"


def write_pack(
    prof: Profile,
    manifest: Path,
    out: Path,
    *,
    per_cell: int,
    seed: int,
    kinds: tuple[str, ...] = KINDS,
    families: Optional[tuple[str, ...]] = None,
    split: str = "unassigned",
) -> list[dict[str, Any]]:
    (out / "contracts").mkdir(parents=True, exist_ok=True)
    (out / "validation").mkdir(parents=True, exist_ok=True)
    tasks: list[dict[str, Any]] = []
    n = seed
    for kind in kinds:
        for family in F.APPLIES[kind]:
            if families and family not in families:
                continue
            for positive in F.positive_forms(family):
                for _ in range(per_cell):
                    n += 1
                    try:
                        draft = F.build(prof, kind, family, positive, n)
                    except F.NotApplicable:
                        break
                    tid = task_id(prof, draft, n)
                    ck = contract_kind(prof, kind)
                    src = contract_file(manifest, ck, draft.write.values)
                    doc = yaml.safe_load(src.read_text(encoding="utf-8"))
                    doc = finish_contract(prof, doc, draft)
                    cpath = out / "contracts" / f"{tid}.yaml"
                    header = (
                        f"# Task {tid}: {ck}, from {src.parent.name}/{src.name} "
                        "(openadapt_flow.generate.verify_contract)"
                        + (
                            ", plus the family's added checks"
                            if draft.pins or draft.attribution
                            else ""
                        )
                        + ".\n"
                    )
                    cpath.write_text(
                        header + yaml.safe_dump(doc, sort_keys=False, width=100),
                        encoding="utf-8",
                    )
                    params = {
                        k: str(v) for k, v in vc.flatten(draft.write.values).items()
                    }
                    params["agent_user"] = prof.agent["user"]
                    setup = {"rows": draft.world.setup_rows(), **draft.setup}
                    task = {
                        "id": tid,
                        "split": split,
                        "instruction": draft.instruction,
                        "start_path": "/login",
                        "params": params,
                        "setup": setup,
                        "feasible": True,
                        "fault": draft.family,
                        "solution": draft.solution,
                        "tags": draft.tags
                        + [
                            f"app:{prof.app}",
                            f"schema:v{prof.version}",
                            f"contract:{ck}",
                        ],
                        "verify": f"contracts/{tid}.yaml",
                    }
                    tasks.append(task)
                    vplan = {
                        "schema_version": TASK_SCHEMA,
                        "id": tid,
                        "app": prof.app,
                        "profile_version": prof.version,
                        "kind": kind,
                        "family": family,
                        "positive": draft.positive,
                        "generated_from": f"{src.parent.name}/{src.name}",
                        "planted": [
                            {"name": p.name, "setup": p.setup, "solution": p.solution}
                            for p in draft.planted
                        ],
                    }
                    (out / "validation" / f"{tid}.json").write_text(
                        json.dumps(vplan, indent=1), encoding="utf-8"
                    )
    with (out / "tasks.jsonl").open("w", encoding="utf-8") as fh:
        for t in tasks:
            fh.write(json.dumps(t, sort_keys=True) + "\n")
    return tasks


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile")
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--per-cell", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--kind", action="append")
    ap.add_argument("--family", action="append")
    args = ap.parse_args(argv)
    prof = load_profile(args.profile)
    tasks = write_pack(
        prof,
        args.manifest,
        args.out,
        per_cell=args.per_cell,
        seed=args.seed,
        kinds=tuple(args.kind) if args.kind else KINDS,
        families=tuple(args.family) if args.family else None,
    )
    print(f"{len(tasks)} tasks -> {args.out / 'tasks.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
