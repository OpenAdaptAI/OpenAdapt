#!/usr/bin/env python3
"""Enforce the freeze boundary on overlapping legacy stacks in packages/flow.

New commit-boundary verification, macro staging, stepwise live-DOM healing, and
shadow-to-autopilot work belongs in ``packages/kernel`` (``openadapt-kernel``).
The overlapping legacy guard/transaction stacks in ``packages/flow`` listed in
``scripts/legacy_flow_freeze_manifest.json`` remain in place for compatibility
with ``openadapt-flow 4.0.0`` and existing callers, and may shrink or receive
bug/security fixes, but must not grow new surfaces:

1. No new Python modules under frozen directories (``tx/``, ``form_tx/``,
   ``txplan/``, ``agent_guard/``, ``flight/``, ``wrap/``, ``hooks/``,
   ``readiness/``) or new root ``shadow*.py`` files in ``openadapt_flow/``.
2. No new top-level ``openadapt-flow`` CLI commands beyond the frozen set in
   ``packages/flow/tests/fixtures/cli_surface.json``.
3. No new references from ``openadapt/`` to frozen legacy flow stacks beyond
   the existing allowlist.
4. Zero ``openadapt_flow`` imports inside ``packages/kernel/src/openadapt_kernel``.

Deleting files or commands is always allowed.

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

    allowed_shadow = set(manifest["frozen_root_shadow_modules"])
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

    frozen_stacks = list(manifest["frozen_directories"].keys()) + ["shadow"]
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
