"""Installing the library must not change the user's install tools."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
INSTALL_TOOLS = {"pip", "setuptools", "wheel", "uv"}


def test_runtime_and_user_extras_do_not_require_install_tools() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = {"dependencies": project.get("dependencies", [])}
    for extra, requirements in project.get("optional-dependencies", {}).items():
        if extra != "dev":
            groups[f"[{extra}]"] = requirements
    offenders = [
        f"{group}: {spec}"
        for group, requirements in groups.items()
        for spec in requirements
        if canonicalize_name(Requirement(spec).name) in INSTALL_TOOLS
    ]
    assert offenders == []
