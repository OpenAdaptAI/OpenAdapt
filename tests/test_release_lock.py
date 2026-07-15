import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verify_release_lock.py"


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


def _run_verifier(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    script_dir = root / "scripts"
    script_dir.mkdir(exist_ok=True)
    script = script_dir / VERIFIER.name
    if script.resolve() != VERIFIER.resolve():
        shutil.copyfile(VERIFIER, script)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_lock_matches_project_version():
    result = _run_verifier(ROOT)

    assert result.returncode == 0, result.stderr


def test_release_lock_rejects_version_drift(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")
    result = _run_verifier(tmp_path)

    assert result.returncode == 1
    assert "pyproject.toml=2.0.0, uv.lock=1.6.0" in result.stderr


def test_release_lock_sync_changes_only_editable_root_and_is_idempotent(tmp_path: Path):
    _write_release_files(tmp_path, project_version="2.0.0", lock_version="1.6.0")
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")

    first = _run_verifier(tmp_path, "--write")
    assert first.returncode == 0, first.stderr
    after = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert after == before.replace(
        'name = "openadapt"\nversion = "1.6.0"',
        'name = "openadapt"\nversion = "2.0.0"',
    )
    second = _run_verifier(tmp_path, "--write")
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == after


def test_release_workflow_syncs_and_verifies_lock_before_build():
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release-and-publish.yml").read_text(
        encoding="utf-8"
    )

    sync_index = metadata.index("python scripts/verify_release_lock.py --write")
    stage_index = metadata.index("git add uv.lock")
    assert sync_index < stage_index

    pull_index = workflow.index("git pull --ff-only")
    verify_index = workflow.index("python scripts/verify_release_lock.py")
    build_index = workflow.index("poetry build")
    assert pull_index < verify_index < build_index
