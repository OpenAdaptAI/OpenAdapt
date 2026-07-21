"""Keep public package metadata aligned with the current product boundary."""

import json
import re
import tomllib
from pathlib import Path

from scripts.check_release_consistency import release_versions, sync_lock_version

ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_identifies_beta_supporting_surface() -> None:
    readme = (ROOT / "README.md").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package = json.loads((ROOT / "package.json").read_text())
    cargo = tomllib.loads((ROOT / "src-tauri" / "Cargo.toml").read_text())

    assert "Lifecycle: Beta supporting surface" in readme
    assert "openadapt-flow" in readme
    assert "AI training data collection" not in readme
    assert "AI training data collection" not in pyproject["project"]["description"]
    assert "AI training data collection" not in package["description"]
    expected_native_description = (
        "Beta installed companion for OpenAdapt authoring, "
        "teaching, and local pairing"
    )
    assert package["description"] == expected_native_description
    assert cargo["package"]["description"] == expected_native_description
    assert pyproject["project"]["readme"] == "README.md"
    assert pyproject["project"]["scripts"] == {"openadapt-desktop": "engine.cli:main"}


def test_readme_does_not_publish_hard_coded_package_version_claims() -> None:
    readme = (ROOT / "README.md").read_text()
    normalized = " ".join(readme.split())
    hard_coded_version_claim = re.compile(
        r"\b(?:Python|JavaScript|Tauri)\b.{0,120}\bversions?\b.{0,120}" r"`?v?\d+\.\d+\.\d+`?",
        flags=re.IGNORECASE,
    )

    assert "synchronized to each engine release" in readme
    assert "not a separate supported desktop release" in readme
    assert hard_coded_version_claim.search(normalized) is None


def test_semantic_release_preserves_pre_one_versions() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    release = pyproject["tool"]["semantic_release"]

    assert release["major_on_zero"] is False
    assert release["allow_zero_version"] is True


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_semantic_release_refreshes_lock_and_builds_before_tagging() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build_command = pyproject["tool"]["semantic_release"]["build_command"]

    assert "python -m ensurepip --upgrade" in build_command
    assert "uv==0.11.29" in build_command
    assert "check_release_consistency.py --sync" in build_command
    assert "git add uv.lock" in build_command
    assert "uv build --wheel --sdist" in build_command
    assert "check_release_consistency.py --require-dist" in build_command
    assert "uv lock" not in build_command
    assert "$PACKAGE_NAME" not in build_command


def test_release_lock_sync_updates_only_editable_root_version(tmp_path: Path) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-desktop"\nversion = "0.3.0"\n'
    )
    (tmp_path / "engine/__init__.py").write_text('__version__ = "0.3.0"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "openadapt-desktop"\n'
        'version = "0.2.0"\nsource = { editable = "." }\n'
        '\n[[package]]\nname = "dependency"\nversion = "1.2.3"\n'
    )

    assert sync_lock_version(tmp_path) == "0.3.0"
    lock = (tmp_path / "uv.lock").read_text()
    assert 'name = "openadapt-desktop"\nversion = "0.3.0"' in lock
    assert 'name = "dependency"\nversion = "1.2.3"' in lock


def test_release_workflow_uses_matching_pinned_actions() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    uses = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert "# v10.6.1" in workflow
    assert "# v9.15.2" not in workflow
    assert "token: ${{ secrets.ADMIN_TOKEN }}" in workflow
    assert workflow.count("github_token: ${{ secrets.ADMIN_TOKEN }}") == 2
    assert "- name: Build package" not in workflow


def test_release_is_manual_and_gated_on_exact_test_and_build_heads() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    triggers = workflow[workflow.index("\non:\n") : workflow.index("\njobs:\n")]
    semantic = workflow[workflow.index("\n  semantic-release:") :]
    wait_index = semantic.index("- name: Wait for exact-head Test and Build workflows")
    head_index = semantic.index("- name: Require dispatched head to remain current protected main")
    release_index = semantic.index("- name: Python Semantic Release")

    assert "  push:" not in triggers
    assert "  workflow_dispatch:" in triggers
    assert "operation:" in triggers
    assert "- semantic-release" in triggers
    assert "- publish-existing-ref" in triggers
    assert "github.ref == 'refs/heads/main'" in semantic
    assert wait_index < head_index < release_index
    for workflow_name in ("test.yml", "build.yml"):
        assert workflow_name in semantic
    assert '--raw-field head_sha="${GITHUB_SHA}"' in semantic
    assert "refs/remotes/origin/main" in semantic
    assert "Refusing stale release dispatch" in semantic


def test_release_recovery_ref_is_main_contained_and_exact_ci_green() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    recovery = workflow[workflow.index("\n  publish-existing-ref:") :]
    build_index = recovery.index("- name: Build and validate exact recovery artifacts")
    publish_index = recovery.index("- name: Publish to PyPI")

    assert "inputs.operation == 'publish-existing-ref'" in recovery
    assert "github.ref == 'refs/heads/main'" in recovery
    assert "git merge-base --is-ancestor" in recovery
    assert 'head_sha="${TARGET_SHA}"' in recovery
    for workflow_name in ("test.yml", "build.yml"):
        assert workflow_name in recovery
    assert "guard/scripts/verify_build_artifact.py python-distribution --root target" in recovery
    assert "packages-dir: target/dist/" in recovery
    assert build_index < publish_index


def test_beta_release_notes_describe_the_bundled_flow_runtime() -> None:
    notes = (ROOT / "docs/BETA_NATIVE_INSTALLERS.md").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = (ROOT / "uv.lock").read_text()
    native_release = (ROOT / ".github/workflows/native-release.yml").read_text()
    build_dependencies = pyproject["project"]["optional-dependencies"]["build"]

    assert not (ROOT / "docs/EXPERIMENTAL_NATIVE_INSTALLERS.md").exists()
    assert native_release.count("--notes-file docs/BETA_NATIVE_INSTALLERS.md") == 2
    assert "EXPERIMENTAL_NATIVE_INSTALLERS" not in native_release
    assert "openadapt-flow==1.19.0" in build_dependencies
    assert 'name = "playwright"\nversion = "1.61.0"' in lock
    assert "openadapt-flow==1.19.0" in notes
    assert "playwright==1.61.0" in notes
    assert "without a separate Python" in notes
    assert "not frozen into these installers" not in notes


def test_readme_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", readme)

    for link in links:
        target = link.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (ROOT / target).exists(), f"README link does not exist: {link}"
