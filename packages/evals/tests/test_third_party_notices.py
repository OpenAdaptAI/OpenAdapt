from __future__ import annotations

import importlib.util
import io
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_third_party_notices.py"
SPEC = importlib.util.spec_from_file_location("check_third_party_notices", SCRIPT)
assert SPEC and SPEC.loader
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


def _files() -> dict[str, bytes]:
    return {path: (ROOT / path).read_bytes() for path in notices.VENDORED_FILES}


def _write_wheel(
    path: Path,
    notice: bytes,
    files: dict[str, bytes],
    *,
    include_notice: bool = True,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        if include_notice:
            archive.writestr("openadapt_evals-0.0.0.dist-info/licenses/NOTICE", notice)
        for name, payload in files.items():
            archive.writestr(name, payload)


def _write_sdist(
    path: Path,
    notice: bytes,
    files: dict[str, bytes],
    *,
    include_notice: bool = True,
) -> None:
    members = dict(files)
    if include_notice:
        members[notices.NOTICE_PATH] = notice
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(f"openadapt_evals-0.0.0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_repository_notice_inventories_exact_vendored_files() -> None:
    notices.verify_repository()


def test_changed_vendored_file_requires_a_provenance_update(tmp_path: Path) -> None:
    (tmp_path / notices.NOTICE_PATH).write_bytes((ROOT / notices.NOTICE_PATH).read_bytes())
    for path in notices.VENDORED_FILES:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    first = tmp_path / next(iter(notices.VENDORED_FILES))
    first.write_bytes(first.read_bytes() + b"\n")

    with pytest.raises(notices.NoticeError, match="update its provenance"):
        notices.verify_repository(tmp_path)


def test_built_archives_carry_the_exact_notice_and_vendored_files(tmp_path: Path) -> None:
    notice = notices.verify_repository()
    files = _files()
    _write_wheel(tmp_path / "package.whl", notice, files)
    _write_sdist(tmp_path / "package.tar.gz", notice, files)

    notices.verify_distributions(tmp_path, notice)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_built_archive_without_notice_is_refused(tmp_path: Path, kind: str) -> None:
    notice = notices.verify_repository()
    files = _files()
    if kind == "wheel":
        _write_wheel(tmp_path / "package.whl", notice, files, include_notice=False)
        _write_sdist(tmp_path / "package.tar.gz", notice, files)
    else:
        _write_wheel(tmp_path / "package.whl", notice, files)
        _write_sdist(tmp_path / "package.tar.gz", notice, files, include_notice=False)

    with pytest.raises(notices.NoticeError, match="must contain one"):
        notices.verify_distributions(tmp_path, notice)


def test_release_runs_the_notice_gate_on_built_archives() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'license-files = ["LICENSE", "NOTICE"]' in metadata
    source_boundary = workflow.index("python scripts/check_source_boundary.py --require-dist")
    notice_boundary = workflow.index("python scripts/check_third_party_notices.py --require-dist")
    publish = workflow.index("pypa/gh-action-pypi-publish@")
    assert source_boundary < notice_boundary < publish
    assert "python scripts/check_third_party_notices.py --require-dist" in test_workflow
