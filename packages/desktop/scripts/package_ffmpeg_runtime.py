#!/usr/bin/env python3
"""Create a deterministic managed FFmpeg runtime archive and manifest entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path

FFMPEG_VERSION = "8.1.2"
RUNTIME_REVISION = "r1"
SOURCE_URL = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
SOURCE_SIGNATURE_URL = f"{SOURCE_URL}.asc"
SOURCE_SHA256 = "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
SIGNING_KEY_FINGERPRINT = "FCF986EA15E6E293A5644F10B4322F04D67658D8"
DOWNLOAD_BASE = (
    "https://github.com/OpenAdaptAI/openadapt-desktop/releases/download/"
    f"ffmpeg-runtime-v{FFMPEG_VERSION}-{RUNTIME_REVISION}"
)
FIXED_ZIP_TIME = (2026, 6, 17, 2, 47, 34)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_files(bundle_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in bundle_dir.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        ),
        key=lambda path: path.relative_to(bundle_dir).as_posix(),
    )


def write_checksums(bundle_dir: Path) -> Path:
    output = bundle_dir / "SHA256SUMS"
    lines = [
        f"{sha256(path)}  {path.relative_to(bundle_dir).as_posix()}"
        for path in runtime_files(bundle_dir)
    ]
    output.write_text("\n".join(lines) + "\n")
    return output


def write_deterministic_zip(bundle_dir: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in runtime_files(bundle_dir) + [bundle_dir / "SHA256SUMS"]:
            relative = path.relative_to(bundle_dir).as_posix()
            info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
            mode = 0o755 if relative in {"bin/ffmpeg", "bin/ffprobe"} else 0o644
            if relative.endswith(".exe"):
                mode = 0o755
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def file_contract(bundle_dir: Path, path: Path) -> dict[str, object]:
    relative = path.relative_to(bundle_dir).as_posix()
    role = None
    if relative in {"bin/ffmpeg", "bin/ffmpeg.exe"}:
        role = "ffmpeg"
    elif relative in {"bin/ffprobe", "bin/ffprobe.exe"}:
        role = "ffprobe"
    result: dict[str, object] = {
        "member": relative,
        "destination": relative,
        "sha256": sha256(path),
        "max_bytes": max(path.stat().st_size + 1024 * 1024, path.stat().st_size * 2),
    }
    if role:
        result["role"] = role
    return result


def manifest_entry(bundle_dir: Path, archive: Path, target: str, build_id: str) -> dict:
    files = runtime_files(bundle_dir) + [bundle_dir / "SHA256SUMS"]
    license_destination = "LICENSES/FFmpeg-LGPL-2.1-or-later.txt"
    if not (bundle_dir / license_destination).is_file():
        raise ValueError(f"missing {license_destination}")
    roles = {
        entry.get("role")
        for entry in (file_contract(bundle_dir, path) for path in files)
        if entry.get("role")
    }
    if roles != {"ffmpeg", "ffprobe"}:
        raise ValueError("bundle must contain exactly ffmpeg and ffprobe")
    return {
        "target": target,
        "build_id": build_id,
        "url": f"{DOWNLOAD_BASE}/{archive.name}",
        "archive_sha256": sha256(archive),
        "archive_max_bytes": max(
            archive.stat().st_size + 5 * 1024 * 1024,
            archive.stat().st_size * 2,
        ),
        "files": [file_contract(bundle_dir, path) for path in files],
        "probe": {
            "version_contains": f"ffmpeg version {FFMPEG_VERSION}",
            "ffprobe_version_contains": f"ffprobe version {FFMPEG_VERSION}",
            "required_build_flags": [
                "--disable-gpl",
                "--disable-nonfree",
                "--disable-version3",
                "--disable-network",
            ],
            "forbidden_build_flags": ["--enable-gpl", "--enable-nonfree"],
            "required_encoders": ["mpeg4", "png"],
            "required_muxers": ["mp4", "image2pipe"],
        },
        "source": {
            "url": SOURCE_URL,
            "sha256": SOURCE_SHA256,
            "signature_url": SOURCE_SIGNATURE_URL,
            "signing_key_fingerprint": SIGNING_KEY_FINGERPRINT,
            "build_workflow": ".github/workflows/ffmpeg-runtime.yml",
        },
        "license": {
            "expression": "LGPL-2.1-or-later",
            "license_destination": license_destination,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--manifest-entry", type=Path, required=True)
    args = parser.parse_args()

    write_checksums(args.bundle_dir)
    write_deterministic_zip(args.bundle_dir, args.output)
    entry = manifest_entry(args.bundle_dir, args.output, args.target, args.build_id)
    args.manifest_entry.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_entry.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "archive": str(args.output),
                "sha256": entry["archive_sha256"],
                "manifest_entry": str(args.manifest_entry),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
