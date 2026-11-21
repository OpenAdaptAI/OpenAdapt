"""Write MANIFEST.json: the sha256 of every file in this directory.

Run last, after answer_key.json and REFERENCE_RUNS.json are final:
``python -m benchmark.form_tx_heldout.make_manifest``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build() -> dict:
    files = {}
    for p in sorted(HERE.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.name == "MANIFEST.json" or p.suffix == ".pyc":
            continue
        files[p.relative_to(HERE).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = {
        "description": "sha256 of every file in benchmark/form_tx_heldout except this manifest",
        "answer_key_sha256": files["answer_key.json"],
        "files": files,
    }
    (HERE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    m = build()
    print(m["answer_key_sha256"], len(m["files"]), "files")
