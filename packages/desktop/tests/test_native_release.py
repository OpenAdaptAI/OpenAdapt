"""Tests for native release staging and integrity metadata."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.native_release import (
    native_version,
    stage_artifacts,
    validate_release_set,
    validate_tag,
    verify_checksums,
    write_checksums,
)

ROOT = Path(__file__).resolve().parents[1]


def test_native_versions_are_synchronized() -> None:
    assert native_version() == "0.1.1"


def test_node_dependencies_are_locked_for_cross_platform_tauri_builds() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    lock = json.loads((ROOT / "package-lock.json").read_text())

    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["version"] == package["version"]
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == "2.11.4"
    assert lock["packages"]["node_modules/@tauri-apps/api"]["version"] == "2.11.1"


def test_native_workflows_are_pinned_and_preserve_experimental_boundary() -> None:
    build = (ROOT / ".github/workflows/build.yml").read_text()
    release = (ROOT / ".github/workflows/native-release.yml").read_text()
    uses = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", build + release, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    for runner in ("macos-15", "macos-15-intel", "windows-2022", "ubuntu-22.04"):
        assert runner in build
    for bundles in ("dmg", "msi,nsis", "deb,appimage"):
        assert f"bundles: {bundles}" in build
    assert "smoke_test_native_installer.py" in build
    assert "native_release.py checksums" in build
    assert 'tags:\n      - "desktop-v*"' in release
    assert "environment: native-release" in release
    assert "subject-checksums: release-assets/SHA256SUMS" in release
    assert "attestations: write" in release
    assert "id-token: write" in release
    assert "contents: write" in release
    assert "--draft" in release and "--prerelease" in release
    assert "ADMIN_TOKEN" not in release
    assert "Experimental" in release
    publish_job = release.split("  publish-draft:", 1)[1]
    assert "actions/setup-python@" in publish_job
    assert 'python-version: "3.12"' in publish_job
    for secret in (
        "LINUX_GPG_PRIVATE_KEY",
        "LINUX_GPG_KEY_ID",
        "LINUX_GPG_PASSPHRASE",
        "LINUX_GPG_FINGERPRINT",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in release


def test_updater_feed_is_disabled_until_signing_key_lifecycle_exists() -> None:
    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())

    assert "plugins" not in config
    assert config["bundle"]["targets"] == ["dmg", "msi", "nsis", "deb", "appimage"]
    assert config["bundle"]["macOS"]["signingIdentity"] == "-"
    assert config["bundle"]["windows"]["tsp"] is True


def test_native_tag_is_distinct_from_python_release_channel() -> None:
    assert validate_tag("desktop-v0.1.1") == "desktop-v0.1.1"
    with pytest.raises(ValueError, match="desktop-v0.1.1"):
        validate_tag("v0.3.2")


@pytest.mark.parametrize(
    ("platform", "signing", "files", "expected_suffixes"),
    [
        ("macos", "adhoc", ["dmg/App_0.1.1_aarch64.dmg"], [".dmg"]),
        (
            "windows",
            "unsigned",
            ["msi/App_0.1.1_x64_en-US.msi", "nsis/App_0.1.1_x64-setup.exe"],
            [".msi", "-nsis-setup.exe"],
        ),
        (
            "linux",
            "unsigned",
            ["deb/app_0.1.1_amd64.deb", "appimage/App_0.1.1_amd64.AppImage"],
            [".deb", ".AppImage"],
        ),
    ],
)
def test_stage_artifacts_renames_and_labels_experimental(
    tmp_path: Path,
    platform: str,
    signing: str,
    files: list[str],
    expected_suffixes: list[str],
) -> None:
    bundle = tmp_path / "bundle"
    for relative in files:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())

    output = tmp_path / "staged"
    staged = stage_artifacts(
        bundle_root=bundle,
        output=output,
        platform=platform,
        architecture="x86_64",
        signing=signing,
    )
    asset_names = [path.name for path in staged if path.suffix != ".json"]
    assert len(asset_names) == len(expected_suffixes)
    assert all("Experimental-v0.1.1" in name for name in asset_names)
    assert all(any(name.endswith(suffix) for name in asset_names) for suffix in expected_suffixes)

    metadata_path = next(path for path in staged if path.suffix == ".json")
    metadata = json.loads(metadata_path.read_text())
    assert metadata["lifecycle"] == "Experimental"
    assert metadata["surface"] == "scaffold-only Tauri shell"
    assert metadata["verification_scope"] == "structural install/uninstall packaging lifecycle"
    assert metadata["artifacts"] == asset_names


def test_stage_rejects_missing_duplicate_and_wrong_signing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "dmg").mkdir(parents=True)
    (bundle / "dmg" / "one.dmg").write_bytes(b"one")
    (bundle / "dmg" / "two.dmg").write_bytes(b"two")

    with pytest.raises(ValueError, match="exactly one"):
        stage_artifacts(
            bundle_root=bundle,
            output=tmp_path / "duplicate",
            platform="macos",
            architecture="arm64",
            signing="adhoc",
        )
    with pytest.raises(ValueError, match="invalid signing mode"):
        stage_artifacts(
            bundle_root=bundle,
            output=tmp_path / "wrong-mode",
            platform="macos",
            architecture="arm64",
            signing="unsigned",
        )


def test_checksum_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"alpha")
    (tmp_path / "b.bin").write_bytes(b"beta")
    manifest = tmp_path / "SHA256SUMS"

    entries = write_checksums(tmp_path, manifest)
    assert entries == sorted(entries, key=lambda entry: entry[1])
    assert (
        dict((name, digest) for digest, name in entries)["a.bin"]
        == hashlib.sha256(b"alpha").hexdigest()
    )
    assert verify_checksums(tmp_path, manifest) == 2

    (tmp_path / "a.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_checksums(tmp_path, manifest)


def test_validate_release_set_requires_every_platform_and_no_extra_files(tmp_path: Path) -> None:
    specifications = [
        ("macos", "arm64", "adhoc", ["dmg/app-arm.dmg"]),
        ("macos", "x86_64", "adhoc", ["dmg/app-intel.dmg"]),
        (
            "windows",
            "x86_64",
            "unsigned",
            ["msi/app.msi", "nsis/app-setup.exe"],
        ),
        ("linux", "x86_64", "unsigned", ["deb/app.deb", "appimage/app.AppImage"]),
    ]
    release = tmp_path / "release"
    for index, (platform, architecture, signing, files) in enumerate(specifications):
        bundle = tmp_path / f"bundle-{index}"
        for relative in files:
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        stage = tmp_path / f"stage-{index}"
        staged = stage_artifacts(
            bundle_root=bundle,
            output=stage,
            platform=platform,
            architecture=architecture,
            signing=signing,
        )
        release.mkdir(exist_ok=True)
        for path in staged:
            path.rename(release / path.name)

    assert validate_release_set(release) == 10
    (release / "unexpected.bin").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="release assets differ"):
        validate_release_set(release)
