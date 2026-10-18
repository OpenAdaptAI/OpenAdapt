"""Locked dependencies must stay at or above their security-fix releases.

The scheduled dependency audit only reports a vulnerable lock after the fact.
These floors stop a later lock refresh from quietly moving a package back to
a version with a known advisory.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

# Package name -> first release that fixes the advisories the audit reported.
PYTHON_FLOORS = {
    "anyio": "4.14.2",  # PYSEC-2026-4024, PYSEC-2026-4025
    "gitpython": "3.1.60",  # PYSEC-2026-3982, PYSEC-2026-3983, PYSEC-2026-3984
    "urllib3": "2.8.0",  # PYSEC-2026-4175, PYSEC-2026-4176, PYSEC-2026-4177
}
NPM_FLOORS = {
    "source-map-js": "1.2.2",  # GHSA-68fv-2mgg-jv7q
    "undici": "8.10.2",  # GHSA-w293-vg96-wgc3 and related undici advisories
}


def _uv_locked_versions(name: str) -> list[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return [package["version"] for package in lock["package"] if package["name"] == name]


def _npm_locked_versions(name: str) -> list[str]:
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    return [
        package["version"]
        for path, package in lock["packages"].items()
        if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")
    ]


@pytest.mark.parametrize(("name", "floor"), sorted(PYTHON_FLOORS.items()))
def test_uv_lock_keeps_python_security_floor(name: str, floor: str) -> None:
    versions = _uv_locked_versions(name)
    assert versions, f"{name} is no longer in uv.lock; remove its floor"
    for version in versions:
        assert Version(version) >= Version(floor), f"{name} {version} is below {floor}"


@pytest.mark.parametrize(("name", "floor"), sorted(NPM_FLOORS.items()))
def test_package_lock_keeps_npm_security_floor(name: str, floor: str) -> None:
    versions = _npm_locked_versions(name)
    assert versions, f"{name} is no longer in package-lock.json; remove its floor"
    for version in versions:
        assert Version(version) >= Version(floor), f"{name} {version} is below {floor}"
