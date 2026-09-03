#!/usr/bin/env python3
"""Generate the authoritative OpenAdapt platform release manifest.

This script produces ``platform-manifest.json`` at the repository root: one
machine-readable statement of which component versions constitute the current
OpenAdapt platform release, where their published artifacts live, and which
qualification evidence backs them.

Design rules (see docs/platform-manifest.md):

* Versions, dependency ranges, artifact URLs, and sha256 digests are read from
  the REAL published sources (the PyPI JSON API). The frozen Desktop sidecar
  closure comes from the exact native release tag's locked environment.
  Nothing is invented or hand-typed. If a source is unreachable or disagrees
  with this repository's ``pyproject.toml``, the script fails loudly instead
  of guessing.
* Substrate/driver availability and the release channel are read from the
  canonical public status document, https://openadapt.ai/status.json, which is
  maintained in openadapt-web and already drives status-aware public surfaces.
* The ``signature`` block is intentionally null. OpenAdapt does not yet run
  manifest-signing infrastructure and this manifest refuses to pretend
  otherwise. The signing plan is documented in docs/platform-manifest.md.

Usage:
    python scripts/generate_platform_manifest.py [--output PATH]
        [--allow-unreleased-launcher]

``--allow-unreleased-launcher`` permits the in-repo launcher version to be
ahead of the latest PyPI release (the normal state mid-release-train). The
manifest always records the PUBLISHED version, never the unreleased one.
"""

from __future__ import annotations

import argparse
import ast
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from render_platform_versions import render_markdown

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = "2.0.0"
MANIFEST_KIND = "openadapt-platform-release-manifest"
STATUS_URL = "https://openadapt.ai/status.json"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
PYPI_VERSION_URL_TEMPLATE = "https://pypi.org/pypi/{package}/{version}/json"
GITHUB_COMMIT_URL_TEMPLATE = (
    "https://api.github.com/repos/{repository}/commits/{release_ref}"
)
GITHUB_CONTENT_URL_TEMPLATE = (
    "https://api.github.com/repos/{repository}/contents/{path}?ref={commit}"
)
GITHUB_TREE_URL_TEMPLATE = (
    "https://api.github.com/repos/{repository}/git/trees/{tree}?recursive=1"
)
DESKTOP_NATIVE_LOCK_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-desktop/"
    "{release_ref}/uv.lock"
)
HTTP_TIMEOUT_SECONDS = 30

# The platform components this manifest pins. The launcher (this repository)
# is the integration point; the others are the runtime, native recorder,
# privacy boundary, shared schemas, desktop shell, and governed agent bridge.
# All seven publish to PyPI.
COMPONENTS: dict[str, str] = {
    "launcher": "openadapt",
    "flow": "openadapt-flow",
    "capture": "openadapt-capture",
    "privacy": "openadapt-privacy",
    "types": "openadapt-types",
    "desktop": "openadapt-desktop",
    "agent": "openadapt-agent",
}

COMPONENT_REPOSITORIES = {
    role: f"OpenAdaptAI/{package}" for role, package in COMPONENTS.items()
}
COMPONENT_REPOSITORIES["launcher"] = "OpenAdaptAI/OpenAdapt"

RUNTIME_UNIT_NAMES = {
    "launcher_environment",
    "customer_runner",
    "desktop_sidecar",
    "agent_bridge",
}

# Cross-component schema probes. The paths and prefixes identify public
# protocol surfaces. Accepted versions are extracted from the exact selected
# release commits; this table never declares a version itself. An optional
# probe appears in the BOM only after its source file exists in a published
# release. This permits an ordered release train without a false future claim.
SCHEMA_PROBES = {
    "workflow_bundle": {
        "source_component": "flow",
        "schema_field": "schema_version",
        "format": "integer",
        "sources": {
            "openadapt_flow/ir.py": [r"(?m)^SCHEMA_VERSION\s*=\s*(\d+)\s*$"],
            "openadapt_flow/bundle_validation.py": [
                r'data\.get\("schema_version",\s*(\d+)\)'
            ],
        },
    },
    "capture_structural_observation": {
        "source_component": "capture",
        "schema_prefix": "openadapt.capture.structural-observation/v",
        "sources": {"openadapt_capture/structural.py": []},
    },
    "control_overlay_frame": {
        "source_component": "types",
        "schema_prefix": "openadapt.control-overlay-frame/v",
        "sources": {
            "openadapt_types/control_overlay.py": [],
            "openadapt_types/control_overlay_tracking.py": [],
        },
    },
    "control_overlay_timeline": {
        "source_component": "types",
        "schema_prefix": "openadapt.control-overlay-timeline/v",
        "sources": {
            "openadapt_types/control_overlay.py": [],
            "openadapt_types/control_overlay_tracking.py": [],
        },
    },
    "human_decision_task": {
        "source_component": "types",
        "schema_prefix": "openadapt.human-decision-task/v",
        "sources": {"openadapt_types/human_decision.py": []},
    },
    "human_decision_receipt": {
        "source_component": "types",
        "schema_prefix": "openadapt.human-decision-receipt/v",
        "sources": {"openadapt_types/human_decision.py": []},
    },
    "human_decision_relay": {
        "source_component": "flow",
        "schema_prefix": "openadapt.human-decision-relay/v",
        "optional": True,
        "sources": {"openadapt_flow/console/decision_relay.py": []},
    },
    "remote_decision_projection": {
        "source_component": "flow",
        "schema_prefix": "openadapt.remote-decision-projection/v",
        "sources": {"openadapt_flow/console/human_decisions.py": []},
    },
    "runtime_validation": {
        "source_component": "flow",
        "schema_prefix": "openadapt.runtime-validation/v",
        "sources": {"openadapt_flow/runtime_validation.py": []},
    },
}

# Operating systems the launcher supports, mirroring pyproject extras
# (windows/macos/linux) and the substrate table in status.json.
SUPPORTED_OS = ["windows", "macos", "linux"]

# Qualification evidence pointers. IDs are stable; consumers resolve them to
# the referenced documents. The public status document is the live summary;
# the flow evidence packs are versioned, replayable evidence bundles.
QUALIFICATION_EVIDENCE = [
    {
        "id": "openadapt-public-status",
        "kind": "status-document",
        "url": STATUS_URL,
        "description": (
            "Canonical machine-readable release, capability, and substrate "
            "qualification status, maintained in openadapt-web "
            "(public/status.json)."
        ),
    },
    {
        "id": "flow-public-demo-evidence-packs",
        "kind": "evidence-pack-collection",
        "source_component": "flow",
        "repository": "OpenAdaptAI/openadapt-flow",
        "path": "public-demo/evidence-packs",
        "description": (
            "Versioned public evidence packs with per-pack manifest files, "
            "replay evidence, and fault-case results."
        ),
    },
    {
        "id": "flow-effectbench-task-pack",
        "kind": "benchmark-manifest",
        "source_component": "flow",
        "repository": "OpenAdaptAI/openadapt-flow",
        "path": "benchmark/effectbench/task_pack/manifest.json",
        "description": (
            "Effect-verification benchmark task pack manifest used by the "
            "flow qualification harness."
        ),
    },
]

# Honest signing placeholder. Validation enforces that this exact structure
# is present and that no signature value is claimed until real signing
# infrastructure exists. Plan: docs/platform-manifest.md ("Signing plan").
UNSIGNED_SIGNATURE = {
    "algorithm": None,
    "value": None,
    "status": "unsigned (signing infrastructure pending)",
    "plan": "docs/platform-manifest.md#signing-plan",
}


class DriftError(RuntimeError):
    """Raised when live sources disagree with each other or the repo."""


def _fetch_json(url: str) -> dict:
    headers = {"User-Agent": "openadapt-platform-manifest-generator"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DriftError(
            f"FATAL: could not fetch {url}: {exc}. "
            "The manifest is generated only from live published sources; "
            "refusing to invent values."
        ) from exc


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "openadapt-platform-manifest-generator"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, UnicodeDecodeError, TimeoutError) as exc:
        raise DriftError(
            f"FATAL: could not fetch {url}: {exc}. The frozen sidecar closure "
            "must come from its exact public release tag; refusing to guess."
        ) from exc


def _fetch_json_optional(
    url: str, *, missing_status_codes: tuple[int, ...] = (404,)
) -> dict | list | None:
    """Fetch JSON, returning ``None`` for configured missing-resource statuses.

    GitHub's commit-by-ref endpoint returns 422 (not 404) when the tag does
    not exist. Callers that probe candidate tags must pass that code.
    """

    headers = {"User-Agent": "openadapt-platform-manifest-generator"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in missing_status_codes:
            return None
        raise DriftError(f"FATAL: could not fetch {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DriftError(f"FATAL: could not fetch {url}: {exc}") from exc


def _github_source_file(
    repository: str,
    commit: str,
    path: str,
    *,
    optional: bool = False,
) -> tuple[str, dict] | None:
    """Read and bind one source file at an exact public commit."""

    url = GITHUB_CONTENT_URL_TEMPLATE.format(
        repository=repository, path=path, commit=commit
    )
    doc = _fetch_json_optional(url)
    if doc is None:
        if optional:
            return None
        raise DriftError(
            f"FATAL: required schema source {repository}@{commit}:{path} is missing."
        )
    if not isinstance(doc, dict) or doc.get("type") != "file":
        raise DriftError(
            f"FATAL: schema source {repository}@{commit}:{path} is not a file."
        )
    try:
        raw = base64.b64decode(doc["content"], validate=False)
        text = raw.decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise DriftError(
            f"FATAL: schema source {repository}@{commit}:{path} has no valid "
            "UTF-8 GitHub content."
        ) from exc
    blob_sha = doc.get("sha")
    if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
        raise DriftError(
            f"FATAL: schema source {repository}@{commit}:{path} has no blob SHA."
        )
    return text, {
        "path": path,
        "url": f"https://github.com/{repository}/blob/{commit}/{path}",
        "git_blob": blob_sha,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _literal_schema_strings(source: str) -> set[str]:
    """Return literal strings from source without importing remote code."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise DriftError(f"FATAL: schema source is not valid Python: {exc}") from exc
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _schema_compatibility(components: dict[str, dict]) -> dict[str, dict]:
    """Derive schema versions from exact selected public release sources."""

    schemas: dict[str, dict] = {}
    cache: dict[tuple[str, str, str], tuple[str, dict] | None] = {}
    for name, probe in SCHEMA_PROBES.items():
        role = str(probe["source_component"])
        component = components[role]
        repository = component["provenance"]["repository"]
        commit = component["provenance"]["commit"]
        source_rows = []
        versions: set[int] = set()
        optional = bool(probe.get("optional"))
        missing_optional = False
        for path, patterns in probe["sources"].items():
            key = (repository, commit, path)
            if key not in cache:
                cache[key] = _github_source_file(
                    repository, commit, path, optional=optional
                )
            source_file = cache[key]
            if source_file is None:
                missing_optional = True
                continue
            source, source_row = source_file
            source_rows.append(source_row)
            if patterns:
                for pattern in patterns:
                    versions.update(int(value) for value in re.findall(pattern, source))
            else:
                prefix = str(probe["schema_prefix"])
                for value in _literal_schema_strings(source):
                    match = re.fullmatch(re.escape(prefix) + r"(\d+)", value)
                    if match:
                        versions.add(int(match.group(1)))
        if missing_optional and not versions:
            continue
        if not versions:
            raise DriftError(
                f"FATAL: exact {role} release sources declare no versions for "
                f"schema contract {name}."
            )
        accepted = sorted(versions)
        row = {
            key: value
            for key, value in probe.items()
            if key not in {"sources", "optional"}
        }
        row.update(
            {
                "accepted_versions": accepted,
                "minimum": accepted[0],
                "maximum": accepted[-1],
                "source_commit": commit,
                "source_files": sorted(source_rows, key=lambda item: item["path"]),
            }
        )
        schemas[name] = row
    return schemas


def _openadapt_requirements(requirements: list[str] | None) -> dict[str, list[str]]:
    """Return normalized openadapt-* dependency specifiers from PEP 508 text."""

    pins: dict[str, set[str]] = {}
    for requirement in requirements or []:
        bare = requirement.split(";", 1)[0].strip()
        for index, char in enumerate(bare):
            if char in "><=!~ [":
                name = bare[:index]
                rest = bare[index:]
                break
        else:
            name, rest = bare, ""
        if not name.startswith("openadapt"):
            continue
        specifier = rest
        if specifier.startswith("["):
            close = specifier.find("]")
            if close < 0:
                raise DriftError(
                    f"FATAL: malformed dependency requirement: {requirement}"
                )
            specifier = specifier[close + 1 :]
        specifier = specifier.strip()
        if specifier:
            pins.setdefault(name, set()).add(specifier)
    return {name: sorted(specs) for name, specs in sorted(pins.items())}


def _openadapt_dependency_constraints(
    requirements: list[str] | None,
) -> list[dict[str, str | None]]:
    """Preserve the activation marker on each OpenAdapt dependency edge."""

    constraints: list[dict[str, str | None]] = []
    for requirement in requirements or []:
        bare, separator, marker = requirement.partition(";")
        bare = bare.strip()
        for index, char in enumerate(bare):
            if char in "><=!~ [":
                name = bare[:index]
                rest = bare[index:]
                break
        else:
            name, rest = bare, ""
        if not name.startswith("openadapt"):
            continue
        extras = None
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                raise DriftError(
                    f"FATAL: malformed dependency requirement: {requirement}"
                )
            extras = rest[1:close]
            rest = rest[close + 1 :]
        specifier = rest.strip()
        if not specifier:
            continue
        constraints.append(
            {
                "package": name,
                "requires": specifier,
                "extras": extras,
                "marker": marker.strip() if separator else None,
            }
        )
    return sorted(
        constraints,
        key=lambda row: (
            str(row["package"]),
            str(row["requires"]),
            str(row["extras"]),
            str(row["marker"]),
        ),
    )


def _release_ref_candidates(role: str, version: str) -> tuple[str, ...]:
    """Return published Git tags that may identify this component version.

    Desktop historically tagged native sidecars ``desktop-vX.Y.Z`` and Python
    packages ``vX.Y.Z``. A release may publish only one of those. Other
    components use ``vX.Y.Z``. The generator binds the first tag that exists;
    it does not invent a tag name.
    """
    if role == "desktop":
        return (f"desktop-v{version}", f"v{version}")
    return (f"v{version}",)


def _source_provenance(role: str, version: str) -> dict:
    repository = COMPONENT_REPOSITORIES[role]
    candidates = _release_ref_candidates(role, version)
    for release_ref in candidates:
        url = GITHUB_COMMIT_URL_TEMPLATE.format(
            repository=repository, release_ref=release_ref
        )
        doc = _fetch_json_optional(url, missing_status_codes=(404, 422))
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise DriftError(
                f"FATAL: {url} did not return a commit object for "
                f"{repository}@{release_ref}."
            )
        commit = doc.get("sha")
        tree = doc.get("commit", {}).get("tree", {}).get("sha")
        if not isinstance(commit, str) or not isinstance(tree, str):
            raise DriftError(
                f"FATAL: {url} did not resolve an exact commit and tree for "
                f"{repository}@{release_ref}."
            )
        return {
            "repository": repository,
            "release_ref": release_ref,
            "commit": commit,
            "tree": tree,
            "url": f"https://github.com/{repository}/tree/{release_ref}",
        }
    raise DriftError(
        f"FATAL: {repository} has no published release tag for {version}. "
        f"Tried {', '.join(candidates)}."
    )


def _pypi_component(role: str, requested_version: str | None = None) -> dict:
    package = COMPONENTS[role]
    url = (
        PYPI_VERSION_URL_TEMPLATE.format(package=package, version=requested_version)
        if requested_version
        else PYPI_URL_TEMPLATE.format(package=package)
    )
    doc = _fetch_json(url)
    version = doc["info"]["version"]
    if requested_version is not None and version != requested_version:
        raise DriftError(
            f"FATAL: PyPI returned {package}=={version} for requested "
            f"version {requested_version}."
        )
    artifacts = []
    for url_entry in doc.get("urls", []):
        artifacts.append(
            {
                "type": url_entry["packagetype"],
                "filename": url_entry["filename"],
                "url": url_entry["url"],
                "sha256": url_entry["digests"]["sha256"],
            }
        )
    if not artifacts:
        raise DriftError(
            f"FATAL: PyPI reports no release artifacts for {package}=={version}."
        )
    return {
        "package": package,
        "version": version,
        "source": "pypi",
        "requires_python": doc["info"].get("requires_python"),
        "requires": _openadapt_requirements(doc["info"].get("requires_dist")),
        "dependency_constraints": _openadapt_dependency_constraints(
            doc["info"].get("requires_dist")
        ),
        "provenance": _source_provenance(role, version),
        "artifacts": sorted(artifacts, key=lambda a: a["filename"]),
    }


def _compatibility_from_pyproject() -> dict:
    """Extract the launcher's real dependency pins for openadapt-* packages."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    requirement_strings: list[str] = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirement_strings.extend(extra_requirements)

    pins: dict[str, set[str]] = {}
    for requirement in requirement_strings:
        # Requirement grammar subset: name[extras]specifier[; marker]
        bare = requirement.split(";")[0].strip()
        for index, char in enumerate(bare):
            if char in "><=!~ [":
                name = bare[:index]
                rest = bare[index:]
                break
        else:
            name, rest = bare, ""
        if not name.startswith("openadapt"):
            continue
        specifier = rest
        if specifier.startswith("["):
            close = specifier.index("]")
            specifier = specifier[close + 1 :]
        specifier = specifier.strip()
        if specifier:
            pins.setdefault(name, set()).add(specifier)

    return {
        "python": project["requires-python"],
        "launcher_requires": {
            name: sorted(specs) for name, specs in sorted(pins.items())
        },
    }


def _release_tuple(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_satisfies(version: str, specifier: str) -> bool:
    actual = _release_tuple(version)
    if actual is None:
        return False
    for raw_term in specifier.split(","):
        term = raw_term.strip()
        operator = next(
            (
                candidate
                for candidate in (">=", "<=", "==", ">", "<")
                if term.startswith(candidate)
            ),
            None,
        )
        if operator is None:
            return False
        expected = _release_tuple(term[len(operator) :].strip())
        if expected is None:
            return False
        if operator == "==" and actual != expected:
            return False
        if operator == ">=" and actual < expected:
            return False
        if operator == "<=" and actual > expected:
            return False
        if operator == ">" and actual <= expected:
            return False
        if operator == "<" and actual >= expected:
            return False
    return True


def _dependency_spec(component: dict, package: str) -> str:
    specs = component.get("requires", {}).get(package, [])
    if len(specs) != 1:
        raise DriftError(
            f"FATAL: {component.get('package')}=={component.get('version')} must "
            f"declare exactly one {package} compatibility range; got {specs!r}."
        )
    return specs[0]


def _runtime_unit_requirements(
    components: dict[str, dict],
) -> dict[str, dict[str, str]]:
    """Build compatibility contracts from the selected published metadata."""

    return {
        "launcher_environment": {
            "openadapt": f"=={components['launcher']['version']}",
            "openadapt-flow": _dependency_spec(
                components["launcher"], "openadapt-flow"
            ),
        },
        "customer_runner": {
            "openadapt-flow": f"=={components['flow']['version']}",
            "openadapt-types": _dependency_spec(components["flow"], "openadapt-types"),
        },
        "desktop_sidecar": {
            package: _dependency_spec(components["desktop"], package)
            for package in (
                "openadapt-flow",
                "openadapt-capture",
                "openadapt-privacy",
                "openadapt-types",
            )
        },
        "agent_bridge": {
            "openadapt-agent": f"=={components['agent']['version']}",
            "openadapt-flow": _dependency_spec(components["agent"], "openadapt-flow"),
        },
    }


def _desktop_sidecar_lock(
    source_ref: str, required_packages: set[str]
) -> tuple[str, str, str, dict[str, str]]:
    """Read the immutable package closure used by the native sidecar build."""

    url = DESKTOP_NATIVE_LOCK_URL_TEMPLATE.format(release_ref=source_ref)
    lock_text = _fetch_text(url)
    try:
        lock = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as exc:
        raise DriftError(f"FATAL: {url} is not valid TOML: {exc}") from exc

    wanted = required_packages
    resolved: dict[str, str] = {}
    for package in lock.get("package", []):
        name = package.get("name")
        package_version = package.get("version")
        if name not in wanted or not isinstance(package_version, str):
            continue
        if name in resolved and resolved[name] != package_version:
            raise DriftError(
                f"FATAL: {source_ref} resolves multiple {name} versions; "
                "the sidecar closure is not exact."
            )
        resolved[name] = package_version
    missing = sorted(wanted - set(resolved))
    if missing:
        raise DriftError(
            f"FATAL: {source_ref} uv.lock omits required sidecar packages: "
            + ", ".join(missing)
        )
    lock_sha256 = hashlib.sha256(lock_text.encode("utf-8")).hexdigest()
    return source_ref, url, lock_sha256, dict(sorted(resolved.items()))


def _runtime_units(components: dict[str, dict]) -> dict[str, dict]:
    requirements = _runtime_unit_requirements(components)
    sidecar_ref, sidecar_lock_url, sidecar_lock_sha256, sidecar_resolved = (
        _desktop_sidecar_lock(
            components["desktop"]["provenance"]["release_ref"],
            set(requirements["desktop_sidecar"]),
        )
    )
    return {
        "launcher_environment": {
            "kind": "launcher-environment",
            "version": components["launcher"]["version"],
            "source_component": "launcher",
            "entry_point": "openadapt",
            "requirements": requirements["launcher_environment"],
            "selected": {
                "openadapt": components["launcher"]["version"],
                "openadapt-flow": components["flow"]["version"],
            },
        },
        "customer_runner": {
            "kind": "customer-controlled-runner",
            "version": components["flow"]["version"],
            "source_component": "flow",
            "entry_point": "openadapt-flow",
            "requirements": requirements["customer_runner"],
            "selected": {
                "openadapt-flow": components["flow"]["version"],
                "openadapt-types": components["types"]["version"],
            },
        },
        "desktop_sidecar": {
            "kind": "frozen-desktop-sidecar",
            "version": components["desktop"]["version"],
            "source_component": "desktop",
            "source_ref": sidecar_ref,
            "lock_url": sidecar_lock_url,
            "lock_sha256": sidecar_lock_sha256,
            "binary": "openadapt-engine",
            "requirements": requirements["desktop_sidecar"],
            "resolved": sidecar_resolved,
        },
        "agent_bridge": {
            "kind": "governed-agent-bridge",
            "version": components["agent"]["version"],
            "source_component": "agent",
            "entry_point": "openadapt-agent",
            "requirements": requirements["agent_bridge"],
            "selected": {
                "openadapt-agent": components["agent"]["version"],
                "openadapt-flow": components["flow"]["version"],
            },
        },
    }


def _dependency_edges(
    components: dict[str, dict], runtime_units: dict[str, dict]
) -> list[dict]:
    """Return every selected-platform dependency edge and its exact target."""

    package_roles = {package: role for role, package in COMPONENTS.items()}
    sidecar = runtime_units["desktop_sidecar"]["resolved"]
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for source_role, component in components.items():
        for constraint in component.get("dependency_constraints", []):
            package = constraint["package"]
            target_role = package_roles.get(package)
            if target_role is None:
                continue
            scope = (
                "desktop-sidecar-lock"
                if source_role == "desktop"
                else "selected-platform"
            )
            resolved = (
                sidecar.get(package)
                if scope == "desktop-sidecar-lock"
                else components[target_role]["version"]
            )
            key = (scope, source_role, target_role, constraint["requires"])
            edge = grouped.setdefault(
                key,
                {
                    "scope": scope,
                    "source_role": source_role,
                    "source_package": component["package"],
                    "source_version": component["version"],
                    "target_role": target_role,
                    "target_package": package,
                    "requires": constraint["requires"],
                    "resolved": resolved,
                    "activations": [],
                },
            )
            activation = {
                "extras": constraint.get("extras"),
                "marker": constraint.get("marker"),
            }
            if activation not in edge["activations"]:
                edge["activations"].append(activation)
    edges = list(grouped.values())
    for edge in edges:
        edge["activations"].sort(
            key=lambda row: (str(row["extras"]), str(row["marker"]))
        )
    return sorted(
        edges,
        key=lambda edge: (
            edge["scope"],
            edge["source_role"],
            edge["target_role"],
            edge["requires"],
        ),
    )


def _compatibility_status(dependency_edges: list[dict]) -> dict:
    failures = []
    for edge in dependency_edges:
        version = edge.get("resolved")
        specifier = edge.get("requires")
        if not isinstance(version, str) or not _version_satisfies(
            version, str(specifier)
        ):
            failures.append(
                {
                    "scope": edge.get("scope"),
                    "source_role": edge.get("source_role"),
                    "target_role": edge.get("target_role"),
                    "package": edge.get("target_package"),
                    "requires": specifier,
                    "resolved": version,
                }
            )
    return {
        "status": (
            "dependency-compatible" if not failures else "dependency-incompatible"
        ),
        "basis": "published-metadata-and-desktop-lock",
        "failures": failures,
    }


def _substrates_from_status(status: dict) -> list[dict]:
    substrates = []
    for entry in status.get("substrates", []):
        substrates.append(
            {
                "name": entry["name"],
                "delivery": entry.get("delivery"),
            }
        )
    if not substrates:
        raise DriftError(
            f"FATAL: {STATUS_URL} contains no substrates; refusing to emit an "
            "empty driver table."
        )
    return substrates


def _supported_os_from_status(status: dict) -> list[str]:
    status_names = {
        str(entry.get("name", "")).casefold() for entry in status.get("substrates", [])
    }
    supported = [name for name in SUPPORTED_OS if name.casefold() in status_names]
    if supported != SUPPORTED_OS:
        missing = sorted(set(SUPPORTED_OS) - set(supported))
        raise DriftError(
            f"FATAL: {STATUS_URL} omits required host operating systems: "
            + ", ".join(missing)
        )
    return supported


def _github_tree_entries(repository: str, tree: str) -> dict[str, dict]:
    url = GITHUB_TREE_URL_TEMPLATE.format(repository=repository, tree=tree)
    doc = _fetch_json(url)
    if doc.get("truncated"):
        raise DriftError(
            f"FATAL: GitHub truncated {repository} tree {tree}; evidence paths "
            "cannot be verified completely."
        )
    rows = doc.get("tree")
    if not isinstance(rows, list):
        raise DriftError(f"FATAL: {repository} tree {tree} has no entries.")
    return {
        str(row["path"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }


def _qualification_evidence(components: dict[str, dict]) -> list[dict]:
    evidence_rows = []
    trees: dict[tuple[str, str], dict[str, dict]] = {}
    for evidence in QUALIFICATION_EVIDENCE:
        row = dict(evidence)
        role = row.get("source_component")
        if isinstance(role, str):
            provenance = components[role]["provenance"]
            repository = row["repository"]
            if repository != provenance["repository"]:
                raise DriftError(
                    f"FATAL: evidence {row['id']} repository does not match "
                    f"selected {role} provenance."
                )
            tree_key = (repository, provenance["tree"])
            if tree_key not in trees:
                trees[tree_key] = _github_tree_entries(*tree_key)
            source_object = trees[tree_key].get(row["path"])
            if source_object is None:
                raise DriftError(
                    f"FATAL: evidence path {repository}@{provenance['commit']}:"
                    f"{row['path']} does not exist."
                )
            object_type = source_object.get("type")
            object_sha = source_object.get("sha")
            if object_type not in {"blob", "tree"} or not isinstance(object_sha, str):
                raise DriftError(
                    f"FATAL: evidence path {row['path']} has no Git object binding."
                )
            row["source_commit"] = provenance["commit"]
            row["source_object_type"] = object_type
            row["source_object_sha"] = object_sha
            url_kind = "blob" if object_type == "blob" else "tree"
            row["url"] = (
                f"https://github.com/{repository}/{url_kind}/"
                f"{provenance['commit']}/{row['path']}"
            )
        evidence_rows.append(row)
    return evidence_rows


def _generation_metadata() -> dict:
    files = []
    for relative_path in (
        "scripts/generate_platform_manifest.py",
        "scripts/render_platform_versions.py",
    ):
        content = (ROOT / relative_path).read_bytes()
        files.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {"files": files}


def _parse_component_versions(values: list[str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for value in values:
        role, separator, version = value.partition("=")
        if not separator or role not in COMPONENTS or not version:
            raise DriftError(
                "FATAL: --component-version must use ROLE=VERSION, where ROLE is "
                + ", ".join(sorted(COMPONENTS))
            )
        if role in selected:
            raise DriftError(f"FATAL: duplicate component selection for {role}.")
        selected[role] = version
    return selected


def generate(
    allow_unreleased_launcher: bool,
    component_versions: dict[str, str] | None = None,
) -> dict:
    status = _fetch_json(STATUS_URL)
    selected_versions = component_versions or {}

    components = {
        role: _pypi_component(role, selected_versions.get(role)) for role in COMPONENTS
    }

    # Drift guard: the in-repo launcher version must match the published one,
    # unless the caller explicitly acknowledges an in-flight release.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    repo_launcher_version = pyproject["project"]["version"]
    published_launcher_version = components["launcher"]["version"]
    if repo_launcher_version != published_launcher_version:
        message = (
            f"launcher version drift: pyproject.toml has "
            f"{repo_launcher_version} but PyPI's latest is "
            f"{published_launcher_version}."
        )
        if not allow_unreleased_launcher:
            raise DriftError(
                "FATAL: " + message + " Pass --allow-unreleased-launcher only "
                "if a release train is in flight; the manifest records the "
                "published version either way."
            )
        print(f"WARNING: {message} Recording the published version.")

    runtime_units = _runtime_units(components)
    dependency_edges = _dependency_edges(components, runtime_units)
    return {
        "manifest_kind": MANIFEST_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "generation": _generation_metadata(),
        "release_selection": {
            "mode": ("explicit-published" if selected_versions else "latest-published"),
            "component_versions": dict(sorted(selected_versions.items())),
        },
        "components": components,
        "runtime_units": runtime_units,
        "dependency_edges": dependency_edges,
        "schema_compatibility": _schema_compatibility(components),
        "compatibility": _compatibility_from_pyproject(),
        "compatibility_status": _compatibility_status(dependency_edges),
        "supported_os": _supported_os_from_status(status),
        "substrate_drivers": _substrates_from_status(status),
        "qualification_evidence": _qualification_evidence(components),
        "signature": UNSIGNED_SIGNATURE,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "platform-manifest.json",
        help="Where to write the manifest (default: repo root).",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=ROOT / "docs" / "platform-compatibility-report.md",
        help="Where to write the generated human report.",
    )
    parser.add_argument(
        "--component-version",
        action="append",
        default=[],
        metavar="ROLE=VERSION",
        help=(
            "Select an exact published component release instead of PyPI's "
            "latest. Repeat for each release-train component. The generator "
            "refuses a version until its immutable artifacts and release tag exist."
        ),
    )
    parser.add_argument(
        "--allow-unreleased-launcher",
        action="store_true",
        help=(
            "Permit pyproject.toml to be ahead of the latest PyPI launcher "
            "release. The published version is recorded regardless."
        ),
    )
    args = parser.parse_args()

    try:
        selected_versions = _parse_component_versions(args.component_version)
        manifest = generate(args.allow_unreleased_launcher, selected_versions)
    except DriftError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    args.report_output.write_text(render_markdown(manifest), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Wrote {args.report_output}")
    for role, component in manifest["components"].items():
        print(f"  {role}: {component['package']}=={component['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
