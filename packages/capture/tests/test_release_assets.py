"""The GitHub Release body and assets come from CHANGELOG.md and PyPI exactly."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_assets  # noqa: E402
from verify_release_publication import PublicationError  # noqa: E402

WHEEL = "openadapt_capture-1.3.0-py3-none-any.whl"
SDIST = "openadapt_capture-1.3.0.tar.gz"
CONTENT = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
CHANGELOG = """# CHANGELOG

<!-- version list -->

## v1.3.0 (2026-10-17)

### Features

- Shadow mode.

## v1.2.2 (2026-07-28)

### Bug Fixes

- Older fix.
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dist(path: Path, files: dict[str, bytes] = CONTENT) -> Path:
    path.mkdir()
    for name, data in files.items():
        (path / name).write_bytes(data)
    (path / ".gitignore").write_text("*\n", encoding="utf-8")
    return path


def _pypi(
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes],
    *,
    host: str = "files.pythonhosted.org",
) -> None:
    document = {
        "urls": [
            {
                "filename": name,
                "url": f"https://{host}/packages/{name}",
                "digests": {"sha256": _sha(data)},
            }
            for name, data in files.items()
        ]
    }

    def request(url: str, *, token: str | None = None, accept: str = "") -> bytes | None:
        if url.startswith("https://pypi.org/pypi/openadapt-capture/1.3.0/json"):
            return json.dumps(document).encode()
        name = url.rsplit("/", 1)[-1]
        return files.get(name)

    monkeypatch.setattr(release_assets, "_request", request)


def test_notes_are_exactly_one_versions_changelog_section() -> None:
    notes = release_assets.release_notes(CHANGELOG, "1.3.0")

    assert notes == "## v1.3.0 (2026-10-17)\n\n### Features\n\n- Shadow mode.\n"
    assert "Older fix" not in notes
    assert release_assets.release_notes(CHANGELOG, "1.2.2").endswith("- Older fix.\n")


@pytest.mark.parametrize("version", ["9.9.9", "1.3", "v1.3.0"])
def test_notes_refuse_a_missing_or_inexact_version(version: str) -> None:
    with pytest.raises(PublicationError):
        release_assets.release_notes(CHANGELOG, version)


def test_notes_refuse_an_empty_section() -> None:
    with pytest.raises(PublicationError, match="empty"):
        release_assets.release_notes(
            "## v1.3.0 (2026-10-17)\n\n## v1.2.2 (2026-07-28)\n- x\n", "1.3.0"
        )


def test_the_real_changelog_has_notes_for_its_latest_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = changelog.split("\n## v", 1)[1].split(" ", 1)[0]

    assert release_assets.release_notes(changelog, version).startswith(f"## v{version} (")


def test_check_pypi_accepts_dist_that_is_exactly_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT)

    digests = release_assets.check_pypi(_dist(tmp_path / "dist"), "openadapt-capture", "1.3.0")

    assert digests == {name: _sha(data) for name, data in CONTENT.items()}


def test_check_pypi_rejects_an_attestation_left_in_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT)
    dist = _dist(tmp_path / "dist")
    (dist / f"{WHEEL}.publish.attestation").write_text("{}", encoding="utf-8")

    with pytest.raises(PublicationError, match="not release artifacts"):
        release_assets.check_pypi(dist, "openadapt-capture", "1.3.0")


def test_check_pypi_rejects_different_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, {WHEEL: b"other wheel", SDIST: CONTENT[SDIST]})

    with pytest.raises(PublicationError, match="differs from PyPI"):
        release_assets.check_pypi(_dist(tmp_path / "dist"), "openadapt-capture", "1.3.0")


def test_fetch_pypi_downloads_bytes_equal_to_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT)
    rebuilt = _dist(tmp_path / "rebuilt")
    dist = tmp_path / "dist"

    release_assets.fetch_pypi(dist, rebuilt, "openadapt-capture", "1.3.0")

    assert sorted(path.name for path in dist.iterdir()) == sorted(CONTENT)
    assert all((dist / name).read_bytes() == data for name, data in CONTENT.items())


def test_fetch_pypi_refuses_a_rebuild_that_differs_from_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT)
    rebuilt = _dist(tmp_path / "rebuilt", {WHEEL: b"rebuilt differently", SDIST: CONTENT[SDIST]})

    with pytest.raises(PublicationError, match="differ from the tagged rebuild"):
        release_assets.fetch_pypi(tmp_path / "dist", rebuilt, "openadapt-capture", "1.3.0")
    assert not (tmp_path / "dist").exists()


def test_fetch_pypi_refuses_a_download_that_does_not_match_pypis_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT)
    rebuilt = _dist(tmp_path / "rebuilt")
    original = release_assets._request

    def tampered(url: str, **kwargs: object) -> bytes | None:
        data = original(url, **kwargs)
        return b"tampered" if url.endswith(WHEEL) else data

    monkeypatch.setattr(release_assets, "_request", tampered)

    with pytest.raises(PublicationError, match="does not match PyPI's digest"):
        release_assets.fetch_pypi(tmp_path / "dist", rebuilt, "openadapt-capture", "1.3.0")


def test_fetch_pypi_refuses_files_served_from_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pypi(monkeypatch, CONTENT, host="example.com")

    with pytest.raises(PublicationError, match="files.pythonhosted.org"):
        release_assets.fetch_pypi(
            tmp_path / "dist", _dist(tmp_path / "rebuilt"), "openadapt-capture", "1.3.0"
        )


def test_fetch_pypi_refuses_a_version_missing_from_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_assets, "_request", lambda url, **kwargs: None)

    with pytest.raises(PublicationError, match="not on PyPI"):
        release_assets.fetch_pypi(
            tmp_path / "dist", _dist(tmp_path / "rebuilt"), "openadapt-capture", "1.3.0"
        )
