"""Write registry/index.json from registry/sources.json and the bundle files.

    python scripts/build_registry_index.py            # write the committed index
    python scripts/build_registry_index.py --check    # fail when it is stale
    python scripts/build_registry_index.py --archives DIR --asset-base URL --out PATH
    python scripts/build_registry_index.py --archives DIR --asset-base URL --out PATH --stamp
    python scripts/build_registry_index.py --check --release
    python scripts/build_registry_index.py ... --stamp --restamp-of registry/index.json

``--archives`` writes each bundle as a reproducible ``.tar.gz`` into DIR and
points its entry at ``<URL><name>`` (the registry release's assets).
Each archive carries a NOTICE naming the app's license. ``--stamp`` writes
``issued_at``, ``expires_at`` and ``serial`` into the index. The registry
release workflow does that, then attests the index and the archives with
GitHub's keyless artifact attestations (``actions/attest``); there is no
signing key.

``--release`` (implied by ``--stamp``) refuses an entry with status
``planned``: a release publishes only entries that have a bundle.
``--restamp-of PATH`` refuses a build that differs from the attested index at
PATH in anything but ``issued_at``, ``expires_at``, ``serial`` and each
archive's URL (which names the release tag). A ``registry-restamp-*`` release
uses it, so a re-stamp renews the expiry of what was already published and
nothing else.
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


def release_problems(index: dict) -> list[str]:
    """Why ``index`` may not be released, or an empty list."""

    return [
        f"{item['id']}: status 'planned' has no bundle and is never released"
        for item in index["workflows"]
        if item.get("status") == "planned"
    ]


#: What a re-stamp may change. An archive's URL names the release tag; its
#: sha256 may not change.
RESTAMP_FIELDS = INDEX_FRESHNESS_FIELDS


def _restamp_view(index: dict) -> dict:
    view = {k: v for k, v in index.items() if k not in RESTAMP_FIELDS}
    workflows = []
    for item in view.get("workflows", []):
        item = dict(item)
        archive = item.get("archive")
        if isinstance(archive, dict):
            item["archive"] = {k: v for k, v in archive.items() if k != "url"}
        workflows.append(item)
    view["workflows"] = workflows
    return view


def restamp_problem(previous: dict, index: dict) -> Optional[str]:
    """Why ``index`` is not a re-stamp of the attested ``previous``, or None."""

    if not isinstance(previous.get("serial"), int):
        return (
            "the committed index carries no serial, so no attested release "
            "exists to re-stamp; release it with a plain suffix first"
        )
    if any("archive" not in item for item in previous.get("workflows", [])):
        return "the committed index has an entry with no archive; it is not attested"
    if _restamp_view(previous) != _restamp_view(index):
        return (
            "the registry changed since the attested index; a re-stamp may "
            "change only issued_at, expires_at, serial and archive URLs. "
            "Release it with a plain suffix instead"
        )
    return None


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
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--asset-base")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--restamp-of", type=Path)
    args = parser.parse_args()
    if args.restamp_of and not args.stamp:
        parser.error("--restamp-of needs --stamp")
    if args.archives:
        args.archives.mkdir(parents=True, exist_ok=True)
    out = args.out or ROOT / "registry/index.json"
    # Read before the output is written: --out may be the same file.
    previous = json.loads(args.restamp_of.read_text()) if args.restamp_of else None
    index = build(args.archives, args.asset_base)
    if args.release or args.stamp:
        problems = release_problems(index)
        if problems:
            print("the registry may not be released:\n  " + "\n  ".join(problems))
            return 1
    if previous is not None:
        problem = restamp_problem(previous, index)
        if problem:
            print(f"not a re-stamp: {problem}")
            return 1
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
