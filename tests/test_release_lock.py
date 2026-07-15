from pathlib import Path

import pytest

from scripts.verify_release_lock import (
    release_versions,
    synchronize_release_lock,
    verify_release_lock,
)


def _write_release_files(root: Path, project_version: str, lock_version: str):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "openadapt"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "click"\nversion = "8.1.8"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "openadapt"\n'
        f'version = "{lock_version}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )


def test_release_lock_matches_project_version():
    project_version, lock_version = release_versions()

    assert project_version == lock_version


def test_release_lock_rejects_version_drift(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")

    with pytest.raises(ValueError, match="pyproject.toml=2.0.0, uv.lock=1.6.0"):
        verify_release_lock(tmp_path)


def test_release_lock_sync_changes_only_editable_root_and_is_idempotent(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")

    assert synchronize_release_lock(tmp_path) is True
    after = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert after == before.replace(
        'name = "openadapt"\nversion = "1.6.0"',
        'name = "openadapt"\nversion = "2.0.0"',
    )
    assert synchronize_release_lock(tmp_path) is False
    assert release_versions(tmp_path) == ("2.0.0", "2.0.0")


def test_release_workflow_syncs_and_verifies_lock_before_build():
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/release-and-publish.yml").read_text(
        encoding="utf-8"
    )

    sync_index = metadata.index("python scripts/verify_release_lock.py --write")
    stage_index = metadata.index("git add uv.lock")
    assert sync_index < stage_index

    pull_index = workflow.index("git pull --ff-only")
    verify_index = workflow.index("python scripts/verify_release_lock.py")
    build_index = workflow.index("poetry build")
    assert pull_index < verify_index < build_index
