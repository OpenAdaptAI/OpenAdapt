"""The launcher must not accept a Flow release with the wrong-case X11 probes.

openadapt-flow 1.33.x and 1.34.x look up Chromium's X11 libraries as
"xcomposite", "xdamage", "xfixes" and "xrandr". `ctypes.util.find_library`
matches case-sensitively, so on Linux those releases report installed
libraries as missing and refuse to download Chromium. 1.35.0 fixed the case.
With a floor below 1.35.0, `pip install --upgrade openadapt` keeps an
installed 1.34 and the false refusal stays. Earlier releases are below the
floor too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

from openadapt.cli import _SUPPORTED_FLOW_RANGE, _supported_flow_version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
# Launcher 1.17 requires Flow 2 (approval by default), so 1.x is below the floor.
BELOW_FLOOR = ["1.29.0", "1.33.0", "1.34.0", "1.34.9", "1.35.0", "1.35.1"]
ACCEPTED = "2.0.0"


def _flow_requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    raw = list(project["dependencies"])
    for values in project["optional-dependencies"].values():
        raw.extend(values)
    requirements = [Requirement(value) for value in raw]
    return [item for item in requirements if item.name == "openadapt-flow"]


def test_every_flow_requirement_is_checked() -> None:
    assert len(_flow_requirements()) >= 9


@pytest.mark.parametrize("version", BELOW_FLOOR)
def test_flow_requirements_exclude_releases_below_the_floor(version: str) -> None:
    for requirement in _flow_requirements():
        assert not requirement.specifier.contains(version), str(requirement)


def test_flow_requirements_accept_the_fixed_release() -> None:
    for requirement in _flow_requirements():
        assert requirement.specifier.contains(ACCEPTED), str(requirement)


@pytest.mark.parametrize("version", BELOW_FLOOR)
def test_doctor_and_deploy_treat_releases_below_the_floor_as_unsupported(
    version: str,
) -> None:
    assert not _supported_flow_version(version)
    assert not Requirement(f"openadapt-flow{_SUPPORTED_FLOW_RANGE}").specifier.contains(
        version
    )


def test_doctor_and_deploy_accept_the_fixed_release() -> None:
    assert _supported_flow_version(ACCEPTED)
    assert Requirement(f"openadapt-flow{_SUPPORTED_FLOW_RANGE}").specifier.contains(
        ACCEPTED
    )
    assert not _supported_flow_version("3.0.0")
