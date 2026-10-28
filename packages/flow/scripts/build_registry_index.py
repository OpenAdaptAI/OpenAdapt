"""Write registry/index.json from registry/sources.json and the bundle files.

    python scripts/build_registry_index.py            # write the committed index
    python scripts/build_registry_index.py --check    # fail when it is stale
    python scripts/build_registry_index.py --archives DIR --asset-base URL --out PATH
    python scripts/build_registry_index.py --sign PATH

``--archives`` writes each bundle as a reproducible ``.tar.gz`` into DIR and
points its entry at ``<URL><name>`` (the registry release's assets).
Each archive carries a NOTICE naming the app's license. ``--sign`` signs
every entry of the index at PATH, stamps ``issued_at``, ``expires_at`` and
``serial``, and signs the index as a whole, in place, with the seed in the
file named by ``OPENADAPT_REGISTRY_SIGNING_SEED`` and the key id in
``OPENADAPT_REGISTRY_KEY_ID``. Only the registry release workflow does that,
in its protected environment; the seed never lives in this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT.parent / "types"))

from openadapt_flow.registry import (  # noqa: E402
    INDEX_FRESHNESS_FIELDS,
    build_archive,
    build_entry_files,
    license_notice,
    sign_index,
    validate_index,
)


def build(archives: Optional[Path] = None, asset_base: Optional[str] = None) -> dict:
    sources = json.loads((ROOT / "registry/sources.json").read_text())
    for item in sources["workflows"]:
        bundle = ROOT / "registry" / item["path"]
        item["files"] = build_entry_files(bundle) if bundle.is_dir() else {}
        if archives is not None and asset_base and bundle.is_dir():
            name = item["id"].replace("/", "-") + (
                f"-{item['version']}" if item.get("version") else ""
            )
            asset = archives / f"{name}.tar.gz"
            item["archive"] = {
                "url": asset_base.rstrip("/") + "/" + asset.name,
                "sha256": build_archive(bundle, asset, notice=license_notice(item)),
            }
    sources["schema_version"] = 1
    validate_index(sources)
    return sources


def sign(path: Path) -> None:
    seed = Path(os.environ["OPENADAPT_REGISTRY_SIGNING_SEED"]).read_bytes()
    key_id = os.environ["OPENADAPT_REGISTRY_KEY_ID"]
    index = json.loads(path.read_text())
    sign_index(index, seed, key_id)
    validate_index(index)
    path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--asset-base")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sign", type=Path)
    args = parser.parse_args()
    if args.sign:
        sign(args.sign)
        return 0
    if args.archives:
        args.archives.mkdir(parents=True, exist_ok=True)
    out = args.out or ROOT / "registry/index.json"
    index = build(args.archives, args.asset_base)
    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if args.check:
        committed = json.loads(out.read_text())
        # The release workflow adds the signatures, the freshness fields and
        # each entry's archive.
        for field in (*INDEX_FRESHNESS_FIELDS, "signature"):
            committed.pop(field, None)
        for item in committed["workflows"]:
            item.pop("signature", None)
            item.pop("archive", None)
        if committed != index:
            print("registry/index.json is stale; run scripts/build_registry_index.py")
            return 1
        return 0
    out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
