from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_platform_versions.py"
SPEC = importlib.util.spec_from_file_location("release_platform_versions", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _manifest() -> dict:
    document = {
        "manifest_kind": "openadapt-platform-release-manifest",
        "schema_version": "2.0.0",
        "components": {
            role: {"package": package, "version": f"1.0.{index}"}
            for index, (role, package) in enumerate(
                MODULE.COMPONENT_PACKAGES.items(), start=1
            )
        },
    }
    document["components"]["launcher"]["version"] = "2.0.0"
    document["release_selection"] = {
        "mode": "explicit-published",
        "component_versions": {
            role: component["version"]
            for role, component in document["components"].items()
        },
    }
    document["compatibility_status"] = {
        "status": "dependency-compatible",
        "basis": "published-metadata-and-desktop-lock",
        "failures": [],
    }
    document["runtime_units"] = {
        "launcher_environment": {
            "selected": {
                "openadapt": "2.0.0",
                "openadapt-flow": "1.0.2",
            }
        },
        "customer_runner": {
            "selected": {
                "openadapt-flow": "1.0.2",
                "openadapt-types": "1.0.5",
            }
        },
        "desktop_sidecar": {
            "resolved": {
                "openadapt-flow": "1.0.2",
                "openadapt-capture": "1.0.3",
                "openadapt-privacy": "1.0.4",
                "openadapt-types": "1.0.5",
            }
        },
        "agent_bridge": {
            "selected": {
                "openadapt-agent": "1.0.7",
                "openadapt-flow": "1.0.2",
            }
        },
    }
    return document


def test_release_uses_new_launcher_and_exact_reviewed_package_defaults() -> None:
    versions = MODULE.release_component_versions(_manifest(), "2.0.0")

    assert versions == {
        "launcher": "2.0.0",
        "flow": "1.0.2",
        "capture": "1.0.3",
        "privacy": "1.0.4",
        "types": "1.0.5",
        "desktop": "1.0.6",
        "agent": "1.0.7",
    }


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "package", "version", "dynamic", "selection"]
)
def test_release_refuses_an_open_or_invalid_package_selection(mutation: str) -> None:
    document = _manifest()
    if mutation == "missing":
        document["components"].pop("agent")
    elif mutation == "extra":
        document["components"]["unknown"] = {
            "package": "unknown",
            "version": "1.0.0",
        }
    elif mutation == "package":
        document["components"]["flow"]["package"] = "other-flow"
    elif mutation == "version":
        document["components"]["flow"]["version"] = "latest"
    elif mutation == "dynamic":
        document["release_selection"] = {
            "mode": "latest-published",
            "component_versions": {},
        }
    else:
        document["release_selection"]["component_versions"]["flow"] = "1.0.99"

    with pytest.raises(ValueError):
        MODULE.release_component_versions(document, "2.0.0")


def test_release_refuses_a_nonstable_launcher_version() -> None:
    with pytest.raises(ValueError, match="stable X.Y.Z"):
        MODULE.release_component_versions(_manifest(), "2.0.0rc1")


def test_release_derives_the_candidate_over_the_exact_published_selection() -> None:
    document = _manifest()
    document["components"]["launcher"]["version"] = "1.16.0"
    document["release_selection"]["component_versions"]["launcher"] = "1.16.0"
    versions = MODULE.release_component_versions(document, "2.0.1")
    assert versions["launcher"] == "2.0.1"

    with pytest.raises(ValueError, match="older than"):
        MODULE.release_component_versions(_manifest(), "1.16.0")


def test_release_refuses_an_incompatible_public_package_set() -> None:
    document = _manifest()
    document["compatibility_status"] = {
        "status": "dependency-incompatible",
        "basis": "published-metadata-and-desktop-lock",
        "failures": [{"source_role": "desktop", "target_role": "flow"}],
    }
    with pytest.raises(ValueError, match="dependency-incompatible"):
        MODULE.release_component_versions(document, "2.0.0")


def test_release_refuses_a_runtime_unit_with_a_different_default() -> None:
    document = _manifest()
    document["runtime_units"]["desktop_sidecar"]["resolved"]["openadapt-flow"] = "1.0.1"
    with pytest.raises(ValueError, match="not the selected default"):
        MODULE.release_component_versions(document, "2.0.0")
