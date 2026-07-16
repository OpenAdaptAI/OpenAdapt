from __future__ import annotations

import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
verify_release_artifacts = runpy.run_path(
    str(ROOT / "scripts/verify_release_artifacts.py")
)["verify_release_artifacts"]


def _metadata(version: str) -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: openadapt\n"
        f"Version: {version}\n"
        "Summary: Beta launcher\n"
        "Requires-Python: >=3.10\n"
        "Classifier: Development Status :: 4 - Beta\n\n"
    ).encode()


def _release_tree(tmp_path: Path, artifact_version: str = "2.0.0") -> tuple[Path, Path]:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()

    wheel = dist / "openadapt-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "openadapt-2.0.0.dist-info/METADATA", _metadata(artifact_version)
        )

    sdist = dist / "openadapt-2.0.0.tar.gz"
    raw = _metadata(artifact_version)
    info = tarfile.TarInfo("openadapt-2.0.0/PKG-INFO")
    info.size = len(raw)
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(raw))
    return dist, wheel


def test_release_artifacts_accept_exact_matching_pair(tmp_path: Path):
    dist, wheel = _release_tree(tmp_path)

    actual_wheel, actual_sdist = verify_release_artifacts(dist, root=tmp_path)

    assert actual_wheel == wheel
    assert actual_sdist == dist / "openadapt-2.0.0.tar.gz"


def test_release_artifacts_accept_uv_ignore_marker(tmp_path: Path):
    dist, _ = _release_tree(tmp_path)
    (dist / ".gitignore").write_text("*", encoding="utf-8")

    verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_unexpected_files(tmp_path: Path):
    dist, _ = _release_tree(tmp_path)
    (dist / "stale.whl").write_bytes(b"stale")

    with pytest.raises(ValueError, match="unexpected=\\[stale.whl\\]"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_metadata_version_drift(tmp_path: Path):
    dist, _ = _release_tree(tmp_path, artifact_version="1.9.9")

    with pytest.raises(ValueError, match="version does not match 2.0.0"):
        verify_release_artifacts(dist, root=tmp_path)
