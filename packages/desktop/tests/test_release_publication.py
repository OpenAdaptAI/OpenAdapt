"""The publication steps of desktop-release.yml (scripts/release_publication.py)."""

from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest

from scripts import release_publication
from scripts.native_release import native_version, python_distribution_names
from tests.test_native_release import _stage_complete_release


def test_notes_state_each_platform_signing_before_the_candidate_notes(tmp_path: Path) -> None:
    release, _manifest, _checksums = _stage_complete_release(tmp_path)
    notes = release_publication.notes(release, version=native_version())
    signing = notes.index("## Signing in this release")
    candidate = notes.index("# Native Release Candidates")
    assert signing < candidate
    assert "Developer ID signed and notarized" in notes
    assert "The MSI and the program it installs are Authenticode-signed" in notes
    assert "Prefer the MSI" in notes
    assert "openadapt-desktop-channel.json" in notes


def test_release_state_finds_one_release_by_tag_including_drafts() -> None:
    pages = [
        [{"tag_name": "openadapt-flow-v2.2.0", "draft": False}],
        [{"tag_name": "x", "draft": True}],
    ]
    assert release_publication.release_state(pages, "openadapt-desktop-v1.0.0") == ("none", None)
    pages[1].append({"tag_name": "openadapt-desktop-v1.0.0", "draft": True})
    assert release_publication.release_state(pages, "openadapt-desktop-v1.0.0")[0] == "draft"
    pages[1][-1]["draft"] = False
    assert release_publication.release_state(pages, "openadapt-desktop-v1.0.0")[0] == "published"
    pages[0].append({"tag_name": "openadapt-desktop-v1.0.0", "draft": False})
    with pytest.raises(ValueError, match="more than one"):
        release_publication.release_state(pages, "openadapt-desktop-v1.0.0")


def test_python_dist_takes_only_the_attested_wheel_and_sdist(tmp_path: Path) -> None:
    release, _manifest, checksums = _stage_complete_release(tmp_path / "assets")
    copied = release_publication.python_dist(
        release, version=native_version(), output=tmp_path / "dist"
    )
    assert {path.name for path in copied} == python_distribution_names(native_version())

    wheel = next(
        name for name in python_distribution_names(native_version()) if name.endswith(".whl")
    )
    (release / wheel).write_bytes(b"swapped after attestation")
    with pytest.raises(ValueError, match="differs from the attested SHA256SUMS"):
        release_publication.python_dist(
            release, version=native_version(), output=tmp_path / "dist-2"
        )
    assert checksums.is_file()


def _dist(tmp_path: Path, version: str) -> tuple[Path, dict[str, str]]:
    directory = tmp_path / "pypi-dist"
    directory.mkdir()
    digests = {}
    for name in python_distribution_names(version):
        (directory / name).write_bytes(name.encode())
        digests[name] = hashlib.sha256(name.encode()).hexdigest()
    return directory, digests


def test_verify_pypi_waits_for_and_requires_the_exact_bytes(tmp_path: Path) -> None:
    directory, digests = _dist(tmp_path, "1.2.3")
    answers = [urllib.error.HTTPError("u", 404, "missing", None, None), {}, digests]

    def fetch(_version: str) -> dict[str, str]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    served = release_publication.verify_pypi(
        directory, version="1.2.3", wait_seconds=60, fetch=fetch, sleep=lambda _s: None
    )
    assert served == digests


def test_verify_pypi_refuses_different_or_extra_files(tmp_path: Path) -> None:
    directory, digests = _dist(tmp_path, "1.2.3")
    other = {name: "0" * 64 for name in digests}
    with pytest.raises(ValueError, match="different bytes"):
        release_publication.verify_pypi(
            directory, version="1.2.3", wait_seconds=0, fetch=lambda _v: other
        )
    extra = {**digests, "openadapt_desktop-1.2.3-cp312-win_amd64.whl": "1" * 64}
    with pytest.raises(ValueError, match="unexpected files"):
        release_publication.verify_pypi(
            directory, version="1.2.3", wait_seconds=0, fetch=lambda _v: extra
        )
    with pytest.raises(ValueError, match="in time"):
        release_publication.verify_pypi(
            directory, version="1.2.3", wait_seconds=0, fetch=lambda _v: {}
        )
    assert json.dumps(digests)
