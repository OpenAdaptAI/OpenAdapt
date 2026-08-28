#!/usr/bin/env python3
"""Stage the exact Flow package selected by one launcher release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from release_platform_versions import load_release_component_versions

CENTRAL_API_REF = "https://api.github.com/repos/OpenAdaptAI/.github/git/ref/heads/main"
CENTRAL_RAW_ROOT = "https://raw.githubusercontent.com/OpenAdaptAI/.github"
CENTRAL_REPOSITORY = "OpenAdaptAI/.github"
CENTRAL_REPOSITORY_ID = "858454062"
CENTRAL_REPOSITORY_OWNER_ID = "132681217"
REFERENCE_SCHEMA = "openadapt.production-evidence-object-reference/v2"
REGISTRY_SCHEMA = "openadapt.production-evidence-registry/v2"
FLOW_REPOSITORY = "OpenAdaptAI/openadapt-flow"
FLOW_REPOSITORY_ID = "1291376938"
FLOW_CLAIM_SCOPE = "production_flow"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
ARTIFACT_PROFILES = {
    "sdist": ("python-sdist", "application/gzip", ".tar.gz"),
    "bdist_wheel": ("python-wheel", "application/zip", ".whl"),
}
REGISTRY_ENTRY_FIELDS = {
    "registry_entry_sha256",
    "kind",
    "object_media_type",
    "object_path",
    "object_schema_version",
    "object_sha256",
    "semantic_identity_sha256",
    "size_bytes",
    "subject_sha256",
}


@dataclass(frozen=True)
class FlowArtifact:
    name: str
    kind: str
    url: str
    sha256: str
    media_type: str


@dataclass(frozen=True)
class FlowCandidate:
    version: str
    tag: str
    source_commit: str
    artifacts: tuple[FlowArtifact, ...]


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("platform manifest path is missing or invalid")
    try:
        return _object(
            json.loads(path.read_text(encoding="utf-8")), "platform manifest"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the platform manifest: {exc}") from exc


def _artifact_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Flow artifact URL is invalid: {name}")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or Path(urllib.parse.unquote(parsed.path)).name != name
    ):
        raise ValueError(
            f"Flow artifact URL is outside the exact PyPI file boundary: {name}"
        )
    return value


def load_flow_candidate(manifest_path: Path, launcher_version: str) -> FlowCandidate:
    """Return the exact published Flow candidate selected by the manifest."""

    document = _load_manifest(manifest_path)
    versions = load_release_component_versions(manifest_path, launcher_version)
    flow = _object(
        _object(document.get("components"), "platform components").get("flow"),
        "Flow component",
    )
    version = versions["flow"]
    if STABLE_VERSION.fullmatch(version) is None or flow.get("version") != version:
        raise ValueError("Flow version is not one exact stable selected version")
    if flow.get("package") != "openadapt-flow" or flow.get("source") != "pypi":
        raise ValueError("Flow component is not the canonical published package")

    provenance = _object(flow.get("provenance"), "Flow provenance")
    tag = f"v{version}"
    source_commit = provenance.get("commit")
    if (
        provenance.get("repository") != FLOW_REPOSITORY
        or provenance.get("release_ref") != tag
        or not isinstance(source_commit, str)
        or HEX40.fullmatch(source_commit) is None
    ):
        raise ValueError(
            "Flow provenance does not bind the exact repository, tag, and commit"
        )

    artifact_values = flow.get("artifacts")
    if not isinstance(artifact_values, list) or len(artifact_values) != 2:
        raise ValueError("Flow must have exactly one wheel and one source distribution")
    artifacts: list[FlowArtifact] = []
    observed_types: set[str] = set()
    for index, raw in enumerate(artifact_values):
        artifact = _object(raw, f"Flow artifact {index}")
        if set(artifact) != {"type", "filename", "url", "sha256"}:
            raise ValueError(
                f"Flow artifact {index} fields differ from the published manifest contract"
            )
        artifact_type = artifact.get("type")
        if artifact_type not in ARTIFACT_PROFILES or artifact_type in observed_types:
            raise ValueError(
                "Flow artifact types must be one wheel and one source distribution"
            )
        observed_types.add(artifact_type)
        kind, media_type, suffix = ARTIFACT_PROFILES[artifact_type]
        name = artifact.get("filename")
        digest = artifact.get("sha256")
        expected_name = (
            f"openadapt_flow-{version}.tar.gz"
            if artifact_type == "sdist"
            else f"openadapt_flow-{version}-py3-none-any.whl"
        )
        if (
            not isinstance(name, str)
            or name != Path(name).name
            or name != expected_name
            or not name.endswith(suffix)
        ):
            raise ValueError(f"Flow artifact filename is invalid: {name}")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ValueError(f"Flow artifact digest is invalid: {name}")
        artifacts.append(
            FlowArtifact(
                name=name,
                kind=kind,
                url=_artifact_url(artifact.get("url"), name),
                sha256=digest,
                media_type=media_type,
            )
        )
    if observed_types != set(ARTIFACT_PROFILES):
        raise ValueError(
            "Flow artifact types must be one wheel and one source distribution"
        )
    artifacts.sort(key=lambda item: (item.kind, item.name, item.sha256))
    return FlowCandidate(
        version=version,
        tag=tag,
        source_commit=source_commit,
        artifacts=tuple(artifacts),
    )


def _download(url: str, token: str | None = None) -> bytes:
    headers = {"User-Agent": "OpenAdapt-launcher-release/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        final = urllib.parse.urlsplit(response.geturl())
        requested = urllib.parse.urlsplit(url)
        if final.scheme != "https" or final.hostname != requested.hostname:
            raise ValueError("download redirected outside its exact host")
        raw = response.read(MAX_ARTIFACT_BYTES + 1)
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("download is empty or exceeds the artifact size limit")
    return raw


def stage_artifacts(
    candidate: FlowCandidate,
    artifact_root: Path,
    *,
    fetch_bytes: Callable[[str], bytes] = _download,
) -> dict[str, Any]:
    """Write exact candidate bytes and return the central artifact inventory."""

    if artifact_root.is_symlink():
        raise ValueError("artifact root cannot be a symlink")
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not artifact_root.is_dir() or any(artifact_root.iterdir()):
        raise ValueError("artifact root must be an empty directory")

    inventory_artifacts: list[dict[str, Any]] = []
    for artifact in candidate.artifacts:
        raw = fetch_bytes(artifact.url)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != artifact.sha256:
            raise ValueError(
                f"Flow artifact bytes differ from the manifest: {artifact.name}"
            )
        target = artifact_root / artifact.name
        with target.open("xb") as handle:
            handle.write(raw)
        inventory_artifacts.append(
            {
                "name": artifact.name,
                "kind": artifact.kind,
                "sha256": f"sha256:{actual}",
                "size_bytes": len(raw),
                "media_type": artifact.media_type,
                "publish_destinations": ["github-release", "pypi"],
            }
        )
    return {
        "schema_version": "openadapt.production-release-artifact-inventory/v1",
        "target": "flow",
        "claim_scope": FLOW_CLAIM_SCOPE,
        "artifacts": inventory_artifacts,
    }


def select_release_admission_reference(
    registry: dict[str, Any], source_commit: str
) -> dict[str, Any]:
    """Select the newest registered release admission from protected central main."""

    required = {
        "$schema",
        "schema_version",
        "repository",
        "repository_id",
        "repository_owner_id",
        "revision",
        "previous_registry_head_sha256",
        "registry_head_sha256",
        "signer_registry",
        "signer_registry_history",
        "entries",
    }
    if set(registry) != required:
        raise ValueError("central evidence registry fields differ")
    revision = registry.get("revision")
    head = registry.get("registry_head_sha256")
    entries = registry.get("entries")
    if (
        registry.get("schema_version") != REGISTRY_SCHEMA
        or registry.get("repository") != CENTRAL_REPOSITORY
        or registry.get("repository_id") != CENTRAL_REPOSITORY_ID
        or registry.get("repository_owner_id") != CENTRAL_REPOSITORY_OWNER_ID
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(head, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", head) is None
        or not isinstance(entries, list)
        or HEX40.fullmatch(source_commit) is None
    ):
        raise ValueError("central evidence registry identity is invalid")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("kind") == "qualification-release"
    ]
    if not matches:
        raise ValueError(
            "central protected main has no registered Flow release admission"
        )
    entry = matches[-1]
    if set(entry) != REGISTRY_ENTRY_FIELDS:
        raise ValueError("central release admission registry entry fields differ")
    return {
        "schema_version": REFERENCE_SCHEMA,
        "repository": CENTRAL_REPOSITORY,
        "repository_id": CENTRAL_REPOSITORY_ID,
        "repository_owner_id": CENTRAL_REPOSITORY_OWNER_ID,
        "registry_source_commit": source_commit,
        "registry_revision": revision,
        "registry_head_sha256": head,
        **entry,
    }


def load_current_admission_reference(token: str) -> tuple[str, dict[str, Any]]:
    ref_raw = _download(CENTRAL_API_REF, token)
    try:
        commit = _object(json.loads(ref_raw), "central main response")["object"]["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("central main response is invalid") from exc
    if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
        raise ValueError("central main response does not contain one exact commit")
    registry_raw = _download(f"{CENTRAL_RAW_ROOT}/{commit}/evidence-registry.json")
    try:
        registry = _object(json.loads(registry_raw), "central evidence registry")
    except json.JSONDecodeError as exc:
        raise ValueError("central evidence registry is not JSON") from exc
    return commit, select_release_admission_reference(registry, commit)


def _write_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"GitHub output contains a line break: {key}")
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--launcher-version", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        parser.exit(1, "GH_TOKEN is required\n")
    try:
        candidate = load_flow_candidate(args.manifest, args.launcher_version)
        inventory = stage_artifacts(candidate, args.artifact_root)
        registry_commit, reference = load_current_admission_reference(token)
        _write_github_outputs(
            args.github_output,
            {
                "admission_reference_json": json.dumps(
                    reference, sort_keys=True, separators=(",", ":")
                ),
                "artifact_inventory_json": json.dumps(
                    inventory, sort_keys=True, separators=(",", ":")
                ),
                "flow_source_commit": candidate.source_commit,
                "flow_version": candidate.version,
                "flow_tag": candidate.tag,
                "central_registry_source_commit": registry_commit,
            },
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(
        "OK: staged the exact selected Flow artifacts and current admission "
        f"reference for {candidate.tag}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
