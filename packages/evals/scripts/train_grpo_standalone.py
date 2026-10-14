#!/usr/bin/env python
"""Thin CLI wrapper for the standalone GRPO trainer.

Usage:
    python scripts/train_grpo_standalone.py \\
        --task-dir example_tasks \\
        --server-url http://localhost:5001 \\
        --model Qwen/Qwen3.5-9B \\
        --num-steps 10 \\
        --output checkpoints/

Or equivalently via module:
    python -m openadapt_evals.training.standalone.trainer --task-dir ...

NOTE: We must avoid triggering openadapt_evals/__init__.py, which eagerly
imports agents/adapters/demo_library/benchmarks. The demo_library import
pulls in open_clip at module level, which can crash in minimal training
environments (e.g., numpy ABI mismatch). We work around this by inserting
a lightweight shim into sys.modules for the top-level package before any
sub-imports run.
"""

import importlib
import sys
import types
from pathlib import Path


def _ensure_lightweight_package(pkg_name: str, pkg_dir: Path) -> None:
    """Register a package in sys.modules without executing its __init__.py.

    This lets us ``import openadapt_evals.training.standalone.trainer``
    without the top-level ``openadapt_evals/__init__.py`` running its
    heavy re-exports (agents, adapters, demo_library, benchmarks).
    """
    if pkg_name in sys.modules:
        return
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(pkg_dir)]
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    pkg_root = root / "openadapt_evals"

    # Shim only the top-level package; sub-packages have lightweight __init__.py
    _ensure_lightweight_package("openadapt_evals", pkg_root)

    # Now the standalone trainer can be imported without pulling in the
    # full agents/adapters/benchmarks dependency tree.
    mod = importlib.import_module("openadapt_evals.training.standalone.trainer")
    mod.main()


if __name__ == "__main__":
    main()
