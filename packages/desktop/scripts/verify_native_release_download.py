#!/usr/bin/env python3
"""Authenticate and verify an OpenAdapt native release download.

The channel mode verifies the GitHub attestations for the release's own
``openadapt-desktop-channel.json`` (schema v3) and ``SHA256SUMS`` before it
accepts any installer bytes. It then checks the descriptor -> checksum -> asset
hash chain, with the exact asset names the channel's signing modes imply.

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

DEFAULT_REPOSITORY = "OpenAdaptAI/OpenAdapt"
# One immutable release per Desktop version carries the wheel, the sdist, the
# installers, and the channel.
NATIVE_TAG_PREFIX = "openadapt-desktop-v"
NATIVE_RELEASE_WORKFLOW = ".github/workflows/desktop-release.yml"
RELEASE_EVENTS = frozenset({"push", "workflow_dispatch"})
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
CHANNEL_NAME = "openadapt-desktop-channel.json"
CHANNEL_SCHEMA = "openadapt.desktop-release-channel/v3"
PROVENANCE_NAME = "openadapt-desktop-native-release-provenance.json"
VERIFIER_NAME = "verify-openadapt-native-release.py"
# The signing modes a release may use, per platform. ``native_release.py``
# states the same set; the standalone copy here cannot import it.
SIGNING_MODES = {
    "macos": {"adhoc", "developer-id-notarized"},
    "windows": {"unsigned", "authenticode"},
    "linux": {"github-attested"},
}
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


def expected_asset_names(version: str, signing: dict[str, str]) -> set[str]:
    """Return the exact asset set one native version publishes.

    ``expected_release_asset_names`` in ``scripts/native_release.py`` states the
    same contract. This file ships beside the installers as a standalone
    stdlib-only verifier, so it cannot import that module.
    ``test_standalone_verifier_states_the_same_asset_contract`` fails when the
    two definitions drift apart. ``SHA256SUMS`` lists these names; the channel
    file binds ``SHA256SUMS`` and is not in it.
    """

    _version_tuple(version)
    if (
        not isinstance(signing, dict)
        or set(signing) != set(SIGNING_MODES)
        or any(signing[platform] not in modes for platform, modes in SIGNING_MODES.items())
    ):
        raise ValueError(f"invalid release signing modes: {signing!r}")
    names = {
        "openadapt-desktop-release-manifest.json",
        PROVENANCE_NAME,
        VERIFIER_NAME,
        f"OpenAdapt-Desktop-v{version}.cyclonedx.json",
        f"openadapt_desktop-{version}-py3-none-any.whl",
        f"openadapt_desktop-{version}.tar.gz",
    }
    platforms = {
        ("macos", "arm64", (".dmg",)),
        ("macos", "x86_64", (".dmg",)),
        ("windows", "x86_64", (".msi", "-nsis-setup.exe")),
        ("linux", "x86_64", (".deb", ".AppImage")),
    }
    for platform, architecture, suffixes in platforms:
        prefix = (
            f"OpenAdapt-Desktop-Candidate-v{version}-{platform}-{architecture}-{signing[platform]}"
        )
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
    """Validate the closed v3 channel descriptor.

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
            "signing",
            "checksums",
            "provenance",
            "previous",
            "promotion",
        },
    )
    if data.get("schema") != CHANNEL_SCHEMA or data.get("repository") != repository:
        raise ValueError("release channel identity is invalid")
    if data.get("channel") != "candidate-native":
        raise ValueError("release channel has the wrong channel name")
    version = str(data.get("native_version") or "")
    _version_tuple(version)
    native_tag = f"{NATIVE_TAG_PREFIX}{version}"
    if data.get("native_tag") != native_tag:
        raise ValueError("release channel tag and version differ")
    if COMMIT_PATTERN.fullmatch(str(data.get("native_source_commit") or "")) is None:
        raise ValueError("release channel native_source_commit is invalid")
    native_url = f"https://github.com/{repository}/releases/tag/{native_tag}"
    if data.get("native_release_url") != native_url:
        raise ValueError("release channel URL is invalid")
    expected_asset_names(version, data.get("signing"))
    assets = f"https://github.com/{repository}/releases/download/{native_tag}"
    _validate_hash_binding(
        data.get("checksums"),
        name="SHA256SUMS",
        url=f"{assets}/SHA256SUMS",
        label="release channel checksum",
    )
    _validate_hash_binding(
        data.get("provenance"),
        name=PROVENANCE_NAME,
        url=f"{assets}/{PROVENANCE_NAME}",
        label="release channel provenance",
    )
    previous = data.get("previous")
    if previous is not None:
        if (
            not isinstance(previous, dict)
            or set(previous) != {"native_tag", "sha256", "url"}
            or SHA256_PATTERN.fullmatch(str(previous.get("sha256") or "")) is None
            or not str(previous.get("native_tag") or "").startswith(NATIVE_TAG_PREFIX)
        ):
            raise ValueError("release channel prior binding is invalid")
        previous_version = previous["native_tag"][len(NATIVE_TAG_PREFIX) :]
        if _version_tuple(previous_version) >= _version_tuple(version):
            raise ValueError("release channel prior binding does not precede it")
        expected_url = (
            f"https://github.com/{repository}/releases/download/{previous['native_tag']}/"
            f"{CHANNEL_NAME}"
        )
        if previous.get("url") != expected_url:
            raise ValueError("release channel prior URL is invalid")
    promotion = data.get("promotion")
    workflow_ref = f"{repository}/{NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    expected_promotion = {
        "workflow_path": NATIVE_RELEASE_WORKFLOW,
        "workflow_ref": workflow_ref,
        "runner_environment": "github-hosted",
    }
    if not isinstance(promotion, dict) or set(promotion) != {
        *expected_promotion,
        "event",
        "workflow_commit",
        "run_id",
        "run_attempt",
    }:
        raise ValueError("release channel promotion binding is invalid")
    if any(promotion.get(field) != value for field, value in expected_promotion.items()):
        raise ValueError("release channel promotion identity is invalid")
    if promotion.get("event") not in RELEASE_EVENTS:
        raise ValueError("release channel promotion event is invalid")
    if promotion.get("workflow_commit") != data["native_source_commit"]:
        raise ValueError("release channel promotion commit differs from the native source")
    for field in ("run_id", "run_attempt"):
        if not isinstance(promotion.get(field), int) or promotion[field] <= 0:
            raise ValueError(f"release channel promotion {field} is invalid")
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
    directory: Path,
    checksums: Path,
    previous_channel: Path | None = None,
    minimum_version: str | None = None,
    repository: str = DEFAULT_REPOSITORY,
) -> int:
    """Authenticate one release's channel and verify its complete asset set."""

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
        bound = channel["previous"]
        if (
            bound is None
            or bound["native_tag"] != previous["native_tag"]
            or bound["sha256"] != hashlib.sha256(previous_bytes).hexdigest()
        ):
            raise ValueError("release channel does not extend the accepted prior descriptor")

    # One reviewed-main workflow dispatch attests the native bytes and the
    # channel, so every certificate carries the same protected main identity.
    checksum_digest, checksum_bytes = _verify_github_attestation(
        checksums,
        repository=repository,
        identity_ref="refs/heads/main",
    )
    if checksum_digest != channel["checksums"]["sha256"]:
        raise ValueError("SHA256SUMS digest differs from the authenticated channel")

    checksum_entries = read_manifest(checksums, raw=checksum_bytes)
    expected = expected_asset_names(channel["native_version"], channel["signing"])
    if set(checksum_entries) != expected:
        raise ValueError("SHA256SUMS does not list the exact asset set the channel implies")
    provenance_digest = checksum_entries.get(PROVENANCE_NAME)
    if provenance_digest != channel["provenance"]["sha256"]:
        raise ValueError("provenance digest differs between SHA256SUMS and the channel")
    # Verify the installers against the entries parsed from the authenticated
    # bytes, never against a fresh read of SHA256SUMS. The channel itself sits
    # beside them and is not one of its own checksummed assets.
    return verify(directory, checksums, entries=checksum_entries, ignore={CHANNEL_NAME})


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


def verify(
    directory: Path,
    manifest: Path,
    *,
    entries: dict[str, str] | None = None,
    ignore: set[str] | frozenset[str] = frozenset(),
) -> int:
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
    actual = {name for name, path in members.items() if path != manifest and name not in ignore}
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
            if args.previous_channel is not None:
                raise ValueError("--previous-channel requires --channel")
            count = verify(args.directory, args.manifest)
        else:
            count = verify_authenticated_channel(
                channel_path=args.channel,
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
