"""Write MANIFEST.json: the sha256 of every file in this directory.

Covers the git-tracked files (``git add`` new files first). Run last, after
answer_key.json and REFERENCE_RUNS.json are final:
``python -m benchmark.form_tx_heldout2.make_manifest``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "."], cwd=HERE, check=True, capture_output=True
    ).stdout.decode()
    return sorted(p for p in out.split("\0") if p and p != "MANIFEST.json")


def build() -> dict:
    files = {rel: hashlib.sha256((HERE / rel).read_bytes()).hexdigest() for rel in tracked_files()}
    manifest = {
        "description": "sha256 of every git-tracked file in benchmark/form_tx_heldout2 except this manifest",
        "answer_key_sha256": files["answer_key.json"],
        "files": files,
    }
    (HERE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    m = build()
    print(m["answer_key_sha256"], len(m["files"]), "files")
