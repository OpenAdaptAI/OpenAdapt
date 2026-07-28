#!/usr/bin/env python3
"""Validate platform-manifest.json against the real published artifacts.

CI-runnable drift detector for the OpenAdapt platform release manifest
(see docs/platform-manifest.md). It fails loudly when the committed manifest
disagrees with reality instead of letting a stale or invented manifest ship.

What it validates TODAY:

* Structure: manifest kind, schema major version, all seven public components,
  four runtime units, exact schema-source bindings, every selected dependency
  edge, and the complete artifact set for each published component.
* Signature honesty: while the signature value is null the status must say
  "unsigned (signing infrastructure pending)". A non-null signature value
  FAILS validation, because no verification infrastructure exists yet and a
  claimed-but-unverifiable signature is worse than an honest null.
* Repo agreement (offline): the openadapt-* compatibility ranges in the
  manifest match this checkout's pyproject.toml, and the manifest's launcher
  version is not AHEAD of pyproject.toml's. A pyproject version ahead of the
  manifest is the normal in-flight-release state and only warns; the PyPI
  comparison below is what authoritatively detects staleness.
* Published-artifact agreement (network): for every component, the manifest
  version equals the latest version on PyPI unless an exact published version
  was selected explicitly. Every artifact filename,
  type, URL, sha256 digest, Python range, and openadapt-* dependency metadata
  match the exact version-specific PyPI record. A manifest version that
  is BEHIND PyPI's latest is drift and fails; one that is AHEAD but already
  published is PyPI index propagation lag and warns (its digests are still
  verified); one PyPI never published fails. An unreachable PyPI warns rather
  than fails -- it is not evidence of drift, and a flaky guard gets ignored --
  unless --require-network is passed. Run with --offline to skip only this
  class of check (for example in an airgapped environment).
* Status-document agreement (network): version skew against
  https://openadapt.ai/status.json is reported as a WARNING by default,
  because status.json lives in another repository and this manifest is not its
  source of truth. It stays a warning so a release here cannot be blocked by an
  edit nobody in this repo can make. Pass --strict-status to escalate the skew
  to a failure.
* Exact-source agreement (network): release refs, schema source files, and
  qualification evidence Git objects match the selected release commit/tree.

  Do NOT read that warning as self-healing. status.json is HAND-MAINTAINED in
  openadapt-web; nothing generates it from PyPI or from this manifest. On
  2026-07-28 it advertised flow 1.24.0 and launcher 1.10.0 while PyPI had
  1.25.1 and 1.10.1. openadapt-web runs its own daily guard
  (scripts/check_published_version_claims.mjs) that fails on exactly this, but
  that is after-the-fact detection and still needs a person to open the
  corrective PR. Skew reported here means someone must go and edit that file.

What it does NOT validate yet (planned, see docs/platform-manifest.md):

* Cryptographic signature verification (sigstore for the manifest itself,
  Authenticode / Apple Developer ID for OS installers).
* Desktop OS installer signatures and notarization. The sidecar package
  closure is checked against the exact native release tag, but installer bytes
  are still covered by the Desktop release's own SHA256SUMS and attestation.

Usage:
    python scripts/validate_platform_manifest.py [--manifest PATH]
        [--offline] [--strict-status] [--require-compatible]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from generate_platform_manifest import (
    COMPONENT_REPOSITORIES,
    COMPONENTS,
    DESKTOP_NATIVE_LOCK_URL_TEMPLATE,
    GITHUB_COMMIT_URL_TEMPLATE,
    PYPI_VERSION_URL_TEMPLATE,
    QUALIFICATION_EVIDENCE,
    RUNTIME_UNIT_NAMES,
    DriftError,
    _compatibility_status,
    _dependency_edges,
    _generation_metadata,
    _github_tree_entries,
    _openadapt_dependency_constraints,
    _openadapt_requirements,
    _release_ref,
    _runtime_unit_requirements,
    _schema_compatibility,
    _substrates_from_status,
    _supported_os_from_status,
)
from render_platform_versions import render_markdown

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_KIND = "openadapt-platform-release-manifest"
EXPECTED_SCHEMA_MAJOR = 1
EXPECTED_UNSIGNED_STATUS = "unsigned (signing infrastructure pending)"
STATUS_URL = "https://openadapt.ai/status.json"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
HTTP_TIMEOUT_SECONDS = 30
REPORT_PATH = ROOT / "docs" / "platform-compatibility-report.md"
RELEASE_CHANNELS = {"stable", "beta", "preview", "experimental", "research"}
EXPECTED_SIGNATURE_PLAN = "docs/platform-manifest.md#signing-plan"

REQUIRED_TOP_LEVEL_FIELDS = (
    "manifest_kind",
    "schema_version",
    "generated_at",
    "generation",
    "release_channel",
    "release_selection",
    "components",
    "runtime_units",
    "dependency_edges",
    "schema_compatibility",
    "compatibility",
    "compatibility_status",
    "supported_os",
    "substrate_drivers",
    "qualification_evidence",
    "signature",
)


def _fetch_json(url: str) -> dict:
    headers = {"User-Agent": "openadapt-platform-manifest-validator"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, headers=headers
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "openadapt-platform-manifest-validator"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def check_structure(manifest: dict, report: Report) -> None:
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in manifest:
            report.error(f"missing required top-level field: {field}")
    if manifest.get("manifest_kind") != EXPECTED_KIND:
        report.error(
            f"manifest_kind is {manifest.get('manifest_kind')!r}, expected "
            f"{EXPECTED_KIND!r}"
        )
    schema_version = str(manifest.get("schema_version", ""))
    major = schema_version.split(".", 1)[0]
    if not major.isdigit() or int(major) != EXPECTED_SCHEMA_MAJOR:
        report.error(
            f"schema_version {schema_version!r} is not major version "
            f"{EXPECTED_SCHEMA_MAJOR}; this validator cannot interpret it"
        )
    generated_at = manifest.get("generated_at")
    try:
        generated = _dt.datetime.fromisoformat(str(generated_at))
    except ValueError:
        generated = None
    if generated is None or generated.tzinfo != _dt.timezone.utc:
        report.error("generated_at must be an ISO 8601 UTC timestamp")
    release_channel = manifest.get("release_channel")
    if release_channel not in RELEASE_CHANNELS:
        report.error(
            f"release_channel must be one of {sorted(RELEASE_CHANNELS)!r}, "
            f"got {release_channel!r}"
        )
    if manifest.get("generation") != _generation_metadata():
        report.error(
            "generation metadata does not match the exact generator and renderer"
        )
    components = manifest.get("components", {})
    if not isinstance(components, dict):
        report.error("components must be an object")
        return
    expected_components = set(COMPONENTS)
    if set(components) != expected_components:
        report.error(
            "components must be exactly "
            f"{sorted(expected_components)!r}, got {sorted(components)!r}"
        )
    for role, component in components.items():
        for key in (
            "package",
            "version",
            "source",
            "requires_python",
            "requires",
            "dependency_constraints",
            "provenance",
        ):
            if key not in component:
                report.error(f"component {role} missing {key}")
        if component.get("package") != COMPONENTS.get(role):
            report.error(
                f"component {role} package is {component.get('package')!r}, "
                f"expected {COMPONENTS.get(role)!r}"
            )
        if component.get("source") != "pypi":
            report.error(f"component {role} source must be 'pypi'")
        provenance = component.get("provenance", {})
        expected_repository = COMPONENT_REPOSITORIES.get(role)
        expected_ref = _release_ref(role, str(component.get("version")))
        if provenance.get("repository") != expected_repository:
            report.error(
                f"component {role} provenance repository is "
                f"{provenance.get('repository')!r}, expected {expected_repository!r}"
            )
        if provenance.get("release_ref") != expected_ref:
            report.error(
                f"component {role} release_ref is "
                f"{provenance.get('release_ref')!r}, expected {expected_ref!r}"
            )
        for digest in ("commit", "tree"):
            if not re.fullmatch(r"[0-9a-f]{40}", str(provenance.get(digest, ""))):
                report.error(f"component {role} provenance {digest} is invalid")
        if not str(provenance.get("url", "")).startswith("https://github.com/"):
            report.error(f"component {role} provenance URL must use GitHub HTTPS")
        artifacts = component.get("artifacts") or []
        if not artifacts:
            report.error(f"component {role} lists no artifacts")
        filenames = [artifact.get("filename") for artifact in artifacts]
        if len(filenames) != len(set(filenames)):
            report.error(f"component {role} has duplicate artifact filenames")
        for artifact in artifacts:
            for key in ("type", "filename", "url", "sha256"):
                if not artifact.get(key):
                    report.error(
                        f"component {role} artifact "
                        f"{artifact.get('filename', '<unnamed>')} missing {key}"
                    )
            if not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", ""))):
                report.error(f"component {role} artifact has an invalid sha256")
            if not str(artifact.get("url", "")).startswith("https://"):
                report.error(f"component {role} artifact URL must use HTTPS")
        constraints = component.get("dependency_constraints")
        if not isinstance(constraints, list):
            report.error(f"component {role} dependency_constraints must be a list")
        for constraint in constraints or []:
            if set(constraint) != {"package", "requires", "extras", "marker"}:
                report.error(
                    f"component {role} dependency constraint has invalid fields"
                )
            if not str(constraint.get("package", "")).startswith("openadapt"):
                report.error(
                    f"component {role} dependency constraint has invalid package"
                )
            if not constraint.get("requires"):
                report.error(
                    f"component {role} dependency constraint has no version range"
                )
    evidence_rows = manifest.get("qualification_evidence")
    if not evidence_rows:
        report.error("qualification_evidence is empty")
    expected_evidence = {row["id"]: row for row in QUALIFICATION_EVIDENCE}
    actual_evidence = {
        row.get("id"): row for row in evidence_rows or [] if isinstance(row, dict)
    }
    if (
        set(actual_evidence) != set(expected_evidence)
        or len(evidence_rows or []) != len(expected_evidence)
    ):
        report.error(
            "qualification_evidence IDs must be exactly "
            f"{sorted(expected_evidence)!r}"
        )
    for evidence in evidence_rows or []:
        if not isinstance(evidence, dict):
            report.error("qualification_evidence rows must be objects")
            continue
        expected = expected_evidence.get(evidence.get("id"))
        if expected is not None:
            for field in ("kind", "source_component", "repository", "path"):
                if expected.get(field) != evidence.get(field):
                    report.error(
                        f"qualification evidence {evidence.get('id')!r} "
                        f"{field} drift"
                    )
            if expected.get("source_component") is None and evidence.get(
                "url"
            ) != expected.get("url"):
                report.error(
                    f"qualification evidence {evidence.get('id')!r} URL drift"
                )
        role = evidence.get("source_component")
        if role is None:
            continue
        component = components.get(role, {})
        source_commit = component.get("provenance", {}).get("commit")
        if evidence.get("source_commit") != source_commit:
            report.error(
                f"qualification evidence {evidence.get('id')!r} is not bound "
                f"to the selected {role} source commit"
            )
        if source_commit not in str(evidence.get("url", "")):
            report.error(
                f"qualification evidence {evidence.get('id')!r} URL is not "
                "commit-pinned"
            )
        object_type = evidence.get("source_object_type")
        if object_type not in {"blob", "tree"}:
            report.error(
                f"qualification evidence {evidence.get('id')!r} has invalid "
                "source_object_type"
            )
        if not re.fullmatch(
            r"[0-9a-f]{40}", str(evidence.get("source_object_sha", ""))
        ):
            report.error(
                f"qualification evidence {evidence.get('id')!r} has invalid "
                "source_object_sha"
            )
        url_kind = "blob" if object_type == "blob" else "tree"
        expected_url = (
            f"https://github.com/{evidence.get('repository')}/{url_kind}/"
            f"{source_commit}/{evidence.get('path')}"
        )
        if evidence.get("url") != expected_url:
            report.error(
                f"qualification evidence {evidence.get('id')!r} URL is not "
                "canonical for its object type"
            )

    selection = manifest.get("release_selection", {})
    if not isinstance(selection, dict):
        report.error("release_selection must be an object")
        selection = {}
    if set(selection) != {"mode", "component_versions"}:
        report.error("release_selection must contain only mode and component_versions")
    if selection.get("mode") not in {"latest-published", "explicit-published"}:
        report.error("release_selection.mode is invalid")
    selected_versions = selection.get("component_versions")
    if not isinstance(selected_versions, dict):
        report.error("release_selection.component_versions must be an object")
    else:
        for role, version in selected_versions.items():
            if role not in COMPONENTS:
                report.error(f"release_selection has unknown component {role!r}")
            elif components.get(role, {}).get("version") != version:
                report.error(
                    f"release_selection requests {role}=={version}, but the "
                    f"component records {components.get(role, {}).get('version')!r}"
                )
        if selection.get("mode") == "latest-published" and selected_versions:
            report.error("latest-published selection cannot contain version overrides")
        if selection.get("mode") == "explicit-published" and not selected_versions:
            report.error("explicit-published selection needs a version override")

    runtime_units = manifest.get("runtime_units", {})
    if not isinstance(runtime_units, dict):
        report.error("runtime_units must be an object")
        return
    expected_units = RUNTIME_UNIT_NAMES
    if set(runtime_units) != expected_units:
        report.error(
            f"runtime_units must be exactly {sorted(expected_units)!r}, "
            f"got {sorted(runtime_units)!r}"
        )
    try:
        expected_requirements = _runtime_unit_requirements(components)
    except DriftError as exc:
        report.error(f"cannot derive runtime requirements: {exc}")
        expected_requirements = {}
    for name, unit in runtime_units.items():
        closure_key = "resolved" if name == "desktop_sidecar" else "selected"
        for key in (
            "kind",
            "version",
            "source_component",
            "requirements",
            closure_key,
        ):
            if key not in unit:
                report.error(f"runtime unit {name} missing {key}")
        if unit.get("requirements") != expected_requirements.get(name):
            report.error(
                f"runtime unit {name} requirements drift: {unit.get('requirements')!r}"
            )
        source_role = unit.get("source_component")
        source = components.get(source_role, {})
        if unit.get("version") != source.get("version"):
            report.error(
                f"runtime unit {name} version {unit.get('version')!r} does not "
                f"match source component {source_role!r} version "
                f"{source.get('version')!r}"
            )

    runner = runtime_units.get("customer_runner", {})
    expected_runner_selected = {
        "openadapt-flow": components.get("flow", {}).get("version"),
        "openadapt-types": components.get("types", {}).get("version"),
    }
    if runner.get("selected") != expected_runner_selected:
        report.error(
            f"customer_runner selection is {runner.get('selected')!r}, expected "
            f"{expected_runner_selected!r}"
        )

    launcher = runtime_units.get("launcher_environment", {})
    expected_launcher_selected = {
        "openadapt": components.get("launcher", {}).get("version"),
        "openadapt-flow": components.get("flow", {}).get("version"),
    }
    if launcher.get("selected") != expected_launcher_selected:
        report.error(
            f"launcher_environment selection is {launcher.get('selected')!r}, "
            f"expected {expected_launcher_selected!r}"
        )

    agent = runtime_units.get("agent_bridge", {})
    expected_agent_selected = {
        "openadapt-agent": components.get("agent", {}).get("version"),
        "openadapt-flow": components.get("flow", {}).get("version"),
    }
    if agent.get("selected") != expected_agent_selected:
        report.error(
            f"agent_bridge selection is {agent.get('selected')!r}, expected "
            f"{expected_agent_selected!r}"
        )

    sidecar = runtime_units.get("desktop_sidecar", {})
    expected_sidecar_ref = f"desktop-v{sidecar.get('version')}"
    if sidecar.get("source_ref") != expected_sidecar_ref:
        report.error(
            f"desktop_sidecar source_ref is {sidecar.get('source_ref')!r}, "
            f"expected {expected_sidecar_ref!r}"
        )
    expected_lock_url = DESKTOP_NATIVE_LOCK_URL_TEMPLATE.format(
        version=sidecar.get("version")
    )
    if sidecar.get("lock_url") != expected_lock_url:
        report.error(
            f"desktop_sidecar lock_url is {sidecar.get('lock_url')!r}, "
            f"expected {expected_lock_url!r}"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(sidecar.get("lock_sha256", ""))):
        report.error("desktop_sidecar lock_sha256 is invalid")

    dependency_edges = manifest.get("dependency_edges")
    if not isinstance(dependency_edges, list):
        report.error("dependency_edges must be a list")
    else:
        try:
            expected_edges = _dependency_edges(components, runtime_units)
        except (DriftError, KeyError, TypeError) as exc:
            report.error(f"cannot derive dependency edges: {exc}")
        else:
            if dependency_edges != expected_edges:
                report.error(
                    "dependency_edges drift: the selected-platform graph does "
                    "not match the published component metadata and Desktop lock"
                )

    schemas = manifest.get("schema_compatibility")
    if not isinstance(schemas, dict) or not schemas:
        report.error("schema_compatibility must be a non-empty object")
    for name, schema in (schemas or {}).items():
        role = schema.get("source_component")
        accepted = schema.get("accepted_versions")
        if role not in components:
            report.error(f"schema {name} has an unknown source_component")
            continue
        if (
            not isinstance(accepted, list)
            or not accepted
            or accepted != sorted(set(accepted))
            or not all(isinstance(version, int) and version > 0 for version in accepted)
        ):
            report.error(f"schema {name} accepted_versions is invalid")
        elif schema.get("minimum") != accepted[0] or schema.get("maximum") != accepted[-1]:
            report.error(f"schema {name} range does not match accepted_versions")
        source_commit = components[role].get("provenance", {}).get("commit")
        if schema.get("source_commit") != source_commit:
            report.error(f"schema {name} is not bound to its selected release commit")
        source_files = schema.get("source_files")
        if not isinstance(source_files, list) or not source_files:
            report.error(f"schema {name} has no source_files")
        for source_file in source_files or []:
            path = source_file.get("path")
            if (
                not isinstance(path, str)
                or path.startswith("/")
                or ".." in Path(path).parts
            ):
                report.error(f"schema {name} has an invalid source path")
            if not re.fullmatch(r"[0-9a-f]{40}", str(source_file.get("git_blob", ""))):
                report.error(f"schema {name} source has an invalid git_blob")
            if not re.fullmatch(r"[0-9a-f]{64}", str(source_file.get("sha256", ""))):
                report.error(f"schema {name} source has an invalid sha256")

    if manifest.get("supported_os") != ["windows", "macos", "linux"]:
        report.error("supported_os must be exactly windows, macos, and linux")
    substrates = manifest.get("substrate_drivers")
    if not isinstance(substrates, list) or not substrates:
        report.error("substrate_drivers must be a non-empty list")
    elif any(
        set(row) != {"name", "public_label", "delivery"} for row in substrates
    ):
        report.error("substrate_drivers has an invalid row")


def check_compatibility_status(manifest: dict, report: Report) -> None:
    dependency_edges = manifest.get("dependency_edges")
    if not isinstance(dependency_edges, list) or not all(
        isinstance(edge, dict) for edge in dependency_edges
    ):
        report.error("cannot compute compatibility_status from invalid edges")
        return
    expected = _compatibility_status(dependency_edges)
    actual = manifest.get("compatibility_status")
    if actual != expected:
        report.error(
            f"compatibility_status is {actual!r}, but the selected dependency "
            f"graph implies {expected!r}"
        )


def check_generated_report(
    manifest: dict, report: Report, report_path: Path = REPORT_PATH
) -> None:
    try:
        actual = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.error(f"cannot read generated compatibility report: {exc}")
        return
    expected = render_markdown(manifest)
    if actual != expected:
        report.error(
            f"{report_path} does not match the exact BOM; "
            "rerun scripts/generate_platform_manifest.py"
        )


def check_signature_honesty(manifest: dict, report: Report) -> None:
    signature = manifest.get("signature") or {}
    expected_unsigned = {
        "algorithm": None,
        "value": None,
        "status": EXPECTED_UNSIGNED_STATUS,
        "plan": EXPECTED_SIGNATURE_PLAN,
    }
    if signature.get("value") is None:
        if signature != expected_unsigned:
            report.error(
                "unsigned signature block must match the exact null-signature "
                "contract and signing-plan path"
            )
        plan_path = ROOT / EXPECTED_SIGNATURE_PLAN.split("#", 1)[0]
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except OSError as exc:
            report.error(f"cannot read signature plan: {exc}")
        else:
            if "## Signing plan" not in plan_text:
                report.error("signature plan anchor does not exist")
    else:
        report.error(
            "signature.value is set, but no signature verification "
            "infrastructure exists yet. Refusing to accept an unverifiable "
            "signature claim. Implement verification before signing "
            "(docs/platform-manifest.md, Signing plan)."
        )


def compare_launcher_to_pyproject(
    manifest_version: str | None, pyproject_version: str | None, report: Report
) -> None:
    """Compare the manifest's launcher version to this checkout's pyproject.

    The manifest records the PUBLISHED launcher, while ``pyproject.toml``
    records the version this checkout would publish next. Those legitimately
    disagree for the ~90 seconds between python-semantic-release pushing its
    version-bump commit and the release workflow's reconcile job regenerating
    the manifest, so:

    * equal -> silent.
    * pyproject AHEAD of the manifest -> WARNING. A release is in flight or
      the bump has not been published yet. Treating this as an error made the
      guard fail on main after every single release, which is precisely how
      a real drift failure became invisible. Staleness against the real world
      is detected by ``compare_component_to_pypi``, which is authoritative.
    * pyproject BEHIND the manifest, or either version unparseable -> ERROR.
      The manifest claims a launcher this repository never produced.
    """
    if manifest_version == pyproject_version:
        return

    manifest_release = _release_tuple(str(manifest_version))
    pyproject_release = _release_tuple(str(pyproject_version))
    if (
        manifest_release is not None
        and pyproject_release is not None
        and pyproject_release > manifest_release
    ):
        report.warning(
            f"launcher: pyproject.toml has {pyproject_version!r} but the "
            f"manifest records the published {manifest_version!r}. A release "
            "is in flight or unpublished; the manifest deliberately records "
            "only published versions. PyPI comparison remains authoritative."
        )
        return

    report.error(
        f"launcher version drift: manifest has {manifest_version!r} "
        f"but pyproject.toml has {pyproject_version!r}. The manifest names a "
        "launcher this repository does not produce. Regenerate with "
        "scripts/generate_platform_manifest.py."
    )


def check_against_pyproject(manifest: dict, report: Report) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]

    launcher = manifest.get("components", {}).get("launcher", {})
    compare_launcher_to_pyproject(launcher.get("version"), project["version"], report)

    compatibility = manifest.get("compatibility", {})
    if compatibility.get("python") != project["requires-python"]:
        report.error(
            f"python compatibility drift: manifest has "
            f"{compatibility.get('python')!r} but pyproject.toml has "
            f"{project['requires-python']!r}"
        )

    # Recompute the openadapt-* pins exactly as the generator does.
    requirement_strings: list[str] = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirement_strings.extend(extra_requirements)
    pins: dict[str, set[str]] = {}
    for requirement in requirement_strings:
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
            specifier = specifier[specifier.index("]") + 1 :]
        specifier = specifier.strip()
        if specifier:
            pins.setdefault(name, set()).add(specifier)
    expected = {name: sorted(specs) for name, specs in sorted(pins.items())}
    actual = compatibility.get("launcher_requires")
    if actual != expected:
        report.error(
            "compatibility.launcher_requires drift: manifest has "
            f"{actual!r} but pyproject.toml implies {expected!r}"
        )


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """Return the numeric release segment of a version, or None if unparseable.

    Only the release segment is compared. Any pre/post/dev suffix makes the
    version unparseable here, which callers treat conservatively (as drift)
    rather than guessing an ordering.
    """
    parts = version.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _files_for_version(doc: dict, version: str) -> list[dict] | None:
    """Return PyPI's file records for ``version``, or None if it has none.

    ``info.version`` lags behind an upload on some PyPI CDN edges, so
    ``urls`` (which describes ``info.version``) is not always the right list.
    ``releases`` is keyed by version and is authoritative for a named one.
    """
    releases = doc.get("releases")
    if isinstance(releases, dict):
        files = releases.get(version)
        if files:
            return list(files)
        if version in releases:
            return []
        return None
    # Older/partial payloads without a `releases` map: fall back to `urls`,
    # which only ever describes `info.version`.
    if doc.get("info", {}).get("version") == version:
        return list(doc.get("urls", []))
    return None


def compare_component_to_pypi(
    role: str,
    component: dict,
    doc: dict,
    report: Report,
    *,
    allow_pinned: bool = False,
    latest_version_override: str | None = None,
) -> None:
    """Compare one manifest component against PyPI's published state.

    Pure (no I/O) so the drift guard can be tested against simulated future
    releases and tampered digests. Classification:

    * manifest version OLDER than PyPI's latest -> ERROR. This is staleness:
      the manifest advertises a superseded release and its digests, so anyone
      verifying today's artifact against it fails. This is the exact bug this
      guard exists to catch.
    * manifest version NEWER than PyPI's latest but already present in
      ``releases`` -> WARNING. PyPI's ``info.version`` lags an upload by up to
      a few minutes on some CDN edges, and a release-time false red trains
      people to ignore this check. Digests are still verified against the
      named version, so a wrong digest still fails.
    * manifest version absent from ``releases`` entirely -> ERROR. The
      manifest names a release that was never published.
    * any filename/URL/digest mismatch -> ERROR, always.
    """
    package = component.get("package")
    manifest_version = component.get("version")
    latest_version = latest_version_override or doc.get("info", {}).get("version")

    files = _files_for_version(doc, manifest_version)
    if not files:
        report.error(
            f"{role} version drift: manifest has {manifest_version!r} but "
            f"PyPI publishes no files for {package}=={manifest_version} "
            f"(PyPI's latest is {latest_version!r}). The manifest names a "
            "release that does not exist. Regenerate the manifest."
        )
        return

    if manifest_version != latest_version:
        manifest_release = _release_tuple(str(manifest_version))
        latest_release = _release_tuple(str(latest_version))
        ahead = (
            manifest_release is not None
            and latest_release is not None
            and manifest_release > latest_release
        )
        if ahead:
            report.warning(
                f"{role}: manifest has {manifest_version!r} but PyPI's "
                f"latest {package} still reads {latest_version!r}. The "
                "manifest version is published, so this is PyPI index "
                "propagation lag, not drift; digests are verified against "
                f"{manifest_version!r}."
            )
        elif allow_pinned:
            report.warning(
                f"{role}: the explicit platform selection pins "
                f"{package}=={manifest_version}; PyPI's latest is "
                f"{latest_version}. The exact selected artifacts remain verified."
            )
        else:
            report.error(
                f"{role} version drift: manifest has {manifest_version!r} "
                f"but PyPI's latest {package} is {latest_version!r}. The "
                "manifest advertises a superseded release and its digests. "
                "Regenerate it with scripts/generate_platform_manifest.py."
            )
            return

    published = {
        entry["filename"]: (
            entry["packagetype"],
            entry["url"],
            entry["digests"]["sha256"],
        )
        for entry in files
    }
    declared = {
        artifact.get("filename") for artifact in component.get("artifacts", [])
    }
    if declared != set(published):
        report.error(
            f"{role} artifact filename-set drift: manifest has "
            f"{sorted(declared, key=str)!r} but PyPI has {sorted(published)!r}"
        )
    for artifact in component.get("artifacts", []):
        filename = artifact.get("filename")
        if filename not in published:
            report.error(
                f"{role} artifact {filename} is not among PyPI's files "
                f"for {package}=={manifest_version}"
            )
            continue
        artifact_type, url, sha256 = published[filename]
        if artifact.get("type") != artifact_type:
            report.error(
                f"{role} artifact {filename} type drift: manifest has "
                f"{artifact.get('type')} but PyPI has {artifact_type}"
            )
        if artifact.get("url") != url:
            report.error(
                f"{role} artifact {filename} URL drift: manifest has "
                f"{artifact.get('url')} but PyPI has {url}"
            )
        if artifact.get("sha256") != sha256:
            report.error(
                f"{role} artifact {filename} sha256 drift: manifest has "
                f"{artifact.get('sha256')} but PyPI has {sha256}. A consumer "
                "verifying the published artifact against this manifest "
                "would fail."
            )

    expected_requirements = _openadapt_requirements(
        doc.get("info", {}).get("requires_dist")
    )
    if component.get("requires") != expected_requirements:
        report.error(
            f"{role} dependency-range drift: manifest has "
            f"{component.get('requires')!r} but PyPI metadata has "
            f"{expected_requirements!r}"
        )
    expected_constraints = _openadapt_dependency_constraints(
        doc.get("info", {}).get("requires_dist")
    )
    if component.get("dependency_constraints") != expected_constraints:
        report.error(
            f"{role} dependency-constraint drift: manifest has "
            f"{component.get('dependency_constraints')!r} but PyPI metadata has "
            f"{expected_constraints!r}"
        )
    expected_python = doc.get("info", {}).get("requires_python")
    if component.get("requires_python") != expected_python:
        report.error(
            f"{role} requires_python drift: manifest has "
            f"{component.get('requires_python')!r} but PyPI metadata has "
            f"{expected_python!r}"
        )


def check_against_pypi(manifest: dict, report: Report, require_network: bool) -> None:
    selected = manifest.get("release_selection", {}).get("component_versions", {})
    for role, component in manifest.get("components", {}).items():
        package = component.get("package")
        try:
            latest_doc = _fetch_json(PYPI_URL_TEMPLATE.format(package=package))
            if role in selected:
                doc = _fetch_json(
                    PYPI_VERSION_URL_TEMPLATE.format(
                        package=package, version=component.get("version")
                    )
                )
            else:
                doc = latest_doc
        except (urllib.error.URLError, TimeoutError) as exc:
            # An unreachable index is not evidence of drift. Failing here
            # makes the guard flaky, and a flaky guard gets ignored -- which
            # is how the stale manifest survived. Release gates pass
            # --require-network so the comparison cannot be silently skipped
            # where it is load-bearing.
            emit = report.error if require_network else report.warning
            emit(f"could not fetch PyPI metadata for {package}: {exc}")
            continue
        compare_component_to_pypi(
            role,
            component,
            doc,
            report,
            allow_pinned=role in selected,
            latest_version_override=latest_doc.get("info", {}).get("version"),
        )


def check_source_provenance(
    manifest: dict, report: Report, require_network: bool
) -> None:
    for role, component in manifest.get("components", {}).items():
        provenance = component.get("provenance", {})
        url = GITHUB_COMMIT_URL_TEMPLATE.format(
            repository=COMPONENT_REPOSITORIES[role],
            release_ref=_release_ref(role, str(component.get("version"))),
        )
        try:
            doc = _fetch_json(url)
        except (urllib.error.URLError, TimeoutError) as exc:
            emit = report.error if require_network else report.warning
            emit(f"could not validate source provenance for {role}: {exc}")
            continue
        commit = doc.get("sha")
        tree = doc.get("commit", {}).get("tree", {}).get("sha")
        if provenance.get("commit") != commit:
            report.error(
                f"{role} source commit drift: manifest has "
                f"{provenance.get('commit')!r}, release ref resolves to {commit!r}"
            )
        if provenance.get("tree") != tree:
            report.error(
                f"{role} source tree drift: manifest has {provenance.get('tree')!r}, "
                f"release ref resolves to {tree!r}"
            )


def check_schema_sources(
    manifest: dict, report: Report, require_network: bool
) -> None:
    try:
        expected = _schema_compatibility(manifest.get("components", {}))
    except (DriftError, KeyError, TypeError) as exc:
        emit = report.error if require_network else report.warning
        emit(f"could not validate exact schema sources: {exc}")
        return
    if manifest.get("schema_compatibility") != expected:
        report.error(
            "schema_compatibility drift: it does not match the schemas derived "
            "from the exact selected release commits"
        )


def check_qualification_evidence(
    manifest: dict, report: Report, require_network: bool
) -> None:
    components = manifest.get("components", {})
    trees: dict[tuple[str, str], dict[str, dict]] = {}
    for evidence in manifest.get("qualification_evidence", []):
        role = evidence.get("source_component")
        if role is None:
            continue
        component = components.get(role, {})
        provenance = component.get("provenance", {})
        key = (str(provenance.get("repository")), str(provenance.get("tree")))
        try:
            if key not in trees:
                trees[key] = _github_tree_entries(*key)
        except DriftError as exc:
            emit = report.error if require_network else report.warning
            emit(f"could not validate qualification evidence: {exc}")
            continue
        source_object = trees[key].get(evidence.get("path"))
        if source_object is None:
            report.error(
                f"qualification evidence {evidence.get('id')!r} path is absent "
                "from the exact selected release tree"
            )
            continue
        if evidence.get("source_object_type") != source_object.get("type"):
            report.error(
                f"qualification evidence {evidence.get('id')!r} object type drift"
            )
        if evidence.get("source_object_sha") != source_object.get("sha"):
            report.error(
                f"qualification evidence {evidence.get('id')!r} object SHA drift"
            )


def check_against_status(manifest: dict, report: Report, strict: bool) -> None:
    try:
        status = _fetch_json(STATUS_URL)
    except (urllib.error.URLError, TimeoutError) as exc:
        emit = report.error if strict else report.warning
        emit(f"could not fetch {STATUS_URL}: {exc}")
        return
    status_versions = status.get("versions", {})
    emit = report.error if strict else report.warning
    for role, component in manifest.get("components", {}).items():
        status_version = status_versions.get(role)
        if status_version is None:
            continue
        if status_version != component.get("version"):
            emit(
                f"{role} skew vs {STATUS_URL}: manifest has "
                f"{component.get('version')!r}, status.json has "
                f"{status_version!r} (status.json is hand-maintained in "
                "openadapt-web and will not correct itself -- edit "
                "public/status.json and data/published-version-claims.json "
                "there)"
            )
    lifecycle = (status.get("product_lifecycle") or "").lower()
    if lifecycle and lifecycle != manifest.get("release_channel"):
        emit(
            f"release_channel skew: manifest has "
            f"{manifest.get('release_channel')!r}, status.json lifecycle is "
            f"{lifecycle!r}"
        )
    try:
        expected_os = _supported_os_from_status(status)
        expected_substrates = _substrates_from_status(status)
    except DriftError as exc:
        report.error(str(exc))
        return
    if manifest.get("supported_os") != expected_os:
        report.error(
            f"supported_os drift: manifest has {manifest.get('supported_os')!r}, "
            f"status.json implies {expected_os!r}"
        )
    if manifest.get("substrate_drivers") != expected_substrates:
        report.error(
            "substrate_drivers drift: manifest does not match the complete "
            "status.json substrate projection"
        )


def _locked_openadapt_versions(lock_text: str, packages: set[str]) -> dict[str, str]:
    lock = tomllib.loads(lock_text)
    resolved: dict[str, str] = {}
    for package in lock.get("package", []):
        name = package.get("name")
        version = package.get("version")
        if name not in packages or not isinstance(version, str):
            continue
        if name in resolved and resolved[name] != version:
            raise ValueError(f"native lock resolves multiple {name} versions")
        resolved[name] = version
    return dict(sorted(resolved.items()))


def check_desktop_sidecar_lock(
    manifest: dict, report: Report, require_network: bool
) -> None:
    unit = manifest.get("runtime_units", {}).get("desktop_sidecar", {})
    version = unit.get("version")
    source_ref = unit.get("source_ref")
    expected_ref = f"desktop-v{version}"
    if source_ref != expected_ref:
        report.error(
            f"desktop_sidecar source_ref is {source_ref!r}, expected {expected_ref!r}"
        )
        return
    url = DESKTOP_NATIVE_LOCK_URL_TEMPLATE.format(version=version)
    try:
        lock_text = _fetch_text(url)
        resolved = _locked_openadapt_versions(
            lock_text, set(unit.get("requirements", {}))
        )
    except (
        urllib.error.URLError,
        UnicodeDecodeError,
        TimeoutError,
        tomllib.TOMLDecodeError,
        ValueError,
    ) as exc:
        emit = report.error if require_network else report.warning
        emit(f"could not validate the Desktop sidecar lock at {source_ref}: {exc}")
        return
    actual_lock_sha256 = hashlib.sha256(lock_text.encode("utf-8")).hexdigest()
    if actual_lock_sha256 != unit.get("lock_sha256"):
        report.error(
            f"desktop_sidecar lock sha256 drift: manifest has "
            f"{unit.get('lock_sha256')!r} but {source_ref} has "
            f"{actual_lock_sha256!r}"
        )
    if resolved != unit.get("resolved"):
        report.error(
            f"desktop_sidecar closure drift: manifest has {unit.get('resolved')!r} "
            f"but {source_ref} uv.lock has {resolved!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "platform-manifest.json"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=REPORT_PATH,
        help="Generated report paired with --manifest.",
    )
    parser.add_argument(
        "--require-compatible",
        action="store_true",
        help=(
            "Fail unless the exact runner and sidecar closures satisfy the "
            "selected platform requirements. Use this at platform "
            "promotion, signing, and installer release gates."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip network checks against PyPI and status.json.",
    )
    parser.add_argument(
        "--strict-status",
        action="store_true",
        help="Treat status.json skew as a failure instead of a warning.",
    )
    parser.add_argument(
        "--require-network",
        action="store_true",
        help=(
            "Treat an unreachable PyPI as a failure instead of a warning. Use "
            "at release gates, where skipping the comparison is not "
            "acceptable."
        ),
    )
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"FATAL: {args.manifest} does not exist", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(args.manifest.read_text())
    except json.JSONDecodeError as exc:
        print(f"FATAL: {args.manifest} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    report = Report()
    check_structure(manifest, report)
    check_signature_honesty(manifest, report)
    check_against_pyproject(manifest, report)
    check_compatibility_status(manifest, report)
    check_generated_report(manifest, report, args.report)
    if (
        args.require_compatible
        and manifest.get("compatibility_status", {}).get("status")
        != "dependency-compatible"
    ):
        report.error(
            "the exact platform BOM is incompatible; resolve every "
            "compatibility_status failure before platform promotion"
        )
    if not args.offline:
        check_against_pypi(manifest, report, require_network=args.require_network)
        check_source_provenance(
            manifest, report, require_network=args.require_network
        )
        check_schema_sources(
            manifest, report, require_network=args.require_network
        )
        check_qualification_evidence(
            manifest, report, require_network=args.require_network
        )
        check_against_status(manifest, report, strict=args.strict_status)
        check_desktop_sidecar_lock(
            manifest, report, require_network=args.require_network
        )

    for warning in report.warnings:
        print(f"WARNING: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if report.errors:
        print(
            f"\nFAILED: {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s).",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: platform manifest validated "
        f"({'offline checks only' if args.offline else 'including published artifacts'}); "
        f"{len(report.warnings)} warning(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
