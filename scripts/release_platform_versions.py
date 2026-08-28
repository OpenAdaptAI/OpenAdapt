#!/usr/bin/env python3
"""Select the exact reviewed package versions for one launcher release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

MANIFEST_KIND = "openadapt-platform-release-manifest"
COMPONENT_PACKAGES = {
    "launcher": "openadapt",
    "flow": "openadapt-flow",
    "capture": "openadapt-capture",
    "privacy": "openadapt-privacy",
    "types": "openadapt-types",
    "desktop": "openadapt-desktop",
    "agent": "openadapt-agent",
}
STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def release_component_versions(
    document: dict[str, Any], launcher_version: str
) -> dict[str, str]:
    """Return the closed seven-package selection for the release transaction."""

    if document.get("manifest_kind") != MANIFEST_KIND:
        raise ValueError("platform manifest kind is invalid")
    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.startswith("2."):
        raise ValueError("platform manifest schema is not v2")
    if STABLE_VERSION.fullmatch(launcher_version) is None:
        raise ValueError("launcher version must be an exact stable X.Y.Z value")

    components = _object(document.get("components"), "platform components")
    if set(components) != set(COMPONENT_PACKAGES):
        raise ValueError("platform manifest must contain the exact public package set")

    selection = _object(document.get("release_selection"), "release selection")
    if set(selection) != {"mode", "component_versions"}:
        raise ValueError(
            "release selection must contain only mode and component_versions"
        )
    if selection.get("mode") != "explicit-published":
        raise ValueError("release selection must use explicit-published mode")
    selected_versions = _object(
        selection.get("component_versions"), "selected component versions"
    )
    if set(selected_versions) != set(COMPONENT_PACKAGES):
        raise ValueError("release selection must pin the exact public package set")

    compatibility = _object(
        document.get("compatibility_status"), "platform compatibility status"
    )
    if set(compatibility) != {"status", "basis", "failures"}:
        raise ValueError("platform compatibility status keys are invalid")
    if (
        compatibility["status"] != "dependency-compatible"
        or not isinstance(compatibility["basis"], str)
        or not compatibility["basis"]
        or compatibility["failures"] != []
    ):
        raise ValueError("the exact public package set is dependency-incompatible")

    versions: dict[str, str] = {}
    for role, package in COMPONENT_PACKAGES.items():
        component = _object(components.get(role), f"{role} component")
        if component.get("package") != package:
            raise ValueError(f"{role} component package is invalid")
        version = component.get("version")
        if not isinstance(version, str) or STABLE_VERSION.fullmatch(version) is None:
            raise ValueError(f"{role} version must be an exact stable X.Y.Z value")
        if selected_versions.get(role) != version:
            raise ValueError(f"{role} release selection does not match its component")
        versions[role] = version

    package_roles = {package: role for role, package in COMPONENT_PACKAGES.items()}
    runtime_units = _object(document.get("runtime_units"), "platform runtime units")
    for unit_name, raw_unit in runtime_units.items():
        unit = _object(raw_unit, f"{unit_name} runtime unit")
        for closure_name in ("selected", "resolved"):
            closure = unit.get(closure_name)
            if closure is None:
                continue
            selected = _object(closure, f"{unit_name} {closure_name} closure")
            for package, version in selected.items():
                role = package_roles.get(package)
                if role is None or role == "launcher":
                    continue
                if version != versions[role]:
                    raise ValueError(
                        f"{unit_name} resolves {package}=={version}, not the "
                        f"selected default {versions[role]}"
                    )
    published_launcher = tuple(int(part) for part in versions["launcher"].split("."))
    candidate_launcher = tuple(int(part) for part in launcher_version.split("."))
    if candidate_launcher < published_launcher:
        raise ValueError("launcher candidate is older than the published selection")
    # The committed manifest can only bind published files. The release admission
    # binds this candidate launcher and its local artifacts before publication.
    versions["launcher"] = launcher_version
    return versions


def load_release_component_versions(
    manifest_path: Path, launcher_version: str
) -> dict[str, str]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("platform manifest path is missing or invalid")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the platform manifest: {exc}") from exc
    return release_component_versions(
        _object(document, "platform manifest"), launcher_version
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--launcher-version", required=True)
    args = parser.parse_args()
    try:
        versions = load_release_component_versions(args.manifest, args.launcher_version)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")

    for role in COMPONENT_PACKAGES:
        print(f"{role}={versions[role]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
