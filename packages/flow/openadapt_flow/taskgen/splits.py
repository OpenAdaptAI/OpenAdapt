"""Assign validated tasks to splits and write the manifests.

    python -m openadapt_flow.taskgen.splits --rules splits.yaml --out release/ packs/*

Only tasks the validator kept go into a split. The rules (a YAML file, kept
with the private packs) name each split by what it holds out:

.. code-block:: yaml

    public: {app: mockmed, schema: 1, exclude_families: [concurrent_edit, interruption], size: 100, seed: 1}
    heldout:
      app: {app: mockloan}
      mechanism: {families: [concurrent_edit, interruption]}
      schema: {schema: 2}
    rest: private_train

A task lands in the first held-out split it matches, else in ``public`` when
it matches the public rule and the stratified sample picks it, else in
``rest``. Every split gets ``<split>.jsonl`` with its contracts beside it, and
``MANIFEST.json`` lists every split's task count and SHA-256, and every
public task's own digest. The public copy of the manifest names the private
splits by digest only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

MANIFEST_SCHEMA = "openadapt.taskgen-manifest/v1"


def canonical(task: Mapping[str, Any]) -> bytes:
    return json.dumps(task, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tags(task: Mapping[str, Any]) -> dict[str, str]:
    return dict(t.split(":", 1) for t in task["tags"] if ":" in t)


def matches(task: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    tags = _tags(task)
    if "app" in rule and tags["app"] != rule["app"]:
        return False
    if "schema" in rule and tags["schema"] != f"v{rule['schema']}":
        return False
    if "families" in rule and tags["family"] not in rule["families"]:
        return False
    if tags["family"] in rule.get("exclude_families", ()):
        return False
    return True


def assign(
    tasks: list[dict[str, Any]], rules: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidates: list[dict[str, Any]] = []
    for t in tasks:
        for name, rule in (rules.get("heldout") or {}).items():
            if matches(t, rule):
                out[f"heldout_{name}"].append(t)
                break
        else:
            if matches(t, rules["public"]):
                candidates.append(t)
            else:
                out[rules.get("rest", "private_train")].append(t)
    # Stratified: round-robin over kind x family cells, seeded.
    pub = rules["public"]
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in candidates:
        tags = _tags(t)
        cells[(tags["kind"], tags["family"])].append(t)
    rng = random.Random(int(pub.get("seed", 1)))
    for c in cells.values():
        rng.shuffle(c)
    picked: list[dict[str, Any]] = []
    order = sorted(cells)
    while len(picked) < int(pub["size"]) and any(cells.values()):
        for key in order:
            if cells[key] and len(picked) < int(pub["size"]):
                picked.append(cells[key].pop())
    ids = {t["id"] for t in picked}
    out["public"] = sorted(picked, key=lambda t: t["id"])
    out[rules.get("rest", "private_train")] += [
        t for t in candidates if t["id"] not in ids
    ]
    return {k: sorted(v, key=lambda t: t["id"]) for k, v in out.items()}


def load_validated(pack: Path) -> list[tuple[dict[str, Any], Path]]:
    validity = json.loads((pack / "validity.json").read_text())
    keep = set(validity["valid_ids"])
    out = []
    for line in (pack / "tasks.jsonl").read_text().splitlines():
        if line.strip():
            t = json.loads(line)
            if t["id"] in keep:
                out.append((t, pack))
    return out


def write_release(
    packs: list[Path], rules: Mapping[str, Any], out: Path
) -> dict[str, Any]:
    loaded = [x for p in packs for x in load_validated(p)]
    source = {t["id"]: p for t, p in loaded}
    splits = assign([t for t, _ in loaded], rules)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "splits": {},
        "rules": dict(rules),
    }
    for name, tasks in sorted(splits.items()):
        sdir = out / name
        (sdir / "contracts").mkdir(parents=True, exist_ok=True)
        lines = []
        digests = {}
        for t in tasks:
            t = {**t, "split": name}
            src = source[t["id"]] / t["verify"]
            shutil.copyfile(src, sdir / t["verify"])
            vplan = source[t["id"]] / "validation" / f"{t['id']}.json"
            (sdir / "validation").mkdir(exist_ok=True)
            shutil.copyfile(vplan, sdir / "validation" / vplan.name)
            line = canonical(t)
            lines.append(line)
            digests[t["id"]] = {"task": sha(line), "contract": sha(src.read_bytes())}
        body = b"\n".join(lines) + b"\n"
        (sdir / "tasks.jsonl").write_bytes(body)
        (sdir / "digests.json").write_text(
            json.dumps(digests, indent=1, sort_keys=True) + "\n"
        )
        manifest["splits"][name] = {
            "tasks": len(tasks),
            "tasks_sha256": sha(body),
            "digests_sha256": sha((sdir / "digests.json").read_bytes()),
        }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    )
    public = {
        "schema_version": MANIFEST_SCHEMA,
        "splits": manifest["splits"],
        "public_tasks": json.loads((out / "public" / "digests.json").read_text())
        if "public" in splits
        else {},
        "about": "Private splits are named by count and digest only; their tasks are kept privately.",
    }
    (out / "PUBLIC_MANIFEST.json").write_text(
        json.dumps(public, indent=1, sort_keys=True) + "\n"
    )
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("packs", nargs="+", type=Path)
    ap.add_argument("--rules", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    rules = yaml.safe_load(args.rules.read_text())
    m = write_release(args.packs, rules, args.out)
    print(json.dumps({k: v["tasks"] for k, v in m["splits"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
