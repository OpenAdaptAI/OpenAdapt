#!/usr/bin/env python3
"""Prepare and verify honest Beta native release assets."""

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
LIFECYCLE = "Beta"
SURFACE = "installed desktop pairing and authoring companion"
NATIVE_TAG_PREFIX = "desktop-v"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SUPERSEDED_MARKER_PREFIX = "<!-- openadapt-superseded-by: "
SUPERSEDED_SEPARATOR = "\n---\n\n"
INSTALLER_POINTER_START = "<!-- openadapt-installer-pointer:start -->"
INSTALLER_POINTER_END = "<!-- openadapt-installer-pointer:end -->"

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
NATIVE_RELEASE_PROVENANCE_SCHEMA = "openadapt.native-release-provenance/v2"
NATIVE_RELEASE_WORKFLOW = ".github/workflows/native-release.yml"
NATIVE_RELEASE_WORKFLOW_NAME = "Native Installer Release"
ENGINE_RELEASE_WORKFLOW = ".github/workflows/release.yml"
ENGINE_RELEASE_PROVENANCE = "openadapt-desktop-engine-release-provenance.json"
ENGINE_RELEASE_PROVENANCE_SCHEMA = "openadapt.engine-release-provenance/v1"
VERIFIED_RELEASE_INDEX = "openadapt-desktop-verified-release.json"
VERIFIED_RELEASE_INDEX_SCHEMA = "openadapt.desktop-verified-release/v1"
VERIFIED_RELEASE_CHANNEL = "openadapt-desktop-channel.json"
VERIFIED_RELEASE_CHANNEL_SCHEMA = "openadapt.desktop-release-channel/v1"
VERIFIED_RELEASE_CHANNEL_TAG = "desktop-channel"
NATIVE_PROMOTION_WORKFLOW = ".github/workflows/native-release.yml"
NATIVE_RELEASE_VERIFIER = "verify-openadapt-native-release.py"
GITHUB_WORKFLOW_BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"
GITHUB_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
INSTALLER_RELEASE_MARKER = "<!-- installer-release -->"
WEBSITE_RELEASE_VERIFICATION = {
    "sha256_manifest": "SHA256SUMS",
    "github_artifact_attestation": "required",
    "macos_native_trust": "Developer ID, notarization, and stapled ticket required",
    "windows_native_trust": "valid timestamped Authenticode required",
    "linux_byte_trust": "GitHub OIDC attestation over exact DEB and AppImage bytes required",
    "installer_smoke": "install, launch, and uninstall",
}
WEBSITE_RELEASE_SBOM_FORMAT = "CycloneDX"


def expected_engine_asset_names(version: str) -> set[str]:
    """Return the exact Python artifacts made by the engine release."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid engine release version: {version!r}")
    return {
        f"openadapt_desktop-{version}-py3-none-any.whl",
        f"openadapt_desktop-{version}.tar.gz",
    }


def expected_release_asset_names(version: str) -> set[str]:
    """Return the complete checksummed asset contract for one native version."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid native release version: {version!r}")
    names = {
        WEBSITE_RELEASE_MANIFEST,
        NATIVE_RELEASE_PROVENANCE,
        NATIVE_RELEASE_VERIFIER,
        f"OpenAdapt-Desktop-desktop-v{version}.cyclonedx.json",
    }
    for platform, architecture in EXPECTED_PLATFORMS:
        signing = PRODUCTION_TRUST_MODES[platform]
        prefix = f"OpenAdapt-Desktop-Beta-v{version}-{platform}-{architecture}-{signing}"
        names.add(f"{prefix}-metadata.json")
        names.update(f"{prefix}{suffix}" for _kind, _pattern, suffix in ARTIFACT_RULES[platform])
    return names


def native_versions(root: Path = ROOT) -> dict[str, str]:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    tauri = json.loads((root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    return {
        "package.json": package["version"],
        "src-tauri/tauri.conf.json": tauri["version"],
        "src-tauri/Cargo.toml": cargo["package"]["version"],
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


def _published_native_releases(releases: object) -> list[dict]:
    """Return marked, published native prereleases from GitHub API data."""

    if not isinstance(releases, list):
        raise ValueError("GitHub releases payload must be a list")
    flattened: list[object] = []
    for entry in releases:
        if isinstance(entry, list):
            flattened.extend(entry)
        else:
            flattened.append(entry)

    selected: list[dict] = []
    seen: set[str] = set()
    for release in flattened:
        if not isinstance(release, dict):
            raise ValueError("GitHub releases payload contains a non-object entry")
        tag = release.get("tag_name")
        body = release.get("body")
        if (
            release.get("draft") is False
            and release.get("prerelease") is True
            and isinstance(tag, str)
            and tag.startswith(NATIVE_TAG_PREFIX)
            and isinstance(body, str)
            and INSTALLER_RELEASE_MARKER in body
        ):
            native_tag_tuple(tag)
            if tag in seen:
                raise ValueError(f"GitHub releases payload repeats native tag: {tag}")
            seen.add(tag)
            selected.append(release)
    return selected


def select_latest_native_release(releases: object) -> dict:
    """Select the highest semantic version from published marked releases."""

    candidates = _published_native_releases(releases)
    if not candidates:
        raise ValueError("no published marked native prerelease exists")
    return max(candidates, key=lambda release: native_tag_tuple(release["tag_name"]))


def native_release_tags(ls_remote_output: str) -> list[str]:
    """Return every ``desktop-v*`` tag in ``git ls-remote --tags`` output.

    The Git tag namespace is immutable release order. A release ``draft`` flag,
    ``prerelease`` flag, and ``body`` are all mutable, and this repository
    rewrites release notes itself, so a comparison set filtered on those fields
    can silently lose a member and admit a lower version.

    Every line must be one object id and one ``refs/tags/`` ref. A malformed
    line, or a ``desktop-v`` tag that is not ``X.Y.Z``, fails closed.
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
    therefore explicit UTF-8 bytes with LF endings.
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
        r'(?m)^version = "[^"]+"$', f'version = "{version}"', read_source(cargo_toml), count=1
    )
    if replaced != 1:
        raise ValueError(f"could not rewrite package version in {cargo_toml}")
    write_source(cargo_toml, text)

    cargo_lock = root / "src-tauri" / "Cargo.lock"
    text, replaced = re.subn(
        r'(name = "openadapt-desktop"\nversion = ")[^"]+(")',
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
        ["git", "show", f"{commit}:{relative_path}"],
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
        ["git", "diff", "--name-only", f"{base_commit}..{candidate_commit}", "--"],
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


def superseded_notes(body: str, newer_tag: str, repo: str) -> str | None:
    """Return release notes marking ``body`` superseded by ``newer_tag``.

    Returns ``None`` when no edit is needed (already marked as superseded by
    the same or a newer native release). Never removes original notes: an
    existing supersession header is replaced, everything else is preserved.
    """
    newer = native_tag_tuple(newer_tag)
    normalized = body.replace("\r\n", "\n")
    if normalized.startswith(SUPERSEDED_MARKER_PREFIX):
        first_line, _, remainder = normalized.partition("\n")
        existing_tag = first_line[len(SUPERSEDED_MARKER_PREFIX) :].removesuffix(" -->")
        if native_tag_tuple(existing_tag) >= newer:
            return None
        separator_index = remainder.find(SUPERSEDED_SEPARATOR)
        if separator_index < 0:
            raise ValueError("existing supersession header is missing its separator")
        normalized = remainder[separator_index + len(SUPERSEDED_SEPARATOR) :]
    header = (
        f"{SUPERSEDED_MARKER_PREFIX}{newer_tag} -->\n"
        "> [!CAUTION]\n"
        f"> **Superseded by [{newer_tag}](https://github.com/{repo}/releases/tag/{newer_tag})"
        " — do not use.**\n"
        "> Newer Beta native installers replace these assets. The assets below are\n"
        "> retained for provenance only; deleting releases or assets is a maintainer\n"
        "> decision made outside CI."
        f"{SUPERSEDED_SEPARATOR}"
    )
    return header + normalized


def installer_pointer_notes(body: str, native_tag: str, repo: str) -> str | None:
    """Return engine-release notes carrying a pointer to ``native_tag``.

    GitHub's ``/releases/latest`` excludes prereleases by definition, so the
    engine release is what a human lands on -- and it carries only the wheel
    and sdist. Without this block that visitor sees no installer at all.

    The block is delimited by stable markers and is rewritten in place, so
    republishing or re-running is idempotent and never accumulates pointers.
    Returns ``None`` when the body already carries an identical block.

    As of the ``mirror-installers-to-engine-release`` job the engine release
    also carries a byte-identical copy of the attested installer set, so this
    block no longer says "this release has no installer". It still names
    ``desktop-vX.Y.Z`` because that tag remains the canonical provenance home
    (attestations and the supersession marker are bound to it), and it still
    states the signing state up front so the copy on "Latest" cannot be read
    as a maturity promotion. See RELEASES.md.
    """
    # Format-only validation. Unlike `validate_tag`, this must not compare
    # against the checked-out sources: the pointer names a release object, and
    # the notes it edits belong to a different tag.
    native_tag_tuple(native_tag)
    version = native_tag[len(NATIVE_TAG_PREFIX) :]
    base = f"https://github.com/{repo}/releases"
    block = (
        f"{INSTALLER_POINTER_START}\n"
        "> [!IMPORTANT]\n"
        "> **Looking for the desktop app? The Beta installers are attached\n"
        "> below.**\n"
        "> macOS DMG (arm64 and x86_64), Windows MSI and NSIS `.exe`, Linux\n"
        "> `.deb` and `.AppImage`, plus `SHA256SUMS` — the same attested bytes\n"
        f"> published at [`{native_tag}`]({base}/tag/{native_tag}), mirrored\n"
        '> here so GitHub\'s "Latest" always carries an installer.\n'
        ">\n"
        "> **These installers are Beta, but the release trust gate is mandatory.**\n"
        "> macOS requires Developer ID plus notarization. Windows requires\n"
        "> timestamped Authenticode. Linux DEB and AppImage bytes require GitHub\n"
        "> OIDC artifact attestations. The trust state is in every filename.\n"
        "> Verify with `sha256sum -c SHA256SUMS` and `gh attestation verify`.\n"
        ">\n"
        f"> This `v{version}` release is also the Python engine package\n"
        f"> (wheel and sdist; `pip install openadapt-desktop`). `{native_tag}`\n"
        "> remains the canonical installer release: build provenance,\n"
        "> attestations, and supersession notices are bound to that tag, and\n"
        "> download pages select from it. See\n"
        f"> [RELEASES.md](https://github.com/{repo}/blob/main/RELEASES.md).\n"
        f"{INSTALLER_POINTER_END}\n\n"
    )

    normalized = body.replace("\r\n", "\n")
    start = normalized.find(INSTALLER_POINTER_START)
    if start >= 0:
        end = normalized.find(INSTALLER_POINTER_END, start)
        if end < 0:
            raise ValueError("existing installer pointer is missing its end marker")
        end += len(INSTALLER_POINTER_END)
        remainder = normalized[end:].lstrip("\n")
        normalized = normalized[:start] + remainder

    updated = block + normalized
    if updated == body.replace("\r\n", "\n"):
        return None
    return updated


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
    prefix = f"OpenAdapt-Desktop-Beta-v{version}-{platform}-{architecture}-{signing}"
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
    engine_tag: str,
    engine_commit: str,
    engine_release_id: int,
    engine_release_url: str,
    root: Path = ROOT,
) -> Path:
    """Write the build identity that the signed subject inventory must bind."""

    _validate_repository(repository)
    validate_tag(tag, root)
    _validate_commit(source_commit)
    _validate_commit(workflow_commit)
    _validate_commit(engine_commit)
    expected_engine_tag = f"v{tag.removeprefix(NATIVE_TAG_PREFIX)}"
    if engine_tag != expected_engine_tag:
        raise ValueError(f"engine tag must be exactly {expected_engine_tag!r}, got {engine_tag!r}")
    if not isinstance(engine_release_id, int) or engine_release_id <= 0:
        raise ValueError("engine release id must be a positive integer")
    expected_engine_url = f"https://github.com/{repository}/releases/tag/{engine_tag}"
    if engine_release_url != expected_engine_url:
        raise ValueError("engine release URL does not match its repository and tag")
    expected_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError(f"workflow ref must be exactly {expected_ref!r}, got {workflow_ref!r}")
    if workflow_commit != source_commit:
        raise ValueError("workflow commit must equal the native tag source commit")
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
        "event": "workflow_dispatch",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "runner_environment": runner_environment,
        "engine_tag": engine_tag,
        "engine_commit": engine_commit,
        "engine_release_id": engine_release_id,
        "engine_release_url": engine_release_url,
        "engine_release_workflow": ENGINE_RELEASE_WORKFLOW,
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
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "engine_release_url",
        "engine_release_workflow",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("native release provenance does not use the closed v2 schema")
    expected_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    expected = {
        "schema": NATIVE_RELEASE_PROVENANCE_SCHEMA,
        "repository": repository,
        "source_tag": tag,
        "source_commit": source_commit,
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": expected_ref,
        "workflow_commit": source_commit,
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
        "engine_tag": f"v{tag.removeprefix(NATIVE_TAG_PREFIX)}",
        "engine_release_url": (
            f"https://github.com/{repository}/releases/tag/v{tag.removeprefix(NATIVE_TAG_PREFIX)}"
        ),
        "engine_release_workflow": ENGINE_RELEASE_WORKFLOW,
    }
    for field, value in expected.items():
        if data.get(field) != value:
            raise ValueError(
                f"native release provenance {field} differs: expected {value!r}, "
                f"got {data.get(field)!r}"
            )
    for field in ("run_id", "run_attempt", "engine_release_id"):
        if not isinstance(data[field], int) or data[field] <= 0:
            raise ValueError(f"native release provenance {field} must be a positive integer")
    _validate_commit(str(data.get("engine_commit") or ""))
    return data


def validate_engine_release(
    release_path: Path,
    *,
    repository: str,
    engine_tag: str,
    engine_commit: str,
    provenance: dict | None = None,
) -> dict:
    """Require one exact, public engine release and immutable tag binding."""

    _validate_repository(repository)
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", engine_tag):
        raise ValueError(f"invalid engine release tag: {engine_tag!r}")
    _validate_commit(engine_commit)
    release = json.loads(release_path.read_text(encoding="utf-8"))
    expected_keys = {
        "databaseId",
        "isDraft",
        "isPrerelease",
        "publishedAt",
        "tagName",
        "url",
    }
    if not isinstance(release, dict) or set(release) != expected_keys:
        raise ValueError("engine release does not use the closed identity schema")
    expected_url = f"https://github.com/{repository}/releases/tag/{engine_tag}"
    if (
        not isinstance(release.get("databaseId"), int)
        or release["databaseId"] <= 0
        or release.get("isDraft") is not False
        or release.get("isPrerelease") is not False
        or not isinstance(release.get("publishedAt"), str)
        or not release["publishedAt"]
        or release.get("tagName") != engine_tag
        or release.get("url") != expected_url
    ):
        raise ValueError("engine release is not the exact published stable release")
    if provenance is not None:
        expected = {
            "engine_tag": engine_tag,
            "engine_commit": engine_commit,
            "engine_release_id": release["databaseId"],
            "engine_release_url": expected_url,
            "engine_release_workflow": ENGINE_RELEASE_WORKFLOW,
        }
        for key, value in expected.items():
            if provenance.get(key) != value:
                raise ValueError(f"engine release {key} differs from signed provenance")
    return release


def write_engine_release_provenance(
    output: Path,
    *,
    directory: Path,
    release_path: Path,
    repository: str,
    engine_tag: str,
    engine_commit: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: int,
    run_attempt: int,
    runner_environment: str,
) -> Path:
    """Write the attested identity receipt for one engine release.

    The receipt binds the exact wheel and sdist bytes to the protected-main
    release workflow that created the tag and public GitHub release.
    """

    release = validate_engine_release(
        release_path,
        repository=repository,
        engine_tag=engine_tag,
        engine_commit=engine_commit,
    )
    _validate_commit(workflow_commit)
    expected_ref = f"{repository}/{ENGINE_RELEASE_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError(f"engine release workflow ref must be exactly {expected_ref!r}")
    if workflow_commit == engine_commit:
        raise ValueError("engine release workflow commit must precede the semantic release commit")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("engine release run id must be a positive integer")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("engine release run attempt must be a positive integer")
    if runner_environment != "github-hosted":
        raise ValueError("engine release provenance requires a GitHub-hosted runner")
    version = engine_tag.removeprefix("v")
    expected_names = expected_engine_asset_names(version)
    members = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if set(members) != expected_names:
        raise ValueError(
            "engine release provenance requires the exact wheel and sdist: "
            f"actual={sorted(members)}, expected={sorted(expected_names)}"
        )
    if output.exists() or output.name != ENGINE_RELEASE_PROVENANCE:
        raise ValueError(f"engine release provenance must be a new {ENGINE_RELEASE_PROVENANCE}")
    payload = {
        "schema": ENGINE_RELEASE_PROVENANCE_SCHEMA,
        "repository": repository,
        "engine_tag": engine_tag,
        "engine_commit": engine_commit,
        "engine_release_id": release["databaseId"],
        "engine_release_url": release["url"],
        "workflow_path": ENGINE_RELEASE_WORKFLOW,
        "workflow_ref": workflow_ref,
        "workflow_commit": workflow_commit,
        "event": "workflow_dispatch",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "runner_environment": runner_environment,
        "assets": [{"name": name, "sha256": digest} for name, digest in sorted(members.items())],
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_engine_release_provenance(
    path: Path,
    *,
    repository: str,
    engine_tag: str,
    engine_commit: str,
    release_path: Path,
    directory: Path | None = None,
) -> dict:
    """Validate a closed engine receipt and its optional downloaded artifacts."""

    if path.name != ENGINE_RELEASE_PROVENANCE or not path.is_file() or path.is_symlink():
        raise ValueError(f"engine release provenance must be a regular {ENGINE_RELEASE_PROVENANCE}")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema",
        "repository",
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "engine_release_url",
        "workflow_path",
        "workflow_ref",
        "workflow_commit",
        "event",
        "run_id",
        "run_attempt",
        "runner_environment",
        "assets",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("engine release provenance does not use the closed v1 schema")
    release = validate_engine_release(
        release_path,
        repository=repository,
        engine_tag=engine_tag,
        engine_commit=engine_commit,
    )
    expected = {
        "schema": ENGINE_RELEASE_PROVENANCE_SCHEMA,
        "repository": repository,
        "engine_tag": engine_tag,
        "engine_commit": engine_commit,
        "engine_release_id": release["databaseId"],
        "engine_release_url": release["url"],
        "workflow_path": ENGINE_RELEASE_WORKFLOW,
        "workflow_ref": f"{repository}/{ENGINE_RELEASE_WORKFLOW}@refs/heads/main",
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
    }
    for field, value in expected.items():
        if data.get(field) != value:
            raise ValueError(f"engine release provenance {field} differs")
    _validate_commit(str(data.get("workflow_commit") or ""))
    if data["workflow_commit"] == engine_commit:
        raise ValueError("engine release provenance does not identify the dispatched parent commit")
    for field in ("run_id", "run_attempt"):
        if not isinstance(data.get(field), int) or data[field] <= 0:
            raise ValueError(f"engine release provenance {field} is invalid")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("engine release provenance assets are invalid")
    entries: dict[str, str] = {}
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or set(asset) != {"name", "sha256"}
            or not isinstance(asset.get("name"), str)
            or Path(asset["name"]).name != asset["name"]
            or asset["name"] in entries
            or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or ""))
        ):
            raise ValueError("engine release provenance contains an invalid asset")
        entries[asset["name"]] = asset["sha256"]
    if list(entries) != sorted(entries) or set(entries) != expected_engine_asset_names(
        engine_tag.removeprefix("v")
    ):
        raise ValueError("engine release provenance does not bind the exact asset set")
    if directory is not None:
        observed = {
            item.name: hashlib.sha256(item.read_bytes()).hexdigest()
            for item in directory.iterdir()
            if item.is_file() and not item.is_symlink() and item.name != ENGINE_RELEASE_PROVENANCE
        }
        if observed != entries:
            raise ValueError("downloaded engine release artifacts differ from provenance")
    return data


def write_verified_release_index(
    output: Path,
    *,
    directory: Path,
    checksums: Path,
    provenance_path: Path,
    repository: str,
    tag: str,
    source_commit: str,
    engine_release_path: Path,
    existing: Path | None = None,
) -> Path:
    """Write the monotonic public authority for the verified installer channel."""

    verify_checksums(directory, checksums)
    provenance = validate_release_provenance(
        provenance_path,
        repository=repository,
        tag=tag,
        source_commit=source_commit,
    )
    release = validate_engine_release(
        engine_release_path,
        repository=repository,
        engine_tag=provenance["engine_tag"],
        engine_commit=provenance["engine_commit"],
        provenance=provenance,
    )
    entries = read_checksums(checksums)
    expected_assets = expected_release_asset_names(tag.removeprefix(NATIVE_TAG_PREFIX))
    if set(entries) != expected_assets:
        raise ValueError(
            "verified release index source does not contain the complete native asset set: "
            f"actual={sorted(entries)}, expected={sorted(expected_assets)}"
        )
    payload = {
        "schema": VERIFIED_RELEASE_INDEX_SCHEMA,
        "repository": repository,
        "native_tag": tag,
        "native_version": tag.removeprefix(NATIVE_TAG_PREFIX),
        "native_source_commit": source_commit,
        "native_release_provenance": NATIVE_RELEASE_PROVENANCE,
        "native_release_run_id": provenance["run_id"],
        "native_release_run_attempt": provenance["run_attempt"],
        "engine_tag": provenance["engine_tag"],
        "engine_commit": provenance["engine_commit"],
        "engine_release_id": release["databaseId"],
        "engine_release_url": release["url"],
        "engine_release_workflow": ENGINE_RELEASE_WORKFLOW,
        "checksums": {
            "name": "SHA256SUMS",
            "sha256": hashlib.sha256(checksums.read_bytes()).hexdigest(),
        },
        "assets": [{"name": name, "sha256": digest} for name, digest in sorted(entries.items())],
    }
    if existing is not None:
        _require_existing_file(existing, label="verified release index")
        prior = validate_verified_release_index(existing)
        prior_tag = prior["native_tag"]
        if native_tag_tuple(tag) < native_tag_tuple(prior_tag):
            raise ValueError(
                f"verified release index cannot move backwards from {prior_tag} to {tag}"
            )
        if native_tag_tuple(tag) == native_tag_tuple(prior_tag) and prior != payload:
            raise ValueError("verified release index cannot rewrite an existing native version")
    if output.exists() or output.name != VERIFIED_RELEASE_INDEX:
        raise ValueError(f"verified release index must be a new {VERIFIED_RELEASE_INDEX}")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_verified_release_index(path: Path) -> dict:
    """Validate the closed public index shape without trusting release notes."""

    if path.name != VERIFIED_RELEASE_INDEX or not path.is_file() or path.is_symlink():
        raise ValueError(f"verified release index must be a regular {VERIFIED_RELEASE_INDEX}")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema",
        "repository",
        "native_tag",
        "native_version",
        "native_source_commit",
        "native_release_provenance",
        "native_release_run_id",
        "native_release_run_attempt",
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "engine_release_url",
        "engine_release_workflow",
        "checksums",
        "assets",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("verified release index does not use the closed v1 schema")
    if data.get("schema") != VERIFIED_RELEASE_INDEX_SCHEMA:
        raise ValueError("verified release index has the wrong schema")
    repository = _validate_repository(str(data.get("repository") or ""))
    native_tag_tuple(str(data.get("native_tag") or ""))
    _validate_commit(str(data.get("native_source_commit") or ""))
    _validate_commit(str(data.get("engine_commit") or ""))
    if data.get("native_version") != str(data["native_tag"]).removeprefix(NATIVE_TAG_PREFIX):
        raise ValueError("verified release index version differs from its native tag")
    if data.get("engine_tag") != f"v{data['native_version']}":
        raise ValueError("verified release index engine tag differs")
    if data.get("engine_release_url") != (
        f"https://github.com/{repository}/releases/tag/{data['engine_tag']}"
    ):
        raise ValueError("verified release index engine URL differs")
    if data.get("native_release_provenance") != NATIVE_RELEASE_PROVENANCE:
        raise ValueError("verified release index names the wrong provenance file")
    if data.get("engine_release_workflow") != ENGINE_RELEASE_WORKFLOW:
        raise ValueError("verified release index names the wrong engine workflow")
    for field in ("native_release_run_id", "native_release_run_attempt", "engine_release_id"):
        if not isinstance(data.get(field), int) or data[field] <= 0:
            raise ValueError(f"verified release index {field} is invalid")
    checksums = data.get("checksums")
    if (
        not isinstance(checksums, dict)
        or set(checksums) != {"name", "sha256"}
        or checksums.get("name") != "SHA256SUMS"
        or not re.fullmatch(r"[0-9a-f]{64}", str(checksums.get("sha256") or ""))
    ):
        raise ValueError("verified release index checksum binding is invalid")
    assets = data.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("verified release index has no assets")
    names: list[str] = []
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or set(asset) != {"name", "sha256"}
            or not isinstance(asset.get("name"), str)
            or Path(asset["name"]).name != asset["name"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or ""))
        ):
            raise ValueError("verified release index contains an invalid asset")
        names.append(asset["name"])
    if names != sorted(set(names)):
        raise ValueError("verified release index assets are not unique and sorted")
    expected_assets = expected_release_asset_names(data["native_version"])
    if set(names) != expected_assets:
        raise ValueError("verified release index does not contain the complete native asset set")
    return data


def write_verified_release_channel(
    output: Path,
    *,
    index_path: Path,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: int,
    run_attempt: int,
    existing: Path | None = None,
) -> Path:
    """Write the stable, attested, strictly monotonic release descriptor."""

    repository = _validate_repository(repository)
    index = validate_verified_release_index(index_path)
    if index["repository"] != repository:
        raise ValueError("verified index belongs to a different repository")
    _validate_commit(workflow_commit)
    expected_ref = f"{repository}/{NATIVE_PROMOTION_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError(f"release channel workflow ref must be exactly {expected_ref!r}")
    if workflow_commit != index["native_source_commit"]:
        raise ValueError("release channel workflow commit must equal the native source commit")
    if not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("release channel run id must be a positive integer")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("release channel run attempt must be a positive integer")
    prior_sha256 = None
    prior_version = None
    if existing is not None:
        _require_existing_file(existing, label="release channel")
        prior = validate_verified_release_channel(existing)
        if prior["repository"] != repository:
            raise ValueError("prior release channel belongs to a different repository")
        if native_tag_tuple(index["native_tag"]) <= native_tag_tuple(prior["native_tag"]):
            raise ValueError(
                "release channel must strictly advance from "
                f"{prior['native_tag']} to {index['native_tag']}"
            )
        prior_sha256 = hashlib.sha256(existing.read_bytes()).hexdigest()
        prior_version = prior["native_version"]
    engine_base = f"https://github.com/{repository}/releases/download/{index['engine_tag']}"
    payload = {
        "schema": VERIFIED_RELEASE_CHANNEL_SCHEMA,
        "repository": repository,
        "channel": "stable-native",
        "native_tag": index["native_tag"],
        "native_version": index["native_version"],
        "native_source_commit": index["native_source_commit"],
        "native_release_url": (
            f"https://github.com/{repository}/releases/tag/{index['native_tag']}"
        ),
        "engine_tag": index["engine_tag"],
        "engine_commit": index["engine_commit"],
        "engine_release_id": index["engine_release_id"],
        "engine_release_url": index["engine_release_url"],
        "verified_index": {
            "name": VERIFIED_RELEASE_INDEX,
            "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            "url": f"{engine_base}/{VERIFIED_RELEASE_INDEX}",
        },
        "checksums": {
            **index["checksums"],
            "url": f"{engine_base}/SHA256SUMS",
        },
        "previous": (
            {"native_version": prior_version, "sha256": prior_sha256}
            if prior_sha256 is not None
            else None
        ),
        "promotion": {
            "workflow_path": NATIVE_PROMOTION_WORKFLOW,
            "workflow_ref": workflow_ref,
            "workflow_commit": workflow_commit,
            "event": "workflow_dispatch",
            "run_id": run_id,
            "run_attempt": run_attempt,
            "runner_environment": "github-hosted",
        },
    }
    if output.exists() or output.name != VERIFIED_RELEASE_CHANNEL:
        raise ValueError(f"release channel must be a new {VERIFIED_RELEASE_CHANNEL}")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_verified_release_channel(path: Path) -> dict:
    """Validate the closed stable-channel descriptor without network trust."""

    if path.name != VERIFIED_RELEASE_CHANNEL or not path.is_file() or path.is_symlink():
        raise ValueError(f"release channel must be a regular {VERIFIED_RELEASE_CHANNEL}")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema",
        "repository",
        "channel",
        "native_tag",
        "native_version",
        "native_source_commit",
        "native_release_url",
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "engine_release_url",
        "verified_index",
        "checksums",
        "previous",
        "promotion",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("release channel does not use the closed v1 schema")
    if data.get("schema") != VERIFIED_RELEASE_CHANNEL_SCHEMA:
        raise ValueError("release channel has the wrong schema")
    repository = _validate_repository(str(data.get("repository") or ""))
    if data.get("channel") != "stable-native":
        raise ValueError("release channel has the wrong channel name")
    native_tag_tuple(str(data.get("native_tag") or ""))
    version = data["native_tag"].removeprefix(NATIVE_TAG_PREFIX)
    if data.get("native_version") != version:
        raise ValueError("release channel version differs from its native tag")
    _validate_commit(str(data.get("native_source_commit") or ""))
    _validate_commit(str(data.get("engine_commit") or ""))
    if data.get("engine_tag") != f"v{version}":
        raise ValueError("release channel engine tag differs")
    expected_urls = {
        "native_release_url": (
            f"https://github.com/{repository}/releases/tag/{data['native_tag']}"
        ),
        "engine_release_url": (
            f"https://github.com/{repository}/releases/tag/{data['engine_tag']}"
        ),
    }
    for field, expected in expected_urls.items():
        if data.get(field) != expected:
            raise ValueError(f"release channel {field} differs")
    if not isinstance(data.get("engine_release_id"), int) or data["engine_release_id"] <= 0:
        raise ValueError("release channel engine release id is invalid")
    engine_base = f"https://github.com/{repository}/releases/download/{data['engine_tag']}"
    for field, name in (
        ("verified_index", VERIFIED_RELEASE_INDEX),
        ("checksums", "SHA256SUMS"),
    ):
        value = data.get(field)
        if (
            not isinstance(value, dict)
            or set(value) != {"name", "sha256", "url"}
            or value.get("name") != name
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or ""))
            or value.get("url") != f"{engine_base}/{name}"
        ):
            raise ValueError(f"release channel {field} binding is invalid")
    previous = data.get("previous")
    if previous is not None and (
        not isinstance(previous, dict)
        or set(previous) != {"native_version", "sha256"}
        or not VERSION_PATTERN.fullmatch(str(previous.get("native_version") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(previous.get("sha256") or ""))
        or native_tag_tuple(f"{NATIVE_TAG_PREFIX}{previous['native_version']}")
        >= native_tag_tuple(data["native_tag"])
    ):
        raise ValueError("release channel prior binding is invalid")
    promotion = data.get("promotion")
    expected_promotion = {
        "workflow_path": NATIVE_PROMOTION_WORKFLOW,
        "workflow_ref": (f"{repository}/{NATIVE_PROMOTION_WORKFLOW}@refs/heads/main"),
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
    }
    if not isinstance(promotion, dict) or set(promotion) != {
        *expected_promotion,
        "workflow_commit",
        "run_id",
        "run_attempt",
    }:
        raise ValueError("release channel promotion binding is invalid")
    for field, expected in expected_promotion.items():
        if promotion.get(field) != expected:
            raise ValueError(f"release channel promotion {field} differs")
    _validate_commit(str(promotion.get("workflow_commit") or ""))
    if promotion.get("workflow_commit") != data["native_source_commit"]:
        raise ValueError("release channel promotion commit differs from the native source")
    for field in ("run_id", "run_attempt"):
        if not isinstance(promotion.get(field), int) or promotion[field] <= 0:
            raise ValueError(f"release channel promotion {field} is invalid")
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
    repository_url = f"https://github.com/{repository}"
    workflow_uri = f"{repository_url}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    invocation_uri = f"{repository_url}/actions/runs/{run_id}/attempts/{run_attempt}"
    certificate_claims = {
        "subjectAlternativeName": workflow_uri,
        "githubWorkflowTrigger": "workflow_dispatch",
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
        "buildTrigger": "workflow_dispatch",
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
    if not isinstance(github, dict) or github.get("event_name") != "workflow_dispatch":
        raise ValueError("attestation was not produced by a workflow dispatch")
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


def validate_release_workflow_run(path: Path, *, provenance: dict) -> int:
    """Require the exact source run and its protected publish job to succeed."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub workflow run payload must be an object")
    expected = {
        "databaseId": provenance["run_id"],
        "attempt": provenance["run_attempt"],
        "conclusion": "success",
        "event": "workflow_dispatch",
        "headBranch": "main",
        "headSha": provenance["source_commit"],
        "name": NATIVE_RELEASE_WORKFLOW_NAME,
        "status": "completed",
        "workflowName": NATIVE_RELEASE_WORKFLOW_NAME,
        "url": (
            f"https://github.com/{provenance['repository']}/actions/runs/{provenance['run_id']}"
        ),
    }
    for field, value in expected.items():
        if data.get(field) != value:
            raise ValueError(
                f"native release workflow run {field} differs: expected {value!r}, "
                f"got {data.get(field)!r}"
            )
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("native release workflow run has no job evidence")
    conclusions: dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            raise ValueError("native release workflow run has malformed job evidence")
        if job["name"] in conclusions:
            raise ValueError(f"native release workflow run repeats job {job['name']!r}")
        conclusions[job["name"]] = job.get("conclusion")
        if job.get("status") != "completed":
            raise ValueError(f"native release workflow job is not complete: {job['name']}")
    required = {
        "Validate reviewed main release source",
        "macOS arm64",
        "macOS x86_64",
        "Windows x86_64",
        "Linux x86_64 (GitHub-attested bytes)",
        "Checksum and attest exact release bytes",
        "Publish the verified Beta prerelease",
    }
    failed = {
        name: conclusions.get(name) for name in required if conclusions.get(name) != "success"
    }
    if failed:
        raise ValueError(f"native release workflow did not pass every required job: {failed}")
    return len(required)


def validate_release_set(directory: Path) -> int:
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
        expected_trust = PRODUCTION_TRUST_MODES.get(platform)
        if signing != expected_trust:
            raise ValueError(
                f"production release requires {platform} trust mode "
                f"{expected_trust!r}, got {signing!r}"
            )
        if metadata.get("native_version") != version:
            raise ValueError(f"wrong native version in {metadata_path}")
        prefix = f"OpenAdapt-Desktop-Beta-v{version}-{platform}-{architecture}-{signing}"
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
        "verification": WEBSITE_RELEASE_VERIFICATION,
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
    if data.get("verification") != WEBSITE_RELEASE_VERIFICATION:
        raise ValueError("website release manifest has an invalid verification contract")
    directory = path.parent
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
    expected_sbom_name = f"OpenAdapt-Desktop-{data['native_tag']}.cyclonedx.json"
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
        | {
            expected_sbom_name,
            WEBSITE_RELEASE_MANIFEST,
            NATIVE_RELEASE_PROVENANCE,
            NATIVE_RELEASE_VERIFIER,
        }
    )
    if set(checksum_entries) != expected_checksum_names:
        raise ValueError("SHA256SUMS does not describe the exact release file set")
    for name in (
        expected_names
        | metadata_names
        | {
            expected_sbom_name,
            WEBSITE_RELEASE_MANIFEST,
            NATIVE_RELEASE_PROVENANCE,
            NATIVE_RELEASE_VERIFIER,
        }
    ):
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

    supersede_parser = subparsers.add_parser("supersede-notes")
    supersede_parser.add_argument("--newer-tag", required=True)
    supersede_parser.add_argument("--candidate-tag", required=True)
    supersede_parser.add_argument("--notes-file", type=Path, required=True)
    supersede_parser.add_argument("--output", type=Path, required=True)
    supersede_parser.add_argument("--repo", default="OpenAdaptAI/openadapt-desktop")

    pointer_parser = subparsers.add_parser("installer-pointer-notes")
    pointer_parser.add_argument("--native-tag", required=True)
    pointer_parser.add_argument("--notes-file", type=Path, required=True)
    pointer_parser.add_argument("--output", type=Path, required=True)
    pointer_parser.add_argument("--repo", default="OpenAdaptAI/openadapt-desktop")

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
    provenance_parser.add_argument("--engine-tag", required=True)
    provenance_parser.add_argument("--engine-commit", required=True)
    provenance_parser.add_argument("--engine-release-id", type=int, required=True)
    provenance_parser.add_argument("--engine-release-url", required=True)

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
    attestation_parser.add_argument("--github-output", type=Path)

    workflow_run_parser = subparsers.add_parser("validate-workflow-run")
    workflow_run_parser.add_argument("--file", type=Path, required=True)
    workflow_run_parser.add_argument("--provenance", type=Path, required=True)
    workflow_run_parser.add_argument("--repository", required=True)
    workflow_run_parser.add_argument("--tag", required=True)
    workflow_run_parser.add_argument("--source-commit", required=True)

    selection_parser = subparsers.add_parser("select-published-native")
    selection_parser.add_argument("--releases", type=Path, required=True)
    selection_parser.add_argument("--event-tag")
    selection_parser.add_argument("--github-output", type=Path)

    order_parser = subparsers.add_parser("validate-release-order")
    order_parser.add_argument("--tags", type=Path, required=True)
    order_parser.add_argument("--candidate-tag", required=True)

    engine_release_parser = subparsers.add_parser("validate-engine-release")
    engine_release_parser.add_argument("--file", type=Path, required=True)
    engine_release_parser.add_argument("--repository", required=True)
    engine_release_parser.add_argument("--engine-tag", required=True)
    engine_release_parser.add_argument("--engine-commit", required=True)
    engine_release_parser.add_argument("--provenance", type=Path)
    engine_release_parser.add_argument("--github-output", type=Path)

    engine_provenance_parser = subparsers.add_parser("write-engine-provenance")
    engine_provenance_parser.add_argument("--output", type=Path, required=True)
    engine_provenance_parser.add_argument("--directory", type=Path, required=True)
    engine_provenance_parser.add_argument("--release", type=Path, required=True)
    engine_provenance_parser.add_argument("--repository", required=True)
    engine_provenance_parser.add_argument("--engine-tag", required=True)
    engine_provenance_parser.add_argument("--engine-commit", required=True)
    engine_provenance_parser.add_argument("--workflow-ref", required=True)
    engine_provenance_parser.add_argument("--workflow-commit", required=True)
    engine_provenance_parser.add_argument("--run-id", type=int, required=True)
    engine_provenance_parser.add_argument("--run-attempt", type=int, required=True)
    engine_provenance_parser.add_argument("--runner-environment", required=True)

    validate_engine_provenance_parser = subparsers.add_parser("validate-engine-provenance")
    validate_engine_provenance_parser.add_argument("--file", type=Path, required=True)
    validate_engine_provenance_parser.add_argument("--directory", type=Path, required=True)
    validate_engine_provenance_parser.add_argument("--release", type=Path, required=True)
    validate_engine_provenance_parser.add_argument("--repository", required=True)
    validate_engine_provenance_parser.add_argument("--engine-tag", required=True)
    validate_engine_provenance_parser.add_argument("--engine-commit", required=True)

    index_parser = subparsers.add_parser("write-verified-index")
    index_parser.add_argument("--output", type=Path, required=True)
    index_parser.add_argument("--directory", type=Path, required=True)
    index_parser.add_argument("--checksums", type=Path, required=True)
    index_parser.add_argument("--provenance", type=Path, required=True)
    index_parser.add_argument("--repository", required=True)
    index_parser.add_argument("--tag", required=True)
    index_parser.add_argument("--source-commit", required=True)
    index_parser.add_argument("--engine-release", type=Path, required=True)
    index_parser.add_argument("--existing", type=Path)
    validate_index_parser = subparsers.add_parser("validate-verified-index")
    validate_index_parser.add_argument("--file", type=Path, required=True)
    channel_parser = subparsers.add_parser("write-release-channel")
    channel_parser.add_argument("--output", type=Path, required=True)
    channel_parser.add_argument("--index", type=Path, required=True)
    channel_parser.add_argument("--repository", required=True)
    channel_parser.add_argument("--workflow-ref", required=True)
    channel_parser.add_argument("--workflow-commit", required=True)
    channel_parser.add_argument("--run-id", type=int, required=True)
    channel_parser.add_argument("--run-attempt", type=int, required=True)
    channel_parser.add_argument("--existing", type=Path)
    validate_channel_parser = subparsers.add_parser("validate-release-channel")
    validate_channel_parser.add_argument("--file", type=Path, required=True)
    return parser


def _write_github_output(path: Path | None, values: dict[str, str | int]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            text = str(value)
            if not re.fullmatch(r"[A-Za-z0-9._/-]+", text):
                raise ValueError(f"unsafe GitHub output value for {key}: {text!r}")
            stream.write(f"{key}={text}\n")


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
        elif args.command == "supersede-notes":
            try:
                candidate = native_tag_tuple(args.candidate_tag)
            except ValueError:
                candidate = None
            if candidate is None or candidate >= native_tag_tuple(args.newer_tag):
                print("skip")
            else:
                notes = superseded_notes(
                    args.notes_file.read_text(encoding="utf-8"), args.newer_tag, args.repo
                )
                if notes is None:
                    print("skip")
                else:
                    args.output.write_text(notes, encoding="utf-8")
                    print("update")
        elif args.command == "installer-pointer-notes":
            notes = installer_pointer_notes(
                args.notes_file.read_text(encoding="utf-8"), args.native_tag, args.repo
            )
            if notes is None:
                print("skip")
            else:
                args.output.write_text(notes, encoding="utf-8")
                print("update")
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
                engine_tag=args.engine_tag,
                engine_commit=args.engine_commit,
                engine_release_id=args.engine_release_id,
                engine_release_url=args.engine_release_url,
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
                f"Validated native release provenance for run {provenance['run_id']} "
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
            _write_github_output(
                args.github_output,
                {
                    "run_id": provenance["run_id"],
                    "run_attempt": provenance["run_attempt"],
                    "engine_tag": provenance["engine_tag"],
                    "engine_commit": provenance["engine_commit"],
                    "engine_release_id": provenance["engine_release_id"],
                },
            )
            print(
                f"Validated exact release attestation for run {provenance['run_id']} "
                f"attempt {provenance['run_attempt']}"
            )
        elif args.command == "validate-workflow-run":
            provenance = validate_release_provenance(
                args.provenance,
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
            )
            count = validate_release_workflow_run(args.file, provenance=provenance)
            print(f"Validated {count} required native release workflow jobs")
        elif args.command == "select-published-native":
            releases = json.loads(args.releases.read_text(encoding="utf-8"))
            selected = select_latest_native_release(releases)
            tag = selected["tag_name"]
            values = {
                "native_tag": tag,
                "event_is_selected": str(args.event_tag == tag).lower(),
            }
            _write_github_output(args.github_output, values)
            print(json.dumps(values, sort_keys=True))
        elif args.command == "validate-release-order":
            tags = native_release_tags(args.tags.read_text(encoding="utf-8"))
            print(validate_native_tag_order(args.candidate_tag, tags))
        elif args.command == "validate-engine-release":
            provenance = None
            if args.provenance is not None:
                raw = json.loads(args.provenance.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("native release provenance is not an object")
                provenance = raw
            release = validate_engine_release(
                args.file,
                repository=args.repository,
                engine_tag=args.engine_tag,
                engine_commit=args.engine_commit,
                provenance=provenance,
            )
            _write_github_output(
                args.github_output,
                {
                    "engine_release_id": release["databaseId"],
                },
            )
            print(f"Validated engine release {args.engine_tag} ({release['databaseId']})")
        elif args.command == "write-engine-provenance":
            print(
                write_engine_release_provenance(
                    args.output,
                    directory=args.directory,
                    release_path=args.release,
                    repository=args.repository,
                    engine_tag=args.engine_tag,
                    engine_commit=args.engine_commit,
                    workflow_ref=args.workflow_ref,
                    workflow_commit=args.workflow_commit,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    runner_environment=args.runner_environment,
                )
            )
        elif args.command == "validate-engine-provenance":
            receipt = validate_engine_release_provenance(
                args.file,
                directory=args.directory,
                release_path=args.release,
                repository=args.repository,
                engine_tag=args.engine_tag,
                engine_commit=args.engine_commit,
            )
            print(
                "Validated protected-main engine release provenance for run "
                f"{receipt['run_id']} attempt {receipt['run_attempt']}"
            )
        elif args.command == "write-verified-index":
            print(
                write_verified_release_index(
                    args.output,
                    directory=args.directory,
                    checksums=args.checksums,
                    provenance_path=args.provenance,
                    repository=args.repository,
                    tag=args.tag,
                    source_commit=args.source_commit,
                    engine_release_path=args.engine_release,
                    existing=args.existing,
                )
            )
        elif args.command == "validate-verified-index":
            index = validate_verified_release_index(args.file)
            print(f"Validated verified release index for {index['native_tag']}")
        elif args.command == "write-release-channel":
            print(
                write_verified_release_channel(
                    args.output,
                    index_path=args.index,
                    repository=args.repository,
                    workflow_ref=args.workflow_ref,
                    workflow_commit=args.workflow_commit,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    existing=args.existing,
                )
            )
        elif args.command == "validate-release-channel":
            channel = validate_verified_release_channel(args.file)
            print(f"Validated stable release channel for {channel['native_tag']}")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
