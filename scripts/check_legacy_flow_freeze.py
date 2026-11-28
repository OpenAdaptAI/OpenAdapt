#!/usr/bin/env python3
"""Enforce the freeze boundary on deleted/experimental stacks in packages/flow.

NOTE (2026-11-27): Wholesale Wave 2 deletions in docs/architecture/KERNEL_CONSOLIDATION.md
are cancelled. ``wrap/``, ``flight/``, ``readiness/``, ``hooks/``, ``txplan/``, and
``shadow*.py`` hold distinct capabilities and are active subsystems (not frozen and
not slated for deletion).

This script enforces:
1. No re-creation of deleted Wave 1 stacks (``form_tx/``).
2. No new Python modules under remaining frozen directories (``tx/``,
   ``agent_guard/``) listed in ``scripts/legacy_flow_freeze_manifest.json``.
3. No new top-level ``openadapt-flow`` CLI commands beyond the snapshot in
   ``packages/flow/tests/fixtures/cli_surface.json``.
4. No new references from ``openadapt/`` to frozen or deleted stacks beyond the
   existing allowlist.
5. Zero ``openadapt_flow`` imports inside ``packages/kernel/src/openadapt_kernel``.

Usage: python3 scripts/check_legacy_flow_freeze.py [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

MANIFEST_REL = Path("scripts/legacy_flow_freeze_manifest.json")
FLOW_PKG_REL = Path("packages/flow/openadapt_flow")
FLOW_CLI_SNAPSHOT_REL = Path("packages/flow/tests/fixtures/cli_surface.json")
LAUNCHER_REL = Path("openadapt")
KERNEL_SRC_REL = Path("packages/kernel/src/openadapt_kernel")


def load_manifest(root: Path) -> dict:
    manifest_path = root / MANIFEST_REL
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Freeze manifest missing: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def check_frozen_modules(root: Path, manifest: dict) -> list[str]:
    violations: list[str] = []
    flow_pkg = root / FLOW_PKG_REL
    if not flow_pkg.is_dir():
        return violations

    deleted_dirs: list[str] = manifest.get("deleted_directories", [])
    for stack in sorted(deleted_dirs):
        stack_dir = flow_pkg / stack
        if stack_dir.exists():
            violations.append(
                f"Deleted legacy stack '{stack}/' was re-created at "
                f"packages/flow/openadapt_flow/{stack}. "
                "Add new runtime capabilities in packages/kernel/ instead."
            )

    frozen_dirs: dict[str, list[str]] = manifest["frozen_directories"]
    for stack, allowed_list in sorted(frozen_dirs.items()):
        stack_dir = flow_pkg / stack
        if not stack_dir.exists():
            # Deleting an entire frozen stack in Phase 2 is allowed.
            continue
        allowed = set(allowed_list)
        actual = {
            str(p.relative_to(flow_pkg).as_posix())
            for p in stack_dir.rglob("*.py")
            if "__pycache__" not in p.parts
        }
        added = sorted(actual - allowed)
        for rel in added:
            violations.append(
                f"Frozen legacy stack '{stack}/' gained new module "
                f"packages/flow/openadapt_flow/{rel}. "
                "Add new runtime capabilities in packages/kernel/ instead."
            )

    raw_shadow = manifest.get("frozen_root_shadow_modules")
    if raw_shadow:
        allowed_shadow = set(raw_shadow)
        actual_shadow = {p.name for p in flow_pkg.glob("shadow*.py")}
        for name in sorted(actual_shadow - allowed_shadow):
            violations.append(
                f"Frozen legacy shadow stack gained new module "
                f"packages/flow/openadapt_flow/{name}. "
                "Add new shadow-to-autopilot work in packages/kernel/ instead."
            )

    return violations


def check_frozen_flow_cli_commands(root: Path, manifest: dict) -> list[str]:
    violations: list[str] = []
    snap_path = root / FLOW_CLI_SNAPSHOT_REL
    if not snap_path.is_file():
        return violations

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    tree = snap.get("tree", {})
    if not tree:
        return violations
    root_key = next(iter(tree.keys()))
    actual_cmds = set(tree[root_key].get("commands", {}).keys())
    allowed_cmds = set(manifest["frozen_flow_cli_commands"])
    added = sorted(actual_cmds - allowed_cmds)
    for cmd in added:
        violations.append(
            f"openadapt-flow CLI gained new top-level command '{cmd}' "
            f"(limit frozen at {len(allowed_cmds)} commands). "
            "Expose new runtime commands under 'openadapt kernel' instead."
        )
    return violations


def check_launcher_imports(root: Path, manifest: dict) -> list[str]:
    violations: list[str] = []
    launcher_dir = root / LAUNCHER_REL
    if not launcher_dir.is_dir():
        return violations

    frozen_stacks = (
        list(manifest.get("deleted_directories", []))
        + list(manifest["frozen_directories"].keys())
        + (["shadow"] if manifest.get("frozen_root_shadow_modules") else [])
    )
    allowed_map: dict[str, set[str]] = {
        k: set(v) for k, v in manifest["allowed_launcher_imports"].items()
    }

    for py_file in sorted(launcher_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = py_file.relative_to(root).as_posix()
        text = py_file.read_text(encoding="utf-8")
        if "openadapt_flow" not in text:
            continue
        allowed_for_file = allowed_map.get(rel, set())
        for stack in frozen_stacks:
            pattern = re.compile(
                rf"openadapt_flow\.{re.escape(stack)}\b"
                rf"|from\s+openadapt_flow\s+import\b[^\n]*\b{re.escape(stack)}\b"
            )
            if pattern.search(text) and stack not in allowed_for_file:
                violations.append(
                    f"{rel} references frozen legacy stack 'openadapt_flow.{stack}'. "
                    "Route new launcher integrations to openadapt_kernel instead."
                )

    return violations


def check_kernel_isolation(root: Path) -> list[str]:
    violations: list[str] = []
    kernel_src = root / KERNEL_SRC_REL
    if not kernel_src.is_dir():
        return violations

    for py_file in sorted(kernel_src.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "openadapt_flow" or alias.name.startswith(
                        "openadapt_flow."
                    ):
                        violations.append(
                            f"{rel} imports '{alias.name}'; "
                            "packages/kernel must remain free of openadapt_flow imports."
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "openadapt_flow" or node.module.startswith(
                    "openadapt_flow."
                ):
                    violations.append(
                        f"{rel} imports from '{node.module}'; "
                        "packages/kernel must remain free of openadapt_flow imports."
                    )

    return violations


def check_all(root: Path) -> list[str]:
    manifest = load_manifest(root)
    violations: list[str] = []
    violations.extend(check_frozen_modules(root, manifest))
    violations.extend(check_frozen_flow_cli_commands(root, manifest))
    violations.extend(check_launcher_imports(root, manifest))
    violations.extend(check_kernel_isolation(root))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root directory.",
    )
    args = parser.parse_args(argv)
    violations = check_all(args.root)
    if violations:
        print("Legacy flow freeze check failed:", file=sys.stderr)
        for item in violations:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("Legacy flow freeze check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
