#!/usr/bin/env python3
"""Merge a complete set of Flow unit-shard JUnit artifacts without losing cases."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
FLOW = ROOT / "packages" / "flow"


def _validator() -> ModuleType:
    name = "_flow_claims_validator"
    spec = importlib.util.spec_from_file_location(
        name, FLOW / "scripts" / "validate_claims.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the claims validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def merge_reports(
    artifacts: Path, shards: int, out: Path, *, repo_root: Path = FLOW
) -> None:
    """Require every named shard report, then preserve all its test suites."""
    if shards < 1:
        raise ValueError("the shard count must be positive")
    expected = {
        f"flow-unit-junit-{index}-of-{shards}": (
            f"flow-unit-shard-{index}-of-{shards}.xml"
        )
        for index in range(1, shards + 1)
    }
    if not artifacts.is_dir():
        raise ValueError("the unit JUnit artifact directory is missing")
    actual = {path.name for path in artifacts.iterdir()}
    if actual != set(expected):
        raise ValueError(
            f"unit JUnit artifact set differs: missing={sorted(set(expected) - actual)}, "
            f"unexpected={sorted(actual - set(expected))}"
        )
    if out.exists():
        raise ValueError("the merged JUnit output already exists")
    validator = _validator()
    merged = ET.Element("testsuites")
    for name, filename in sorted(expected.items()):
        directory = artifacts / name
        if not directory.is_dir() or {p.name for p in directory.iterdir()} != {
            filename
        }:
            raise ValueError(f"unit JUnit artifact {name} needs exactly {filename}")
        report = directory / filename
        # Reuse the existing fail-closed parser: every artifact must contain
        # repository test cases, not merely well-formed or nonempty XML.
        validator.parse_junit(report, repo_root=repo_root)
        document = ET.parse(report).getroot()
        if document.tag == "testsuite":
            merged.append(document)
        elif document.tag == "testsuites":
            merged.extend(document)
        else:
            raise ValueError(f"unit JUnit artifact {name} has an invalid root")
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(out, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        merge_reports(args.artifacts_dir, args.shards, args.out)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"Flow claims evidence FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Merged all {args.shards} Flow unit JUnit reports into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
