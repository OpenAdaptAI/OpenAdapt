"""System / dashboard state for the control panel — the P0 read-only page.

This is the panel's equivalent of the meta-package's ``doctor``/``version``
commands and follows the same rule: **never import a sibling package here.**
Sibling packages can be expensive or have import-time side effects
(``openadapt-capture`` takes a screenshot at import, which crashes headless).
Installed-state and versions are inspected with ``importlib.util.find_spec``
and ``importlib.metadata.version`` — metadata only, no package code executed.
"""

from __future__ import annotations

import os
import platform
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from importlib.util import find_spec
from typing import Optional

# Distribution names (as installed) — mirrors openadapt/cli.py's doctor/version.
CORE_PACKAGES = [
    "openadapt-capture",
    "openadapt-ml",
    "openadapt-evals",
    "openadapt-viewer",
]
OPTIONAL_PACKAGES = [
    "openadapt-grounding",
    "openadapt-retrieval",
    "openadapt-privacy",
]

API_KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


def _package_info(dist_name: str) -> dict:
    """Installed-state + version for one distribution, without importing it."""
    module_name = dist_name.replace("-", "_")
    installed = find_spec(module_name) is not None
    version: Optional[str] = None
    try:
        version = dist_version(dist_name)
    except PackageNotFoundError:
        version = None
    return {"name": dist_name, "installed": installed, "version": version}


def _gpu_info() -> dict:
    """GPU availability — mirrors doctor. torch is not a sibling package."""
    try:
        import torch
    except ImportError:
        return {"available": False, "kind": None, "name": None, "torch": False}

    if torch.cuda.is_available():
        return {
            "available": True,
            "kind": "cuda",
            "name": torch.cuda.get_device_name(0),
            "torch": True,
        }
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return {
            "available": True,
            "kind": "mps",
            "name": "Apple Silicon (MPS)",
            "torch": True,
        }
    return {"available": False, "kind": "cpu", "name": None, "torch": True}


def system_report() -> dict:
    """A JSON-serializable snapshot of system + ecosystem state.

    Backs ``GET /api/system`` and the P0 Dashboard page. Cheap and safe to call
    on every page load: it never imports a sibling package.
    """
    return {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "packages": {
            "openadapt": _package_info("openadapt"),
            "core": [_package_info(name) for name in CORE_PACKAGES],
            "optional": [_package_info(name) for name in OPTIONAL_PACKAGES],
        },
        "gpu": _gpu_info(),
        "api_keys": {key: bool(os.environ.get(key)) for key in API_KEYS},
    }
