"""Write registry/index.json from registry/sources.json and the bundle files.

With ``OPENADAPT_REGISTRY_SIGNING_SEED`` (the path of a file holding the
registry key's 32-byte Ed25519 seed, kept off this repository) and
``OPENADAPT_REGISTRY_KEY_ID``, every entry is signed. Without them the index is
unsigned, and ``pull`` takes it only with ``--allow-unsigned``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openadapt_flow.registry import (  # noqa: E402
    build_entry_files,
    sign_entry,
    validate_index,
)


def build() -> dict:
    sources = json.loads((ROOT / "registry/sources.json").read_text())
    for item in sources["workflows"]:
        bundle = ROOT / "registry" / item["path"]
        item["files"] = build_entry_files(bundle) if bundle.is_dir() else {}
    sources["schema_version"] = 1
    validate_index(sources)
    seed_path = os.environ.get("OPENADAPT_REGISTRY_SIGNING_SEED")
    if seed_path:
        seed = Path(seed_path).read_bytes()
        key_id = os.environ["OPENADAPT_REGISTRY_KEY_ID"]
        for item in sources["workflows"]:
            if item["status"] != "planned":
                item["signature"] = sign_entry(item, seed, key_id)
    return sources


if __name__ == "__main__":
    out = ROOT / "registry/index.json"
    text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if out.read_text() != text:
            sys.exit(
                "registry/index.json is stale; run scripts/build_registry_index.py"
            )
    else:
        out.write_text(text)
