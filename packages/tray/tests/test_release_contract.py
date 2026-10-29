"""Regression tests for versioned release inputs.

openadapt-tray releases from the OpenAdapt monorepo through the root
``Release package`` workflow, which refuses a version whose changelog section is
missing. A release bump changes pyproject.toml, the module ``__version__`` and
the changelog in one reviewed pull request; ``python
scripts/check_release_consistency.py --write-lock`` then stamps the lock.
"""

import re
import shutil
from pathlib import Path

import pytest

from scripts.check_release_consistency import (
    release_versions,
    synchronize_release_lock,
)

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT.parents[1] / ".github/workflows/release-package.yml"


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def _copy_release_inputs(destination: Path) -> None:
    (destination / "src/openadapt_tray").mkdir(parents=True)
    shutil.copy(ROOT / "pyproject.toml", destination / "pyproject.toml")
    shutil.copy(ROOT / "uv.lock", destination / "uv.lock")
    shutil.copy(
        ROOT / "src/openadapt_tray/__init__.py",
        destination / "src/openadapt_tray/__init__.py",
    )


def test_release_bump_synchronizes_only_editable_root_version(tmp_path: Path) -> None:
    _copy_release_inputs(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    package_init_path = tmp_path / "src/openadapt_tray/__init__.py"
    lock_path = tmp_path / "uv.lock"
    current_version = release_versions(tmp_path)["pyproject.toml"]
    simulated_version = "999.999.999"

    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            f'version = "{current_version}"',
            f'version = "{simulated_version}"',
            1,
        ),
        encoding="utf-8",
    )
    package_init_path.write_text(
        package_init_path.read_text(encoding="utf-8").replace(
            f'__version__ = "{current_version}"',
            f'__version__ = "{simulated_version}"',
            1,
        ),
        encoding="utf-8",
    )
    before = lock_path.read_text(encoding="utf-8")

    assert synchronize_release_lock(tmp_path)
    after = lock_path.read_text(encoding="utf-8")
    assert release_versions(tmp_path) == {
        "pyproject.toml": simulated_version,
        "src/openadapt_tray/__init__.py": simulated_version,
        "uv.lock": simulated_version,
    }
    assert after == before.replace(
        f'name = "openadapt-tray"\nversion = "{current_version}"',
        f'name = "openadapt-tray"\nversion = "{simulated_version}"',
        1,
    )
    assert not synchronize_release_lock(tmp_path)


def test_release_lock_sync_rejects_ambiguous_editable_root(tmp_path: Path) -> None:
    _copy_release_inputs(tmp_path)
    lock_path = tmp_path / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    root_entry = re.search(
        r'(?ms)^\[\[package\]\]\s*\nname = "openadapt-tray".*?(?=^\[\[package\]\]|\Z)',
        lock,
    )
    assert root_entry
    lock_path.write_text(lock + "\n" + root_entry.group(0), encoding="utf-8")

    with pytest.raises(ValueError, match="expected one editable"):
        synchronize_release_lock(tmp_path)


def test_changelog_starts_with_the_project_version() -> None:
    version = release_versions()["pyproject.toml"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headers = re.findall(
        r"^## v([^ ]+) \(\d{4}-\d{2}-\d{2}\)$", changelog, re.MULTILINE
    )
    assert headers, "CHANGELOG.md has no release sections"
    assert headers[0] == version


def test_monorepo_release_workflow_publishes_this_package() -> None:
    if not RELEASE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # The workflow's plan job decides releases from this package map.
    plan = (RELEASE_WORKFLOW.parents[2] / "scripts/ci/release_plan.py").read_text(
        encoding="utf-8"
    )
    assert '"tray": "openadapt-tray"' in plan
    assert "release_plan.py" in workflow
    assert re.search(r"options: \[[^\]]*\btray\b", workflow)
