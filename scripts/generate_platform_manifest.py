#!/usr/bin/env python3
"""Generate the authoritative OpenAdapt platform release manifest.

This script produces ``platform-manifest.json`` at the repository root: one
machine-readable statement of which component versions constitute the current
OpenAdapt platform release, where their published artifacts live, and which
qualification evidence backs them.

Design rules (see docs/platform-manifest.md):

* Versions, artifact URLs, and sha256 digests are read from the REAL published
  sources (the PyPI JSON API). Nothing is invented or hand-typed. If a source
  is unreachable or disagrees with this repository's ``pyproject.toml``, the
  script fails loudly instead of guessing.
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
import datetime as _dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = "1.0.0"
MANIFEST_KIND = "openadapt-platform-release-manifest"
STATUS_URL = "https://openadapt.ai/status.json"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
HTTP_TIMEOUT_SECONDS = 30

# The platform components this manifest pins. The launcher (this repository)
# is the integration point; the others are the runtime, the native recorder,
# and the desktop shell. All four publish to PyPI.
COMPONENTS: dict[str, str] = {
    "launcher": "openadapt",
    "flow": "openadapt-flow",
    "capture": "openadapt-capture",
    "desktop": "openadapt-desktop",
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
        "repository": "OpenAdaptAI/openadapt-flow",
        "path": "public-demo/evidence-packs",
        "description": (
            "Versioned public evidence packs (for example mockmed-triage-v3) "
            "with per-pack manifest.json files: synthetic-patient replay "
            "evidence including fault-injection trials."
        ),
    },
    {
        "id": "flow-effectbench-task-pack",
        "kind": "benchmark-manifest",
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
    request = urllib.request.Request(
        url, headers={"User-Agent": "openadapt-platform-manifest-generator"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DriftError(
            f"FATAL: could not fetch {url}: {exc}. "
            "The manifest is generated only from live published sources; "
            "refusing to invent values."
        ) from exc


def _pypi_component(package: str) -> dict:
    doc = _fetch_json(PYPI_URL_TEMPLATE.format(package=package))
    version = doc["info"]["version"]
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


def _substrates_from_status(status: dict) -> list[dict]:
    substrates = []
    for entry in status.get("substrates", []):
        substrates.append(
            {
                "name": entry["name"],
                "public_label": entry["public_label"],
                "delivery": entry.get("delivery"),
            }
        )
    if not substrates:
        raise DriftError(
            f"FATAL: {STATUS_URL} contains no substrates; refusing to emit an "
            "empty driver table."
        )
    return substrates


def generate(allow_unreleased_launcher: bool) -> dict:
    status = _fetch_json(STATUS_URL)

    components = {
        role: _pypi_component(package) for role, package in COMPONENTS.items()
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

    release_channel = status.get("product_lifecycle", "").lower() or None
    if release_channel is None:
        raise DriftError(
            f"FATAL: {STATUS_URL} has no product_lifecycle; cannot determine "
            "the release channel."
        )

    return {
        "manifest_kind": MANIFEST_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "release_channel": release_channel,
        "components": components,
        "compatibility": _compatibility_from_pyproject(),
        "supported_os": SUPPORTED_OS,
        "substrate_drivers": _substrates_from_status(status),
        "qualification_evidence": QUALIFICATION_EVIDENCE,
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
        "--allow-unreleased-launcher",
        action="store_true",
        help=(
            "Permit pyproject.toml to be ahead of the latest PyPI launcher "
            "release. The published version is recorded regardless."
        ),
    )
    args = parser.parse_args()

    try:
        manifest = generate(args.allow_unreleased_launcher)
    except DriftError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {args.output}")
    for role, component in manifest["components"].items():
        print(f"  {role}: {component['package']}=={component['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
