from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release_lock.py"
SPEC = importlib.util.spec_from_file_location("verify_release_lock", SCRIPT)
assert SPEC and SPEC.loader
release_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_lock)


def _write_release_files(root: Path, project_version: str, lock_version: str) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "example-package"\n'
        f'version = "{project_version}"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "dependency"\nversion = "8.1.8"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "example-package"\n'
        f'version = "{lock_version}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )


def test_real_release_metadata_is_consistent() -> None:
    project_version, lock_version = release_lock.release_versions()
    assert project_version == lock_version
    release_lock.verify_release_lock()


def test_release_lock_rejects_version_drift(tmp_path: Path) -> None:
    _write_release_files(tmp_path, "0.4.0", "0.3.0")
    try:
        release_lock.verify_release_lock(tmp_path)
    except ValueError as exc:
        assert "pyproject.toml=0.4.0, uv.lock=0.3.0" in str(exc)
    else:
        raise AssertionError("version drift was accepted")


def test_sync_changes_only_editable_root_and_is_idempotent(tmp_path: Path) -> None:
    _write_release_files(tmp_path, "0.4.0", "0.3.0")
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert release_lock.synchronize_release_lock(tmp_path) is True
    after = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert after == before.replace(
        'name = "example-package"\nversion = "0.3.0"',
        'name = "example-package"\nversion = "0.4.0"',
    )
    assert release_lock.synchronize_release_lock(tmp_path) is False
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == after


def test_release_configuration_is_fail_closed() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    test_workflow = (ROOT / ".github/workflows/test.yml").read_text(
        encoding="utf-8"
    )
    assert "major_on_zero = false" in metadata
    assert "allow_zero_version = true" in metadata
    assert (
        "python -m pip install uv==0.11.29 && "
        "python scripts/verify_release_lock.py --write && "
        "git add uv.lock && uv build"
    ) in metadata
    assert metadata.index("python -m pip install uv==0.11.29") < metadata.index(
        "python scripts/verify_release_lock.py --write"
    ) < metadata.index("git add uv.lock") < metadata.index("uv build")
    assert "run: uv build" not in workflow
    assert "astral-sh/setup-uv" not in workflow
    assert "actions/setup-python" not in workflow
    assert workflow.count("secrets.ADMIN_TOKEN") >= 3
    assert "secrets.GITHUB_TOKEN" not in workflow
    assert 'version: "0.11.29"' in test_workflow
    assert 'python-version: "3.12"' in test_workflow
    assert "uv sync --locked --extra dev" in test_workflow


def test_all_third_party_actions_are_commit_pinned() -> None:
    action_pattern = re.compile(r"uses:\s*([^\s@]+)@([^\s#]+)")
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for action, action_ref in action_pattern.findall(path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"[0-9a-f]{40}", action_ref), (
                f"{path.name}: {action}@{action_ref} is not pinned to a commit"
            )
