#!/usr/bin/env python3
"""Authenticate and verify an OpenAdapt native release download.

The channel mode verifies GitHub attestations for the candidate descriptor, its
selected index, and ``SHA256SUMS`` before it accepts any installer bytes. It
then checks the complete descriptor -> index -> checksum -> asset hash chain.

The inventory-only mode is retained for release automation that has already
authenticated ``SHA256SUMS``. It refuses missing, extra, linked, non-regular,
duplicate, or digest-mismatched files. This helper uses only the Python
standard library so it can run before installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

DEFAULT_REPOSITORY = "OpenAdaptAI/openadapt-desktop"
NATIVE_TAG_PREFIX = "desktop-v"
NATIVE_RELEASE_WORKFLOW = ".github/workflows/native-release.yml"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
CHANNEL_NAME = "openadapt-desktop-channel.json"
CHANNEL_SCHEMA = "openadapt.desktop-release-channel/v2"
LEGACY_CHANNEL_SCHEMA = "openadapt.desktop-release-channel/v1"
INDEX_NAME = "openadapt-desktop-verified-release.json"
INDEX_SCHEMA = "openadapt.desktop-verified-release/v1"
PROVENANCE_NAME = "openadapt-desktop-native-release-provenance.json"
VERIFIER_NAME = "verify-openadapt-native-release.py"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _no_follow_opener(path: str, flags: int) -> int:
    """Open a path, refusing a symbolic link at the final component.

    ``O_NOFOLLOW`` exists on POSIX. Windows has no such flag and no equivalent,
    so the value falls back to zero there and the caller keeps its explicit
    ``is_symlink`` check.
    """

    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def _read_bytes(path: Path) -> bytes:
    """Read the exact bytes of one path without following a symbolic link.

    A separate ``is_symlink`` test and a later ``read_bytes`` resolve the path
    twice, so a local writer can replace a checked file with a link between the
    two calls. This reads through one descriptor that the kernel refuses to
    open on a link.
    """

    with open(path, "rb", opener=_no_follow_opener) as stream:
        return stream.read()


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    """Read one regular file exactly once.

    Every authenticated check parses the same bytes it hashed. Re-reading a
    file between the hash and the parse would let a local writer swap the
    content after it passed verification.
    """

    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file")
    return _read_bytes(path)


def _sha256(path: Path, *, label: str) -> str:
    return hashlib.sha256(_read_regular_bytes(path, label=label)).hexdigest()


def _load_closed_json(
    path: Path, *, name: str, keys: set[str], label: str, data: bytes | None = None
) -> dict:
    if path.name != name or not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular {name}")
    if data is None:
        data = _read_regular_bytes(path, label=label)
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict) or set(document) != keys:
        raise ValueError(f"{label} does not use its closed schema")
    return document


def _version_tuple(version: str) -> tuple[int, int, int]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(f"invalid native version: {version!r}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def expected_asset_names(version: str) -> set[str]:
    """Return the exact asset set one native version publishes.

    ``expected_release_asset_names`` in ``scripts/native_release.py`` states the
    same contract. This file ships beside the installers as a standalone
    stdlib-only verifier, so it cannot import that module.
    ``test_standalone_verifier_states_the_same_asset_contract`` fails when the
    two definitions drift apart.
    """

    _version_tuple(version)
    names = {
        "openadapt-desktop-release-manifest.json",
        PROVENANCE_NAME,
        VERIFIER_NAME,
        f"OpenAdapt-Desktop-desktop-v{version}.cyclonedx.json",
    }
    platforms = {
        ("macos", "arm64", "developer-id-notarized", (".dmg",)),
        ("macos", "x86_64", "developer-id-notarized", (".dmg",)),
        ("windows", "x86_64", "authenticode", (".msi", "-nsis-setup.exe")),
        ("linux", "x86_64", "github-attested", (".deb", ".AppImage")),
    }
    for platform, architecture, signing, suffixes in platforms:
        prefix = f"OpenAdapt-Desktop-Candidate-v{version}-{platform}-{architecture}-{signing}"
        names.add(f"{prefix}-metadata.json")
        names.update(f"{prefix}{suffix}" for suffix in suffixes)
    return names


def _validate_hash_binding(value: object, *, name: str, url: str, label: str) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "sha256", "url"}
        or value.get("name") != name
        or SHA256_PATTERN.fullmatch(str(value.get("sha256") or "")) is None
        or value.get("url") != url
    ):
        raise ValueError(f"{label} binding is invalid")
    return value


def validate_channel(
    path: Path, *, repository: str = DEFAULT_REPOSITORY, raw: bytes | None = None
) -> dict:
    """Validate the closed candidate-channel descriptor.

    Pass ``raw`` to parse exactly the bytes an earlier check already hashed.
    """

    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"invalid repository: {repository!r}")
    data = _load_closed_json(
        path,
        data=raw,
        name=CHANNEL_NAME,
        label="release channel",
        keys={
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
        },
    )
    schema = data.get("schema")
    if (
        schema not in {CHANNEL_SCHEMA, LEGACY_CHANNEL_SCHEMA}
        or data.get("repository") != repository
    ):
        raise ValueError("release channel identity is invalid")
    expected_channel = "candidate-native" if schema == CHANNEL_SCHEMA else "stable-native"
    if data.get("channel") != expected_channel:
        raise ValueError("release channel has the wrong channel name")
    version = str(data.get("native_version") or "")
    _version_tuple(version)
    native_tag = f"{NATIVE_TAG_PREFIX}{version}"
    engine_tag = f"v{version}"
    if data.get("native_tag") != native_tag or data.get("engine_tag") != engine_tag:
        raise ValueError("release channel tag and version differ")
    for field in ("native_source_commit", "engine_commit"):
        if COMMIT_PATTERN.fullmatch(str(data.get(field) or "")) is None:
            raise ValueError(f"release channel {field} is invalid")
    native_url = f"https://github.com/{repository}/releases/tag/{native_tag}"
    engine_url = f"https://github.com/{repository}/releases/tag/{engine_tag}"
    if data.get("native_release_url") != native_url or data.get("engine_release_url") != engine_url:
        raise ValueError("release channel URL is invalid")
    if not isinstance(data.get("engine_release_id"), int) or data["engine_release_id"] <= 0:
        raise ValueError("release channel engine release id is invalid")
    engine_assets = f"https://github.com/{repository}/releases/download/{engine_tag}"
    _validate_hash_binding(
        data.get("verified_index"),
        name=INDEX_NAME,
        url=f"{engine_assets}/{INDEX_NAME}",
        label="release channel index",
    )
    _validate_hash_binding(
        data.get("checksums"),
        name="SHA256SUMS",
        url=f"{engine_assets}/SHA256SUMS",
        label="release channel checksum",
    )
    previous = data.get("previous")
    if previous is not None and (
        not isinstance(previous, dict)
        or set(previous) != {"native_version", "sha256"}
        or VERSION_PATTERN.fullmatch(str(previous.get("native_version") or "")) is None
        or SHA256_PATTERN.fullmatch(str(previous.get("sha256") or "")) is None
        or _version_tuple(previous["native_version"]) >= _version_tuple(version)
    ):
        raise ValueError("release channel prior binding is invalid")
    promotion = data.get("promotion")
    workflow_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    expected_promotion = {
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": workflow_ref,
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
    if any(promotion.get(field) != value for field, value in expected_promotion.items()):
        raise ValueError("release channel promotion identity is invalid")
    if COMMIT_PATTERN.fullmatch(str(promotion.get("workflow_commit") or "")) is None:
        raise ValueError("release channel promotion commit is invalid")
    if promotion.get("workflow_commit") != data["native_source_commit"]:
        raise ValueError("release channel promotion commit differs from the native source")
    for field in ("run_id", "run_attempt"):
        if not isinstance(promotion.get(field), int) or promotion[field] <= 0:
            raise ValueError(f"release channel promotion {field} is invalid")
    return data


def validate_index(
    path: Path, *, repository: str = DEFAULT_REPOSITORY, raw: bytes | None = None
) -> dict:
    """Validate the closed selected-release index.

    Pass ``raw`` to parse exactly the bytes an earlier check already hashed.
    """

    data = _load_closed_json(
        path,
        data=raw,
        name=INDEX_NAME,
        label="verified release index",
        keys={
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
        },
    )
    if data.get("schema") != INDEX_SCHEMA or data.get("repository") != repository:
        raise ValueError("verified release index identity is invalid")
    version = str(data.get("native_version") or "")
    _version_tuple(version)
    if data.get("native_tag") != f"{NATIVE_TAG_PREFIX}{version}":
        raise ValueError("verified release index native tag differs")
    if data.get("engine_tag") != f"v{version}":
        raise ValueError("verified release index engine tag differs")
    for field in ("native_source_commit", "engine_commit"):
        if COMMIT_PATTERN.fullmatch(str(data.get(field) or "")) is None:
            raise ValueError(f"verified release index {field} is invalid")
    if data.get("native_release_provenance") != PROVENANCE_NAME:
        raise ValueError("verified release index provenance name differs")
    if data.get("engine_release_workflow") != ".github/workflows/release.yml":
        raise ValueError("verified release index engine workflow differs")
    expected_engine_url = f"https://github.com/{repository}/releases/tag/v{version}"
    if data.get("engine_release_url") != expected_engine_url:
        raise ValueError("verified release index engine URL differs")
    for field in ("native_release_run_id", "native_release_run_attempt", "engine_release_id"):
        if not isinstance(data.get(field), int) or data[field] <= 0:
            raise ValueError(f"verified release index {field} is invalid")
    checksums = data.get("checksums")
    if (
        not isinstance(checksums, dict)
        or set(checksums) != {"name", "sha256"}
        or checksums.get("name") != "SHA256SUMS"
        or SHA256_PATTERN.fullmatch(str(checksums.get("sha256") or "")) is None
    ):
        raise ValueError("verified release index checksum binding is invalid")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("verified release index assets are invalid")
    entries: dict[str, str] = {}
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or set(asset) != {"name", "sha256"}
            or not isinstance(asset.get("name"), str)
            or Path(asset["name"]).name != asset["name"]
            or asset["name"] in entries
            or SHA256_PATTERN.fullmatch(str(asset.get("sha256") or "")) is None
        ):
            raise ValueError("verified release index contains an invalid asset")
        entries[asset["name"]] = asset["sha256"]
    if list(entries) != sorted(entries) or set(entries) != expected_asset_names(version):
        raise ValueError("verified release index does not contain the exact asset set")
    return data


def _verify_github_attestation(
    path: Path, *, repository: str, identity_ref: str
) -> tuple[str, bytes]:
    """Authenticate one file and return its digest with the exact bytes read.

    The caller parses the returned bytes. Reading the file again after this
    check would reopen the window this function closes.
    """

    before = _sha256(path, label=path.name)
    identity = f"https://github.com/{repository}/{NATIVE_RELEASE_WORKFLOW}@{identity_ref}"
    # `gh attestation verify` treats --cert-identity, --cert-identity-regex,
    # --signer-repo, and --signer-workflow as one mutually exclusive group: it
    # refuses the command outright when two of them are set. Keep only
    # --cert-identity. It is the strictest of the four, because it pins the
    # complete subject alternative name -- repository, workflow path, and ref --
    # rather than the repository and workflow path alone.
    command = [
        "gh",
        "attestation",
        "verify",
        str(path.resolve()),
        "--repo",
        repository,
        "--cert-identity",
        identity,
        "--cert-oidc-issuer",
        GITHUB_OIDC_ISSUER,
        "--deny-self-hosted-runners",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError("GitHub CLI is required for attestation verification") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "verification failed").strip()
        raise ValueError(f"GitHub attestation verification failed: {detail}") from exc
    data = _read_regular_bytes(path, label=path.name)
    after = hashlib.sha256(data).hexdigest()
    if after != before:
        raise ValueError(f"{path.name} changed during attestation verification")
    return after, data


def verify_authenticated_channel(
    *,
    channel_path: Path,
    index_path: Path,
    directory: Path,
    checksums: Path,
    previous_channel: Path | None = None,
    minimum_version: str | None = None,
    repository: str = DEFAULT_REPOSITORY,
) -> int:
    """Authenticate one channel selection and verify its complete asset set."""

    _, channel_bytes = _verify_github_attestation(
        channel_path, repository=repository, identity_ref="refs/heads/main"
    )
    channel = validate_channel(channel_path, repository=repository, raw=channel_bytes)
    if minimum_version is not None and _version_tuple(channel["native_version"]) < _version_tuple(
        minimum_version
    ):
        raise ValueError(f"release channel version is below the trusted minimum {minimum_version}")

    if previous_channel is not None:
        previous_bytes = _read_regular_bytes(previous_channel, label="previous release channel")
        previous = validate_channel(previous_channel, repository=repository, raw=previous_bytes)
        previous_digest = hashlib.sha256(previous_bytes).hexdigest()
        expected_previous = {
            "native_version": previous["native_version"],
            "sha256": previous_digest,
        }
        if channel["previous"] != expected_previous:
            raise ValueError("release channel does not extend the accepted prior descriptor")
        if _version_tuple(channel["native_version"]) <= _version_tuple(previous["native_version"]):
            raise ValueError("release channel does not advance the accepted version")

    # One reviewed-main workflow dispatch attests the native bytes, verified
    # index, and channel. Every certificate therefore carries the same protected
    # main workflow identity.
    index_digest, index_bytes = _verify_github_attestation(
        index_path,
        repository=repository,
        identity_ref="refs/heads/main",
    )
    if index_digest != channel["verified_index"]["sha256"]:
        raise ValueError("verified release index digest differs from the authenticated channel")
    index = validate_index(index_path, repository=repository, raw=index_bytes)

    identity_fields = {
        "native_tag",
        "native_version",
        "native_source_commit",
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "engine_release_url",
    }
    if any(channel[field] != index[field] for field in identity_fields):
        raise ValueError("verified release index identity differs from the authenticated channel")
    if channel["checksums"]["sha256"] != index["checksums"]["sha256"]:
        raise ValueError("checksum digest differs between the channel and index")
    checksum_digest, checksum_bytes = _verify_github_attestation(
        checksums,
        repository=repository,
        identity_ref="refs/heads/main",
    )
    if checksum_digest != channel["checksums"]["sha256"]:
        raise ValueError("SHA256SUMS digest differs from the authenticated channel")

    checksum_entries = read_manifest(checksums, raw=checksum_bytes)
    index_entries = {asset["name"]: asset["sha256"] for asset in index["assets"]}
    if checksum_entries != index_entries:
        raise ValueError("SHA256SUMS entries differ from the authenticated release index")
    # Verify the installers against the entries parsed from the authenticated
    # bytes, never against a fresh read of SHA256SUMS.
    return verify(directory, checksums, entries=checksum_entries)


def read_manifest(path: Path, *, raw: bytes | None = None) -> dict[str, str]:
    """Parse ``SHA256SUMS``.

    Pass ``raw`` to parse exactly the bytes an earlier check already hashed.
    """

    if raw is None:
        raw = _read_regular_bytes(path, label="SHA256SUMS")
    entries: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not name
            or Path(name).name != name
            or name in entries
        ):
            raise ValueError("SHA256SUMS contains an invalid or duplicate entry")
        entries[name] = digest
    if not entries:
        raise ValueError("SHA256SUMS is empty")
    return entries


def verify(directory: Path, manifest: Path, *, entries: dict[str, str] | None = None) -> int:
    """Verify a download directory against ``SHA256SUMS``.

    Pass ``entries`` when the caller already parsed an authenticated copy of
    the manifest, so the installer hashes are checked against those exact
    entries rather than a fresh read of the file.
    """

    directory = directory.resolve()
    manifest = manifest.resolve()
    if manifest.parent != directory or manifest.name != "SHA256SUMS":
        raise ValueError("SHA256SUMS must be inside the download directory")
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("SHA256SUMS must be a regular file")
    if entries is None:
        entries = read_manifest(manifest)
    members = {path.name: path for path in directory.iterdir()}
    unsafe = [
        name
        for name, path in members.items()
        if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    ]
    if unsafe:
        raise ValueError("download directory contains a link or non-regular file")
    actual = {name for name, path in members.items() if path != manifest}
    if actual != set(entries):
        raise ValueError("downloaded files do not equal the signed checksum inventory")
    for name, expected in entries.items():
        # Hash the exact path object the check above proved regular and
        # unlinked, and open it with O_NOFOLLOW. Rebuilding `directory / name`
        # here would resolve the name a second time and follow a link that
        # appeared in between.
        observed = hashlib.sha256(_read_bytes(members[name])).hexdigest()
        if observed != expected:
            raise ValueError(f"checksum mismatch for {name}")
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=Path("SHA256SUMS"))
    parser.add_argument(
        "--channel",
        type=Path,
        help="authenticate this candidate channel descriptor before verifying assets",
    )
    parser.add_argument(
        "--index",
        type=Path,
        help="selected release index bound by --channel",
    )
    parser.add_argument(
        "--previous-channel",
        type=Path,
        help="last accepted channel descriptor for rollback protection",
    )
    parser.add_argument(
        "--minimum-version",
        help="trusted minimum X.Y.Z version for first-use rollback protection",
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    args = parser.parse_args()
    try:
        if args.channel is None:
            if args.index is not None or args.previous_channel is not None:
                raise ValueError("--index and --previous-channel require --channel")
            count = verify(args.directory, args.manifest)
        else:
            if args.index is None:
                raise ValueError("--channel requires --index")
            count = verify_authenticated_channel(
                channel_path=args.channel,
                index_path=args.index,
                directory=args.directory,
                checksums=args.manifest,
                previous_channel=args.previous_channel,
                minimum_version=args.minimum_version,
                repository=args.repository,
            )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        parser.exit(2, f"verification failed: {exc}\n")
    if args.channel is None:
        print(f"Verified the exact {count}-file native release inventory.")
    else:
        print(f"Authenticated the release channel and verified its exact {count}-file inventory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
