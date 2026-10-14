"""Independent, hash-bound store observer for the complex visual campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmark.complex_visual.protocol import canonical_json


@dataclass(frozen=True)
class Snapshot:
    sqlite_records: dict[str, dict[str, str]]
    csv_records: dict[str, dict[str, str]]
    actions: list[dict[str, str | int]]
    documents: dict[str, str]
    mail: dict[str, str]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def inventory_mail(maildir: Path) -> dict[str, str]:
    """Inventory every Maildir entry without following symbolic links."""
    entries: dict[str, str] = {}
    for path in sorted(maildir.rglob("*")):
        relative = path.relative_to(maildir).as_posix()
        if path.is_symlink():
            target = path.readlink().as_posix().encode("utf-8")
            entries[relative] = f"symlink:{sha256_bytes(target)}"
        elif path.is_dir():
            entries[f"{relative}/"] = "directory"
        elif path.is_file():
            entries[relative] = sha256_bytes(path.read_bytes())
        else:
            entries[relative] = f"special:{path.lstat().st_mode}"
    return entries


def observe(root: Path) -> Snapshot:
    """Open every store through new read-only handles and return exact state."""
    with sqlite3.connect(f"file:{root / 'effects.sqlite'}?mode=ro", uri=True) as conn:
        records = {
            row[0]: {"status": row[1], "route": row[2]}
            for row in conn.execute("select record_id, status, route from records")
        }
        actions = [
            {
                "action_id": row[0],
                "record_id": row[1],
                "route": row[2],
                "attachment_count": row[3],
                "document_sha256": row[4],
            }
            for row in conn.execute(
                "select action_id, record_id, route, attachment_count, document_sha256 from actions"
            )
        ]
    with (root / "worklist.csv").open(newline="", encoding="utf-8") as stream:
        csv_records = {
            row["record_id"]: {"status": row["status"], "route": row["route"]}
            for row in csv.DictReader(stream)
        }
    documents = {
        path.stem: sha256_bytes(path.read_bytes())
        for path in sorted((root / "documents").glob("*.txt"))
    }
    mail = inventory_mail(root / "maildir")
    return Snapshot(records, csv_records, actions, documents, mail)


def bind_observation(root: Path, truth_path: Path, phase: str) -> dict:
    """Bind an independent snapshot to the exact coordinator truth file."""
    snapshot = asdict(observe(root))
    truth_sha256 = sha256_bytes(truth_path.read_bytes())
    snapshot_sha256 = sha256_bytes(canonical_json(snapshot))
    binding = {
        "phase": phase,
        "snapshot_sha256": snapshot_sha256,
        "truth_sha256": truth_sha256,
    }
    return {
        **binding,
        "binding_sha256": sha256_bytes(canonical_json(binding)),
        "snapshot": snapshot,
    }


def verify_observation(payload: dict, truth_path: Path, phase: str) -> Snapshot:
    """Reject observer evidence whose snapshot, phase, or truth binding changed."""
    truth_sha256 = sha256_bytes(truth_path.read_bytes())
    snapshot_sha256 = sha256_bytes(canonical_json(payload["snapshot"]))
    binding = {
        "phase": phase,
        "snapshot_sha256": snapshot_sha256,
        "truth_sha256": truth_sha256,
    }
    if payload.get("phase") != phase:
        raise ValueError("observer phase binding is invalid")
    if payload.get("truth_sha256") != truth_sha256:
        raise ValueError("observer truth binding is invalid")
    if payload.get("snapshot_sha256") != snapshot_sha256:
        raise ValueError("observer snapshot hash is invalid")
    if payload.get("binding_sha256") != sha256_bytes(canonical_json(binding)):
        raise ValueError("observer evidence binding is invalid")
    return Snapshot(**payload["snapshot"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = bind_observation(args.root, args.truth, args.phase)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
