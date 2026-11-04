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
  write to them.

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
from openadapt_flow.taskgen import secret as S
from openadapt_flow.taskgen.kinds import KINDS, contract_kind
from openadapt_flow.taskgen.profile import PROFILE_DIR, Profile, load_profile

TASK_SCHEMA = "openadapt.taskgen-task/v1"


def contract_file(manifest_path: Path, kind: str, values: dict[str, Any]) -> Path:
    """The generated ``create`` or ``update`` contract for a task's field set.

    This is the file :func:`~openadapt_flow.generate.verify_contract.choose`
    picks for these values when the store doesn't already hold them (else
    it picks ``found``). The generator never writes a task whose record is
    already there (``families`` makes every create's values new), and it
    refuses kinds whose manifest dispatches on a value or holds lists,
    which ``choose`` would resolve differently.
    """

    manifest = vc.load_yaml(manifest_path) or {}
    if manifest.get("schema_version") != vc.MANIFEST_SCHEMA:
        raise vc.GenerateError(f"{manifest_path} is not a contract manifest")
    if manifest.get("questions_open"):
        raise vc.GenerateError(f"{manifest_path} still has open questions")
    entry = (manifest.get("kinds") or {}).get(kind)
    if not entry:
        raise vc.GenerateError(f"no contract for kind {kind!r}")
    if entry.get("dispatch") or any(v.get("lists") for v in entry["variants"]):
        raise vc.GenerateError(f"{kind}: dispatch and list kinds are not supported")
    given = {k for k, v in values.items() if v is not None and not isinstance(v, list)}
    for var in entry["variants"]:
        if set(var["fields"]) == given:
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
        doc["frame"].setdefault("identity_field", "id")
        doc["frame"]["attribution"] = attribution(prof, read)
    return doc


def write_pack(
    prof: Profile,
    manifest: Path,
    out: Path,
    *,
    per_cell: int,
    seed: int,
    kinds: tuple[str, ...] = KINDS,
    families: Optional[tuple[str, ...]] = None,
    split: str = "public",
) -> list[dict[str, Any]]:
    """Write a pack. A ``public`` pack uses public seeds and only the bundled
    profiles and families; any other split needs the private salt
    (:mod:`openadapt_flow.taskgen.secret`), which seeds its tasks and ids."""

    salt = S.load_salt(split)
    if salt is None and (prof.path is None or prof.path.parent != PROFILE_DIR):
        raise S.SaltMissing(
            f"{prof.name} is not a bundled profile: only private splits use it"
        )
    if salt is None and F.REGISTERED:
        raise S.SaltMissing(
            "registered (private) families can't go into a public split"
        )
    (out / "contracts").mkdir(parents=True, exist_ok=True)
    (out / "validation").mkdir(parents=True, exist_ok=True)
    tasks: list[dict[str, Any]] = []
    n = seed
    for kind in kinds:
        for family in F.applies(kind):
            if families and family not in families:
                continue
            for positive in F.positive_forms(family):
                for i in range(per_cell):
                    n += 1
                    form = "pos" if positive else "adv"
                    name = f"{prof.app}-v{prof.version}-{kind}-{family}-{form}-{n}"
                    try:
                        draft = F.build(
                            prof, kind, family, positive, S.seed(salt, name, n)
                        )
                    except F.NotApplicable:
                        break
                    tid = S.task_id(salt, name, f"{prof.app}-v{prof.version}")
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
                        # The family stays out of the tags (and the id): only
                        # the private validation plan names it.
                        "tags": [
                            f"app:{prof.app}",
                            f"schema:v{prof.version}",
                            f"kind:{kind}",
                        ],
                        "verify": f"contracts/{tid}.yaml",
                    }
                    tasks.append(task)
                    vplan = {
                        "schema_version": TASK_SCHEMA,
                        "id": tid,
                        "name": name,
                        "seeded": "public" if salt is None else "salt",
                        "app": prof.app,
                        "schema": prof.version,
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
    ap.add_argument(
        "--split",
        default="public",
        help="public (public seeds) or a private split name (needs TASKGEN_SALT_FILE)",
    )
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
        split=args.split,
    )
    print(f"{len(tasks)} tasks -> {args.out / 'tasks.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
