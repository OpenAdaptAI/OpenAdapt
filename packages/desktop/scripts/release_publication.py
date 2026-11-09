#!/usr/bin/env python3
"""Publication steps of .github/workflows/desktop-release.yml.

Every command works on the attested release set the attest job uploaded: the
installers and their metadata, the wheel and sdist, the SBOM, the website
manifest, the provenance, the verifier, ``SHA256SUMS``, and the channel.

    reverify     check SHA256SUMS and every attestation again, then assemble the
                 exact upload set
    notes        release notes that state each platform's signing
    state        whether the GitHub release is absent, a draft, or published
    python-dist  the wheel and sdist, checked against SHA256SUMS
    verify-pypi  require PyPI to serve exactly the attested wheel and sdist
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import native_release  # noqa: E402

OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGNING_LINES = {
    ("macos", "adhoc"): (
        "- **macOS**: ad-hoc signed, not notarized. macOS asks you to confirm the first "
        "time you open it (System Settings > Privacy & Security)."
    ),
    ("macos", "developer-id-notarized"): "- **macOS**: Developer ID signed and notarized.",
    ("windows", "unsigned"): (
        "- **Windows**: unsigned. SmartScreen warns before install; choose More info > Run anyway."
    ),
    ("windows", "authenticode"): (
        "- **Windows**: signed through SignPath. The MSI and the program it installs are "
        "Authenticode-signed; the NSIS setup is signed, but the files it installs and its "
        "uninstaller are not. Prefer the MSI."
    ),
    ("linux", "github-attested"): (
        "- **Linux**: no package signature; the GitHub attestation covers the exact DEB "
        "and AppImage bytes."
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(repository: str) -> str:
    return (
        f"https://github.com/{repository}/{native_release.NATIVE_RELEASE_WORKFLOW}@refs/heads/main"
    )


def _attestation(path: Path, repository: str, *, as_json: bool = False) -> str:
    command = [
        "gh",
        "attestation",
        "verify",
        str(path),
        "--repo",
        repository,
        "--cert-identity",
        _identity(repository),
        "--cert-oidc-issuer",
        OIDC_ISSUER,
        "--deny-self-hosted-runners",
    ]
    if as_json:
        command += ["--format", "json"]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def reverify(
    directory: Path, *, repository: str, tag: str, source_commit: str, upload: Path
) -> list[str]:
    """Verify the attested set again and copy it, channel included, to ``upload``."""

    channel = directory / native_release.VERIFIED_RELEASE_CHANNEL
    if not channel.is_file():
        raise ValueError("the attested release set has no channel")
    with_channel = sorted(path.name for path in directory.iterdir())
    staged = directory.parent / f"{directory.name}-checksummed"
    if staged.exists():
        raise ValueError(f"refusing to reuse {staged}")
    shutil.copytree(directory, staged)
    (staged / channel.name).unlink()
    native_release.verify_checksums(staged, staged / "SHA256SUMS")
    provenance = staged / native_release.NATIVE_RELEASE_PROVENANCE
    attestation = staged.parent / "provenance-attestation.json"
    attestation.write_text(_attestation(provenance, repository, as_json=True), encoding="utf-8")
    _attestation(staged / "SHA256SUMS", repository)
    _attestation(channel, repository)
    native_release.validate_release_attestation(
        attestation,
        directory=staged,
        checksums=staged / "SHA256SUMS",
        provenance_path=provenance,
        repository=repository,
        tag=tag,
        source_commit=source_commit,
    )
    native_release.validate_native_release_channel(channel, directory=staged)
    if upload.exists():
        raise ValueError(f"refusing to reuse {upload}")
    shutil.copytree(directory, upload)
    return with_channel


def notes(directory: Path, *, version: str, root: Path = ROOT) -> str:
    """Release notes: the signing each platform used, then the candidate notes."""

    signing = native_release.release_signing(directory)
    lines = [f"OpenAdapt Desktop {version}: the PyPI package and the native installers.", ""]
    lines += ["## Signing in this release", ""]
    lines += [
        SIGNING_LINES[(platform, signing[platform])] for platform in ("macos", "windows", "linux")
    ]
    lines += [
        "",
        "Verify any download against `SHA256SUMS` and its GitHub attestation with "
        "`verify-openadapt-native-release.py`, using this release's "
        "`openadapt-desktop-channel.json`.",
        "",
    ]
    body = (root / "docs" / "RELEASE_CANDIDATE_INSTALLERS.md").read_text(encoding="utf-8")
    return "\n".join(lines) + "\n" + body


def release_state(releases: list, tag: str) -> tuple[str, dict | None]:
    """Find one release by tag in a paginated listing that includes drafts."""

    found = [release for page in releases for release in page if release.get("tag_name") == tag]
    if len(found) > 1:
        raise ValueError(f"more than one release is tagged {tag}")
    if not found:
        return "none", None
    return ("draft" if found[0].get("draft") else "published"), found[0]


def python_dist(directory: Path, *, version: str, output: Path) -> list[Path]:
    """Copy the wheel and sdist, requiring the digests SHA256SUMS names."""

    entries = native_release.read_checksums(directory / "SHA256SUMS")
    output.mkdir(parents=True, exist_ok=False)
    copied = []
    for name in sorted(native_release.python_distribution_names(version)):
        source = directory / name
        if entries.get(name) != _sha256(source):
            raise ValueError(f"{name} differs from the attested SHA256SUMS")
        copied.append(Path(shutil.copy2(source, output / name)))
    return copied


def pypi_digests(version: str) -> dict[str, str]:
    url = f"https://pypi.org/pypi/openadapt-desktop/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "openadapt-desktop-release"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    return {item["filename"]: item["digests"]["sha256"] for item in data.get("urls", [])}


# pypa/gh-action-pypi-publish writes one of these next to each file it uploads
# with attestations enabled. They are PyPI's upload attestations, not
# distributions, and PyPI never lists them as files of the release.
PUBLISH_ATTESTATION_SUFFIX = ".publish.attestation"


def verify_pypi(
    directory: Path,
    *,
    version: str,
    checksums: Path,
    wait_seconds: int,
    fetch=None,
    sleep=time.sleep,
) -> dict[str, str]:
    """Require PyPI to serve exactly the attested wheel and sdist, byte for byte.

    The expected digests come from the attested ``checksums`` (SHA256SUMS), and
    the files in ``directory`` must be those bytes. The publish action's
    ``*.publish.attestation`` files beside them are ignored; anything else is
    refused.
    """

    fetch = fetch or pypi_digests
    names = native_release.python_distribution_names(version)
    attested = native_release.read_checksums(checksums)
    missing = sorted(names - set(attested))
    if missing:
        raise ValueError(f"the attested SHA256SUMS does not name {missing}")
    expected = {name: attested[name] for name in sorted(names)}
    present = {
        path.name
        for path in directory.iterdir()
        if not (path.name.endswith(PUBLISH_ATTESTATION_SUFFIX) and path.is_file())
    }
    if present != names:
        raise ValueError(f"unexpected distribution set: {sorted(present)}")
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or _sha256(path) != digest:
            raise ValueError(f"{name} differs from the attested SHA256SUMS")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            served = fetch(version)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            served = {}
        if served == expected:
            return served
        if served and set(served) == set(expected):
            raise ValueError("PyPI serves different bytes for openadapt-desktop " + version)
        if set(served) - set(expected):
            raise ValueError(f"PyPI serves unexpected files: {sorted(set(served) - set(expected))}")
        if time.monotonic() >= deadline:
            raise ValueError(f"PyPI did not serve openadapt-desktop {version} in time")
        sleep(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("reverify")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--source-commit", required=True)
    p.add_argument("--upload", type=Path, required=True)
    p = sub.add_parser("notes")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("state")
    p.add_argument("--repository", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--cli", default="gh")
    p.add_argument("--metadata", type=Path, required=True)
    p = sub.add_parser("python-dist")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("verify-pypi")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--checksums", type=Path, required=True)
    p.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == "reverify":
            names = reverify(
                args.directory,
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
                upload=args.upload,
            )
            print(f"Reverified {len(names)} release files")
        elif args.command == "notes":
            args.output.write_text(notes(args.directory, version=args.version), encoding="utf-8")
            print(args.output)
        elif args.command == "state":
            listing = subprocess.run(
                [
                    args.cli,
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{args.repository}/releases?per_page=100",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            state, release = release_state(json.loads(listing), args.tag)
            if release is not None:
                args.metadata.write_text(json.dumps(release), encoding="utf-8")
            print(state)
        elif args.command == "python-dist":
            for path in python_dist(args.directory, version=args.version, output=args.output):
                print(path)
        elif args.command == "verify-pypi":
            served = verify_pypi(
                args.directory,
                version=args.version,
                checksums=args.checksums,
                wait_seconds=args.wait_seconds,
            )
            print(f"PyPI serves exactly {sorted(served)}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
