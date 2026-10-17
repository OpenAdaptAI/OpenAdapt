"""Write registry/index.json from registry/sources.json and the bundle files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openadapt_flow.registry import build_entry_files, validate_index  # noqa: E402


def build() -> dict:
    sources = json.loads((ROOT / "registry/sources.json").read_text())
    for item in sources["workflows"]:
        bundle = ROOT / "registry" / item["path"]
        item["files"] = build_entry_files(bundle) if bundle.is_dir() else {}
    sources["schema_version"] = 1
    validate_index(sources)
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
