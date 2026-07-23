"""Tests for the separately provisioned FFmpeg runtime package."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from scripts.package_ffmpeg_runtime import (
    FFMPEG_VERSION,
    SIGNING_KEY_FINGERPRINT,
    SOURCE_SHA256,
    manifest_entry,
    write_checksums,
    write_deterministic_zip,
)


def _bundle(root: Path) -> Path:
    bundle = root / "bundle"
    for name, value in {
        "bin/ffmpeg": b"ffmpeg",
        "bin/ffprobe": b"ffprobe",
        "LICENSES/FFmpeg-LGPL-2.1-or-later.txt": b"license",
        "PROVENANCE/SOURCE.json": b"{}",
        "PROVENANCE/BUILD.json": b"{}",
    }.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return bundle


def test_runtime_archive_is_deterministic_and_manifest_is_exact(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    archive_a = tmp_path / "runtime-a.zip"
    archive_b = tmp_path / "runtime-b.zip"
    write_checksums(bundle)
    write_deterministic_zip(bundle, archive_a)
    write_deterministic_zip(bundle, archive_b)

    assert archive_a.read_bytes() == archive_b.read_bytes()
    with zipfile.ZipFile(archive_a) as archive:
        assert set(archive.namelist()) == {
            "bin/ffmpeg",
            "bin/ffprobe",
            "LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
            "PROVENANCE/SOURCE.json",
            "PROVENANCE/BUILD.json",
            "SHA256SUMS",
        }

    entry = manifest_entry(
        bundle,
        archive_a,
        "aarch64-apple-darwin",
        f"ffmpeg-{FFMPEG_VERSION}-r1-aarch64-apple-darwin",
    )
    assert entry["source"]["sha256"] == SOURCE_SHA256
    assert entry["source"]["signing_key_fingerprint"] == SIGNING_KEY_FINGERPRINT
    assert entry["license"]["expression"] == "LGPL-2.1-or-later"
    assert {file.get("role") for file in entry["files"]} >= {
        "ffmpeg",
        "ffprobe",
    }
    assert entry["probe"]["forbidden_build_flags"] == [
        "--enable-gpl",
        "--enable-nonfree",
    ]


def test_runtime_manifest_refuses_missing_probe_or_license(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    archive = tmp_path / "runtime.zip"
    write_checksums(bundle)
    write_deterministic_zip(bundle, archive)

    (bundle / "bin/ffprobe").unlink()
    with pytest.raises(ValueError, match="ffmpeg and ffprobe"):
        manifest_entry(bundle, archive, "target", "build")

    _bundle(tmp_path)
    (bundle / "LICENSES/FFmpeg-LGPL-2.1-or-later.txt").unlink()
    with pytest.raises(ValueError, match="missing"):
        manifest_entry(bundle, archive, "target", "build")


def test_runtime_workflow_is_pinned_attested_and_separate_from_installers() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "ffmpeg-runtime.yml").read_text()
    script = (root / "scripts" / "build_managed_ffmpeg_runtime.sh").read_text()

    revisions = re.findall(
        r"^\s*(?:-\s+)?uses:\s+\S+@([^\s#]+)",
        workflow,
        flags=re.MULTILINE,
    )
    assert revisions
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions)
    assert SOURCE_SHA256 in workflow
    assert SIGNING_KEY_FINGERPRINT in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "environment: native-release" in workflow
    assert "--prerelease" in workflow
    assert "src-tauri/binaries" not in workflow
    for target in (
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
    ):
        assert target in workflow

    for flag in (
        "--disable-gpl",
        "--disable-nonfree",
        "--disable-version3",
        "--enable-ffmpeg",
        "--enable-ffprobe",
        "--enable-demuxer=concat,image2,mov,rawvideo",
        "--enable-muxer=mp4,null,image2,image2pipe",
        "--enable-filter=scale,format,setpts,select",
    ):
        assert flag in script
    assert "h264_videotoolbox" in script
    assert "h264_mf" not in script
    assert "--enable-mediafoundation" not in script
    assert "software_fallback_encoder" in script


def test_runtime_builder_normalizes_windows_paths_and_materializes_smoke_bytes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "build_managed_ffmpeg_runtime.sh").read_text()

    source_conversion = 'SOURCE_ARCHIVE="$(cygpath -u "${SOURCE_ARCHIVE}")"'
    output_conversion = 'OUTPUT_DIR="$(cygpath -u "${OUTPUT_DIR}")"'
    temp_conversion = 'temp_root="$(cygpath -u "${temp_root}")"'
    assert source_conversion in script
    assert output_conversion in script
    assert temp_conversion in script
    assert script.index(source_conversion) < script.index('bundle_dir="${OUTPUT_DIR}/bundle"')
    assert script.index(output_conversion) < script.index('bundle_dir="${OUTPUT_DIR}/bundle"')
    assert script.index(temp_conversion) < script.index(
        'work_root="${temp_root}/openadapt-ffmpeg-${TARGET_TRIPLE}"'
    )
    assert script.index('exe_suffix=".exe"') < script.index(
        'make -j"${jobs}" "ffmpeg${exe_suffix}" "ffprobe${exe_suffix}"'
    )

    assert 'frames = b"".join(' in script
    assert '(root / "frames.rgb").write_bytes(frames)' in script
    assert '(root / "frames.rgb").write_bytes(\n' not in script
    assert '"ffconcat version 1.0\\n"' in script
    assert '-f concat -safe 1 -i "${smoke_dir}/frames.ffconcat"' in script
    assert r"'select=eq(n\,0)'" in script
    assert r"'select=eq(n\\,0)'" not in script
