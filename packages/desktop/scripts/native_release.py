#!/usr/bin/env python3
"""Prepare and verify honest Beta native release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
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
    "linux": {"unsigned"},
}
EXPECTED_PLATFORMS = {
    ("macos", "arm64"),
    ("macos", "x86_64"),
    ("windows", "x86_64"),
    ("linux", "x86_64"),
}


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


def set_native_version(version: str, root: Path = ROOT) -> dict[str, str]:
    """Synchronize every native version source (and lockfiles) to ``version``."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"native version must be X.Y.Z, got {version!r}")

    def rewrite_json(path: Path, mutate) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

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
        r'(?m)^version = "[^"]+"$', f'version = "{version}"', cargo_toml.read_text(), count=1
    )
    if replaced != 1:
        raise ValueError(f"could not rewrite package version in {cargo_toml}")
    cargo_toml.write_text(text, encoding="utf-8")

    cargo_lock = root / "src-tauri" / "Cargo.lock"
    text, replaced = re.subn(
        r'(name = "openadapt-desktop"\nversion = ")[^"]+(")',
        rf"\g<1>{version}\g<2>",
        cargo_lock.read_text(),
        count=1,
    )
    if replaced != 1:
        raise ValueError(f"could not rewrite package version in {cargo_lock}")
    cargo_lock.write_text(text, encoding="utf-8")

    synchronized = native_version(root)
    if synchronized != version:
        raise ValueError(f"native version sources disagree after sync: {native_versions(root)}")
    return native_versions(root)


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
        "> here so GitHub's \"Latest\" always carries an installer.\n"
        ">\n"
        "> **These installers are Beta and are ad-hoc-signed (macOS) or\n"
        "> unsigned (Windows, Linux)** pending signing credentials; the signing\n"
        "> state is in every filename. Your OS will warn. Verify before\n"
        "> overriding that warning:\n"
        "> `sha256sum -c SHA256SUMS` and `gh attestation verify`.\n"
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
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.resolve() != output.resolve()
    )
    if not files:
        raise ValueError(f"no release assets found in {directory}")

    entries: list[tuple[str, str]] = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, path.name))
    output.write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")
    return entries


def verify_checksums(directory: Path, manifest: Path) -> int:
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if not separator or len(digest) != 64 or Path(name).name != name:
            raise ValueError(f"invalid checksum line: {line!r}")
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
        if not path.name.endswith("-metadata.json") and path.name != "SHA256SUMS"
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version")
    tag_parser = subparsers.add_parser("validate-tag")
    tag_parser.add_argument("tag")

    set_version_parser = subparsers.add_parser("set-version")
    set_version_parser.add_argument("version")
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
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "version":
            print(native_version())
        elif args.command == "validate-tag":
            print(validate_tag(args.tag))
        elif args.command == "set-version":
            versions = set_native_version(args.version)
            for source, value in sorted(versions.items()):
                print(f"{source}: {value}")
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
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
