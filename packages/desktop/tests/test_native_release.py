"""Tests for native release staging and integrity metadata."""

import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

import scripts.verify_native_release_download as download_verifier
from scripts.native_release import (
    NATIVE_RELEASE_PROVENANCE,
    NATIVE_RELEASE_VERIFIER,
    VERIFIED_RELEASE_CHANNEL,
    WEBSITE_RELEASE_MANIFEST,
    expected_release_asset_names,
    native_release_tags,
    native_tag_tuple,
    native_version,
    native_version_at_ref,
    native_versions,
    python_distribution_names,
    set_native_version,
    stage_artifacts,
    sync_native_version_from_engine,
    validate_git_version_advance,
    validate_git_version_transform,
    validate_native_release_channel,
    validate_native_tag_order,
    validate_release_attestation,
    validate_release_provenance,
    validate_release_set,
    validate_sbom,
    validate_tag,
    validate_website_release_manifest,
    verify_checksums,
    write_checksums,
    write_native_release_channel,
    write_release_provenance,
    write_website_release_manifest,
)
from scripts.verify_native_release_download import (
    verify as verify_download_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
# Desktop's workflows run from the OpenAdapt monorepo root.
MONOREPO_WORKFLOWS = ROOT.parents[1] / ".github" / "workflows"
WORKFLOW_FILES = {
    "native-release.yml": "desktop-release.yml",
    "build.yml": "desktop-build.yml",
    "test.yml": "desktop-test.yml",
}


def _workflow_path(name: str) -> Path:
    if name in WORKFLOW_FILES:
        return MONOREPO_WORKFLOWS / WORKFLOW_FILES[name]
    return ROOT / ".github" / "workflows" / name


def _workflow(name: str) -> dict:
    payload = yaml.safe_load(_workflow_path(name).read_text())
    assert isinstance(payload, dict)
    return payload


def _job_steps(job: dict) -> dict[str, dict]:
    return {str(step.get("name") or step.get("uses")): step for step in job["steps"]}


def _workflow_uses(payload: dict) -> list[str]:
    return [
        step["uses"] for job in payload["jobs"].values() for step in job["steps"] if "uses" in step
    ]


def test_native_versions_are_synchronized() -> None:
    version = native_version()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    assert set(native_versions()) == {
        "package.json",
        "package-lock.json",
        'package-lock.json packages[""]',
        "src-tauri/tauri.conf.json",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
    }


def test_node_dependencies_are_locked_for_cross_platform_tauri_builds() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    lock = json.loads((ROOT / "package-lock.json").read_text())

    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["version"] == package["version"]
    assert lock["packages"]["node_modules/@tauri-apps/cli"]["version"] == "2.12.3"
    assert lock["packages"]["node_modules/@tauri-apps/api"]["version"] == "2.12.4"


def test_native_workflows_are_pinned_and_preserve_candidate_boundary() -> None:
    build = _workflow("build.yml")
    release = _workflow("native-release.yml")
    uses = _workflow_uses(build) + _workflow_uses(release)
    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)

    trigger = release[True]
    assert set(trigger) == {"workflow_run", "schedule", "workflow_dispatch"}
    assert trigger["workflow_run"] == {
        "workflows": ["Packages"],
        "types": ["completed"],
        "branches": ["main"],
    }
    assert release["permissions"] == {"contents": "read"}
    assert release["defaults"] == {"run": {"working-directory": "packages/desktop"}}
    assert release["concurrency"] == {
        "group": "desktop-release",
        "cancel-in-progress": False,
        "queue": "max",
    }

    jobs = release["jobs"]
    assert set(jobs) == {
        "gate",
        "build-python",
        "build-macos",
        "build-windows",
        "build-linux",
        "attest",
        "stage",
        "publish-pypi",
        "publish",
    }
    assert jobs["gate"]["if"].startswith("github.ref == 'refs/heads/main' && (")
    for name in ("build-python", "build-macos", "build-windows", "build-linux"):
        assert jobs[name]["needs"] == "gate"
        assert jobs[name]["if"] == "needs.gate.outputs.release == 'true'"
    # Nothing publishes until every build and the attestation succeeded.
    assert jobs["attest"]["needs"] == [
        "gate",
        "build-python",
        "build-macos",
        "build-windows",
        "build-linux",
    ]
    assert jobs["stage"]["needs"] == ["gate", "attest"]
    assert jobs["publish-pypi"]["needs"] == ["gate", "stage"]
    assert jobs["publish"]["needs"] == ["gate", "stage", "publish-pypi"]
    assert jobs["build-macos"]["environment"] == "native-release"
    assert jobs["build-windows"]["environment"] == "native-release"
    assert jobs["stage"]["environment"] == "release-identity"
    assert jobs["publish"]["environment"] == "release-identity"
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi-desktop"
    assert jobs["publish-pypi"]["permissions"] == {"id-token": "write"}
    assert jobs["attest"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    gate_steps = _job_steps(jobs["gate"])
    order = gate_steps["Require one version everywhere, its changelog, and a new tag in order"][
        "run"
    ]
    for check in (
        "check_release_consistency.py",
        "validate-tag",
        "CHANGELOG.md",
        "check_source_boundary.py",
    ):
        assert check in order
    attest_steps = _job_steps(jobs["attest"])
    assert attest_steps["Attest the exact files named by SHA256SUMS"]["with"] == {
        "subject-checksums": "packages/desktop/release-assets/SHA256SUMS"
    }
    assert attest_steps["Attest SHA256SUMS as the consumer trust root"]["with"] == {
        "subject-path": "packages/desktop/release-assets/SHA256SUMS"
    }
    assert attest_steps["Attest this release's channel"]["with"] == {
        "subject-path": "packages/desktop/release-assets/openadapt-desktop-channel.json"
    }
    text = _workflow_path("native-release.yml").read_text()
    retired_steps = (
        "--clobber",
        "installer-pointer-notes",
        "supersede-notes",
        "write-verified-index",
    )
    for retired in retired_steps:
        assert retired not in text


def test_qualification_runs_the_in_tree_tray_protocol_peer() -> None:
    # The Tray client is packages/tray in the same monorepo commit, so the
    # contract needs no checkout of another repository.
    workflow = _workflow("test.yml")
    steps = workflow["jobs"]["qualification-contract"]["steps"]
    assert not any("repository" in (step.get("with") or {}) for step in steps)
    contract = next(
        step for step in steps if step.get("name") == "Exercise the Desktop and Tray IPC contract"
    )
    assert contract["env"] == {"OPENADAPT_TRAY_SOURCE": "../tray/src"}
    assert contract["run"] == "uv run --locked pytest tests/test_tray_integration.py -v"
    assert (ROOT / contract["env"]["OPENADAPT_TRAY_SOURCE"] / "openadapt_tray" / "ipc.py").is_file()


def test_linux_sidecar_installs_browser_libraries_before_the_frozen_smoke() -> None:
    workflow = _workflow("build.yml")
    steps = workflow["jobs"]["python-sidecar"]["steps"]
    libraries = next(
        step for step in steps if step.get("name") == "Install Linux Chromium runtime libraries"
    )
    assert libraries["if"] == "runner.os == 'Linux'"
    assert libraries["run"] == "uv run --locked python -m playwright install-deps chromium"
    aliases = next(
        step for step in steps if step.get("name") == "Alias lowercase X11 names for Flow 1.34.0"
    )
    assert aliases["if"] == "runner.os == 'Linux'"
    assert aliases["run"] == "sudo python scripts/alias_lowercase_x11_sonames.py"
    smoke_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Prove frozen browser record, compile, and replay"
    )
    assert steps.index(libraries) < steps.index(aliases) < smoke_index


def test_windows_installer_lifecycle_has_an_overall_fail_closed_timeout() -> None:
    build = _workflow_path("build.yml").read_text()
    release = _workflow_path("native-release.yml").read_text()

    build_smoke = build.split(
        "- name: Smoke-test Windows MSI and NSIS install, launch, and uninstall", 1
    )[1].split("\n      - name:", 1)[0]
    release_smoke = release.split(
        "- name: Smoke-test MSI and NSIS install, signature policy, launch, and uninstall", 1
    )[1].split("\n      - name:", 1)[0]
    assert "timeout-minutes: 15" in build_smoke
    assert "timeout-minutes: 15" in release_smoke


def test_validate_sbom_requires_cyclonedx_generator_and_named_components(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release.cyclonedx.json"
    path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {"tools": [{"vendor": "Anchore", "name": "Syft", "version": "1.44.0"}]},
                "components": [
                    {
                        "type": "library",
                        "name": "openadapt-flow",
                        "version": "1.22.0",
                        "purl": "pkg:pypi/openadapt-flow@1.22.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert validate_sbom(path) == 1

    empty = json.loads(path.read_text(encoding="utf-8"))
    empty["components"] = []
    path.write_text(json.dumps(empty), encoding="utf-8")
    with pytest.raises(ValueError, match="no detected components"):
        validate_sbom(path)


def test_dependabot_covers_every_desktop_release_ecosystem() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text()

    for ecosystem in ("github-actions", "pip", "npm", "cargo"):
        assert f"package-ecosystem: {ecosystem}" in config
    assert 'directory: "/src-tauri"' in config


def test_security_workflows_cover_all_languages_and_pin_every_dependency() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    security_workflows = [
        workflow_dir / "codeql.yml",
        workflow_dir / "dependency-review.yml",
        workflow_dir / "secret-scan.yml",
    ]
    text = "\n".join(path.read_text() for path in security_workflows)
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s+\S+@([^\s#]+)", text, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    for language in ("python", "javascript-typescript", "rust"):
        assert f'"{language}"' in (workflow_dir / "codeql.yml").read_text()
    dependency_audit = (workflow_dir / "dependency-review.yml").read_text()
    assert "macos-15-intel" in dependency_audit
    assert "uv export --quiet --locked --all-extras --no-emit-project" in dependency_audit
    assert "pip-audit==2.10.1" in dependency_audit
    assert "npm audit --audit-level=high --package-lock-only" in dependency_audit
    assert "cargo install cargo-audit --version 0.22.1 --locked" in dependency_audit
    assert "cargo audit --file src-tauri/Cargo.lock" in dependency_audit
    assert "schedule:" in dependency_audit
    secret_scan = (workflow_dir / "secret-scan.yml").read_text()
    assert "fetch-depth: 0" in secret_scan
    assert "GITLEAKS_VERSION: 8.30.1" in secret_scan
    assert (
        "GITLEAKS_SHA256: "
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in secret_scan
    )
    assert "sha256sum --check --strict" in secret_scan
    assert "tests/test_gitleaks_ci.sh" in secret_scan
    assert "scripts/run_gitleaks_ci.sh" in secret_scan
    assert (
        "GITLEAKS_BASE_SHA: ${{ github.event.pull_request.base.sha || github.event.before }}"
        in secret_scan
    )
    assert (
        "GITLEAKS_HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in secret_scan
    )
    assert "GITLEAKS_TREE_SHA: ${{ github.sha }}" in secret_scan


def test_gitleaks_runner_scans_only_the_owned_range_and_the_tested_tree() -> None:
    runner = (ROOT / "scripts" / "run_gitleaks_ci.sh").read_text()

    assert 'log_options="${base_sha}..${head_sha}"' in runner
    assert 'gitleaks git . --log-opts="${log_options}"' in runner
    assert 'git archive "${tree_sha}"' in runner
    assert 'gitleaks dir "${tree_directory}"' in runner
    assert "gitleaks git . --redact --no-banner" not in runner


def test_updater_feed_is_disabled_until_signing_key_lifecycle_exists() -> None:
    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())

    assert config["plugins"] == {"deep-link": {"desktop": {"schemes": ["openadapt"]}}}
    assert "updater" not in config["plugins"]
    assert config["bundle"]["targets"] == ["dmg", "msi", "nsis", "deb", "appimage"]
    # Target releases inherit APPLE_SIGNING_IDENTITY and keep hardened runtime.
    # The explicit ad-hoc overlay is only for unsigned CI artifacts.
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


def test_update_check_is_hidden_while_the_updater_is_not_registered() -> None:
    """Without the plugin the update check can only fail, so Settings asks first."""

    main_rs = (ROOT / "src-tauri/src/main.rs").read_text()
    assert ".manage(commands::UpdaterAvailable(updater_configured))" in main_rs
    assert "commands::updater_available," in main_rs
    settings = (ROOT / "src/screens/Settings.tsx").read_text()
    assert "updaterAvailable()" in settings
    assert "Update check unavailable: ${String(e)}" not in settings


def test_installer_smoke_gates_on_a_real_launch() -> None:
    """Every installer smoke invocation must also prove the app launches.

    The structural install/uninstall lifecycle cannot see startup panics, so
    each smoke_test_native_installer.py call in the CI and release lanes must
    pass --launch-seconds (issue #26).
    """

    for workflow in ("build.yml", "native-release.yml"):
        text = _workflow_path(workflow).read_text()
        invocations = text.count("smoke_test_native_installer.py")
        assert invocations >= 3, workflow
        assert text.count("--launch-seconds") == invocations, workflow
        # Headless Linux launches need a display server and a session bus for
        # WebKitGTK and the ayatana tray indicator.
        assert "xvfb-run" in text, workflow
        assert "dbus-run-session" in text, workflow
        assert "WEBKIT_DISABLE_COMPOSITING_MODE" in text, workflow


def test_native_tag_is_distinct_from_python_release_channel() -> None:
    tag = f"openadapt-desktop-v{native_version()}"
    assert validate_tag(tag) == tag
    with pytest.raises(ValueError, match="openadapt-desktop-v"):
        validate_tag("v0.3.2")


def _write_source(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings, so a fixture is identical on every platform."""

    path.write_bytes(text.encode("utf-8"))


def _write_native_version_fixture(root: Path, version: str) -> None:
    (root / "src-tauri").mkdir()
    _write_source(
        root / "package.json",
        json.dumps({"name": "openadapt-desktop", "version": version}, indent=2) + "\n",
    )
    _write_source(
        root / "package-lock.json",
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
        + "\n",
    )
    _write_source(
        root / "src-tauri/tauri.conf.json",
        json.dumps({"productName": "OpenAdapt Desktop", "version": version}, indent=2) + "\n",
    )
    _write_source(
        root / "src-tauri/Cargo.toml",
        f'[package]\nname = "openadapt-desktop"\nversion = "{version}"\nedition = "2021"\n'
        '\n[dependencies]\nserde = { version = "1.0" }\n',
    )
    _write_source(
        root / "src-tauri/Cargo.lock",
        'version = 4\n\n[[package]]\nname = "openadapt-desktop"\n'
        f'version = "{version}"\ndependencies = []\n'
        '\n[[package]]\nname = "serde"\nversion = "1.0.200"\n',
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
    assert validate_tag("openadapt-desktop-v0.5.0", tmp_path) == "openadapt-desktop-v0.5.0"


@pytest.mark.parametrize("lock_source", ["package-root", "package-entry", "cargo"])
def test_native_version_refuses_a_drifted_lockfile(tmp_path: Path, lock_source: str) -> None:
    _write_native_version_fixture(tmp_path, "0.5.0")
    if lock_source.startswith("package"):
        path = tmp_path / "package-lock.json"
        lock = json.loads(path.read_text(encoding="utf-8"))
        if lock_source == "package-root":
            lock["version"] = "0.4.0"
        else:
            lock["packages"][""]["version"] = "0.4.0"
        path.write_text(json.dumps(lock), encoding="utf-8")
    else:
        path = tmp_path / "src-tauri" / "Cargo.lock"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'name = "openadapt-desktop"\nversion = "0.5.0"',
                'name = "openadapt-desktop"\nversion = "0.4.0"',
            ),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="native versions differ"):
        native_version(tmp_path)


def test_set_native_version_writes_utf8_lf_bytes_on_every_platform(tmp_path: Path) -> None:
    """The transform must be byte-deterministic, not platform-dependent.

    `validate_git_version_transform` reconstructs this transform and compares
    the result with Git blobs, which store LF. Text-mode writes rewrite every
    newline as CRLF on Windows and decode with the locale default, so the
    comparison fails there for reasons that have nothing to do with the tag.
    """

    _write_native_version_fixture(tmp_path, "0.1.1")

    set_native_version("0.5.0", tmp_path)

    for relative in (
        "package.json",
        "package-lock.json",
        "src-tauri/Cargo.toml",
        "src-tauri/Cargo.lock",
        "src-tauri/tauri.conf.json",
    ):
        raw = (tmp_path / relative).read_bytes()
        assert b"\r\n" not in raw, relative
        assert raw.decode("utf-8")


def test_sync_native_version_from_engine_uses_python_release_version(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "0.1.1")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-desktop"\nversion = "0.5.0"\n',
        encoding="utf-8",
    )

    versions = sync_native_version_from_engine(tmp_path)

    assert set(versions.values()) == {"0.5.0"}
    assert native_version(tmp_path) == "0.5.0"


def test_set_native_version_rejects_non_semver_input(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "0.1.1")
    for bad in ("v0.5.0", "0.5", "0.5.0-rc.1", "0.5.0;rm -rf /"):
        with pytest.raises(ValueError, match="X.Y.Z"):
            set_native_version(bad, tmp_path)
    assert native_version(tmp_path) == "0.1.1"


def _git(tmp_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_git_version_transform_requires_the_exact_reconstructed_tree(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "0.1.1")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    # Store the fixture bytes verbatim. Git for Windows defaults to
    # core.autocrlf=true, which would rewrite the blobs the transform is
    # compared against.
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    set_native_version("0.5.0", tmp_path)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "exact version transform")
    exact = _git(tmp_path, "rev-parse", "HEAD")
    assert validate_git_version_transform(base, exact, "0.5.0", root=tmp_path) == 5

    package = json.loads((tmp_path / "package.json").read_text())
    package["scripts"] = {"postinstall": "run-unreviewed-code"}
    _write_source(tmp_path / "package.json", json.dumps(package, indent=2) + "\n")
    _git(tmp_path, "add", "package.json")
    _git(tmp_path, "commit", "-qm", "tamper inside allowed file")
    tampered = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="deterministic set-version output"):
        validate_git_version_transform(base, tampered, "0.5.0", root=tmp_path)

    (tmp_path / "unexpected.py").write_text("print('not a version change')\n")
    _git(tmp_path, "add", "unexpected.py")
    _git(tmp_path, "commit", "-qm", "tamper outside allowed files")
    unexpected = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="outside the version transformation"):
        validate_git_version_transform(base, unexpected, "0.5.0", root=tmp_path)


def test_git_version_advance_refuses_a_stale_or_equal_target(tmp_path: Path) -> None:
    _write_native_version_fixture(tmp_path, "1.4.0")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    # Store the fixture bytes verbatim. Git for Windows defaults to
    # core.autocrlf=true, which would rewrite the blobs the transform is
    # compared against.
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    set_native_version("1.5.0", tmp_path)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "advance")
    candidate = _git(tmp_path, "rev-parse", "HEAD")
    assert validate_git_version_advance(base, candidate, "1.5.0", root=tmp_path) == 5

    for stale in ("1.4.0", "1.3.9"):
        with pytest.raises(ValueError, match="does not advance protected base"):
            validate_git_version_advance(base, candidate, stale, root=tmp_path)


def test_native_tag_tuple_orders_versions_and_rejects_foreign_tags() -> None:
    assert native_tag_tuple("openadapt-desktop-v0.10.2") == (0, 10, 2)
    assert native_tag_tuple("openadapt-desktop-v0.9.9") < native_tag_tuple(
        "openadapt-desktop-v0.10.0"
    )
    for bad in (
        "v0.5.0",
        "openadapt-desktop-v0.5",
        "openadapt-desktop-v0.5.0-rc.1",
        "desktop-0.5.0",
    ):
        with pytest.raises(ValueError):
            native_tag_tuple(bad)


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
def test_stage_artifacts_renames_and_labels_candidate(
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
    assert all(f"Candidate-v{current_version}" in name for name in asset_names)
    assert all(any(name.endswith(suffix) for name in asset_names) for suffix in expected_suffixes)

    metadata_path = next(path for path in staged if path.suffix == ".json")
    metadata = json.loads(metadata_path.read_text())
    assert metadata["native_version"] == current_version
    assert metadata["lifecycle"] == "Candidate"
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
    (tmp_path / "b.bin").write_bytes(b"candidate")
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


def test_checksum_verification_rejects_an_unlisted_release_asset(tmp_path: Path) -> None:
    (tmp_path / "installer.bin").write_bytes(b"installer")
    manifest = tmp_path / "SHA256SUMS"
    write_checksums(tmp_path, manifest)

    (tmp_path / "unlisted.bin").write_bytes(b"not attested")

    with pytest.raises(ValueError, match="exact release file set"):
        verify_checksums(tmp_path, manifest)


def _release(tag: str, *, marked: bool = True, draft: bool = False) -> dict:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": True,
        "body": "<!-- installer-release -->\n" if marked else "Candidate installer",
    }


def _ls_remote(*tags: str) -> str:
    return "".join(f"{'a' * 40}\trefs/tags/{tag}\n" for tag in tags)


def test_release_order_refuses_a_tag_below_the_immutable_tag_namespace() -> None:
    tags = native_release_tags(_ls_remote("openadapt-desktop-v1.11.0", "v1.11.0"))
    assert tags == ["openadapt-desktop-v1.11.0"]

    with pytest.raises(ValueError, match="is below existing native tag"):
        validate_native_tag_order("openadapt-desktop-v1.10.9", tags)
    assert (
        validate_native_tag_order("openadapt-desktop-v1.11.1", tags) == "openadapt-desktop-v1.11.1"
    )
    # The tag write is idempotent, so a re-run legitimately presents the tag
    # that already leads. That is still monotonic.
    assert (
        validate_native_tag_order("openadapt-desktop-v1.11.0", tags) == "openadapt-desktop-v1.11.0"
    )
    assert validate_native_tag_order("openadapt-desktop-v0.1.0", []) == "openadapt-desktop-v0.1.0"


def test_native_tag_namespace_parsing_fails_closed() -> None:
    # An annotated tag adds a peeled line for the same tag.
    peeled = f"{'b' * 40}\trefs/tags/openadapt-desktop-v2.0.0^{{}}\n"
    assert native_release_tags(_ls_remote("openadapt-desktop-v2.0.0") + peeled) == [
        "openadapt-desktop-v2.0.0"
    ]

    with pytest.raises(ValueError, match="invalid line"):
        native_release_tags("not-an-object-id\trefs/tags/openadapt-desktop-v1.0.0\n")
    with pytest.raises(ValueError, match="not restricted to tags"):
        native_release_tags(f"{'a' * 40}\trefs/heads/main\n")
    with pytest.raises(ValueError, match="native release tag"):
        native_release_tags(_ls_remote("openadapt-desktop-v1.2"))
    with pytest.raises(ValueError, match="list of tag names"):
        validate_native_tag_order("openadapt-desktop-v1.0.0", {"openadapt-desktop-v2.0.0": True})


def test_release_order_reads_the_tag_namespace_in_every_workflow() -> None:
    body = _workflow_path("native-release.yml").read_text()
    order = body.split("validate-release-order", 1)[1].split("\n\n", 1)[0]
    assert '--candidate-tag "${TAG}"' in order
    assert "--tags remote-tags.txt" in order
    assert "--releases" not in order
    assert "git ls-remote --tags origin > remote-tags.txt" in body


def test_release_provenance_rejects_modified_workflow_or_source(tmp_path: Path) -> None:
    tag = f"openadapt-desktop-v{native_version()}"
    common = {
        "repository": "OpenAdaptAI/OpenAdapt",
        "tag": tag,
        "source_commit": "a" * 40,
        "workflow_ref": (
            "OpenAdaptAI/OpenAdapt/.github/workflows/desktop-release.yml@refs/heads/main"
        ),
        "workflow_commit": "a" * 40,
        "run_id": 123456,
        "run_attempt": 1,
        "runner_environment": "github-hosted",
        "event": "push",
    }
    with pytest.raises(ValueError, match="workflow ref"):
        write_release_provenance(
            tmp_path / NATIVE_RELEASE_PROVENANCE,
            **{**common, "workflow_ref": "OpenAdaptAI/OpenAdapt/evil.yml@main"},
        )
    with pytest.raises(ValueError, match="workflow commit"):
        write_release_provenance(
            tmp_path / NATIVE_RELEASE_PROVENANCE,
            **{**common, "workflow_commit": "b" * 40},
        )
    with pytest.raises(ValueError, match="GitHub-hosted"):
        write_release_provenance(
            tmp_path / NATIVE_RELEASE_PROVENANCE,
            **{**common, "runner_environment": "self-hosted"},
        )
    with pytest.raises(ValueError, match="release event"):
        write_release_provenance(
            tmp_path / NATIVE_RELEASE_PROVENANCE,
            **{**common, "event": "pull_request"},
        )


def _attestation_record(provenance: dict, checksums: dict[str, str]) -> dict:
    repository = provenance["repository"]
    commit = provenance["source_commit"]
    repository_url = f"https://github.com/{repository}"
    workflow_uri = f"{repository_url}/.github/workflows/desktop-release.yml@refs/heads/main"
    invocation = (
        f"{repository_url}/actions/runs/{provenance['run_id']}/attempts/{provenance['run_attempt']}"
    )
    return {
        "verificationResult": {
            "signature": {
                "certificate": {
                    "subjectAlternativeName": workflow_uri,
                    "githubWorkflowTrigger": "workflow_dispatch",
                    "githubWorkflowSHA": commit,
                    "githubWorkflowName": "Desktop release",
                    "githubWorkflowRepository": repository,
                    "githubWorkflowRef": "refs/heads/main",
                    "buildSignerURI": workflow_uri,
                    "buildSignerDigest": commit,
                    "runnerEnvironment": "github-hosted",
                    "sourceRepositoryURI": repository_url,
                    "sourceRepositoryDigest": commit,
                    "sourceRepositoryRef": "refs/heads/main",
                    "buildConfigURI": workflow_uri,
                    "buildConfigDigest": commit,
                    "buildTrigger": "workflow_dispatch",
                    "runInvocationURI": invocation,
                }
            },
            "statement": {
                "subject": [
                    {"name": name, "digest": {"sha256": digest}}
                    for name, digest in sorted(checksums.items())
                ],
                "predicate": {
                    "buildDefinition": {
                        "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                        "externalParameters": {
                            "workflow": {
                                "path": ".github/workflows/desktop-release.yml",
                                "ref": "refs/heads/main",
                                "repository": repository_url,
                            }
                        },
                        "internalParameters": {
                            "github": {
                                "event_name": "workflow_dispatch",
                                "runner_environment": "github-hosted",
                            }
                        },
                        "resolvedDependencies": [
                            {
                                "digest": {"gitCommit": commit},
                                "uri": f"git+{repository_url}@refs/heads/main",
                            }
                        ],
                    },
                    "runDetails": {
                        "builder": {"id": workflow_uri},
                        "metadata": {"invocationId": invocation},
                    },
                },
            },
        }
    }


def _read_checksum_lines(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        assert separator
        entries.append((name, digest))
    return entries


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("certificate", "githubWorkflowSHA", "b" * 40, "githubWorkflowSHA differs"),
        ("certificate", "runnerEnvironment", "self-hosted", "runnerEnvironment differs"),
        ("workflow", "path", ".github/workflows/evil.yml", "workflow identity differs"),
    ],
)
def test_release_attestation_rejects_modified_signed_claims(
    tmp_path: Path, target: str, field: str, value: str, message: str
) -> None:
    release, _manifest, checksums_path = _stage_complete_release(tmp_path)
    tag = f"openadapt-desktop-v{native_version()}"
    commit = "a" * 40
    repository = "OpenAdaptAI/OpenAdapt"
    provenance_path = release / NATIVE_RELEASE_PROVENANCE
    provenance = validate_release_provenance(
        provenance_path,
        repository=repository,
        tag=tag,
        source_commit=commit,
    )
    record = _attestation_record(provenance, dict(_read_checksum_lines(checksums_path)))
    statement = record["verificationResult"]["statement"]
    if target == "certificate":
        record["verificationResult"]["signature"]["certificate"][field] = value
    else:
        statement["predicate"]["buildDefinition"]["externalParameters"]["workflow"][field] = value
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps([record]), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_release_attestation(
            attestation,
            directory=release,
            checksums=checksums_path,
            provenance_path=provenance_path,
            repository=repository,
            tag=tag,
            source_commit=commit,
        )


def test_release_attestation_rejects_a_forged_subject_inventory(tmp_path: Path) -> None:
    release, _manifest, checksums_path = _stage_complete_release(tmp_path)
    tag = f"openadapt-desktop-v{native_version()}"
    commit = "a" * 40
    repository = "OpenAdaptAI/OpenAdapt"
    provenance_path = release / NATIVE_RELEASE_PROVENANCE
    provenance = validate_release_provenance(
        provenance_path,
        repository=repository,
        tag=tag,
        source_commit=commit,
    )
    record = _attestation_record(provenance, dict(_read_checksum_lines(checksums_path)))
    record["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = "f" * 64
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps([record]), encoding="utf-8")

    with pytest.raises(ValueError, match="subjects differ"):
        validate_release_attestation(
            attestation,
            directory=release,
            checksums=checksums_path,
            provenance_path=provenance_path,
            repository=repository,
            tag=tag,
            source_commit=commit,
        )


def test_validate_release_set_requires_every_platform_and_no_extra_files(tmp_path: Path) -> None:
    specifications = [
        ("macos", "arm64", "developer-id-notarized", ["dmg/app-arm.dmg"]),
        ("macos", "x86_64", "developer-id-notarized", ["dmg/app-intel.dmg"]),
        (
            "windows",
            "x86_64",
            "authenticode",
            ["msi/app.msi", "nsis/app-setup.exe"],
        ),
        (
            "linux",
            "x86_64",
            "github-attested",
            ["deb/app.deb", "appimage/app.AppImage"],
        ),
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


def _stage_complete_release(tmp_path: Path) -> tuple[Path, Path, Path]:
    specifications = [
        ("macos", "arm64", "developer-id-notarized", ["dmg/app-arm.dmg"]),
        ("macos", "x86_64", "developer-id-notarized", ["dmg/app-intel.dmg"]),
        ("windows", "x86_64", "authenticode", ["msi/app.msi", "nsis/app-setup.exe"]),
        (
            "linux",
            "x86_64",
            "github-attested",
            ["deb/app.deb", "appimage/app.AppImage"],
        ),
    ]
    release = tmp_path / "release"
    release.mkdir(parents=True)
    for index, (platform, architecture, signing, files) in enumerate(specifications):
        bundle = tmp_path / f"bundle-{index}"
        for relative in files:
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        for path in stage_artifacts(
            bundle_root=bundle,
            output=tmp_path / f"stage-{index}",
            platform=platform,
            architecture=architecture,
            signing=signing,
        ):
            path.rename(release / path.name)
    tag = f"openadapt-desktop-v{native_version()}"
    sbom = release / f"OpenAdapt-Desktop-v{native_version()}.cyclonedx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {"tools": [{"name": "Syft"}]},
                "components": [{"name": "openadapt-flow"}],
            }
        ),
        encoding="utf-8",
    )
    output = write_website_release_manifest(release, tag=tag, sbom=sbom)
    (release / NATIVE_RELEASE_VERIFIER).write_text(
        "#!/usr/bin/env python3\nprint('fixture verifier')\n",
        encoding="utf-8",
    )
    source_commit = "a" * 40
    write_release_provenance(
        release / NATIVE_RELEASE_PROVENANCE,
        repository="OpenAdaptAI/OpenAdapt",
        tag=tag,
        source_commit=source_commit,
        workflow_ref=(
            "OpenAdaptAI/OpenAdapt/.github/workflows/desktop-release.yml@refs/heads/main"
        ),
        workflow_commit=source_commit,
        run_id=123456,
        run_attempt=1,
        runner_environment="github-hosted",
        event="workflow_dispatch",
    )
    for name in python_distribution_names(native_version()):
        (release / name).write_bytes(f"fixture {name}".encode())
    checksums = release / "SHA256SUMS"
    write_checksums(release, checksums)
    return release, output, checksums


def _engine_release_file(tmp_path: Path, *, version: str | None = None) -> Path:
    version = version or native_version()
    path = tmp_path / "engine-release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "databaseId": 654321,
                "isDraft": False,
                "isPrerelease": False,
                "publishedAt": "2026-08-18T12:00:00Z",
                "tagName": f"openadapt-desktop-v{version}",
                "url": (
                    f"https://github.com/OpenAdaptAI/OpenAdapt/releases/tag/openadapt-desktop-v{version}"
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_public_download_verifier_refuses_an_expanded_inventory(tmp_path: Path) -> None:
    asset = tmp_path / "installer.bin"
    asset.write_bytes(b"installer")
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(
        f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n",
        encoding="utf-8",
    )
    assert verify_download_inventory(tmp_path, checksums) == 1

    (tmp_path / "extra.bin").write_bytes(b"not signed")
    with pytest.raises(ValueError, match="do not equal"):
        verify_download_inventory(tmp_path, checksums)


def test_every_workflow_checksum_manifest_uses_the_exact_name() -> None:
    """`verify_checksums` accepts only a manifest named exactly SHA256SUMS.

    The native installer jobs are skipped on pull requests and run on `main`
    pushes, so a manifest named anything else fails after merge rather than in
    review. Check the contract as text instead.
    """

    checked = 0
    for workflow in sorted(MONOREPO_WORKFLOWS.glob("desktop-*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "--output" not in line and "--manifest" not in line:
                continue
            value = line.removesuffix("\\").strip().split()[-1]
            if "SHA256SUMS" in value:
                assert value.endswith("SHA256SUMS"), f"{workflow.name}: {line.strip()}"
                checked += 1
    assert checked >= 2


def test_release_workflow_attestation_checks_use_one_exact_identity_flag() -> None:
    """Every workflow attestation check must be a command `gh` can run.

    `gh attestation verify` treats --cert-identity, --cert-identity-regex,
    --signer-repo, and --signer-workflow as one mutually exclusive group and
    refuses the command when two are set. A release that cannot run its own
    verification step cannot publish, so this contract is checked as text.
    """

    workflow = _workflow_path("native-release.yml").read_text(encoding="utf-8")
    invocations = workflow.count("gh attestation verify")
    assert invocations >= 1
    for excluded in ("--signer-workflow", "--signer-repo", "--cert-identity-regex"):
        assert excluded not in workflow
    assert workflow.count("--cert-identity ") == invocations
    assert (
        workflow.count('--cert-oidc-issuer "https://token.actions.githubusercontent.com"')
        == invocations
    )
    assert workflow.count("--deny-self-hosted-runners") == invocations


def test_website_release_manifest_is_an_honest_index_of_staged_bytes(tmp_path: Path) -> None:
    _, output, checksums = _stage_complete_release(tmp_path)
    assert validate_website_release_manifest(output, checksums=checksums) == 6
    manifest = json.loads(output.read_text())
    assert {asset["signing"] for asset in manifest["artifacts"]} == {
        "developer-id-notarized",
        "authenticode",
        "github-attested",
    }
    assert manifest["verification"]["github_artifact_attestation"] == "required"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256_manifest", "checksums.txt"),
        ("github_artifact_attestation", "optional"),
        ("installer_smoke", "install only"),
        ("unexpected", "accepted"),
    ],
)
def test_website_release_manifest_rejects_modified_verification_contract(
    tmp_path: Path, field: str, value: str
) -> None:
    release, output, checksums = _stage_complete_release(tmp_path)
    manifest = json.loads(output.read_text())
    manifest["verification"][field] = value
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(release, checksums)
    with pytest.raises(ValueError, match="invalid verification contract"):
        validate_website_release_manifest(output, checksums=checksums)


def test_website_release_manifest_rejects_modified_sbom_format(tmp_path: Path) -> None:
    release, output, checksums = _stage_complete_release(tmp_path)
    manifest = json.loads(output.read_text())
    manifest["sbom"]["format"] = "SPDX"
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(release, checksums)
    with pytest.raises(ValueError, match="invalid SBOM format"):
        validate_website_release_manifest(output, checksums=checksums)


def test_website_release_manifest_rejects_duplicate_and_nonexistent_assets(
    tmp_path: Path,
) -> None:
    _, output, checksums = _stage_complete_release(tmp_path)
    manifest = json.loads(output.read_text())
    manifest["artifacts"][1] = dict(manifest["artifacts"][0])
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(output.parent, checksums)
    with pytest.raises(ValueError, match="invalid artifact digest|incomplete"):
        validate_website_release_manifest(output, checksums=checksums)

    _, output, checksums = _stage_complete_release(tmp_path / "nonexistent")
    manifest = json.loads(output.read_text())
    manifest["artifacts"][0]["name"] = "OpenAdapt-Desktop-does-not-exist.dmg"
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(output.parent, checksums)
    with pytest.raises(ValueError, match="invalid artifact digest"):
        validate_website_release_manifest(output, checksums=checksums)


def test_website_release_manifest_rejects_hash_metadata_and_byte_tampering(
    tmp_path: Path,
) -> None:
    _, output, checksums = _stage_complete_release(tmp_path)
    manifest = json.loads(output.read_text())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(output.parent, checksums)
    with pytest.raises(ValueError, match="digest differs"):
        validate_website_release_manifest(output, checksums=checksums)

    _, output, checksums = _stage_complete_release(tmp_path / "metadata")
    manifest = json.loads(output.read_text())
    manifest["artifacts"][0]["architecture"] = "mips64"
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(output.parent, checksums)
    with pytest.raises(ValueError, match="platform metadata"):
        validate_website_release_manifest(output, checksums=checksums)

    release, output, checksums = _stage_complete_release(tmp_path / "bytes")
    manifest = json.loads(output.read_text())
    (release / manifest["artifacts"][0]["name"]).write_bytes(b"tampered")
    write_checksums(release, checksums)
    with pytest.raises(ValueError, match="digest differs"):
        validate_website_release_manifest(output, checksums=checksums)


def test_website_release_manifest_rejects_sbom_and_checksum_tampering(tmp_path: Path) -> None:
    release, output, checksums = _stage_complete_release(tmp_path)
    manifest = json.loads(output.read_text())
    manifest["sbom"]["sha256"] = "f" * 64
    output.write_text(json.dumps(manifest), encoding="utf-8")
    write_checksums(release, checksums)
    with pytest.raises(ValueError, match="SBOM digest"):
        validate_website_release_manifest(output, checksums=checksums)

    release, output, checksums = _stage_complete_release(tmp_path / "checksum")
    lines = checksums.read_text().splitlines()
    checksums.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact release file set"):
        validate_website_release_manifest(output, checksums=checksums)

    release, output, checksums = _stage_complete_release(tmp_path / "missing-sbom")
    manifest = json.loads(output.read_text())
    (release / manifest["sbom"]["name"]).unlink()
    with pytest.raises(ValueError, match="missing SBOM"):
        validate_website_release_manifest(output, checksums=checksums)


def test_website_release_manifest_rejects_forged_metadata_checksum(tmp_path: Path) -> None:
    _, output, checksums = _stage_complete_release(tmp_path)
    lines = checksums.read_text(encoding="utf-8").splitlines()
    metadata_index = next(
        index for index, line in enumerate(lines) if line.endswith("-metadata.json")
    )
    _digest, separator, name = lines[metadata_index].partition("  ")
    assert separator and name
    lines[metadata_index] = f"{'0' * 64}  {name}"
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA256SUMS digest differs"):
        validate_website_release_manifest(output, checksums=checksums)


def test_native_publish_only_resumes_identical_bytes_and_never_clobbers() -> None:
    """OpenAdapt's releases are immutable: build a draft, verify it, publish once."""

    body = _workflow_path("native-release.yml").read_text()
    stage = body.split("\n  stage:", 1)[1].split("\n  publish-pypi:", 1)[0]
    create = stage.index('release create "${TAG}" upload/*')
    verify_draft = stage.index("verify --state draft --downloaded-dir")
    assert create < verify_draft
    for flag in ("--draft", "--verify-tag", "--latest=false"):
        assert flag in stage[create:verify_draft]
    assert "--prerelease" not in stage
    assert "release-app.outputs.token" in stage

    pypi = body.split("\n  publish-pypi:", 1)[1].split("\n  publish:", 1)[0]
    assert "release_publication.py python-dist" in pypi
    assert "release_publication.py verify-pypi" in pypi

    publish = body.split("\n  publish:", 1)[1]
    draft = publish.index("verify --state draft --downloaded-dir")
    edit = publish.index("--draft=false")
    published = publish.index('verify --downloaded-dir "${RUNNER_TEMP}/published"')
    attestation = publish.index('release verify "${TAG}"')
    assert draft < edit < published < attestation
    assert 'release verify-asset "${TAG}"' in publish
    assert "--clobber" not in body


def test_standalone_verifier_states_the_same_asset_contract() -> None:
    """The shipped verifier duplicates the asset contract; keep them equal."""

    modes = [
        {"macos": "developer-id-notarized", "windows": "authenticode", "linux": "github-attested"},
        {"macos": "adhoc", "windows": "unsigned", "linux": "github-attested"},
        {"macos": "adhoc", "windows": "authenticode", "linux": "github-attested"},
    ]
    for version in ("0.1.0", "1.11.0", "10.20.30", native_version()):
        for signing in modes:
            assert download_verifier.expected_asset_names(
                version, signing
            ) == expected_release_asset_names(version, signing)
    signing = modes[0]
    for invalid in ("1.2", "v1.2.3", "1.2.3-rc1"):
        with pytest.raises(ValueError):
            expected_release_asset_names(invalid, signing)
        with pytest.raises(ValueError):
            download_verifier.expected_asset_names(invalid, signing)
    with pytest.raises(ValueError):
        download_verifier.expected_asset_names("1.0.0", {**signing, "windows": "pfx"})


def test_download_verification_reads_through_the_checked_path(tmp_path: Path) -> None:
    """Hash the member the inventory check proved regular, not a fresh lookup."""

    directory = tmp_path / "download"
    directory.mkdir()
    (directory / "installer.bin").write_bytes(b"installer")
    write_checksums(directory, directory / "SHA256SUMS")
    assert verify_download_inventory(directory, directory / "SHA256SUMS") == 1

    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"installer")
    (directory / "installer.bin").unlink()
    (directory / "installer.bin").symlink_to(secret)

    # A link never reaches the hash loop. This is the whole defence on every
    # platform, so assert it before the platform-specific open behaviour.
    with pytest.raises(ValueError, match="link or non-regular file"):
        verify_download_inventory(directory, directory / "SHA256SUMS")

    link = directory / "installer.bin"
    if hasattr(os, "O_NOFOLLOW"):
        # POSIX refuses the open itself, so the hash can never follow a link
        # that appears between the check and the read.
        with pytest.raises(OSError):
            download_verifier._read_bytes(link)
    else:
        # Windows has no O_NOFOLLOW and no equivalent. The open succeeds there
        # by design, and the explicit is_symlink check above is the defence.
        assert download_verifier._read_bytes(link) == b"installer"


def test_git_helpers_refuse_an_option_shaped_ref(tmp_path: Path) -> None:
    """A ref that begins with ``-`` must never reach Git as an option."""

    stolen = tmp_path / "stolen.txt"
    for injected in (f"--output={stolen}", "-h", ""):
        with pytest.raises(ValueError, match="invalid Git ref|does not name exactly one commit"):
            validate_git_version_transform(injected, "HEAD", native_version())
        with pytest.raises(ValueError, match="invalid Git ref|does not name exactly one commit"):
            native_version_at_ref(injected)
    assert not stolen.exists()

    with pytest.raises(ValueError, match="does not name exactly one commit"):
        native_version_at_ref("refs/heads/no-such-branch-for-this-test")


def test_native_version_at_ref_reads_git_objects_not_the_working_tree() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert native_version_at_ref(head) == native_version()
    assert native_version_at_ref("HEAD") == native_version()


def test_frontend_and_native_plugins_share_the_same_protocol_version() -> None:
    """A JS plugin update must ship with its matching native IPC implementation."""
    root = Path(__file__).resolve().parents[1]
    npm = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "src-tauri/Cargo.lock").read_text(encoding="utf-8"))
    native_versions = {package["name"]: package["version"] for package in cargo["package"]}
    for name, package in npm["packages"].items():
        prefix = "node_modules/@tauri-apps/plugin-"
        if name.startswith(prefix):
            native_name = "tauri-plugin-" + name.removeprefix(prefix)
            assert package["version"] == native_versions[native_name], native_name


MAIN_WORKFLOW_REF = "OpenAdaptAI/OpenAdapt/.github/workflows/desktop-release.yml@refs/heads/main"


def _write_v3_channel(release: Path, *, previous: Path | None = None) -> Path:
    return write_native_release_channel(
        release / VERIFIED_RELEASE_CHANNEL,
        directory=release,
        repository="OpenAdaptAI/OpenAdapt",
        tag=f"openadapt-desktop-v{native_version()}",
        source_commit="a" * 40,
        workflow_ref=MAIN_WORKFLOW_REF,
        workflow_commit="a" * 40,
        run_id=123456,
        run_attempt=1,
        previous=previous,
    )


def _prior_channel(tmp_path: Path, current: Path) -> Path:
    """A valid v3 channel for openadapt-desktop-v0.1.0, derived from ``current``."""

    prior = json.loads(current.read_text(encoding="utf-8"))
    prior["native_version"] = "0.1.0"
    prior["native_tag"] = "openadapt-desktop-v0.1.0"
    base = "https://github.com/OpenAdaptAI/OpenAdapt/releases"
    prior["native_release_url"] = f"{base}/tag/openadapt-desktop-v0.1.0"
    for field in ("checksums", "provenance"):
        prior[field] = dict(prior[field])
        prior[field]["url"] = f"{base}/download/openadapt-desktop-v0.1.0/{prior[field]['name']}"
    prior["previous"] = None
    path = tmp_path / "prior" / VERIFIED_RELEASE_CHANNEL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(prior, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_native_release_channel_binds_this_release_and_its_signing(tmp_path: Path) -> None:
    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    channel = _write_v3_channel(release)
    data = validate_native_release_channel(channel, directory=release)

    assert data["schema"] == "openadapt.desktop-release-channel/v3"
    assert data["native_tag"] == f"openadapt-desktop-v{native_version()}"
    assert data["signing"] == {
        "macos": "developer-id-notarized",
        "windows": "authenticode",
        "linux": "github-attested",
    }
    assert data["checksums"]["sha256"] == hashlib.sha256(checksums.read_bytes()).hexdigest()
    assert data["checksums"]["url"].endswith(
        f"/download/openadapt-desktop-v{native_version()}/SHA256SUMS"
    )
    assert data["previous"] is None
    # The channel binds SHA256SUMS, so SHA256SUMS never lists the channel.
    assert VERIFIED_RELEASE_CHANNEL not in checksums.read_text(encoding="utf-8")

    checksums.write_text(checksums.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksums hash differs"):
        validate_native_release_channel(channel, directory=release)


def test_native_release_channel_states_ad_hoc_and_unsigned_builds(tmp_path: Path) -> None:
    """A candidate may ship without paid signing, and says exactly which."""

    release, _manifest, _checksums = _stage_complete_release(tmp_path / "assets")
    for old, new in (("developer-id-notarized", "adhoc"), ("authenticode", "unsigned")):
        for path in sorted(release.glob(f"*-{old}*")):
            renamed = release / path.name.replace(old, new)
            if path.name.endswith("-metadata.json"):
                metadata = json.loads(path.read_text(encoding="utf-8"))
                metadata["signing"] = new
                metadata["artifacts"] = [name.replace(old, new) for name in metadata["artifacts"]]
                renamed.write_text(json.dumps(metadata), encoding="utf-8")
                path.unlink()
            else:
                path.rename(renamed)
    for name in (
        WEBSITE_RELEASE_MANIFEST,
        "SHA256SUMS",
    ):
        (release / name).unlink()
    sbom = release / f"OpenAdapt-Desktop-v{native_version()}.cyclonedx.json"
    manifest = write_website_release_manifest(
        release, tag=f"openadapt-desktop-v{native_version()}", sbom=sbom
    )
    write_checksums(release, release / "SHA256SUMS")
    verification = json.loads(manifest.read_text(encoding="utf-8"))["verification"]
    assert "ad-hoc" in verification["macos_native_trust"]
    assert "unsigned" in verification["windows_native_trust"]

    data = validate_native_release_channel(_write_v3_channel(release), directory=release)
    assert data["signing"] == {"macos": "adhoc", "windows": "unsigned", "linux": "github-attested"}
    assert download_verifier.expected_asset_names(native_version(), data["signing"]) == (
        expected_release_asset_names(native_version(), data["signing"])
    )
    with pytest.raises(ValueError, match="production release requires"):
        validate_release_set(release, require_production=True)


def test_native_release_channel_chains_to_the_previous_release(tmp_path: Path) -> None:
    release, _manifest, _checksums = _stage_complete_release(tmp_path / "assets")
    seed = _write_v3_channel(release)
    prior = _prior_channel(tmp_path, seed)
    seed.unlink()

    channel = _write_v3_channel(release, previous=prior)
    data = validate_native_release_channel(channel, directory=release, previous=prior)
    assert data["previous"] == {
        "native_tag": "openadapt-desktop-v0.1.0",
        "sha256": hashlib.sha256(prior.read_bytes()).hexdigest(),
        "url": (
            "https://github.com/OpenAdaptAI/OpenAdapt/releases/download/openadapt-desktop-v0.1.0/"
            "openadapt-desktop-channel.json"
        ),
    }
    prior.write_text(prior.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="previous release channel bytes differ"):
        validate_native_release_channel(channel, previous=prior)


def test_native_release_channel_refuses_a_rollback_or_foreign_workflow(tmp_path: Path) -> None:
    release, _manifest, _checksums = _stage_complete_release(tmp_path / "assets")
    seed = _write_v3_channel(release)
    newer = json.loads(seed.read_text(encoding="utf-8"))
    newer["native_tag"] = "openadapt-desktop-v99.0.0"
    newer_path = tmp_path / "newer" / VERIFIED_RELEASE_CHANNEL
    newer_path.parent.mkdir()
    newer_path.write_text(json.dumps(newer), encoding="utf-8")
    seed.unlink()
    with pytest.raises(ValueError):
        _write_v3_channel(release, previous=newer_path)

    with pytest.raises(ValueError, match="workflow ref must be exactly"):
        write_native_release_channel(
            release / VERIFIED_RELEASE_CHANNEL,
            directory=release,
            repository="OpenAdaptAI/OpenAdapt",
            tag=f"openadapt-desktop-v{native_version()}",
            source_commit="a" * 40,
            workflow_ref="OpenAdaptAI/OpenAdapt/.github/workflows/evil.yml@refs/heads/main",
            workflow_commit="a" * 40,
            run_id=1,
            run_attempt=1,
        )


def test_public_download_verifier_authenticates_the_channel_and_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    channel = _write_v3_channel(release)
    commands: list[list[str]] = []

    def _record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "verified", "")

    monkeypatch.setattr(download_verifier.subprocess, "run", _record)

    assert download_verifier.verify_authenticated_channel(
        channel_path=channel,
        directory=release,
        checksums=checksums,
        minimum_version=native_version(),
        repository="OpenAdaptAI/OpenAdapt",
    ) == len(_read_checksum_lines(checksums))
    assert [Path(command[3]).name for command in commands] == [
        VERIFIED_RELEASE_CHANNEL,
        "SHA256SUMS",
    ]
    main_identity = f"https://github.com/{MAIN_WORKFLOW_REF}"
    exclusive = {"--cert-identity", "--cert-identity-regex", "--signer-repo", "--signer-workflow"}
    for command in commands:
        assert command[:3] == ["gh", "attestation", "verify"]
        assert "--deny-self-hosted-runners" in command
        # `gh attestation verify` refuses two flags from this group at once.
        assert exclusive.intersection(command) == {"--cert-identity"}
        assert command[command.index("--cert-identity") + 1] == main_identity
        issuer = command[command.index("--cert-oidc-issuer") + 1]
        assert issuer == "https://token.actions.githubusercontent.com"

    with pytest.raises(ValueError, match="below the trusted minimum"):
        download_verifier.verify_authenticated_channel(
            channel_path=channel,
            directory=release,
            checksums=checksums,
            minimum_version="999.0.0",
        )


def test_public_download_verifier_uses_the_authenticated_checksum_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local writer cannot swap SHA256SUMS after it is authenticated."""

    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    channel = _write_v3_channel(release)
    monkeypatch.setattr(
        download_verifier.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "verified", ""),
    )
    expected = _read_checksum_lines(checksums)
    real_verify = download_verifier.verify

    def _swap_then_verify(directory: Path, manifest: Path, **kwargs: object) -> int:
        manifest.write_text(f"{'0' * 64}  OpenAdapt-Desktop-attacker.dmg\n", encoding="utf-8")
        return real_verify(directory, manifest, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(download_verifier, "verify", _swap_then_verify)
    assert download_verifier.verify_authenticated_channel(
        channel_path=channel, directory=release, checksums=checksums
    ) == len(expected)


def test_public_download_verifier_rejects_substitution_and_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    channel = _write_v3_channel(release)
    monkeypatch.setattr(
        download_verifier.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "verified", ""),
    )
    original = checksums.read_text(encoding="utf-8")
    checksums.write_text(original + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256SUMS digest differs"):
        download_verifier.verify_authenticated_channel(
            channel_path=channel, directory=release, checksums=checksums
        )
    checksums.write_text(original, encoding="utf-8")

    def _mutate(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        subject = Path(command[3])
        subject.write_text(subject.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "verified", "")

    monkeypatch.setattr(download_verifier.subprocess, "run", _mutate)
    with pytest.raises(ValueError, match="changed during attestation"):
        download_verifier.verify_authenticated_channel(
            channel_path=channel, directory=release, checksums=checksums
        )


def test_public_download_verifier_checks_the_retained_prior_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    seed = _write_v3_channel(release)
    prior = _prior_channel(tmp_path, seed)
    seed.unlink()
    channel = _write_v3_channel(release, previous=prior)
    monkeypatch.setattr(
        download_verifier.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "verified", ""),
    )

    assert download_verifier.verify_authenticated_channel(
        channel_path=channel, directory=release, checksums=checksums, previous_channel=prior
    )
    prior.write_text(prior.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not extend"):
        download_verifier.verify_authenticated_channel(
            channel_path=channel, directory=release, checksums=checksums, previous_channel=prior
        )
