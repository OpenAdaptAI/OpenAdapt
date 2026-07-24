"""Tests for native release staging and integrity metadata."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.native_release import (
    native_tag_tuple,
    native_version,
    set_native_version,
    stage_artifacts,
    superseded_notes,
    validate_release_set,
    validate_tag,
    verify_checksums,
    write_checksums,
)

ROOT = Path(__file__).resolve().parents[1]


def test_native_versions_are_synchronized() -> None:
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", native_version())


def test_node_dependencies_are_locked_for_cross_platform_tauri_builds() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    lock = json.loads((ROOT / "package-lock.json").read_text())

    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["version"] == package["version"]
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == "2.11.4"
    assert lock["packages"]["node_modules/@tauri-apps/api"]["version"] == "2.11.1"


def test_native_workflows_are_pinned_and_preserve_beta_boundary() -> None:
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
    assert "Beta" in release
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


def test_windows_installer_lifecycle_has_an_overall_fail_closed_timeout() -> None:
    build = (ROOT / ".github/workflows/build.yml").read_text()
    release = (ROOT / ".github/workflows/native-release.yml").read_text()

    build_smoke = build.split(
        "- name: Smoke-test Windows MSI and NSIS install, launch, and uninstall", 1
    )[1].split("\n      - name:", 1)[0]
    release_smoke = release.split(
        "- name: Smoke-test MSI and NSIS install, signature policy, launch, and uninstall", 1
    )[1].split("\n      - name:", 1)[0]
    assert "timeout-minutes: 15" in build_smoke
    assert "timeout-minutes: 15" in release_smoke


def test_freshness_workflow_syncs_engine_releases_into_the_native_lane() -> None:
    freshness = (ROOT / ".github/workflows/native-freshness.yml").read_text()
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s+\S+@([^\s#]+)", freshness, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    # Fires on published engine releases and manual backfill only.
    assert "types: [published]" in freshness
    assert "workflow_dispatch:" in freshness
    # Never re-triggers itself from desktop-v* prereleases or drafts.
    assert "startsWith(github.event.release.tag_name, 'v')" in freshness
    assert "!github.event.release.prerelease" in freshness
    assert "!github.event.release.draft" in freshness
    # Pushing to protected main and triggering native-release.yml both
    # require the PAT; the default token would not start downstream workflows.
    assert "token: ${{ secrets.ADMIN_TOKEN }}" in freshness
    # Reuses the guarded version sync and stays idempotent per tag.
    assert "native_release.py set-version" in freshness
    assert "git ls-remote --exit-code --tags origin" in freshness
    # An absent historical tag may not label newer application code with an
    # older engine version.
    provenance_gate = freshness.split(
        "- name: Require exact engine-release application sources", 1
    )[1].split("- name: Sync native version sources and lockfiles", 1)[0]
    assert "refs/tags/v${NATIVE_VERSION}^{commit}" in provenance_gate
    assert "git merge-base --is-ancestor" in provenance_gate
    assert "release_versions" in provenance_gate
    assert 'git diff --quiet "${engine_commit}..HEAD"' in provenance_gate
    for protected_path in (
        "engine",
        "src",
        "src-tauri/src",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
        "src-tauri/tauri.conf.json",
    ):
        assert protected_path in provenance_gate
    publish_step = freshness.split("- name: Commit the sync, tag desktop-v*, and push", 1)[1].split(
        "  supersede-published-native:", 1
    )[0]
    assert 'git push --atomic origin HEAD:main "refs/tags/${NATIVE_TAG}"' in publish_step
    assert publish_step.count("git push origin") == 1
    # No builds here: the existing native release workflow owns the matrix,
    # signing preflight, checksums, and attestation.
    assert "tauri build" not in freshness
    assert "git add -A" not in freshness
    assert "git add ." not in freshness


def test_supersession_edits_notes_only_and_never_deletes() -> None:
    release = (ROOT / ".github/workflows/native-release.yml").read_text()
    freshness = (ROOT / ".github/workflows/native-freshness.yml").read_text()

    # Draft creation never invalidates the currently published installer.
    assert "openadapt-superseded-by" not in release
    assert "  supersede-published-native:" in freshness
    supersede_job = freshness.split("  supersede-published-native:", 1)[1]
    assert "github.event_name == 'release'" in supersede_job
    assert "github.event.release.prerelease" in supersede_job
    assert "!github.event.release.draft" in supersede_job
    assert "contains(github.event.release.body, '<!-- installer-release -->')" in supersede_job
    # The supersede job carries no `environment:` gate: it runs only after a
    # maintainer has already published (un-drafted) a native prerelease, so the
    # publish decision is already made. Only the publish step keeps the
    # `environment: native-release` approval gate.
    assert "environment: native-release" not in supersede_job
    assert "permissions:\n      contents: write" in supersede_job
    assert "native_release.py supersede-notes" in supersede_job
    assert "gh release edit" in supersede_job
    assert "gh release delete" not in release + freshness
    assert "delete-asset" not in release + freshness
    assert "--clobber" not in supersede_job


def test_updater_feed_is_disabled_until_signing_key_lifecycle_exists() -> None:
    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())

    assert config["plugins"] == {"deep-link": {"desktop": {"schemes": ["openadapt"]}}}
    assert "updater" not in config["plugins"]
    assert config["bundle"]["targets"] == ["dmg", "msi", "nsis", "deb", "appimage"]
    # Target releases inherit APPLE_SIGNING_IDENTITY and keep hardened runtime.
    # The explicit ad-hoc overlay is only for unsigned beta artifacts.
    assert "signingIdentity" not in config["bundle"]["macOS"]
    assert config["bundle"]["macOS"]["entitlements"] == "Entitlements.plist"
    entitlements = (ROOT / "src-tauri" / config["bundle"]["macOS"]["entitlements"]).read_text()
    assert "com.apple.security.cs.disable-library-validation" in entitlements
    adhoc = json.loads((ROOT / "src-tauri/tauri.adhoc.conf.json").read_text())
    assert adhoc["bundle"]["macOS"] == {
        "signingIdentity": "-",
        "hardenedRuntime": False,
    }
    assert config["bundle"]["windows"]["tsp"] is True

    # With no `plugins.updater` config, Tauri hands the updater plugin JSON
    # `null`, its required Config fails to deserialize, and every launch on
    # every platform aborts with PluginInitialization("updater", ...) -- the
    # shipped v0.6.1 DMG bug (issue #26).  While the key above stays forbidden,
    # the plugin registration must stay behind the config-presence gate.
    main_rs = (ROOT / "src-tauri/src/main.rs").read_text()
    assert main_rs.count("tauri_plugin_updater::Builder::new().build()") == 1
    assert 'get("updater")' in main_rs
    guarded = main_rs.split("if updater_configured", 1)
    assert len(guarded) == 2
    assert "tauri_plugin_updater::Builder::new().build()" in guarded[1]


def test_installer_smoke_gates_on_a_real_launch() -> None:
    """Every installer smoke invocation must also prove the app launches.

    The structural install/uninstall lifecycle cannot see startup panics, so
    each smoke_test_native_installer.py call in the CI and release lanes must
    pass --launch-seconds (issue #26).
    """

    for workflow in ("build.yml", "native-release.yml"):
        text = (ROOT / ".github/workflows" / workflow).read_text()
        invocations = text.count("smoke_test_native_installer.py")
        assert invocations >= 3, workflow
        assert text.count("--launch-seconds") == invocations, workflow
        # Headless Linux launches need a display server and a session bus for
        # WebKitGTK and the ayatana tray indicator.
        assert "xvfb-run" in text, workflow
        assert "dbus-run-session" in text, workflow
        assert "WEBKIT_DISABLE_COMPOSITING_MODE" in text, workflow


def test_native_tag_is_distinct_from_python_release_channel() -> None:
    tag = f"desktop-v{native_version()}"
    assert validate_tag(tag) == tag
    with pytest.raises(ValueError, match="desktop-v"):
        validate_tag("v0.3.2")


def _write_native_version_fixture(root: Path, version: str) -> None:
    (root / "src-tauri").mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "openadapt-desktop", "version": version}, indent=2) + "\n"
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "openadapt-desktop",
                "version": version,
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "openadapt-desktop", "version": version},
                    "node_modules/left-pad": {"version": "1.3.0"},
                },
            },
            indent=2,
        )
        + "\n"
    )
    (root / "src-tauri/tauri.conf.json").write_text(
        json.dumps({"productName": "OpenAdapt Desktop", "version": version}, indent=2) + "\n"
    )
    (root / "src-tauri/Cargo.toml").write_text(
        f'[package]\nname = "openadapt-desktop"\nversion = "{version}"\nedition = "2021"\n'
        '\n[dependencies]\nserde = { version = "1.0" }\n'
    )
    (root / "src-tauri/Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "openadapt-desktop"\n'
        f'version = "{version}"\ndependencies = []\n'
        '\n[[package]]\nname = "serde"\nversion = "1.0.200"\n'
    )


def test_set_native_version_synchronizes_every_source_and_lockfile(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "0.1.1")

    versions = set_native_version("0.5.0", tmp_path)

    assert set(versions.values()) == {"0.5.0"}
    assert native_version(tmp_path) == "0.5.0"
    lock = json.loads((tmp_path / "package-lock.json").read_text())
    assert lock["version"] == "0.5.0"
    assert lock["packages"][""]["version"] == "0.5.0"
    assert lock["packages"]["node_modules/left-pad"]["version"] == "1.3.0"
    cargo_lock = (tmp_path / "src-tauri/Cargo.lock").read_text()
    assert 'name = "openadapt-desktop"\nversion = "0.5.0"' in cargo_lock
    assert 'name = "serde"\nversion = "1.0.200"' in cargo_lock
    cargo_toml = (tmp_path / "src-tauri/Cargo.toml").read_text()
    assert 'version = "0.5.0"' in cargo_toml
    assert 'serde = { version = "1.0" }' in cargo_toml
    assert validate_tag("desktop-v0.5.0", tmp_path) == "desktop-v0.5.0"


def test_set_native_version_rejects_non_semver_input(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "0.1.1")
    for bad in ("v0.5.0", "0.5", "0.5.0-rc.1", "0.5.0;rm -rf /"):
        with pytest.raises(ValueError, match="X.Y.Z"):
            set_native_version(bad, tmp_path)
    assert native_version(tmp_path) == "0.1.1"


def test_native_tag_tuple_orders_versions_and_rejects_foreign_tags() -> None:
    assert native_tag_tuple("desktop-v0.10.2") == (0, 10, 2)
    assert native_tag_tuple("desktop-v0.9.9") < native_tag_tuple("desktop-v0.10.0")
    for bad in ("v0.5.0", "desktop-v0.5", "desktop-v0.5.0-rc.1", "desktop-0.5.0"):
        with pytest.raises(ValueError):
            native_tag_tuple(bad)


def test_superseded_notes_prepends_marker_and_preserves_body() -> None:
    body = "<!-- installer-release -->\n\n# Beta Native Installers\n\nDetails.\n"

    updated = superseded_notes(body, "desktop-v0.5.0", "OpenAdaptAI/openadapt-desktop")

    assert updated is not None
    assert updated.startswith("<!-- openadapt-superseded-by: desktop-v0.5.0 -->\n")
    assert "**Superseded by [desktop-v0.5.0]" in updated
    assert "do not use" in updated
    assert "releases/tag/desktop-v0.5.0" in updated
    assert updated.endswith(body)


def test_superseded_notes_is_idempotent_and_upgrades_to_newer_pointer() -> None:
    body = "<!-- installer-release -->\n\nDetails.\n"
    once = superseded_notes(body, "desktop-v0.5.0", "OpenAdaptAI/openadapt-desktop")
    assert once is not None

    assert superseded_notes(once, "desktop-v0.5.0", "OpenAdaptAI/openadapt-desktop") is None
    assert superseded_notes(once, "desktop-v0.4.0", "OpenAdaptAI/openadapt-desktop") is None

    upgraded = superseded_notes(once, "desktop-v0.6.0", "OpenAdaptAI/openadapt-desktop")
    assert upgraded is not None
    assert upgraded.count("openadapt-superseded-by") == 1
    assert "desktop-v0.6.0" in upgraded
    assert upgraded.endswith(body)


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
def test_stage_artifacts_renames_and_labels_beta(
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
    current_version = native_version()
    assert all(f"Beta-v{current_version}" in name for name in asset_names)
    assert all(any(name.endswith(suffix) for name in asset_names) for suffix in expected_suffixes)

    metadata_path = next(path for path in staged if path.suffix == ".json")
    metadata = json.loads(metadata_path.read_text())
    assert metadata["native_version"] == current_version
    assert metadata["lifecycle"] == "Beta"
    assert metadata["surface"] == "installed desktop pairing and authoring companion"
    assert metadata["verification_scope"] == (
        "cross-platform install/uninstall, self-contained Flow runtime, "
        "browser provision, and protocol-handler packaging"
    )
    assert metadata["limitations"] == [
        (
            "The first browser workflow downloads the Chromium revision pinned by the "
            "bundled Playwright runtime unless PLAYWRIGHT_BROWSERS_PATH points at an "
            "approved offline prebundle."
        ),
        "Installer verification does not replace qualification of a complete real workflow.",
    ]
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
