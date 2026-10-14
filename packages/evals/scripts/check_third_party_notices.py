#!/usr/bin/env python3
"""Verify vendored-code provenance and notices in source and distributions."""

from __future__ import annotations

import argparse
import hashlib
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
NOTICE_PATH = "NOTICE"
SOURCE_REPOSITORY = "https://github.com/RAGEN-AI/VAGEN"
SOURCE_COMMIT = "fe4b11db336bb9474aa5b30651460caeb598f97f"
VENDORED_FILES = {
    "openadapt_evals/adapters/_vendored/gym_base_env.py": {
        "upstream_path": "vagen/envs/gym_base_env.py",
        "upstream_sha256": "62e820752a244df252f9a57c5d86a1f7ea2b5d74688a51e4484bf53800414aeb",
        "distributed_sha256": "e50f5ddd09da49bcb8dd944c6140dd7dab1fd54877d65f78a28045049f132ceb",
    },
    "openadapt_evals/adapters/_vendored/gym_image_env.py": {
        "upstream_path": "vagen/envs/gym_image_env.py",
        "upstream_sha256": "89ab3991c8517e60eb25a90401d7a07260f5f7276a22e2ec9f598f40aba336ce",
        "distributed_sha256": "637e132044ab385b27b7ec0310e0cf0a02fce7572ea8b78776a3b15a12ccf15e",
    },
}


class NoticeError(RuntimeError):
    """A vendored source or required notice is absent or inconsistent."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_repository(root: Path = ROOT) -> bytes:
    """Verify the checked-in notice and each vendored file it inventories."""

    notice_path = root / NOTICE_PATH
    try:
        notice = notice_path.read_bytes()
        notice_text = notice.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NoticeError(f"cannot read {notice_path}: {exc}") from exc

    required = (
        SOURCE_REPOSITORY,
        SOURCE_COMMIT,
        "Copyright (c) 2025 RAGEN.AI",
        "The above copyright notice and this permission notice shall be included",
    )
    for value in required:
        if value not in notice_text:
            raise NoticeError(f"{NOTICE_PATH} is missing {value!r}")

    for path, provenance in VENDORED_FILES.items():
        try:
            payload = (root / path).read_bytes()
        except OSError as exc:
            raise NoticeError(f"cannot read vendored file {path}: {exc}") from exc
        actual = _sha256(payload)
        expected = provenance["distributed_sha256"]
        if actual != expected:
            raise NoticeError(
                f"vendored file {path} has SHA-256 {actual}, expected {expected}; "
                f"update its provenance before release"
            )
        for value in (
            path,
            provenance["upstream_path"],
            provenance["upstream_sha256"],
            expected,
        ):
            if value not in notice_text:
                raise NoticeError(f"{NOTICE_PATH} does not inventory {value!r}")
    return notice


def _verify_wheel(path: Path, notice: bytes) -> None:
    with zipfile.ZipFile(path) as archive:
        notice_names = [
            name
            for name in archive.namelist()
            if PurePosixPath(name).name == NOTICE_PATH
            and ".dist-info/licenses" in PurePosixPath(name).as_posix()
        ]
        if len(notice_names) != 1:
            raise NoticeError(f"{path.name} must contain one dist-info/licenses/NOTICE")
        if archive.read(notice_names[0]) != notice:
            raise NoticeError(f"{path.name} contains a changed NOTICE")
        for vendored_path, provenance in VENDORED_FILES.items():
            try:
                payload = archive.read(vendored_path)
            except KeyError as exc:
                raise NoticeError(f"{path.name} is missing {vendored_path}") from exc
            if _sha256(payload) != provenance["distributed_sha256"]:
                raise NoticeError(f"{path.name} contains an unrecorded {vendored_path}")


def _verify_sdist(path: Path, notice: bytes) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        notice_members = [
            member
            for member in members
            if len(PurePosixPath(member.name).parts) == 2
            and PurePosixPath(member.name).name == NOTICE_PATH
        ]
        if len(notice_members) != 1:
            raise NoticeError(f"{path.name} must contain one top-level NOTICE")
        notice_stream = archive.extractfile(notice_members[0])
        if notice_stream is None or notice_stream.read() != notice:
            raise NoticeError(f"{path.name} contains a changed NOTICE")
        root_name = PurePosixPath(notice_members[0].name).parts[0]
        by_name = {member.name: member for member in members}
        for vendored_path, provenance in VENDORED_FILES.items():
            member_name = f"{root_name}/{vendored_path}"
            member = by_name.get(member_name)
            if member is None:
                raise NoticeError(f"{path.name} is missing {vendored_path}")
            stream = archive.extractfile(member)
            if stream is None or _sha256(stream.read()) != provenance["distributed_sha256"]:
                raise NoticeError(f"{path.name} contains an unrecorded {vendored_path}")


def verify_distributions(directory: Path, notice: bytes) -> None:
    """Require and verify one or more wheels and source distributions."""

    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if not wheels or not sdists:
        raise NoticeError("distribution directory must contain a wheel and an sdist")
    for path in wheels:
        _verify_wheel(path, notice)
    for path in sdists:
        _verify_sdist(path, notice)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--require-dist", action="store_true")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    try:
        notice = verify_repository(root)
        if arguments.require_dist:
            directory = (arguments.directory or root / "dist").resolve()
            verify_distributions(directory, notice)
    except (NoticeError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FATAL: {exc}")
        return 1
    scope = "source and distributions" if arguments.require_dist else "source"
    print(f"OK: third-party notices match the {scope} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
