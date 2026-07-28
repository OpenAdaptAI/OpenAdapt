"""Regression tests for versioned release inputs."""

import re
import shutil
from pathlib import Path

import pytest

from scripts.check_release_consistency import (
    release_versions,
    synchronize_release_lock,
)

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_release_uv_pin_is_declared_once() -> None:
    """The `release` extra and the build command must install the same uv.

    The pin lives in two places in pyproject.toml. Only the `release` extra is
    reflected in uv.lock, so if a bump touches one and not the other, the
    release build silently installs a uv that is neither declared nor locked.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pins = set(re.findall(r'"uv==([^"]+)"', pyproject))
    assert len(pins) == 1, f"pyproject.toml pins uv at more than one version: {pins}"


def test_semantic_release_refreshes_and_stages_lock_before_tagging() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'version_variables = ["src/openadapt_tray/__init__.py:__version__"]'
        in pyproject
    )
    assert re.search(r"(?m)^allow_zero_version\s*=\s*true\s*$", pyproject)
    assert re.search(r"(?m)^major_on_zero\s*=\s*false\s*$", pyproject)
    assert "$PACKAGE_NAME" not in pyproject
    assert "uv lock --upgrade-package" not in pyproject

    install = pyproject.index(
        'python -m pip install --disable-pip-version-check "uv==0.11.32"'
    )
    synchronize = pyproject.index(
        "python scripts/check_release_consistency.py --write-lock"
    )
    validate = pyproject.index("uv lock --locked --offline")
    stage = pyproject.index("git add uv.lock")
    build = pyproject.index("uv build --wheel --sdist")
    verify = pyproject.index(
        "python scripts/check_release_consistency.py --require-dist"
    )
    assert install < synchronize < validate < stage < build < verify

    build_command = re.search(
        r'(?s)build_command = """(.*?)"""', pyproject
    )
    assert build_command
    commands = [
        line.strip() for line in build_command.group(1).splitlines() if line.strip()
    ]
    assert all(command.endswith("&&") for command in commands[:-1])


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


def test_release_actions_are_pinned_to_commits() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", workflow, flags=re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)


def test_release_uses_protected_branch_credential_everywhere() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "token: ${{ secrets.ADMIN_TOKEN }}" in workflow
    assert workflow.count("github_token: ${{ secrets.ADMIN_TOKEN }}") == 2
    assert "secrets.GITHUB_TOKEN" not in workflow
