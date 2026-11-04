#!/usr/bin/env python3
"""Fail closed unless the pinned Flow and agent releases match PyPI exactly.

A launcher release ships the exact openadapt-flow and openadapt-agent versions
selected by platform-manifest.json. Before any tag or publish, the release
workflow runs this script. For each of the two components it:

1. requires one exact stable version, the canonical package, PyPI as the
   source, and provenance that binds the GitHub repository, tag and commit;
2. requires exactly one wheel and one sdist on files.pythonhosted.org;
3. reads PyPI's JSON API for that exact version and requires PyPI's SHA-256
   digests to equal the manifest's, file for file;
4. downloads both files and requires their bytes to hash to the same digests.

Anything missing, extra, yanked or different fails the release. Publication
itself runs in the protected ``pypi`` GitHub environment with PyPI Trusted
Publishing, so no long-lived token or manual signature is involved.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_platform_versions import load_release_component_versions  # noqa: E402

COMPONENTS = {
    "flow": ("openadapt-flow", "openadapt_flow", "OpenAdaptAI/openadapt-flow"),
    "agent": ("openadapt-agent", "openadapt_agent", "OpenAdaptAI/openadapt-agent"),
}
# Releases from before the fold are tagged v<version> in the standalone
# repository above; later ones <package>-v<version> here, under packages/<dir>.
MONOREPO_REPOSITORY = "OpenAdaptAI/OpenAdapt"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Artifact:
    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Component:
    role: str
    package: str
    version: str
    tag: str
    source_commit: str
    artifacts: tuple[Artifact, ...]


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _artifact_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"artifact URL is invalid: {name}")
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
            f"artifact URL is outside the exact PyPI file boundary: {name}"
        )
    return value


def load_component(manifest_path: Path, launcher_version: str, role: str) -> Component:
    """Return the exact published component selected by the manifest."""

    package, dist, repository = COMPONENTS[role]
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("platform manifest path is missing or invalid")
    document = _object(
        json.loads(manifest_path.read_text(encoding="utf-8")), "manifest"
    )
    versions = load_release_component_versions(manifest_path, launcher_version)
    entry = _object(
        _object(document.get("components"), "components").get(role), f"{role} component"
    )
    version = versions[role]
    if STABLE_VERSION.fullmatch(version) is None or entry.get("version") != version:
        raise ValueError(f"{role} version is not one exact stable selected version")
    if entry.get("package") != package or entry.get("source") != "pypi":
        raise ValueError(f"{role} component is not the canonical published package")
    provenance = _object(entry.get("provenance"), f"{role} provenance")
    commit = provenance.get("commit")
    bound = (
        provenance.get("repository"),
        provenance.get("release_ref"),
        provenance.get("source_root", ""),
    )
    releases = {
        (MONOREPO_REPOSITORY, f"{package}-v{version}", f"packages/{role}"),
        (repository, f"v{version}", ""),
    }
    tag = str(provenance.get("release_ref"))
    if (
        bound not in releases
        or not isinstance(commit, str)
        or HEX40.fullmatch(commit) is None
    ):
        raise ValueError(
            f"{role} provenance does not bind the exact repository, tag, and commit"
        )
    expected = {
        "bdist_wheel": f"{dist}-{version}-py3-none-any.whl",
        "sdist": f"{dist}-{version}.tar.gz",
    }
    raw_artifacts = entry.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 2:
        raise ValueError(
            f"{role} must have exactly one wheel and one source distribution"
        )
    artifacts: list[Artifact] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_artifacts):
        item = _object(raw, f"{role} artifact {index}")
        if set(item) != {"type", "filename", "url", "sha256"}:
            raise ValueError(
                f"{role} artifact {index} fields differ from the manifest contract"
            )
        kind = item.get("type")
        if kind not in expected or kind in seen:
            raise ValueError(f"{role} artifact types must be one wheel and one sdist")
        seen.add(kind)
        name = item.get("filename")
        digest = item.get("sha256")
        if name != expected[kind]:
            raise ValueError(f"{role} artifact filename is invalid: {name}")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ValueError(f"{role} artifact digest is invalid: {name}")
        artifacts.append(Artifact(name, _artifact_url(item.get("url"), name), digest))
    return Component(
        role=role,
        package=package,
        version=version,
        tag=tag,
        source_commit=commit,
        artifacts=tuple(sorted(artifacts, key=lambda a: a.name)),
    )


def _download(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "OpenAdapt-launcher-release/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        final = urllib.parse.urlsplit(response.geturl())
        if (
            final.scheme != "https"
            or final.hostname != urllib.parse.urlsplit(url).hostname
        ):
            raise ValueError("download redirected outside its exact host")
        raw = response.read(MAX_ARTIFACT_BYTES + 1)
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("download is empty or exceeds the artifact size limit")
    return raw


def pypi_files(
    package: str, version: str, fetch: Callable[[str], bytes] = _download
) -> dict[str, dict]:
    """Return PyPI's file records for one exact version, keyed by filename."""

    document = _object(
        json.loads(fetch(f"https://pypi.org/pypi/{package}/{version}/json")),
        "PyPI response",
    )
    info = _object(document.get("info"), "PyPI info")
    if info.get("version") != version:
        raise ValueError(f"PyPI does not serve {package}=={version}")
    return {item["filename"]: item for item in document.get("urls") or []}


def verify_component(
    component: Component, fetch: Callable[[str], bytes] = _download
) -> None:
    published = pypi_files(component.package, component.version, fetch)
    for artifact in component.artifacts:
        record = published.get(artifact.name)
        if record is None:
            raise ValueError(f"PyPI has no file {artifact.name}")
        if record.get("yanked"):
            raise ValueError(f"PyPI file is yanked: {artifact.name}")
        if record.get("digests", {}).get("sha256") != artifact.sha256:
            raise ValueError(f"PyPI digest differs from the manifest: {artifact.name}")
        if hashlib.sha256(fetch(artifact.url)).hexdigest() != artifact.sha256:
            raise ValueError(
                f"downloaded bytes differ from the manifest: {artifact.name}"
            )


def _project_version(root: Path) -> str:
    # Python 3.10 has no tomllib; the [project] version line is enough here.
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project)
    if match is None:
        raise ValueError("pyproject.toml has no [project] version")
    return match.group(1)


def main(
    argv: list[str] | None = None, fetch: Callable[[str], bytes] = _download
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest = args.manifest or root / "platform-manifest.json"
    try:
        launcher_version = _project_version(root)
        for role in COMPONENTS:
            component = load_component(manifest, launcher_version, role)
            verify_component(component, fetch)
            print(
                f"OK: {component.package}=={component.version} ({component.tag}, "
                f"{component.source_commit[:12]}) matches PyPI and the manifest."
            )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
