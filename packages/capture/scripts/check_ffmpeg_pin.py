#!/usr/bin/env python3
"""Prove every pinned FFmpeg artifact is still exactly what the package claims.

Three things are checked against the live release asset, not against a copy of
the pin:

1. The archive still serves the pinned SHA-256 at the pinned URL.
2. Every member the installer would extract still has its pinned digest.
3. The build is the LGPL configuration. The archive carries the exact
   ``configure`` arguments it was built with, so the licensing claim in
   ``openadapt_capture/ffmpeg_runtime.py`` is verified rather than asserted.

Run it whenever the pin changes, and on a schedule so a retagged or replaced
release asset is caught rather than silently installed.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openadapt_capture import ffmpeg_runtime as fr  # noqa: E402

CONFIGURE_ARGS_MEMBER = "PROVENANCE/configure-args.txt"
LICENSE_STATEMENT_MEMBER = "LICENSES/FFmpeg-LICENSE.md"
REQUIRED_BUILD_FLAGS = ("--disable-gpl", "--disable-nonfree", "--disable-version3")
FORBIDDEN_BUILD_FLAGS = ("--enable-gpl", "--enable-nonfree", "--enable-version3")


class PinMismatch(SystemExit):
    """The published artifact and the compiled pin disagree."""


def _check_build_is_lgpl(archive: zipfile.ZipFile, target: str) -> None:
    try:
        arguments = archive.read(CONFIGURE_ARGS_MEMBER).decode("utf-8")
    except KeyError:
        raise PinMismatch(
            f"{target}: the archive has no {CONFIGURE_ARGS_MEMBER}, so its "
            "licence cannot be established from the artifact itself"
        ) from None
    tokens = arguments.split()
    for flag in REQUIRED_BUILD_FLAGS:
        if flag not in tokens:
            raise PinMismatch(
                f"{target}: the build is missing {flag}. Only a build with "
                f"{', '.join(REQUIRED_BUILD_FLAGS)} carries the "
                f"{fr.LICENSE_EXPRESSION} licence this package pins."
            )
    for flag in FORBIDDEN_BUILD_FLAGS:
        if flag in tokens:
            raise PinMismatch(
                f"{target}: the build passes {flag}, which changes FFmpeg's "
                f"licence away from {fr.LICENSE_EXPRESSION}."
            )
    if LICENSE_STATEMENT_MEMBER not in archive.namelist():
        raise PinMismatch(f"{target}: the archive omits {LICENSE_STATEMENT_MEMBER}")


def check_artifact(artifact: fr.PinnedArtifact, scratch: Path) -> None:
    print(f"{artifact.target}: {artifact.url}")
    download = scratch / artifact.archive_name
    actual = fr._download_to(artifact.url, download, artifact.archive_max_bytes)
    if actual != artifact.archive_sha256:
        raise PinMismatch(
            f"{artifact.target}: the published archive has digest {actual}, "
            f"not the pinned {artifact.archive_sha256}"
        )
    print(f"  archive sha256 {actual} matches the pin")

    with zipfile.ZipFile(download) as archive:
        fr._reject_unpinned_members(archive, artifact)
        for pinned in artifact.files:
            try:
                content = archive.read(pinned.member)
            except KeyError:
                raise PinMismatch(
                    f"{artifact.target}: the archive has no pinned member {pinned.member}"
                ) from None
            if len(content) > pinned.max_bytes:
                raise PinMismatch(
                    f"{artifact.target}: {pinned.member} is {len(content)} bytes, "
                    f"above its pinned bound of {pinned.max_bytes}"
                )
            digest = hashlib.sha256(content).hexdigest()
            if digest != pinned.sha256:
                raise PinMismatch(
                    f"{artifact.target}: {pinned.member} has digest {digest}, "
                    f"not the pinned {pinned.sha256}"
                )
        print(f"  {len(artifact.files)} pinned members match their digests")
        _check_build_is_lgpl(archive, artifact.target)
        print(f"  build configuration confirms {fr.LICENSE_EXPRESSION}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        action="append",
        choices=sorted(fr.PINNED_ARTIFACTS),
        help="Check only these targets (default: all of them).",
    )
    args = parser.parse_args()
    targets = args.target or sorted(fr.PINNED_ARTIFACTS)

    print(f"openadapt-capture pins FFmpeg {fr.RUNTIME_VERSION} ({fr.LICENSE_EXPRESSION})")
    print(f"corresponding source: {fr.SOURCE_URL} sha256:{fr.SOURCE_SHA256}")
    print()
    with tempfile.TemporaryDirectory(prefix="ffmpeg-pin-") as scratch:
        for target in targets:
            check_artifact(fr.PINNED_ARTIFACTS[target], Path(scratch))
    print()
    print(f"verified {len(targets)} pinned artifact(s)")


if __name__ == "__main__":
    main()
