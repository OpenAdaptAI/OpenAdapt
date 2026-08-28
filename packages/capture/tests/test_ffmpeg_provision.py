"""Contract tests for the opt-in, pinned FFmpeg runtime.

The licensing boundary these protect: the package ships no FFmpeg bytes, it
downloads nothing unless an operator asks, it installs only what its compiled
digests describe, and it never makes an unverified file executable.
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import re
import zipfile
from pathlib import Path

import pytest

from openadapt_capture import ffmpeg_runtime as fr
from scripts.verify_distribution import (
    REQUIRED_OBSERVER_PATHS,
    _archive_files,
    verify_distribution,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------
# The pin itself
# --------------------------------------------------------------------------


def test_every_supported_target_is_pinned() -> None:
    assert set(fr.PINNED_ARTIFACTS) == {
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
        "x86_64-unknown-linux-gnu",
        "x86_64-pc-windows-msvc",
    }


@pytest.mark.parametrize("target", sorted(fr.PINNED_ARTIFACTS))
def test_pin_is_complete_and_well_formed(target: str) -> None:
    artifact = fr.PINNED_ARTIFACTS[target]
    assert artifact.target == target
    assert HEX64.fullmatch(artifact.archive_sha256)
    assert artifact.archive_max_bytes > 0
    assert artifact.url.startswith("https://github.com/OpenAdaptAI/openadapt-desktop/")
    assert fr.RELEASE_TAG in artifact.url

    members = {file.member: file for file in artifact.files}
    for member, file in members.items():
        assert HEX64.fullmatch(file.sha256), member
        assert 0 < file.max_bytes <= artifact.archive_max_bytes, member
        assert not member.startswith("/") and ".." not in Path(member).parts, member

    for required in (artifact.ffmpeg_member, artifact.ffprobe_member, artifact.license_member):
        assert required in members, required
    assert members[artifact.ffmpeg_member].executable
    assert members[artifact.ffprobe_member].executable
    assert not members[artifact.license_member].executable
    # Only the two executables are ever made executable.
    assert sum(1 for file in artifact.files if file.executable) == 2


def test_pinned_build_is_not_gpl() -> None:
    """The hard licensing rule turns on this: an LGPL build, never a GPL one."""
    assert fr.LICENSE_EXPRESSION == "LGPL-2.1-or-later"
    assert "GPL-2" not in fr.LICENSE_EXPRESSION.replace("LGPL-2", "")
    # Every artifact installs FFmpeg's own licence text beside the binaries.
    for artifact in fr.PINNED_ARTIFACTS.values():
        assert artifact.license_member.startswith("LICENSES/")
        assert any(file.member == "LICENSES/FFmpeg-LICENSE.md" for file in artifact.files)


def test_corresponding_source_is_pinned_too() -> None:
    """The LGPL asks a distributor to offer the source. Record which source."""
    assert fr.SOURCE_URL.endswith(f"ffmpeg-{fr.FFMPEG_VERSION}.tar.xz")
    assert HEX64.fullmatch(fr.SOURCE_SHA256)
    assert fr.SOURCE_SIGNATURE_URL == f"{fr.SOURCE_URL}.asc"


# --------------------------------------------------------------------------
# Platform selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform_name", "machine", "expected"),
    [
        ("darwin", "arm64", "aarch64-apple-darwin"),
        ("darwin", "x86_64", "x86_64-apple-darwin"),
        ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("win32", "AMD64", "x86_64-pc-windows-msvc"),
    ],
)
def test_current_target(platform_name: str, machine: str, expected: str) -> None:
    assert fr.current_target(platform_name, machine) == expected


@pytest.mark.parametrize(
    ("platform_name", "machine"),
    [("linux", "aarch64"), ("freebsd13", "x86_64"), ("darwin", "ppc")],
)
def test_unsupported_platform_names_the_manual_route(platform_name: str, machine: str) -> None:
    with pytest.raises(fr.UnsupportedPlatformError) as excinfo:
        fr.current_target(platform_name, machine)
    assert "OPENADAPT_FFMPEG_PATH" in str(excinfo.value)


def test_data_root_is_separate_from_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENADAPT_CAPTURE_DATA_DIR", raising=False)
    monkeypatch.setenv("OPENADAPT_PLATFORM_OVERRIDE", "darwin")
    root = fr.data_root()
    assert root.name == "ai.openadapt.capture"
    assert "ai.openadapt.desktop" not in str(root)


# --------------------------------------------------------------------------
# A synthetic pinned artifact, so the real install path can be exercised
# --------------------------------------------------------------------------

FAKE_TARGET = "test-target"


def _build_archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Register a fake pinned target whose digests describe content we control."""
    monkeypatch.setenv("OPENADAPT_CAPTURE_DATA_DIR", str(tmp_path / "data"))

    members = {
        "LICENSES/FFmpeg-LGPL-2.1-or-later.txt": b"lgpl text\n",
        "bin/ffmpeg": b"not really ffmpeg\n",
        "bin/ffprobe": b"not really ffprobe\n",
        "PROVENANCE/BUILD.json": b"{}\n",
    }

    def pinned(member: str, executable: bool = False) -> fr.PinnedFile:
        return fr.PinnedFile(
            member=member,
            sha256=hashlib.sha256(members[member]).hexdigest(),
            max_bytes=len(members[member]) + 16,
            executable=executable,
        )

    archive_bytes = _build_archive(members)
    artifact = fr.PinnedArtifact(
        target=FAKE_TARGET,
        archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        archive_max_bytes=len(archive_bytes) + 16,
        files=(
            pinned("LICENSES/FFmpeg-LGPL-2.1-or-later.txt"),
            pinned("bin/ffmpeg", executable=True),
            pinned("bin/ffprobe", executable=True),
        ),
        ffmpeg_member="bin/ffmpeg",
        ffprobe_member="bin/ffprobe",
        license_member="LICENSES/FFmpeg-LGPL-2.1-or-later.txt",
    )
    monkeypatch.setitem(fr.PINNED_ARTIFACTS, FAKE_TARGET, artifact)

    served = {"bytes": archive_bytes}

    def fake_download(url: str, destination: Path, max_bytes: int) -> str:
        assert url == artifact.url
        payload = served["bytes"]
        assert len(payload) <= max_bytes
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(fr, "_download_to", fake_download)
    return artifact, members, served


def test_install_writes_verified_files_and_a_receipt(synthetic) -> None:
    artifact, members, _ = synthetic
    installed = fr.install(target=FAKE_TARGET)

    ffmpeg = Path(installed.ffmpeg)
    assert ffmpeg.read_bytes() == members["bin/ffmpeg"]
    assert ffmpeg.stat().st_mode & 0o100, "ffmpeg is not executable"
    assert Path(installed.ffprobe).stat().st_mode & 0o100
    licence = Path(installed.license_path)
    assert licence.read_bytes() == members["LICENSES/FFmpeg-LGPL-2.1-or-later.txt"]
    assert not licence.stat().st_mode & 0o111, "the licence text must not be executable"

    # Only pinned members are installed. Unpinned provenance is left behind.
    assert not (ffmpeg.parent.parent / "PROVENANCE").exists()

    receipt = json.loads(fr.receipt_path().read_text())
    assert receipt["schema"] == fr.RECEIPT_SCHEMA
    assert receipt["url"] == artifact.url
    assert receipt["archive_sha256"] == artifact.archive_sha256
    assert receipt["license"]["expression"] == fr.LICENSE_EXPRESSION
    assert receipt["license"]["source_url"] == fr.SOURCE_URL
    assert receipt["installed_at"]

    assert fr.find_installed_runtime() == (installed.ffmpeg, installed.ffprobe)


def test_every_digest_is_checked_before_anything_becomes_executable(
    synthetic,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering the licensing and safety story depends on."""
    events: list[tuple[str, str]] = []

    original_extract = fr._extract_member

    def spy_extract(archive, pinned, destination):  # type: ignore[no-untyped-def]
        original_extract(archive, pinned, destination)
        # Reaching here means this member's digest matched.
        events.append(("verified", pinned.member))

    original_chmod = pathlib.Path.chmod

    def spy_chmod(self, mode, **kwargs):  # type: ignore[no-untyped-def]
        if mode & 0o111:
            events.append(("chmod+x", self.name))
        return original_chmod(self, mode, **kwargs)

    monkeypatch.setattr(fr, "_extract_member", spy_extract)
    monkeypatch.setattr(pathlib.Path, "chmod", spy_chmod)

    fr.install(target=FAKE_TARGET)

    kinds = [kind for kind, _ in events]
    assert "chmod+x" in kinds and "verified" in kinds
    assert max(index for index, kind in enumerate(kinds) if kind == "verified") < min(
        index for index, kind in enumerate(kinds) if kind == "chmod+x"
    ), f"an executable bit was set before a digest was checked: {events}"


def test_a_tampered_archive_installs_nothing(synthetic) -> None:
    artifact, _, served = synthetic
    served["bytes"] = served["bytes"] + b"tampered"

    with pytest.raises(fr.FFmpegProvisionError) as excinfo:
        fr.install(target=FAKE_TARGET)

    assert artifact.archive_sha256 in str(excinfo.value)
    assert not (fr.runtime_root() / artifact.build_id).exists()
    assert not fr.receipt_path().exists()
    assert fr.find_installed_runtime() is None


def test_a_tampered_member_installs_nothing(synthetic) -> None:
    """The archive digest can match while a member does not, if the pin is stale."""
    artifact, members, served = synthetic
    swapped = dict(members)
    swapped["bin/ffmpeg"] = b"a different executable entirely\n"
    served["bytes"] = _build_archive(swapped)
    # Re-pin the archive digest so only the member check can fail.
    tampered = fr.PinnedArtifact(
        target=FAKE_TARGET,
        archive_sha256=hashlib.sha256(served["bytes"]).hexdigest(),
        archive_max_bytes=len(served["bytes"]) + 64,
        files=artifact.files,
        ffmpeg_member=artifact.ffmpeg_member,
        ffprobe_member=artifact.ffprobe_member,
        license_member=artifact.license_member,
    )
    fr.PINNED_ARTIFACTS[FAKE_TARGET] = tampered

    with pytest.raises(fr.FFmpegProvisionError, match="bin/ffmpeg"):
        fr.install(target=FAKE_TARGET)
    assert not (fr.runtime_root() / tampered.build_id).exists()
    assert not fr.receipt_path().exists()


def test_an_unpinned_executable_member_is_refused(synthetic) -> None:
    _, members, served = synthetic
    smuggled = dict(members)
    smuggled["bin/extra-tool"] = b"smuggled\n"
    served["bytes"] = _build_archive(smuggled)
    artifact = fr.PINNED_ARTIFACTS[FAKE_TARGET]
    fr.PINNED_ARTIFACTS[FAKE_TARGET] = fr.PinnedArtifact(
        target=FAKE_TARGET,
        archive_sha256=hashlib.sha256(served["bytes"]).hexdigest(),
        archive_max_bytes=len(served["bytes"]) + 64,
        files=artifact.files,
        ffmpeg_member=artifact.ffmpeg_member,
        ffprobe_member=artifact.ffprobe_member,
        license_member=artifact.license_member,
    )
    with pytest.raises(fr.FFmpegProvisionError, match="unpinned executable member"):
        fr.install(target=FAKE_TARGET)


def test_an_oversized_member_is_refused(synthetic) -> None:
    artifact, members, served = synthetic
    fat = dict(members)
    fat["bin/ffmpeg"] = b"x" * 4096
    served["bytes"] = _build_archive(fat)
    fr.PINNED_ARTIFACTS[FAKE_TARGET] = fr.PinnedArtifact(
        target=FAKE_TARGET,
        archive_sha256=hashlib.sha256(served["bytes"]).hexdigest(),
        archive_max_bytes=len(served["bytes"]) + 64,
        files=artifact.files,
        ffmpeg_member=artifact.ffmpeg_member,
        ffprobe_member=artifact.ffprobe_member,
        license_member=artifact.license_member,
    )
    with pytest.raises(fr.FFmpegProvisionError, match="bounded regular file|pinned bound"):
        fr.install(target=FAKE_TARGET)


def test_uninstall_removes_the_runtime(synthetic) -> None:
    fr.install(target=FAKE_TARGET)
    assert fr.find_installed_runtime() is not None
    assert fr.uninstall() is True
    assert fr.find_installed_runtime() is None
    assert fr.uninstall() is False


def test_a_receipt_pointing_outside_the_runtime_root_is_ignored(
    synthetic,
    tmp_path: Path,
) -> None:
    fr.install(target=FAKE_TARGET)
    outsider = tmp_path / "elsewhere" / "ffmpeg"
    outsider.parent.mkdir(parents=True)
    outsider.write_text("#!/bin/sh\n")
    receipt = json.loads(fr.receipt_path().read_text())
    receipt["executables"]["ffmpeg"] = str(outsider)
    fr.receipt_path().write_text(json.dumps(receipt))
    assert fr.find_installed_runtime() is None


def test_a_receipt_with_an_unknown_schema_is_ignored(synthetic) -> None:
    fr.install(target=FAKE_TARGET)
    receipt = json.loads(fr.receipt_path().read_text())
    receipt["schema"] = "something.else/v9"
    fr.receipt_path().write_text(json.dumps(receipt))
    assert fr.read_receipt() is None
    assert fr.find_installed_runtime() is None


# --------------------------------------------------------------------------
# The download is opt-in and stays on the pinned host
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/OpenAdaptAI/openadapt-desktop/x.zip",
        "https://example.invalid/openadapt-ffmpeg.zip",
        "file:///etc/passwd",
        "https://github.com.attacker.invalid/x.zip",
    ],
)
def test_download_refuses_an_unpinned_url(url: str, tmp_path: Path) -> None:
    with pytest.raises(fr.FFmpegProvisionError, match="Refusing to download"):
        fr._download_to(url, tmp_path / "out.zip", 1024)


def test_nothing_downloads_without_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing or resolving must never reach the network."""

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("resolution attempted a network call")

    monkeypatch.setattr(fr, "_download_to", explode)
    monkeypatch.setenv("OPENADAPT_CAPTURE_DATA_DIR", "/nonexistent-capture-data")
    assert fr.find_installed_runtime() is None
    assert fr.read_receipt() is None
    # plan() only describes; it never fetches.
    assert fr.plan("aarch64-apple-darwin")["archive_sha256"]


def test_plan_describes_the_exact_artifact_without_fetching() -> None:
    planned = fr.plan("x86_64-unknown-linux-gnu")
    artifact = fr.PINNED_ARTIFACTS["x86_64-unknown-linux-gnu"]
    assert planned["url"] == artifact.url
    assert planned["archive_sha256"] == artifact.archive_sha256
    assert planned["license"] == fr.LICENSE_EXPRESSION
    assert planned["source_url"] == fr.SOURCE_URL


# --------------------------------------------------------------------------
# Resolution precedence is unchanged; the install is only a last fallback
# --------------------------------------------------------------------------


def _install_and_clear_env(monkeypatch: pytest.MonkeyPatch) -> str:
    for name in (
        "OPENADAPT_FFMPEG_PATH",
        "OPENADAPT_FFPROBE_PATH",
        "OPENADAPT_DESKTOP_FFMPEG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    installed = fr.install(target=FAKE_TARGET)
    return installed.ffmpeg


def test_managed_runtime_is_used_only_when_nothing_else_resolves(
    synthetic,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openadapt_capture import video

    managed = _install_and_clear_env(monkeypatch)
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [])
    monkeypatch.setattr(video.shutil, "which", lambda name: None)

    provision = video.resolve_ffmpeg()
    assert provision.executable == managed
    assert provision.source == "capture install-ffmpeg"


def test_path_still_outranks_the_managed_runtime(
    synthetic,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openadapt_capture import video

    _install_and_clear_env(monkeypatch)
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [])
    on_path = tmp_path / "path-ffmpeg"
    on_path.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        video.shutil,
        "which",
        lambda name: str(on_path) if name == "ffmpeg" else None,
    )

    provision = video.resolve_ffmpeg()
    assert provision.executable == str(on_path.resolve())
    assert provision.source == "PATH"


def test_explicit_path_still_outranks_the_managed_runtime(
    synthetic,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openadapt_capture import video

    _install_and_clear_env(monkeypatch)
    explicit = tmp_path / "explicit-ffmpeg"
    explicit.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OPENADAPT_FFMPEG_PATH", str(explicit))

    provision = video.resolve_ffmpeg()
    assert provision.executable == str(explicit.resolve())
    assert provision.source == "explicit path"


def test_the_missing_ffmpeg_error_names_one_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from openadapt_capture import video

    for name in (
        "OPENADAPT_FFMPEG_PATH",
        "OPENADAPT_FFPROBE_PATH",
        "OPENADAPT_DESKTOP_FFMPEG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENADAPT_CAPTURE_DATA_DIR", "/nonexistent-capture-data")
    monkeypatch.setattr(video, "_desktop_data_dirs", lambda: [])
    monkeypatch.setattr(video.shutil, "which", lambda name: None)

    with pytest.raises(video.FFmpegUnavailableError) as excinfo:
        video.resolve_ffmpeg()
    message = str(excinfo.value)
    assert "capture install-ffmpeg" in message
    # One command, offered first. The old message listed four mechanisms.
    assert message.index("capture install-ffmpeg") < message.index("OPENADAPT_FFMPEG_PATH")


def test_the_cli_exposes_the_command() -> None:
    from openadapt_capture import cli

    assert callable(cli.install_ffmpeg)
    assert callable(cli.uninstall_ffmpeg)
    source = Path(cli.__file__).read_text()
    assert '"install-ffmpeg": install_ffmpeg' in source


# --------------------------------------------------------------------------
# The release gate: no FFmpeg bytes reach a built archive, under any name
# --------------------------------------------------------------------------


def _wheel_with(tmp_path: Path, extra: dict[str, bytes]) -> Path:
    wheel = tmp_path / "openadapt_capture-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in sorted(REQUIRED_OBSERVER_PATHS):
            archive.writestr(name, "")
        archive.writestr("openadapt_capture-1.2.3.dist-info/licenses/LICENSE", "MIT\n")
        archive.writestr(
            "openadapt_capture-1.2.3.dist-info/METADATA",
            "Name: openadapt-capture\nProvides-Extra: linux\n"
            "Requires-Dist: pygobject<3.50,>=3.46; sys_platform == 'linux' "
            "and extra == 'linux'\n",
        )
        for name, content in extra.items():
            archive.writestr(name, content)
    return wheel


@pytest.mark.parametrize(
    ("name", "content", "match"),
    [
        # An honestly named binary.
        ("openadapt_capture/bin/ffmpeg", b"\x7fELF\x02\x01", "external-process boundary"),
        # An honestly named binary with its magic bytes stripped.
        ("openadapt_capture/bin/ffprobe", b"\x00opaque\x00", "external-process boundary"),
        # The same binary renamed, which a name-only gate would miss.
        ("openadapt_capture/data/helper.dat", b"\x7fELF\x02\x01", "native executable"),
        ("openadapt_capture/data/helper.dat", b"\xcf\xfa\xed\xfe\x0c", "native executable"),
        ("openadapt_capture/data/helper.dat", b"MZ\x90\x00\x03", "native executable"),
        # A shared library lifted out of an FFmpeg build.
        ("openadapt_capture/lib/libavcodec.62.dylib", b"\xca\xfe\xba\xbe", "boundary"),
        # A container hiding one from a per-member scan.
        ("openadapt_capture/data/tools.zip", b"PK\x03\x04\x14\x00", "nested archive"),
        ("openadapt_capture/data/tools.tar.gz", b"\x1f\x8b\x08\x00", "nested archive"),
        # FFmpeg build bytes inside a file with no telling name or magic.
        (
            "openadapt_capture/data/blob.dat",
            b"\x00\x01ffmpeg version 8.1.2 Copyright\x00",
            "FFmpeg build bytes",
        ),
        (
            "openadapt_capture/data/blob.dat",
            b"\x00libavformat/mov.c\x00",
            "FFmpeg build bytes",
        ),
    ],
)
def test_release_gate_refuses_ffmpeg_bytes(
    tmp_path: Path,
    name: str,
    content: bytes,
    match: str,
) -> None:
    wheel = _wheel_with(tmp_path, {name: content})
    with pytest.raises(AssertionError, match=match):
        verify_distribution(wheel)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        # The README documents the FFmpeg build, and its text reaches METADATA.
        (
            "openadapt_capture-1.2.3.dist-info/METADATA.extra",
            b"configured with --disable-gpl, so it stays LGPL\n",
        ),
        # Documentation may name libavcodec without shipping it.
        ("openadapt_capture/notes.txt", b"Capture never links libavcodec.\n"),
    ],
)
def test_release_gate_allows_text_that_names_ffmpeg(
    tmp_path: Path,
    name: str,
    content: bytes,
) -> None:
    verify_distribution(_wheel_with(tmp_path, {name: content}))


def test_release_gate_allows_the_provisioning_source(tmp_path: Path) -> None:
    """The module that fetches FFmpeg is text, and its name must stay legal."""
    source = Path(fr.__file__).read_bytes()
    wheel = _wheel_with(tmp_path, {"openadapt_capture/ffmpeg_runtime.py": source})
    verify_distribution(wheel)


def test_the_built_wheel_and_sdist_carry_the_provisioning_source_only() -> None:
    """The shipped package must contain the fetcher and no FFmpeg bytes."""
    dist = Path(__file__).resolve().parent.parent / "dist"
    archives = sorted(dist.glob("openadapt_capture-*.whl")) + sorted(
        dist.glob("openadapt_capture-*.tar.gz")
    )
    if not archives:
        pytest.skip("no built distributions; the package-contract job builds them")
    for archive in archives:
        verify_distribution(archive)
        names = set(_archive_files(archive))
        assert any(name.endswith("openadapt_capture/ffmpeg_runtime.py") for name in names)


def test_the_release_gate_runs_on_every_pull_request_and_every_release() -> None:
    """A gate nobody runs is not a gate."""
    workflows = Path(__file__).resolve().parent.parent / ".github/workflows"
    command = "python scripts/verify_distribution.py dist/*"
    tests = (workflows / "test.yml").read_text(encoding="utf-8")
    release = (workflows / "release.yml").read_text(encoding="utf-8")
    assert command in tests, "package-contract must verify the built archives"
    assert release.count(command) >= 2, "both release paths must verify the archives"


def test_the_pin_is_checked_when_it_changes_and_on_a_schedule() -> None:
    workflow = (
        Path(__file__).resolve().parent.parent / ".github/workflows/ffmpeg-pin.yml"
    ).read_text(encoding="utf-8")
    assert "openadapt_capture/ffmpeg_runtime.py" in workflow
    assert "scripts/check_ffmpeg_pin.py" in workflow
    assert "schedule:" in workflow
    assert "capture install-ffmpeg" in workflow
