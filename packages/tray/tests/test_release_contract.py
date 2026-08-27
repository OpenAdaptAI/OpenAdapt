"""Regression tests for versioned release inputs."""

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.check_release_consistency import (
    release_versions,
    synchronize_release_lock,
)
from scripts.run_semantic_release import (
    REQUIRED_RUNTIME,
    ReleaseRuntimeError,
    run_release,
)

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_release_uv_pin_is_declared_once() -> None:
    """The reviewed lock must supply the only uv used during a release."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject.count('"uv==0.12.5"') == 1

    build_command = re.search(r'(?s)build_command = """(.*?)"""', pyproject)
    assert build_command
    assert "pip install" not in build_command.group(1)
    assert "uv build --no-build-isolation --wheel --sdist" in build_command.group(1)


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

    synchronize = pyproject.index(
        "python scripts/check_release_consistency.py --write-lock"
    )
    validate = pyproject.index("uv lock --locked --offline")
    stage = pyproject.index("git add uv.lock")
    build = pyproject.index("uv build --no-build-isolation --wheel --sdist")
    verify = pyproject.index(
        "python scripts/check_release_consistency.py --require-dist"
    )
    assert synchronize < validate < stage < build < verify

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


def test_release_uses_the_reviewed_locked_psr_runtime() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert "python-semantic-release/python-semantic-release@" not in workflow
    assert 'version: "0.12.5"' in workflow
    assert "uv sync --locked --extra release" in workflow
    assert "uv run --locked --extra release" in workflow
    assert "python scripts/run_semantic_release.py" in workflow
    assert "GH_TOKEN: ${{ secrets.ADMIN_TOKEN }}" in workflow
    assert '"python-semantic-release==10.6.1"' in pyproject
    assert '"GitPython==3.1.59"' in pyproject
    assert pyproject.count('"hatchling==1.31.0"') == 2
    assert re.search(
        r'(?ms)^name = "python-semantic-release"\nversion = "10\.6\.1"$', lock
    )
    assert re.search(r'(?ms)^name = "gitpython"\nversion = "3\.1\.59"$', lock)
    assert re.search(r'(?ms)^name = "hatchling"\nversion = "1\.31\.0"$', lock)


def test_locked_wrapper_preserves_psr_github_outputs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    real_python = runtime_dir / "python"
    real_python.touch()
    python = bin_dir / "python"
    python.symlink_to(real_python)
    cli = bin_dir / "semantic-release"
    cli.touch()
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "github-output"
    environment = {"GH_TOKEN": "test-token", "GITHUB_OUTPUT": str(output)}

    def fake_runner(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[object]:
        assert command == [str(cli), "-v", "version"]
        assert check is False
        Path(env["GITHUB_OUTPUT"]).write_text(
            "released=true\nversion=0.3.3\ntag=v0.3.3\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    result = run_release(
        environment=environment,
        version_reader=lambda name: REQUIRED_RUNTIME[name],
        python_executable=str(python),
        runner=fake_runner,
    )

    assert result == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "released=true",
        "version=0.3.3",
        "tag=v0.3.3",
    ]


def test_locked_wrapper_refuses_runtime_drift(tmp_path: Path) -> None:
    with pytest.raises(ReleaseRuntimeError, match="GitPython version differs"):
        run_release(
            environment={
                "GH_TOKEN": "test-token",
                "GITHUB_OUTPUT": str(tmp_path / "output"),
            },
            version_reader=lambda name: (
                "3.1.60" if name == "GitPython" else REQUIRED_RUNTIME[name]
            ),
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
        )


def test_release_uses_protected_branch_credential_everywhere() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "token: ${{ secrets.ADMIN_TOKEN }}" in workflow
    assert workflow.count("github_token: ${{ secrets.ADMIN_TOKEN }}") == 1
    assert workflow.count("GH_TOKEN: ${{ secrets.ADMIN_TOKEN }}") == 1
    assert "secrets.GITHUB_TOKEN" not in workflow
