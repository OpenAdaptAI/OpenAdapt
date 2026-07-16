import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

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

    verify_index = workflow.index("python scripts/verify_release_lock.py")
    build_index = workflow.index("uv build --wheel --sdist")
    artifact_index = workflow.index("python scripts/verify_release_artifacts.py")
    attest_index = workflow.index("- name: Attest release artifacts")
    transfer_index = workflow.index("- name: Transfer release artifacts")
    assert verify_index < build_index < artifact_index < attest_index < transfer_index


def test_release_workflow_pins_actions_and_separates_permissions():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    action_refs = re.findall(r"(?m)^\s*uses:\s+\S+@([^\s#]+)", workflow)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert 'requires = ["hatchling==1.31.0"]' in metadata

    assert document["permissions"] == {"contents": "read"}
    jobs = document["jobs"]
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert jobs["build-and-attest"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["publish-pypi"]["permissions"] == {"contents": "read"}
    assert jobs["publish-github"]["permissions"] == {"contents": "write"}
    assert jobs["report-release-failure"]["permissions"] == {"issues": "write"}


def test_release_workflow_publishes_the_attested_bytes_to_both_destinations():
    workflow_path = ROOT / ".github/workflows/release-and-publish.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = document["jobs"]

    build_steps = jobs["build-and-attest"]["steps"]
    attest = next(
        step for step in build_steps if step["name"] == "Attest release artifacts"
    )
    transfer = next(
        step for step in build_steps if step["name"] == "Transfer release artifacts"
    )
    assert attest["with"]["subject-path"].splitlines() == [
        "dist/*.whl",
        "dist/*.tar.gz",
    ]
    assert transfer["with"]["path"].splitlines() == [
        "dist/*.whl",
        "dist/*.tar.gz",
    ]
    assert transfer["with"]["if-no-files-found"] == "error"

    pypi_steps = jobs["publish-pypi"]["steps"]
    github_steps = jobs["publish-github"]["steps"]
    assert pypi_steps[0]["with"]["name"] == transfer["with"]["name"]
    assert github_steps[1]["with"]["name"] == transfer["with"]["name"]
    assert pypi_steps[1]["with"]["attestations"] is False
    assert github_steps[2]["with"]["tag"] == "${{ needs.release.outputs.tag }}"
