"""Classify whether a Desktop change needs full native artifact builds.

Pull requests keep the representative Linux frozen-sidecar build. Release
candidates and explicit dispatches keep the complete platform matrix. A push
to protected ``main`` repeats the expensive matrix only when a dependency,
toolchain, packaging, or native-runtime input changed. The immutable native
release workflow always rebuilds every installer from its release tag.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FULL_ARTIFACT_FILES = {
    ".github/workflows/build.yml",
    ".github/workflows/native-freshness.yml",
    ".github/workflows/native-release.yml",
    ".github/workflows/release.yml",
    ".python-version",
    "LICENSE",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "rust-toolchain.toml",
    "uv.lock",
}

FULL_ARTIFACT_PREFIXES = (
    ".github/actions/",
    # build_frozen_engine.py freezes engine/__main__.py and its local imports
    # into the sidecar that every native installer embeds.
    "engine/",
    "src-tauri/",
)

FULL_ARTIFACT_SCRIPTS = {
    "scripts/alias_lowercase_x11_sonames.py",
    "scripts/build_frozen_engine.py",
    "scripts/check_release_consistency.py",
    "scripts/classify_build_changes.py",
    "scripts/frozen_notices.py",
    "scripts/native_release.py",
    "scripts/native_signing.py",
    "scripts/package_ffmpeg_runtime.py",
    "scripts/smoke_test_frozen_flow.py",
    "scripts/smoke_test_native_installer.py",
    "scripts/sync_frozen_dependencies.py",
    "scripts/verify_build_artifact.py",
}

# These paths get the normal PR checks and the exact-main frontend, Python
# distribution, and cross-platform test jobs. They do not change the native
# packaging contract. Everything not named here fails toward the full matrix.
CHEAP_MAIN_PREFIXES = (
    "docs/",
    "src/",
    "tests/",
)

CHEAP_MAIN_FILES = {
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
}


@dataclass(frozen=True)
class BuildScope:
    run_sidecar: bool
    run_native: bool
    reason: str


def _normalize(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value:
        return ""
    normalized = PurePosixPath(value).as_posix()
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise ValueError(f"changed path must be repository-relative: {path!r}")
    return normalized.removeprefix("./")


def _requires_full_artifacts(path: str) -> bool:
    if path in FULL_ARTIFACT_FILES:
        return True
    if path in FULL_ARTIFACT_SCRIPTS:
        return True
    if path.startswith(FULL_ARTIFACT_PREFIXES):
        return True
    if path.startswith("THIRD_PARTY_NOTICES"):
        return True
    if path in CHEAP_MAIN_FILES or path.startswith(CHEAP_MAIN_PREFIXES):
        return False
    return True


def classify_build_scope(event: str, changed_paths: Iterable[str]) -> BuildScope:
    """Return the artifact scope for one GitHub event."""

    if event == "workflow_dispatch":
        return BuildScope(True, True, "explicit release-candidate build")
    if event == "pull_request":
        return BuildScope(True, False, "representative pull-request sidecar")
    if event != "push":
        return BuildScope(True, True, f"unknown event {event!r}; fail toward full evidence")

    paths = tuple(path for raw in changed_paths if (path := _normalize(raw)))
    if not paths:
        return BuildScope(True, True, "no changed paths; fail toward full evidence")

    full_paths = tuple(path for path in paths if _requires_full_artifacts(path))
    if full_paths:
        sample = ", ".join(full_paths[:3])
        if len(full_paths) > 3:
            sample += f", and {len(full_paths) - 3} more"
        return BuildScope(True, True, f"artifact-sensitive change: {sample}")

    return BuildScope(False, False, "application-only change; release tag rebuilds all artifacts")


def _write_github_output(path: Path, scope: BuildScope) -> None:
    reason = scope.reason.replace("\n", " ").replace("\r", " ")
    with path.open("a", encoding="utf-8") as output:
        output.write(f"run_sidecar={str(scope.run_sidecar).lower()}\n")
        output.write(f"run_native={str(scope.run_native).lower()}\n")
        output.write(f"reason={reason}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    paths: list[str] = []
    if args.paths_file:
        paths = args.paths_file.read_text(encoding="utf-8").splitlines()
    scope = classify_build_scope(args.event, paths)
    if args.github_output:
        _write_github_output(args.github_output, scope)
    print(f"sidecar={str(scope.run_sidecar).lower()}")
    print(f"native={str(scope.run_native).lower()}")
    print(f"reason={scope.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
