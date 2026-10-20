#!/usr/bin/env python3
"""Prepare and verify unadmitted OpenAdapt Desktop release-candidate assets.

In the OpenAdapt monorepo, ``.github/workflows/desktop-release.yml`` publishes
one immutable ``openadapt-desktop-vX.Y.Z`` prerelease per version. It carries
the Python wheel and sdist (also published to PyPI), the native installers,
the integrity documents, and its own attested ``openadapt-desktop-channel.json``
(schema v3). Everything is attached to a draft that is published once, because
OpenAdapt's releases are immutable; if any build fails, nothing is published.
Consumers pick the newest valid ``openadapt-desktop-v*`` release; each channel
names its predecessor's channel by hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE = "Candidate"
SURFACE = "installed desktop pairing and authoring companion"
# One tag per Desktop version, shared by PyPI and the installers. ``v*`` belongs
# to the launcher, and the release-authority policy allows one
# ``openadapt-<package>-v*`` namespace per package.
NATIVE_TAG_PREFIX = "openadapt-desktop-v"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

ARTIFACT_RULES = {
    "macos": (("dmg", "*.dmg", ".dmg"),),
    "windows": (
        ("msi", "*.msi", ".msi"),
        ("nsis", "*-setup.exe", "-nsis-setup.exe"),
    ),
    "linux": (
        ("deb", "*.deb", ".deb"),
        ("appimage", "*.AppImage", ".AppImage"),
    ),
}
SIGNING_MODES = {
    "macos": {"adhoc", "developer-id-notarized"},
    "windows": {"unsigned", "authenticode"},
    "linux": {"unsigned", "github-attested"},
}
PRODUCTION_TRUST_MODES = {
    "macos": "developer-id-notarized",
    "windows": "authenticode",
    # Linux has no one native trust format shared by DEB and AppImage. The
    # production boundary is the GitHub OIDC attestation over the exact bytes.
    "linux": "github-attested",
}
EXPECTED_PLATFORMS = {
    ("macos", "arm64"),
    ("macos", "x86_64"),
    ("windows", "x86_64"),
    ("linux", "x86_64"),
}
WEBSITE_RELEASE_MANIFEST = "openadapt-desktop-release-manifest.json"
NATIVE_RELEASE_PROVENANCE = "openadapt-desktop-native-release-provenance.json"
NATIVE_RELEASE_PROVENANCE_SCHEMA = "openadapt.native-release-provenance/v3"
# The workflow lives at the root of the OpenAdapt monorepo, so attested workflow
# paths name the root file.
NATIVE_RELEASE_WORKFLOW = ".github/workflows/desktop-release.yml"
NATIVE_RELEASE_WORKFLOW_NAME = "Desktop release"
# Releases start from a successful Packages run on main (workflow_run) or a
# dispatch; "push" stays valid for the releases made before that.
NATIVE_RELEASE_EVENTS = frozenset({"push", "workflow_dispatch", "workflow_run"})
DEFAULT_REPOSITORY = "OpenAdaptAI/OpenAdapt"
VERIFIED_RELEASE_CHANNEL = "openadapt-desktop-channel.json"
NATIVE_RELEASE_CHANNEL_SCHEMA = "openadapt.desktop-release-channel/v3"
NATIVE_RELEASE_VERIFIER = "verify-openadapt-native-release.py"
GITHUB_WORKFLOW_BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"
GITHUB_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
# What each permitted signing mode means for the person downloading. The
# website manifest states the modes this exact release used; it never claims a
# trust mode the bytes do not carry.
NATIVE_TRUST_STATEMENTS = {
    ("macos", "developer-id-notarized"): "Developer ID, notarization, and stapled ticket",
    ("macos", "adhoc"): "ad-hoc signature only; macOS asks for confirmation on first open",
    ("windows", "authenticode"): (
        "timestamped Authenticode signatures (SignPath) on the MSI and every program "
        "file in it, and on the NSIS setup; the files the NSIS setup installs are not "
        "signed"
    ),
    ("windows", "unsigned"): "unsigned; Windows SmartScreen warns before install",
    ("linux", "github-attested"): "GitHub OIDC attestation over exact DEB and AppImage bytes",
}


def website_release_verification(signing: dict[str, str]) -> dict[str, str]:
    """Return the manifest's verification contract for these signing modes."""

    if set(signing) != {platform for platform, _ in EXPECTED_PLATFORMS}:
        raise ValueError(f"signing modes must name every platform: {signing}")
    contract = {
        "sha256_manifest": "SHA256SUMS",
        "github_artifact_attestation": "required",
        "installer_smoke": "install, launch, and uninstall",
    }
    for platform, key in (
        ("macos", "macos_native_trust"),
        ("windows", "windows_native_trust"),
        ("linux", "linux_byte_trust"),
    ):
        statement = NATIVE_TRUST_STATEMENTS.get((platform, signing[platform]))
        if statement is None:
            raise ValueError(f"{platform} signing mode {signing[platform]!r} is not releasable")
        contract[key] = statement
    return contract


# The production-grade contract. Production admission (production_release.py)
# still requires it; candidate releases state their actual modes instead.
WEBSITE_RELEASE_VERIFICATION = website_release_verification(PRODUCTION_TRUST_MODES)
WEBSITE_RELEASE_SBOM_FORMAT = "CycloneDX"


def python_distribution_names(version: str) -> set[str]:
    """Return the wheel and sdist that ship in the release and on PyPI."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid release version: {version!r}")
    return {
        f"openadapt_desktop-{version}-py3-none-any.whl",
        f"openadapt_desktop-{version}.tar.gz",
    }


def sbom_name(version: str) -> str:
    return f"OpenAdapt-Desktop-v{version}.cyclonedx.json"


def expected_release_asset_names(version: str, signing: dict[str, str] | None = None) -> set[str]:
    """Return the complete checksummed asset contract for one native version.

    ``signing`` maps each platform to the mode the release used; it defaults to
    the production trust modes.
    """

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid native release version: {version!r}")
    modes = PRODUCTION_TRUST_MODES if signing is None else signing
    for platform, mode in modes.items():
        if mode not in SIGNING_MODES.get(platform, set()):
            raise ValueError(f"invalid {platform} signing mode: {mode!r}")
    names = {
        WEBSITE_RELEASE_MANIFEST,
        NATIVE_RELEASE_PROVENANCE,
        NATIVE_RELEASE_VERIFIER,
        sbom_name(version),
        *python_distribution_names(version),
    }
    for platform, architecture in EXPECTED_PLATFORMS:
        signing_mode = modes[platform]
        prefix = f"OpenAdapt-Desktop-Candidate-v{version}-{platform}-{architecture}-{signing_mode}"
        names.add(f"{prefix}-metadata.json")
        names.update(f"{prefix}{suffix}" for _kind, _pattern, suffix in ARTIFACT_RULES[platform])
    return names


def native_versions(root: Path = ROOT) -> dict[str, str]:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads((root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    cargo_lock = tomllib.loads((root / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8"))
    cargo_lock_packages = [
        item
        for item in cargo_lock.get("package", [])
        if isinstance(item, dict) and item.get("name") == "openadapt-desktop"
    ]
    if len(cargo_lock_packages) != 1:
        raise ValueError("Cargo.lock must contain exactly one openadapt-desktop package")
    return {
        "package.json": package["version"],
        "package-lock.json": package_lock["version"],
        'package-lock.json packages[""]': package_lock["packages"][""]["version"],
        "src-tauri/tauri.conf.json": tauri["version"],
        "src-tauri/Cargo.toml": cargo["package"]["version"],
        "src-tauri/Cargo.lock": cargo_lock_packages[0]["version"],
    }


def native_version(root: Path = ROOT) -> str:
    versions = native_versions(root)
    unique = set(versions.values())
    if len(unique) != 1:
        raise ValueError(f"native versions differ: {versions}")
    return unique.pop()


def validate_tag(tag: str, root: Path = ROOT) -> str:
    expected = f"{NATIVE_TAG_PREFIX}{native_version(root)}"
    if tag != expected:
        raise ValueError(f"native release tag must be exactly {expected!r}, got {tag!r}")
    return expected


def native_tag_tuple(tag: str) -> tuple[int, int, int]:
    if not tag.startswith(NATIVE_TAG_PREFIX):
        raise ValueError(f"not a native release tag: {tag!r}")
    version = tag[len(NATIVE_TAG_PREFIX) :]
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"native release tag must be {NATIVE_TAG_PREFIX}X.Y.Z, got {tag!r}")
    major, minor, patch = version.split(".")
    return (int(major), int(minor), int(patch))


def native_release_tags(ls_remote_output: str) -> list[str]:
    """Return every ``openadapt-desktop-v*`` tag in ``git ls-remote --tags`` output.

    The Git tag namespace is immutable release order. A release ``draft`` flag,
    ``prerelease`` flag, and ``body`` are all mutable, and this repository
    rewrites release notes itself, so a comparison set filtered on those fields
    can silently lose a member and admit a lower version.

    Every line must be one object id and one ``refs/tags/`` ref. A malformed
    line, or an ``openadapt-desktop-v`` tag that is not ``X.Y.Z``, fails closed.
    """

    tags: set[str] = set()
    for line in ls_remote_output.splitlines():
        if not line.strip():
            continue
        object_id, separator, ref = line.partition("\t")
        if not separator or not GIT_COMMIT_PATTERN.fullmatch(object_id):
            raise ValueError(f"git ls-remote output has an invalid line: {line!r}")
        if not ref.startswith("refs/tags/"):
            raise ValueError(f"git ls-remote output is not restricted to tags: {ref!r}")
        name = ref[len("refs/tags/") :].removesuffix("^{}")
        if not name.startswith(NATIVE_TAG_PREFIX):
            continue
        native_tag_tuple(name)
        tags.add(name)
    return sorted(tags, key=native_tag_tuple)


def validate_native_tag_order(candidate_tag: str, tags: object) -> str:
    """Refuse a candidate that a higher immutable native tag already leads.

    A re-run may present a tag that already exists, because the tag write is
    idempotent. That is still monotonic. Only a tag below the highest existing
    native tag moves the release line backwards.
    """

    candidate = native_tag_tuple(candidate_tag)
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("native tag comparison set must be a list of tag names")
    if not tags:
        return candidate_tag
    highest_tag = max(tags, key=native_tag_tuple)
    if candidate < native_tag_tuple(highest_tag):
        raise ValueError(
            f"native release {candidate_tag} is below existing native tag {highest_tag}"
        )
    return candidate_tag


def set_native_version(version: str, root: Path = ROOT) -> dict[str, str]:
    """Synchronize every native version source (and lockfiles) to ``version``.

    This transformation must be byte-deterministic on every platform, because
    :func:`validate_git_version_transform` reconstructs it and compares the
    result with the Git blobs of the candidate tag. Text-mode I/O would break
    that: on Windows it rewrites every ``\\n`` as ``\\r\\n``, and an omitted
    encoding decodes with the locale default. All reads and writes below are
    therefore explicit UTF-8 bytes with LF endings. The Cargo patterns also
    accept CRLF, because Git on Windows checks files out with CRLF by default,
    and they keep whatever ending each line already has.
    """

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"native version must be X.Y.Z, got {version!r}")

    def read_source(path: Path) -> str:
        return path.read_bytes().decode("utf-8")

    def write_source(path: Path, text: str) -> None:
        path.write_bytes(text.encode("utf-8"))

    def rewrite_json(path: Path, mutate) -> None:
        data = json.loads(read_source(path))
        mutate(data)
        write_source(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def set_lock_versions(lock: dict) -> None:
        lock["version"] = version
        lock["packages"][""]["version"] = version

    rewrite_json(root / "package.json", lambda data: data.__setitem__("version", version))
    rewrite_json(root / "package-lock.json", set_lock_versions)
    rewrite_json(
        root / "src-tauri" / "tauri.conf.json",
        lambda data: data.__setitem__("version", version),
    )

    cargo_toml = root / "src-tauri" / "Cargo.toml"
    text, replaced = re.subn(
        r'(?m)^version = "[^"]+"(?=\r?$)',
        f'version = "{version}"',
        read_source(cargo_toml),
        count=1,
    )
    if replaced != 1:
        raise ValueError(f"could not rewrite package version in {cargo_toml}")
    write_source(cargo_toml, text)

    cargo_lock = root / "src-tauri" / "Cargo.lock"
    text, replaced = re.subn(
        r'(name = "openadapt-desktop"\r?\nversion = ")[^"]+(")',
        rf"\g<1>{version}\g<2>",
        read_source(cargo_lock),
        count=1,
    )
    if replaced != 1:
        raise ValueError(f"could not rewrite package version in {cargo_lock}")
    write_source(cargo_lock, text)

    synchronized = native_version(root)
    if synchronized != version:
        raise ValueError(f"native version sources disagree after sync: {native_versions(root)}")
    return native_versions(root)


VERSION_TRANSFORM_PATHS = (
    "package.json",
    "package-lock.json",
    "src-tauri/Cargo.toml",
    "src-tauri/Cargo.lock",
    "src-tauri/tauri.conf.json",
)


def _require_existing_file(path: Path, *, label: str) -> Path:
    """Require a named prior document to be present and regular.

    A caller passes ``--existing`` to demand a monotonicity check. Treating an
    absent file as "no prior document" would delete that check exactly when a
    download failed, so a missing path is an error, never a silent skip.
    """

    if not path.is_file() or path.is_symlink():
        raise ValueError(f"prior {label} {str(path)!r} is missing or is not a regular file")
    return path


def _resolve_commit(root: Path, ref: str) -> str:
    """Resolve one caller ref to its 40-character commit id.

    Every later Git call receives that object id rather than the caller string.
    A hexadecimal object id can never begin with ``-``, so a ref such as
    ``--output=/tmp/stolen`` cannot reach Git as an option.
    """

    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise ValueError(f"invalid Git ref: {ref!r}")
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"Git ref {ref!r} does not name exactly one commit")
    return commit


def _git_bytes(root: Path, commit: str, relative_path: str) -> bytes:
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"expected a resolved commit id, got {commit!r}")
    result = subprocess.run(
        # "./" resolves the path from cwd=root, so this works whether root is
        # the repository root or packages/desktop inside the OpenAdapt monorepo.
        ["git", "show", f"{commit}:./{relative_path}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Git commit {commit!r} does not contain {relative_path!r}")
    return result.stdout


def native_version_at_ref(ref: str, root: Path = ROOT) -> str:
    """Return the one native version that ``ref`` records.

    This reads Git objects, so a caller can ask about a commit that is not
    checked out. The three sources must agree, exactly as they must in a
    working tree.
    """

    commit = _resolve_commit(root, ref)
    package = json.loads(_git_bytes(root, commit, "package.json"))
    tauri = json.loads(_git_bytes(root, commit, "src-tauri/tauri.conf.json"))
    cargo = tomllib.loads(_git_bytes(root, commit, "src-tauri/Cargo.toml").decode("utf-8"))
    observed = {
        str(package.get("version") or ""),
        str(tauri.get("version") or ""),
        str(cargo.get("package", {}).get("version") or ""),
    }
    if len(observed) != 1:
        raise ValueError(f"native versions differ at {ref}: {sorted(observed)}")
    version = observed.pop()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"native version at {ref} is invalid: {version!r}")
    return version


def validate_git_version_transform(
    base_ref: str,
    candidate_ref: str,
    version: str,
    *,
    root: Path = ROOT,
) -> int:
    """Require ``candidate_ref`` to equal the exact set-version result.

    A filename allowlist is not sufficient here. The five version files also
    contain executable package scripts, Rust dependencies, and Tauri build
    configuration. This function reconstructs the deterministic transformation
    from ``base_ref`` and compares every resulting byte with ``candidate_ref``.
    """

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"native version must be X.Y.Z, got {version!r}")
    base_commit = _resolve_commit(root, base_ref)
    candidate_commit = _resolve_commit(root, candidate_ref)
    changed = subprocess.run(
        # --relative: paths from root, limited to it (packages/desktop in the monorepo).
        ["git", "diff", "--relative", "--name-only", f"{base_commit}..{candidate_commit}", "--"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if changed.returncode != 0:
        raise ValueError("could not compare engine and native tag trees")
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    unexpected = changed_paths.difference(VERSION_TRANSFORM_PATHS)
    if unexpected:
        raise ValueError(
            "native tag contains changes outside the version transformation: "
            + ", ".join(sorted(unexpected))
        )

    with tempfile.TemporaryDirectory(prefix="openadapt-native-version-") as temporary:
        reconstructed = Path(temporary)
        for relative in VERSION_TRANSFORM_PATHS:
            destination = reconstructed / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_git_bytes(root, base_commit, relative))
        set_native_version(version, reconstructed)
        mismatches = [
            relative
            for relative in VERSION_TRANSFORM_PATHS
            if (reconstructed / relative).read_bytes()
            != _git_bytes(root, candidate_commit, relative)
        ]
    if mismatches:
        raise ValueError(
            "native tag differs from the exact deterministic set-version output: "
            + ", ".join(mismatches)
        )
    return len(VERSION_TRANSFORM_PATHS)


def validate_git_version_advance(
    base_ref: str,
    candidate_ref: str,
    version: str,
    *,
    root: Path = ROOT,
) -> int:
    """Require an exact version transform that strictly advances its base.

    A version pull request is safe to merge only against the current protected
    base that it was built from. A transform from an older engine commit can
    otherwise overwrite newer native versions when GitHub merges it later.
    """

    base_version = native_version_at_ref(base_ref, root=root)
    if native_tag_tuple(f"{NATIVE_TAG_PREFIX}{version}") <= native_tag_tuple(
        f"{NATIVE_TAG_PREFIX}{base_version}"
    ):
        raise ValueError(f"native version {version} does not advance protected base {base_version}")
    return validate_git_version_transform(base_ref, candidate_ref, version, root=root)


def sync_native_version_from_engine(root: Path = ROOT) -> dict[str, str]:
    """Set every native version source from the Python engine version."""

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid engine version in pyproject.toml: {version!r}")
    return set_native_version(version, root)


def _single_match(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(path for path in directory.rglob(pattern) if path.is_file())
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label} artifact under {directory}: {matches}")
    if matches[0].stat().st_size == 0:
        raise ValueError(f"empty {label} artifact: {matches[0]}")
    return matches[0]


def stage_artifacts(
    *,
    bundle_root: Path,
    output: Path,
    platform: str,
    architecture: str,
    signing: str,
    root: Path = ROOT,
) -> list[Path]:
    if platform not in ARTIFACT_RULES:
        raise ValueError(f"unsupported platform: {platform}")
    if signing not in SIGNING_MODES[platform]:
        raise ValueError(f"invalid signing mode {signing!r} for {platform}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to stage into non-empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)

    version = native_version(root)
    prefix = f"OpenAdapt-Desktop-Candidate-v{version}-{platform}-{architecture}-{signing}"
    staged: list[Path] = []
    artifact_names: list[str] = []
    for kind, pattern, suffix in ARTIFACT_RULES[platform]:
        source = _single_match(bundle_root, pattern, kind)
        destination = output / f"{prefix}{suffix}"
        shutil.copy2(source, destination)
        staged.append(destination)
        artifact_names.append(destination.name)

    metadata = {
        "schema_version": 1,
        "lifecycle": LIFECYCLE,
        "surface": SURFACE,
        "native_version": version,
        "platform": platform,
        "architecture": architecture,
        "signing": signing,
        "source_commit": os.environ.get("GITHUB_SHA", "local"),
        "artifacts": artifact_names,
        "verification_scope": (
            "cross-platform install/uninstall, self-contained Flow runtime, "
            "browser provision, and protocol-handler packaging"
        ),
        "limitations": [
            (
                "The first browser workflow downloads the Chromium revision pinned by the "
                "bundled Playwright runtime unless PLAYWRIGHT_BROWSERS_PATH points at an "
                "approved offline prebundle."
            ),
            "Installer verification does not replace qualification of a complete real workflow.",
        ],
    }
    metadata_path = output / f"{prefix}-metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    staged.append(metadata_path)
    return staged


def write_checksums(directory: Path, output: Path) -> list[tuple[str, str]]:
    if output.parent.resolve() != directory.resolve():
        raise ValueError("checksum manifest must be written inside the artifact directory")
    members = [path for path in directory.iterdir() if path.resolve() != output.resolve()]
    invalid = [path for path in members if not path.is_file() or path.is_symlink()]
    if invalid:
        raise ValueError(f"release directory contains a non-regular file: {invalid}")
    files = sorted(members)
    if not files:
        raise ValueError(f"no release assets found in {directory}")

    entries: list[tuple[str, str]] = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, path.name))
    output.write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")
    return entries


def verify_checksums(directory: Path, manifest: Path) -> int:
    if manifest.parent.resolve() != directory.resolve() or manifest.name != "SHA256SUMS":
        raise ValueError("SHA256SUMS must be inside the release asset directory")
    entries = read_checksums(manifest)
    members = [path for path in directory.iterdir() if path.resolve() != manifest.resolve()]
    invalid = [path for path in members if not path.is_file() or path.is_symlink()]
    if invalid:
        raise ValueError(f"release directory contains a non-regular file: {invalid}")
    actual_names = {path.name for path in members}
    if actual_names != set(entries):
        raise ValueError(
            "SHA256SUMS does not describe the exact release file set: "
            f"actual={sorted(actual_names)}, checksummed={sorted(entries)}"
        )
    checked = 0
    for name, digest in entries.items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"checksum target is missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"checksum mismatch for {name}: expected {digest}, got {actual}")
        checked += 1
    if checked == 0:
        raise ValueError("checksum manifest is empty")
    return checked


def read_checksums(manifest: Path) -> dict[str, str]:
    """Read a strict SHA256SUMS file and reject duplicate asset names."""

    entries: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or Path(name).name != name
            or not name
        ):
            raise ValueError(f"invalid checksum line: {line!r}")
        if name in entries:
            raise ValueError(f"duplicate checksum target: {name}")
        entries[name] = digest
    if not entries:
        raise ValueError("checksum manifest is empty")
    return entries


def _validate_repository(repository: str) -> str:
    if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError(f"invalid GitHub repository: {repository!r}")
    return repository


def _validate_commit(commit: str) -> str:
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"invalid Git commit: {commit!r}")
    return commit


def write_release_provenance(
    output: Path,
    *,
    repository: str,
    tag: str,
    source_commit: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: int,
    run_attempt: int,
    runner_environment: str,
    event: str,
    root: Path = ROOT,
) -> Path:
    """Write the build identity that the signed subject inventory must bind."""

    _validate_repository(repository)
    validate_tag(tag, root)
    _validate_commit(source_commit)
    _validate_commit(workflow_commit)
    expected_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError(f"workflow ref must be exactly {expected_ref!r}, got {workflow_ref!r}")
    if workflow_commit != source_commit:
        raise ValueError("workflow commit must equal the release tag source commit")
    if event not in NATIVE_RELEASE_EVENTS:
        raise ValueError(f"release event must be one of {sorted(NATIVE_RELEASE_EVENTS)}")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("GitHub run id must be a positive integer")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("GitHub run attempt must be a positive integer")
    if runner_environment != "github-hosted":
        raise ValueError("native release provenance requires a GitHub-hosted runner")
    if output.name != NATIVE_RELEASE_PROVENANCE or not output.parent.is_dir():
        raise ValueError(f"native release provenance must be named {NATIVE_RELEASE_PROVENANCE!r}")
    if output.exists():
        raise ValueError(f"native release provenance already exists: {output}")

    payload = {
        "schema": NATIVE_RELEASE_PROVENANCE_SCHEMA,
        "repository": repository,
        "source_tag": tag,
        "source_commit": source_commit,
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": workflow_ref,
        "workflow_commit": workflow_commit,
        "event": event,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "runner_environment": runner_environment,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_release_provenance(
    path: Path, *, repository: str, tag: str, source_commit: str
) -> dict:
    """Validate a closed release-provenance object against resolved Git refs."""

    _validate_repository(repository)
    native_tag_tuple(tag)
    _validate_commit(source_commit)
    if path.name != NATIVE_RELEASE_PROVENANCE or not path.is_file() or path.is_symlink():
        raise ValueError(f"missing exact native release provenance file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema",
        "repository",
        "source_tag",
        "source_commit",
        "workflow_path",
        "workflow_ref",
        "workflow_commit",
        "event",
        "run_id",
        "run_attempt",
        "runner_environment",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("native release provenance does not use the closed v3 schema")
    expected = {
        "schema": NATIVE_RELEASE_PROVENANCE_SCHEMA,
        "repository": repository,
        "source_tag": tag,
        "source_commit": source_commit,
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main",
        "workflow_commit": source_commit,
        "runner_environment": "github-hosted",
    }
    for field, value in expected.items():
        if data.get(field) != value:
            raise ValueError(
                f"native release provenance {field} differs: expected {value!r}, "
                f"got {data.get(field)!r}"
            )
    if data.get("event") not in NATIVE_RELEASE_EVENTS:
        raise ValueError("native release provenance event differs")
    for field in ("run_id", "run_attempt"):
        if not isinstance(data[field], int) or data[field] <= 0:
            raise ValueError(f"native release provenance {field} must be a positive integer")
    return data


NATIVE_RELEASE_CHANNEL_KEYS = frozenset(
    {
        "schema",
        "repository",
        "channel",
        "native_tag",
        "native_version",
        "native_source_commit",
        "native_release_url",
        "signing",
        "checksums",
        "provenance",
        "previous",
        "promotion",
    }
)


def _release_download_url(repository: str, tag: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/{tag}/{name}"


def write_native_release_channel(
    output: Path,
    *,
    directory: Path,
    repository: str,
    tag: str,
    source_commit: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: int,
    run_attempt: int,
    previous: Path | None = None,
) -> Path:
    """Write the v3 channel descriptor that ships inside one release.

    It binds this release's ``SHA256SUMS`` and provenance by hash, states each
    platform's signing mode, and names the newest earlier release's channel by
    hash, so consumers can walk an append-only chain without a mutable index.
    """

    repository = _validate_repository(repository)
    native_tag_tuple(tag)
    _validate_commit(source_commit)
    _validate_commit(workflow_commit)
    expected_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError(f"release channel workflow ref must be exactly {expected_ref!r}")
    if workflow_commit != source_commit:
        raise ValueError("release channel workflow commit must equal the release source commit")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("release channel run id must be a positive integer")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("release channel run attempt must be a positive integer")
    if output.exists() or output.name != VERIFIED_RELEASE_CHANNEL:
        raise ValueError(f"release channel must be a new {VERIFIED_RELEASE_CHANNEL}")
    if output.parent.resolve() != directory.resolve():
        raise ValueError("release channel must be written inside the release directory")
    checksums = directory / "SHA256SUMS"
    verify_checksums(directory, checksums)
    if VERIFIED_RELEASE_CHANNEL in read_checksums(checksums):
        raise ValueError("the release channel binds SHA256SUMS, so SHA256SUMS cannot list it")
    provenance = validate_release_provenance(
        directory / NATIVE_RELEASE_PROVENANCE,
        repository=repository,
        tag=tag,
        source_commit=source_commit,
    )
    if provenance["run_id"] != run_id or provenance["run_attempt"] != run_attempt:
        raise ValueError("release channel and provenance name different workflow runs")
    validate_release_set(directory)
    prior = None
    if previous is not None:
        _require_existing_file(previous, label="previous release channel")
        prior_data = validate_native_release_channel(previous)
        if prior_data["repository"] != repository:
            raise ValueError("previous release channel belongs to a different repository")
        if native_tag_tuple(prior_data["native_tag"]) >= native_tag_tuple(tag):
            raise ValueError(
                f"release channel must advance from {prior_data['native_tag']} to {tag}"
            )
        prior = {
            "native_tag": prior_data["native_tag"],
            "sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
            "url": _release_download_url(
                repository, prior_data["native_tag"], VERIFIED_RELEASE_CHANNEL
            ),
        }
    payload = {
        "schema": NATIVE_RELEASE_CHANNEL_SCHEMA,
        "repository": repository,
        "channel": "candidate-native",
        "native_tag": tag,
        "native_version": tag.removeprefix(NATIVE_TAG_PREFIX),
        "native_source_commit": source_commit,
        "native_release_url": f"https://github.com/{repository}/releases/tag/{tag}",
        "signing": release_signing(directory),
        "checksums": {
            "name": "SHA256SUMS",
            "sha256": hashlib.sha256(checksums.read_bytes()).hexdigest(),
            "url": _release_download_url(repository, tag, "SHA256SUMS"),
        },
        "provenance": {
            "name": NATIVE_RELEASE_PROVENANCE,
            "sha256": hashlib.sha256(
                (directory / NATIVE_RELEASE_PROVENANCE).read_bytes()
            ).hexdigest(),
            "url": _release_download_url(repository, tag, NATIVE_RELEASE_PROVENANCE),
        },
        "previous": prior,
        "promotion": {
            "workflow_path": NATIVE_RELEASE_WORKFLOW,
            "workflow_ref": workflow_ref,
            "workflow_commit": workflow_commit,
            "event": provenance["event"],
            "run_id": run_id,
            "run_attempt": run_attempt,
            "runner_environment": "github-hosted",
        },
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_native_release_channel(
    path: Path, *, directory: Path | None = None, previous: Path | None = None
) -> dict:
    """Validate a closed v3 channel; with ``directory``, also bind its bytes.

    ``previous`` is the channel file of the release this one names as its
    predecessor; when given, its hash must match.
    """

    if path.name != VERIFIED_RELEASE_CHANNEL or not path.is_file() or path.is_symlink():
        raise ValueError(f"release channel must be a regular {VERIFIED_RELEASE_CHANNEL}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != NATIVE_RELEASE_CHANNEL_KEYS:
        raise ValueError("release channel does not use the closed v3 schema")
    if data.get("schema") != NATIVE_RELEASE_CHANNEL_SCHEMA:
        raise ValueError("release channel has the wrong schema")
    if data.get("channel") != "candidate-native":
        raise ValueError("release channel has the wrong channel name")
    repository = _validate_repository(str(data.get("repository") or ""))
    tag = str(data.get("native_tag") or "")
    native_tag_tuple(tag)
    if data.get("native_version") != tag.removeprefix(NATIVE_TAG_PREFIX):
        raise ValueError("release channel version differs from its tag")
    _validate_commit(str(data.get("native_source_commit") or ""))
    if data.get("native_release_url") != f"https://github.com/{repository}/releases/tag/{tag}":
        raise ValueError("release channel native_release_url differs")
    signing = data.get("signing")
    if not isinstance(signing, dict):
        raise ValueError("release channel signing modes are invalid")
    website_release_verification(signing)
    for field, name in (("checksums", "SHA256SUMS"), ("provenance", NATIVE_RELEASE_PROVENANCE)):
        value = data.get(field)
        if (
            not isinstance(value, dict)
            or set(value) != {"name", "sha256", "url"}
            or value.get("name") != name
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or ""))
            or value.get("url") != _release_download_url(repository, tag, name)
        ):
            raise ValueError(f"release channel {field} binding is invalid")
    prior = data.get("previous")
    if prior is not None:
        if (
            not isinstance(prior, dict)
            or set(prior) != {"native_tag", "sha256", "url"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(prior.get("sha256") or ""))
        ):
            raise ValueError("release channel prior binding is invalid")
        native_tag_tuple(str(prior.get("native_tag") or ""))
        if native_tag_tuple(prior["native_tag"]) >= native_tag_tuple(tag):
            raise ValueError("release channel prior binding does not precede it")
        if prior.get("url") != _release_download_url(
            repository, prior["native_tag"], VERIFIED_RELEASE_CHANNEL
        ):
            raise ValueError("release channel prior URL differs")
    if previous is not None:
        if prior is None:
            raise ValueError("release channel names no predecessor")
        if hashlib.sha256(previous.read_bytes()).hexdigest() != prior["sha256"]:
            raise ValueError("previous release channel bytes differ from the bound hash")
    promotion = data.get("promotion")
    expected_promotion = {
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main",
        "workflow_commit": data["native_source_commit"],
        "runner_environment": "github-hosted",
    }
    if not isinstance(promotion, dict) or set(promotion) != {
        *expected_promotion,
        "event",
        "run_id",
        "run_attempt",
    }:
        raise ValueError("release channel promotion binding is invalid")
    for field, value in expected_promotion.items():
        if promotion.get(field) != value:
            raise ValueError(f"release channel promotion {field} differs")
    if promotion.get("event") not in NATIVE_RELEASE_EVENTS:
        raise ValueError("release channel promotion event differs")
    for field in ("run_id", "run_attempt"):
        if not isinstance(promotion.get(field), int) or promotion[field] <= 0:
            raise ValueError(f"release channel promotion {field} is invalid")
    if directory is not None:
        for field in ("checksums", "provenance"):
            binding = data[field]
            target = directory / binding["name"]
            if not target.is_file():
                raise ValueError(f"release channel {field} target is missing: {target}")
            if hashlib.sha256(target.read_bytes()).hexdigest() != binding["sha256"]:
                raise ValueError(f"release channel {field} hash differs from the release bytes")
        if release_signing(directory) != signing:
            raise ValueError("release channel signing modes differ from the release metadata")
    return data


def _attestation_subjects(statement: dict) -> dict[str, str]:
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("attestation has no subjects")
    result: dict[str, str] = {}
    for subject in subjects:
        if not isinstance(subject, dict) or set(subject) != {"name", "digest"}:
            raise ValueError("attestation contains a malformed subject")
        name = subject.get("name")
        digest = subject.get("digest")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in result
            or not isinstance(digest, dict)
            or set(digest) != {"sha256"}
            or not isinstance(digest.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest["sha256"])
        ):
            raise ValueError("attestation contains an invalid or duplicate subject")
        result[name] = digest["sha256"]
    return result


def _validate_attestation_record(record: object, *, provenance: dict, checksums: dict) -> None:
    if not isinstance(record, dict):
        raise ValueError("attestation result is not an object")
    verification = record.get("verificationResult")
    if not isinstance(verification, dict):
        raise ValueError("attestation has no verified result")
    signature = verification.get("signature")
    statement = verification.get("statement")
    certificate = signature.get("certificate") if isinstance(signature, dict) else None
    if not isinstance(certificate, dict) or not isinstance(statement, dict):
        raise ValueError("attestation lacks a certificate or statement")
    if _attestation_subjects(statement) != checksums:
        raise ValueError("signed attestation subjects differ from SHA256SUMS")

    repository = provenance["repository"]
    commit = provenance["source_commit"]
    run_id = provenance["run_id"]
    run_attempt = provenance["run_attempt"]
    # A release runs when Packages passes on main, or on a dispatch; releases
    # before 0.22.0 ran on the push that merged the version.
    trigger = provenance["event"]
    repository_url = f"https://github.com/{repository}"
    workflow_uri = f"{repository_url}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    invocation_uri = f"{repository_url}/actions/runs/{run_id}/attempts/{run_attempt}"
    certificate_claims = {
        "subjectAlternativeName": workflow_uri,
        "githubWorkflowTrigger": trigger,
        "githubWorkflowSHA": commit,
        "githubWorkflowName": NATIVE_RELEASE_WORKFLOW_NAME,
        "githubWorkflowRepository": repository,
        "githubWorkflowRef": "refs/heads/main",
        "buildSignerURI": workflow_uri,
        "buildSignerDigest": commit,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": repository_url,
        "sourceRepositoryDigest": commit,
        "sourceRepositoryRef": "refs/heads/main",
        "buildConfigURI": workflow_uri,
        "buildConfigDigest": commit,
        "buildTrigger": trigger,
        "runInvocationURI": invocation_uri,
    }
    for field, value in certificate_claims.items():
        if certificate.get(field) != value:
            raise ValueError(
                f"attestation certificate {field} differs: expected {value!r}, "
                f"got {certificate.get(field)!r}"
            )

    predicate = statement.get("predicate")
    build_definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    run_details = predicate.get("runDetails") if isinstance(predicate, dict) else None
    if not isinstance(build_definition, dict) or not isinstance(run_details, dict):
        raise ValueError("attestation lacks GitHub workflow provenance")
    if build_definition.get("buildType") != GITHUB_WORKFLOW_BUILD_TYPE:
        raise ValueError("attestation has the wrong GitHub workflow build type")
    external = build_definition.get("externalParameters")
    workflow = external.get("workflow") if isinstance(external, dict) else None
    if workflow != {
        "path": NATIVE_RELEASE_WORKFLOW,
        "ref": "refs/heads/main",
        "repository": repository_url,
    }:
        raise ValueError("attestation external workflow identity differs")
    internal = build_definition.get("internalParameters")
    github = internal.get("github") if isinstance(internal, dict) else None
    if not isinstance(github, dict) or github.get("event_name") != trigger:
        raise ValueError("attestation was not produced by the provenance's trigger")
    if github.get("runner_environment") != "github-hosted":
        raise ValueError("attestation was not produced on a GitHub-hosted runner")
    if build_definition.get("resolvedDependencies") != [
        {
            "digest": {"gitCommit": commit},
            "uri": f"git+{repository_url}@refs/heads/main",
        }
    ]:
        raise ValueError("attestation resolved source differs")
    builder = run_details.get("builder")
    metadata = run_details.get("metadata")
    if not isinstance(builder, dict) or builder.get("id") != workflow_uri:
        raise ValueError("attestation builder identity differs")
    if not isinstance(metadata, dict) or metadata.get("invocationId") != invocation_uri:
        raise ValueError("attestation workflow invocation differs")


def validate_release_attestation(
    attestation: Path,
    *,
    directory: Path,
    checksums: Path,
    provenance_path: Path,
    repository: str,
    tag: str,
    source_commit: str,
) -> dict:
    """Bind downloaded bytes to a verified GitHub workflow invocation."""

    verify_checksums(directory, checksums)
    provenance = validate_release_provenance(
        provenance_path,
        repository=repository,
        tag=tag,
        source_commit=source_commit,
    )
    records = json.loads(attestation.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("gh attestation verify returned no records")
    matches = 0
    failures: list[str] = []
    for record in records:
        try:
            _validate_attestation_record(
                record,
                provenance=provenance,
                checksums=read_checksums(checksums),
            )
        except ValueError as exc:
            failures.append(str(exc))
        else:
            matches += 1
    if matches != 1:
        raise ValueError(
            f"expected exactly one matching native release attestation, got {matches}; "
            f"rejections={failures}"
        )
    return provenance


def release_signing(directory: Path) -> dict[str, str]:
    """Return the one signing mode each platform's builds used in this set."""

    modes: dict[str, str] = {}
    for metadata_path in sorted(directory.glob("*-metadata.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        platform = metadata.get("platform")
        signing = metadata.get("signing")
        if signing not in SIGNING_MODES.get(platform, set()):
            raise ValueError(f"invalid signing metadata: {metadata_path}")
        if modes.setdefault(platform, signing) != signing:
            raise ValueError(f"{platform} builds disagree on their signing mode")
    if set(modes) != {platform for platform, _ in EXPECTED_PLATFORMS}:
        raise ValueError(f"release set does not cover every platform: {sorted(modes)}")
    return modes


def validate_release_set(directory: Path, *, require_production: bool = False) -> int:
    """Validate the exact staged installer set.

    A candidate release may use any permitted signing mode per platform, as
    long as every build of a platform agrees and the file names say which.
    ``require_production`` additionally demands the production trust modes.
    """

    signing_by_platform: dict[str, str] = {}
    files = sorted(path for path in directory.iterdir() if path.is_file())
    metadata_paths = [path for path in files if path.name.endswith("-metadata.json")]
    if len(metadata_paths) != len(EXPECTED_PLATFORMS):
        raise ValueError(
            f"expected {len(EXPECTED_PLATFORMS)} platform metadata files, got {metadata_paths}"
        )

    observed_platforms: set[tuple[str, str]] = set()
    referenced_assets: set[str] = set()
    for metadata_path in metadata_paths:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("lifecycle") != LIFECYCLE or metadata.get("surface") != SURFACE:
            raise ValueError(f"incorrect lifecycle metadata: {metadata_path}")
        platform = metadata.get("platform")
        architecture = metadata.get("architecture")
        pair = (platform, architecture)
        if pair in observed_platforms:
            raise ValueError(f"duplicate platform metadata for {pair}")
        observed_platforms.add(pair)
        if metadata.get("signing") not in SIGNING_MODES.get(platform, set()):
            raise ValueError(f"invalid signing metadata: {metadata_path}")
        version = native_version()
        signing = metadata["signing"]
        if signing != signing_by_platform.setdefault(platform, signing):
            raise ValueError(f"{platform} builds disagree on their signing mode")
        if require_production and signing != PRODUCTION_TRUST_MODES.get(platform):
            raise ValueError(
                f"production release requires {platform} trust mode "
                f"{PRODUCTION_TRUST_MODES.get(platform)!r}, got {signing!r}"
            )
        if metadata.get("native_version") != version:
            raise ValueError(f"wrong native version in {metadata_path}")
        prefix = f"OpenAdapt-Desktop-Candidate-v{version}-{platform}-{architecture}-{signing}"
        if metadata_path.name != f"{prefix}-metadata.json":
            raise ValueError(f"metadata filename does not match its labels: {metadata_path.name}")
        expected_artifacts = {f"{prefix}{suffix}" for _, _, suffix in ARTIFACT_RULES[platform]}
        metadata_artifacts = set(metadata.get("artifacts", []))
        if metadata_artifacts != expected_artifacts:
            raise ValueError(
                f"artifact names do not match platform metadata in {metadata_path}: "
                f"expected={expected_artifacts}, got={metadata_artifacts}"
            )
        expected_commit = os.environ.get("GITHUB_SHA")
        if expected_commit and metadata.get("source_commit") != expected_commit:
            raise ValueError(f"source commit differs in {metadata_path}")
        for name in metadata_artifacts:
            if Path(name).name != name or name in referenced_assets:
                raise ValueError(f"invalid or duplicate staged artifact name: {name!r}")
            if not (directory / name).is_file():
                raise ValueError(f"metadata references missing artifact: {name}")
            referenced_assets.add(name)

    if observed_platforms != EXPECTED_PLATFORMS:
        raise ValueError(
            f"platform set differs: expected {EXPECTED_PLATFORMS}, got {observed_platforms}"
        )
    actual_assets = {
        path.name
        for path in files
        if not path.name.endswith("-metadata.json")
        and path.name != "SHA256SUMS"
        # These release-wide integrity documents are generated after the
        # per-platform staging metadata. They describe the installer set but
        # are not installers themselves.
        and not path.name.endswith(".cyclonedx.json")
        and path.name != WEBSITE_RELEASE_MANIFEST
        and path.name != NATIVE_RELEASE_PROVENANCE
        and path.name != NATIVE_RELEASE_VERIFIER
        # The Python wheel and sdist ship in the same release (and on PyPI).
        and path.name not in python_distribution_names(native_version())
    }
    if actual_assets != referenced_assets:
        raise ValueError(
            f"release assets differ from metadata: actual={actual_assets}, "
            f"referenced={referenced_assets}"
        )
    return len(actual_assets) + len(metadata_paths)


def validate_sbom(path: Path) -> int:
    """Validate the minimum contract for the published CycloneDX release SBOM."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("bomFormat") != "CycloneDX":
        raise ValueError(f"SBOM is not CycloneDX JSON: {path}")
    spec_version = data.get("specVersion")
    if not isinstance(spec_version, str) or not re.fullmatch(r"1\.[4-9]", spec_version):
        raise ValueError(f"unsupported CycloneDX specVersion in {path}: {spec_version!r}")
    if data.get("version") != 1:
        raise ValueError(f"SBOM document version must be 1 in {path}")
    metadata = data.get("metadata")
    tools = metadata.get("tools") if isinstance(metadata, dict) else None
    if not isinstance(tools, (dict, list)) or not tools:
        raise ValueError(f"SBOM does not identify its generator in {path}")
    components = data.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError(f"SBOM contains no detected components: {path}")
    named = [
        component
        for component in components
        if isinstance(component, dict)
        and isinstance(component.get("name"), str)
        and component["name"].strip()
    ]
    if len(named) != len(components):
        raise ValueError(f"SBOM contains an unnamed or malformed component: {path}")
    return len(components)


def write_website_release_manifest(
    directory: Path, *, tag: str, sbom: Path, root: Path = ROOT
) -> Path:
    """Write the verified, public release index consumed by download pages.

    This is deliberately a description of the exact staged bytes and their
    platform-specific trust contracts. ``SHA256SUMS`` subsequently binds the
    manifest itself into the GitHub provenance attestation.
    """

    validate_tag(tag, root)
    validate_release_set(directory)
    validate_sbom(sbom)
    if sbom.parent.resolve() != directory.resolve():
        raise ValueError("SBOM must be inside the release asset directory")
    output = directory / WEBSITE_RELEASE_MANIFEST
    if output.exists():
        raise ValueError(f"release manifest already exists: {output}")

    assets: list[dict[str, str]] = []
    for metadata_path in sorted(directory.glob("*-metadata.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for name in metadata["artifacts"]:
            path = directory / name
            assets.append(
                {
                    "name": name,
                    "platform": metadata["platform"],
                    "architecture": metadata["architecture"],
                    "signing": metadata["signing"],
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    assets.sort(key=lambda item: item["name"])
    payload = {
        "schema_version": 1,
        "lifecycle": LIFECYCLE,
        "native_tag": tag,
        "native_version": native_version(root),
        "source_commit": os.environ.get("GITHUB_SHA", "local"),
        "verification": website_release_verification(release_signing(directory)),
        "sbom": {
            "name": sbom.name,
            "sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
            "format": WEBSITE_RELEASE_SBOM_FORMAT,
        },
        "artifacts": assets,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_website_release_manifest(path: Path, *, checksums: Path, root: Path = ROOT) -> int:
    """Validate the public index against metadata, bytes, SBOM, and checksums."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("lifecycle") != LIFECYCLE:
        raise ValueError(f"invalid website release manifest: {path}")
    validate_tag(str(data.get("native_tag", "")), root)
    if data.get("native_version") != native_version(root):
        raise ValueError(f"wrong native version in website release manifest: {path}")
    directory = path.parent
    if data.get("verification") != website_release_verification(release_signing(directory)):
        raise ValueError("website release manifest has an invalid verification contract")
    if path.name != WEBSITE_RELEASE_MANIFEST or checksums.parent.resolve() != directory.resolve():
        raise ValueError("website manifest and SHA256SUMS must be in the release directory")
    validate_release_set(directory)
    metadata_by_asset: dict[str, tuple[str, str, str]] = {}
    for metadata_path in sorted(directory.glob("*-metadata.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for name in metadata.get("artifacts", []):
            if name in metadata_by_asset:
                raise ValueError(f"duplicate artifact in platform metadata: {name}")
            metadata_by_asset[name] = (
                metadata.get("platform"),
                metadata.get("architecture"),
                metadata.get("signing"),
            )
    # Each release has one signing mode per platform. Platform metadata is the
    # source of truth for which permitted mode the build selected.
    expected_names = set(metadata_by_asset)
    assets = data.get("artifacts")
    if not isinstance(assets, list) or len(assets) != len(expected_names):
        raise ValueError(f"website release manifest has an incomplete artifact set: {path}")
    observed_names: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("signing") not in SIGNING_MODES.get(
            asset.get("platform"), set()
        ):
            raise ValueError(f"website release manifest has invalid signing metadata: {path}")
        name = asset.get("name")
        digest = asset.get("sha256")
        if (
            not isinstance(name, str)
            or name in observed_names
            or name not in expected_names
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError(f"website release manifest has invalid artifact digest: {path}")
        observed_names.add(name)
        platform, architecture, signing = metadata_by_asset[name]
        if (asset.get("platform"), asset.get("architecture"), asset.get("signing")) != (
            platform,
            architecture,
            signing,
        ):
            raise ValueError(f"website release manifest disagrees with platform metadata: {name}")
        artifact_path = directory / name
        if not artifact_path.is_file():
            raise ValueError(f"website release manifest references missing artifact: {name}")
        actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != actual_digest:
            raise ValueError(f"website release manifest digest differs for {name}")
    if observed_names != expected_names:
        raise ValueError("website release manifest artifact names are incomplete")

    sbom = data.get("sbom")
    expected_sbom_name = sbom_name(str(data["native_version"]))
    if (
        not isinstance(sbom, dict)
        or set(sbom) != {"name", "sha256", "format"}
        or sbom.get("name") != expected_sbom_name
    ):
        raise ValueError("website release manifest names the wrong SBOM")
    if sbom.get("format") != WEBSITE_RELEASE_SBOM_FORMAT:
        raise ValueError("website release manifest has an invalid SBOM format")
    sbom_path = directory / expected_sbom_name
    if not sbom_path.is_file():
        raise ValueError(f"website release manifest references missing SBOM: {sbom_path}")
    validate_sbom(sbom_path)
    sbom_digest = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
    if sbom.get("sha256") != sbom_digest:
        raise ValueError("website release manifest SBOM digest differs")

    checksum_entries = read_checksums(checksums)
    metadata_names = {metadata.name for metadata in directory.glob("*-metadata.json")}
    expected_checksum_names = (
        expected_names
        | metadata_names
        | python_distribution_names(str(data["native_version"]))
        | {
            expected_sbom_name,
            WEBSITE_RELEASE_MANIFEST,
            NATIVE_RELEASE_PROVENANCE,
            NATIVE_RELEASE_VERIFIER,
        }
    )
    if set(checksum_entries) != expected_checksum_names:
        raise ValueError("SHA256SUMS does not describe the exact release file set")
    for name in expected_checksum_names:
        actual_digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if checksum_entries.get(name) != actual_digest:
            raise ValueError(f"SHA256SUMS digest differs for {name}")
    expected_commit = os.environ.get("GITHUB_SHA")
    if expected_commit and data.get("source_commit") != expected_commit:
        raise ValueError("website release manifest source commit differs")
    return len(assets)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    version_parser = subparsers.add_parser("version")
    version_parser.add_argument(
        "--ref",
        help="read the version from this Git commit instead of the working tree",
    )
    tag_parser = subparsers.add_parser("validate-tag")
    tag_parser.add_argument("tag")

    set_version_parser = subparsers.add_parser("set-version")
    set_version_parser.add_argument("version")
    transform_parser = subparsers.add_parser("validate-version-transform")
    transform_parser.add_argument("--base-ref", required=True)
    transform_parser.add_argument("--candidate-ref", required=True)
    transform_parser.add_argument("--version", required=True)
    advance_parser = subparsers.add_parser("validate-version-advance")
    advance_parser.add_argument("--base-ref", required=True)
    advance_parser.add_argument("--candidate-ref", required=True)
    advance_parser.add_argument("--version", required=True)
    subparsers.add_parser("sync-from-engine")

    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--bundle-root", type=Path, required=True)
    stage_parser.add_argument("--output", type=Path, required=True)
    stage_parser.add_argument("--platform", choices=sorted(ARTIFACT_RULES), required=True)
    stage_parser.add_argument("--architecture", choices=("arm64", "x86_64"), required=True)
    stage_parser.add_argument("--signing", required=True)

    checksums_parser = subparsers.add_parser("checksums")
    checksums_parser.add_argument("--directory", type=Path, required=True)
    checksums_parser.add_argument("--output", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify-checksums")
    verify_parser.add_argument("--directory", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)

    validate_set_parser = subparsers.add_parser("validate-set")
    validate_set_parser.add_argument("--directory", type=Path, required=True)
    validate_sbom_parser = subparsers.add_parser("validate-sbom")
    validate_sbom_parser.add_argument("--file", type=Path, required=True)
    sbom_name_parser = subparsers.add_parser("sbom-name")
    sbom_name_parser.add_argument("--version", required=True)
    website_manifest_parser = subparsers.add_parser("website-manifest")
    website_manifest_parser.add_argument("--directory", type=Path, required=True)
    website_manifest_parser.add_argument("--tag", required=True)
    website_manifest_parser.add_argument("--sbom", type=Path, required=True)
    validate_website_manifest_parser = subparsers.add_parser("validate-website-manifest")
    validate_website_manifest_parser.add_argument("--file", type=Path, required=True)
    validate_website_manifest_parser.add_argument("--checksums", type=Path, required=True)

    provenance_parser = subparsers.add_parser("write-provenance")
    provenance_parser.add_argument("--output", type=Path, required=True)
    provenance_parser.add_argument("--repository", required=True)
    provenance_parser.add_argument("--tag", required=True)
    provenance_parser.add_argument("--source-commit", required=True)
    provenance_parser.add_argument("--workflow-ref", required=True)
    provenance_parser.add_argument("--workflow-commit", required=True)
    provenance_parser.add_argument("--run-id", type=int, required=True)
    provenance_parser.add_argument("--run-attempt", type=int, required=True)
    provenance_parser.add_argument("--runner-environment", required=True)
    provenance_parser.add_argument("--event", choices=sorted(NATIVE_RELEASE_EVENTS), required=True)

    validate_provenance_parser = subparsers.add_parser("validate-provenance")
    validate_provenance_parser.add_argument("--file", type=Path, required=True)
    validate_provenance_parser.add_argument("--repository", required=True)
    validate_provenance_parser.add_argument("--tag", required=True)
    validate_provenance_parser.add_argument("--source-commit", required=True)

    attestation_parser = subparsers.add_parser("validate-attestation")
    attestation_parser.add_argument("--file", type=Path, required=True)
    attestation_parser.add_argument("--directory", type=Path, required=True)
    attestation_parser.add_argument("--checksums", type=Path, required=True)
    attestation_parser.add_argument("--provenance", type=Path, required=True)
    attestation_parser.add_argument("--repository", required=True)
    attestation_parser.add_argument("--tag", required=True)
    attestation_parser.add_argument("--source-commit", required=True)

    order_parser = subparsers.add_parser("validate-release-order")
    order_parser.add_argument("--tags", type=Path, required=True)
    order_parser.add_argument("--candidate-tag", required=True)

    channel_parser = subparsers.add_parser("write-channel")
    channel_parser.add_argument("--output", type=Path, required=True)
    channel_parser.add_argument("--directory", type=Path, required=True)
    channel_parser.add_argument("--repository", required=True)
    channel_parser.add_argument("--tag", required=True)
    channel_parser.add_argument("--source-commit", required=True)
    channel_parser.add_argument("--workflow-ref", required=True)
    channel_parser.add_argument("--workflow-commit", required=True)
    channel_parser.add_argument("--run-id", type=int, required=True)
    channel_parser.add_argument("--run-attempt", type=int, required=True)
    channel_parser.add_argument("--previous", type=Path)
    validate_channel_parser = subparsers.add_parser("validate-channel")
    validate_channel_parser.add_argument("--file", type=Path, required=True)
    validate_channel_parser.add_argument("--directory", type=Path)
    validate_channel_parser.add_argument("--previous", type=Path)
    signing_parser = subparsers.add_parser("release-signing")
    signing_parser.add_argument("--directory", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "version":
            print(native_version() if args.ref is None else native_version_at_ref(args.ref))
        elif args.command == "validate-tag":
            print(validate_tag(args.tag))
        elif args.command == "set-version":
            versions = set_native_version(args.version)
            for source, value in sorted(versions.items()):
                print(f"{source}: {value}")
        elif args.command == "validate-version-transform":
            count = validate_git_version_transform(
                args.base_ref,
                args.candidate_ref,
                args.version,
            )
            print(f"Validated {count} deterministic native version files")
        elif args.command == "validate-version-advance":
            count = validate_git_version_advance(
                args.base_ref,
                args.candidate_ref,
                args.version,
            )
            print(f"Validated {count} advancing native version files")
        elif args.command == "sync-from-engine":
            versions = sync_native_version_from_engine()
            for source, value in sorted(versions.items()):
                print(f"{source}: {value}")
        elif args.command == "stage":
            staged = stage_artifacts(
                bundle_root=args.bundle_root,
                output=args.output,
                platform=args.platform,
                architecture=args.architecture,
                signing=args.signing,
            )
            print("\n".join(str(path) for path in staged))
        elif args.command == "checksums":
            entries = write_checksums(args.directory, args.output)
            print(f"Wrote {len(entries)} checksums to {args.output}")
        elif args.command == "verify-checksums":
            count = verify_checksums(args.directory, args.manifest)
            print(f"Verified {count} checksums from {args.manifest}")
        elif args.command == "validate-set":
            count = validate_release_set(args.directory)
            print(f"Validated {count} exact release files in {args.directory}")
        elif args.command == "validate-sbom":
            count = validate_sbom(args.file)
            print(f"Validated {count} components in {args.file}")
        elif args.command == "sbom-name":
            print(sbom_name(args.version))
        elif args.command == "website-manifest":
            path = write_website_release_manifest(args.directory, tag=args.tag, sbom=args.sbom)
            print(path)
        elif args.command == "validate-website-manifest":
            count = validate_website_release_manifest(args.file, checksums=args.checksums)
            print(f"Validated {count} website release artifacts in {args.file}")
        elif args.command == "write-provenance":
            path = write_release_provenance(
                args.output,
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
                workflow_ref=args.workflow_ref,
                workflow_commit=args.workflow_commit,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                runner_environment=args.runner_environment,
                event=args.event,
            )
            print(path)
        elif args.command == "validate-provenance":
            provenance = validate_release_provenance(
                args.file,
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
            )
            print(
                f"Validated release provenance for run {provenance['run_id']} "
                f"attempt {provenance['run_attempt']}"
            )
        elif args.command == "validate-attestation":
            provenance = validate_release_attestation(
                args.file,
                directory=args.directory,
                checksums=args.checksums,
                provenance_path=args.provenance,
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
            )
            print(
                f"Validated exact release attestation for run {provenance['run_id']} "
                f"attempt {provenance['run_attempt']}"
            )
        elif args.command == "validate-release-order":
            tags = native_release_tags(args.tags.read_text(encoding="utf-8"))
            print(validate_native_tag_order(args.candidate_tag, tags))
        elif args.command == "write-channel":
            print(
                write_native_release_channel(
                    args.output,
                    directory=args.directory,
                    repository=args.repository,
                    tag=args.tag,
                    source_commit=args.source_commit,
                    workflow_ref=args.workflow_ref,
                    workflow_commit=args.workflow_commit,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    previous=args.previous,
                )
            )
        elif args.command == "validate-channel":
            data = validate_native_release_channel(
                args.file, directory=args.directory, previous=args.previous
            )
            print(f"{data['native_tag']} channel is valid")
        elif args.command == "release-signing":
            print(json.dumps(release_signing(args.directory), sort_keys=True))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
