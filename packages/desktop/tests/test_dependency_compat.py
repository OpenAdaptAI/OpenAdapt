"""Desktop must install next to the OpenAdapt launcher without moving Flow.

Desktop and Flow both depend on ``openadapt-types``. If Desktop pins one exact
release, installing Desktop next to ``openadapt`` either downgrades the shared
types package under a newer Flow or makes the resolver fall back to an older
Flow that accepts the pin. Desktop therefore declares the same bounded range
as the Flow release it bundles.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
TYPES = "openadapt-types"
FLOW = "openadapt-flow"


def _canonical(name: str) -> str:
    return name.replace("_", "-").lower()


def _desktop_types_requirement() -> Requirement:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    matches = [
        Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
        if _canonical(Requirement(raw).name) == TYPES
    ]
    assert len(matches) == 1, f"Desktop must declare {TYPES} exactly once"
    return matches[0]


def _locked_version(name: str) -> Version:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [package["version"] for package in lock["package"] if package["name"] == name]
    assert len(versions) == 1, f"uv.lock must resolve exactly one {name}"
    return Version(versions[0])


def _bundled_flow_types_specifier() -> SpecifierSet:
    try:
        requires = distribution(FLOW).requires or []
    except PackageNotFoundError:
        pytest.skip(
            "the bundled openadapt-flow runtime is not installed; the "
            "qualification-contract job runs this file with --extra build"
        )
    for raw in requires:
        requirement = Requirement(raw)
        if _canonical(requirement.name) == TYPES and requirement.marker is None:
            return requirement.specifier
    pytest.fail(f"the bundled {FLOW} no longer declares a base {TYPES} requirement")


def test_types_requirement_is_a_bounded_range_not_an_exact_pin() -> None:
    specifier = _desktop_types_requirement().specifier
    operators = {item.operator for item in specifier}
    assert not operators & {"==", "==="}, (
        f"{TYPES}{specifier} is an exact pin; installing Desktop next to the "
        "launcher would downgrade the shared types package or the Flow engine"
    )
    assert ">=" in operators, f"{TYPES}{specifier} needs a lower bound"
    assert "<" in operators, f"{TYPES}{specifier} needs an upper bound"
    assert _locked_version(TYPES) in specifier


def test_types_range_matches_the_bundled_flow_runtime() -> None:
    """Desktop accepts every shared-types release the bundled Flow accepts."""

    desktop = _desktop_types_requirement().specifier
    flow = _bundled_flow_types_specifier()
    assert desktop == flow, (
        f"Desktop declares {TYPES}{desktop} but the bundled {FLOW} declares "
        f"{TYPES}{flow}; release Desktop with the same range as its Flow pin"
    )
