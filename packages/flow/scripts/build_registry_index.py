"""Write registry/index.json from registry/sources.json and the bundle files.

    python scripts/build_registry_index.py            # write the committed index
    python scripts/build_registry_index.py --check    # fail when it is stale
    python scripts/build_registry_index.py --archives DIR --asset-base URL --out PATH
    python scripts/build_registry_index.py --archives DIR --asset-base URL --out PATH --stamp

``--archives`` writes each bundle as a reproducible ``.tar.gz`` into DIR and
points its entry at ``<URL><name>`` (the registry release's assets).
Each archive carries a NOTICE naming the app's license. ``--stamp`` writes
``issued_at``, ``expires_at`` and ``serial`` into the index. The registry
release workflow does that, then attests the index and the archives with
GitHub's keyless artifact attestations (``actions/attest``); there is no
signing key.
"""

from __future__ import annotations

import argparse
import json
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
    stamp_index,
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
    for item in sources["workflows"]:
        check_shipped_contract(item)
    return sources


def check_shipped_contract(item: dict) -> None:
    """Refuse a bundle whose shipped record check could run code or write.

    The same rules ``skill qualify`` applies on the user's machine
    (``skills.shipped_contract_problems``): every address, credential name,
    path and command is a declared variable, and a SQL read-back is one
    parameterized, read-only SELECT.
    """

    if not item.get("contract"):
        return
    import yaml

    from openadapt_flow.skills import shipped_contract_problems

    path = ROOT / "registry" / item["path"] / item["contract"]
    kinds = {v["name"]: v["kind"] for v in item.get("contract_vars") or []}
    problems = shipped_contract_problems(yaml.safe_load(path.read_text()), kinds)
    if problems:
        raise SystemExit(
            f"{item['id']}: its shipped record check may not ship:\n  "
            + "\n  ".join(problems)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--asset-base")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--stamp", action="store_true")
    args = parser.parse_args()
    if args.archives:
        args.archives.mkdir(parents=True, exist_ok=True)
    out = args.out or ROOT / "registry/index.json"
    index = build(args.archives, args.asset_base)
    if args.stamp:
        stamp_index(index)
    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if args.check:
        committed = json.loads(out.read_text())
        # The release workflow adds the freshness fields and each entry's
        # archive.
        for field in INDEX_FRESHNESS_FIELDS:
            committed.pop(field, None)
        for item in committed["workflows"]:
            item.pop("archive", None)
        if committed != index:
            print("registry/index.json is stale; run scripts/build_registry_index.py")
            return 1
        return 0
    out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
