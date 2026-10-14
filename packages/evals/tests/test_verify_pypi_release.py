"""Tests for exact immutable PyPI release verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_pypi_release import (
    MAX_METADATA_BYTES,
    PYPI_PROJECT,
    PublicationAbsent,
    PublicationPending,
    ReleaseVerificationError,
    verify_pypi_release,
)

VERSION = "9.8.7"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
    directory = tmp_path / "dist"
    directory.mkdir()
    local = {
        f"openadapt_evals-{VERSION}-py3-none-any.whl": b"reviewed wheel bytes",
        f"openadapt_evals-{VERSION}.tar.gz": b"reviewed sdist bytes",
    }
    urls = []
    bodies: dict[str, bytes] = {}
    for filename, body in local.items():
        path = directory / filename
        path.write_bytes(body)
        url = f"https://files.pythonhosted.org/packages/test/{filename}"
        bodies[url] = body
        urls.append(
            {
                "filename": filename,
                "url": url,
                "size": len(body),
                "digests": {"sha256": hashlib.sha256(body).hexdigest()},
                "packagetype": "bdist_wheel" if filename.endswith(".whl") else "sdist",
                "yanked": False,
            }
        )
    metadata_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{VERSION}/json"
    bodies[metadata_url] = json.dumps(
        {
            "info": {"name": "openadapt-evals", "version": VERSION},
            "urls": urls,
        }
    ).encode()
    return directory, local, bodies


def _fetch(bodies: dict[str, bytes]):
    def fetch(url: str, limit: int) -> bytes:
        body = bodies[url]
        assert len(body) <= limit
        return body

    return fetch


def _metadata(bodies: dict[str, bytes]) -> tuple[str, dict]:
    metadata_url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{VERSION}/json"
    return metadata_url, json.loads(bodies[metadata_url])


def test_exact_public_files_match_names_metadata_hashes_and_bytes(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    calls: list[tuple[str, int]] = []

    def fetch(url: str, limit: int) -> bytes:
        calls.append((url, limit))
        return bodies[url]

    verify_pypi_release(directory, VERSION, fetch=fetch)

    assert calls[0] == (
        f"https://pypi.org/pypi/{PYPI_PROJECT}/{VERSION}/json",
        MAX_METADATA_BYTES,
    )
    assert {url for url, _ in calls[1:]} == {
        url for url in bodies if url.startswith("https://files.pythonhosted.org/")
    }


def test_missing_public_file_is_pending_for_bounded_recovery(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    metadata["urls"].pop()
    bodies[metadata_url] = json.dumps(metadata).encode()

    with pytest.raises(PublicationPending, match="not visible yet"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_matching_public_subset_is_accepted_before_publication(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    missing = metadata["urls"].pop()
    bodies.pop(missing["url"])
    bodies[metadata_url] = json.dumps(metadata).encode()

    verify_pypi_release(
        directory,
        VERSION,
        fetch=_fetch(bodies),
        allow_matching_subset=True,
    )


def test_absent_release_is_accepted_only_before_publication(tmp_path: Path) -> None:
    directory, _, _ = _fixture(tmp_path)

    def absent(_url: str, _limit: int) -> bytes:
        raise PublicationAbsent("not published")

    verify_pypi_release(
        directory,
        VERSION,
        fetch=absent,
        allow_matching_subset=True,
    )
    with pytest.raises(PublicationAbsent):
        verify_pypi_release(directory, VERSION, fetch=absent)


def test_empty_release_inventory_is_accepted_before_publication(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    metadata["urls"] = []
    bodies[metadata_url] = json.dumps(metadata).encode()

    verify_pypi_release(
        directory,
        VERSION,
        fetch=_fetch(bodies),
        allow_matching_subset=True,
    )


def test_extra_immutable_public_file_is_refused(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    metadata["urls"].append(dict(metadata["urls"][0], filename="openadapt_evals-9.8.7-extra.whl"))
    bodies[metadata_url] = json.dumps(metadata).encode()

    for allow_matching_subset in (False, True):
        with pytest.raises(ReleaseVerificationError, match="unexpected immutable"):
            verify_pypi_release(
                directory,
                VERSION,
                fetch=_fetch(bodies),
                allow_matching_subset=allow_matching_subset,
            )


@pytest.mark.parametrize("field", ["digest", "size", "type", "yanked", "bytes"])
def test_changed_existing_subset_is_refused_before_publication(tmp_path: Path, field: str) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    missing = metadata["urls"].pop()
    bodies.pop(missing["url"])
    entry = metadata["urls"][0]
    if field == "digest":
        entry["digests"]["sha256"] = "0" * 64
    elif field == "size":
        entry["size"] += 1
    elif field == "type":
        entry["packagetype"] = "sdist"
    elif field == "yanked":
        entry["yanked"] = True
    else:
        bodies[entry["url"]] = b"X" * entry["size"]
    bodies[metadata_url] = json.dumps(metadata).encode()

    with pytest.raises(ReleaseVerificationError):
        verify_pypi_release(
            directory,
            VERSION,
            fetch=_fetch(bodies),
            allow_matching_subset=True,
        )


@pytest.mark.parametrize("field", ["digest", "size", "type", "yanked"])
def test_changed_public_metadata_is_refused(tmp_path: Path, field: str) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    entry = metadata["urls"][0]
    if field == "digest":
        entry["digests"]["sha256"] = "0" * 64
    elif field == "size":
        entry["size"] += 1
    elif field == "type":
        entry["packagetype"] = "sdist"
    else:
        entry["yanked"] = True
    bodies[metadata_url] = json.dumps(metadata).encode()

    with pytest.raises(ReleaseVerificationError, match="metadata"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_changed_public_bytes_are_refused_even_when_metadata_matches(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    artifact_url = next(url for url in bodies if url.startswith("https://files.pythonhosted.org/"))
    original = bodies[artifact_url]
    bodies[artifact_url] = b"X" * len(original)

    with pytest.raises(ReleaseVerificationError, match="bytes differ"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


@pytest.mark.parametrize("mutation", ["project", "version", "host", "duplicate"])
def test_wrong_release_identity_or_file_source_is_refused(tmp_path: Path, mutation: str) -> None:
    directory, _, bodies = _fixture(tmp_path)
    metadata_url, metadata = _metadata(bodies)
    if mutation == "project":
        metadata["info"]["name"] = "other-project"
    elif mutation == "version":
        metadata["info"]["version"] = "9.8.8"
    elif mutation == "host":
        metadata["urls"][0]["url"] = "https://example.com/package.whl"
    else:
        metadata["urls"].append(dict(metadata["urls"][0]))
    bodies[metadata_url] = json.dumps(metadata).encode()

    with pytest.raises(ReleaseVerificationError):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_local_release_requires_only_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    (directory / "notes.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ReleaseVerificationError, match="unexpected local"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_local_distribution_names_must_identify_the_exact_version(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    wheel = next(directory.glob("*.whl"))
    wheel.rename(directory / wheel.name.replace(VERSION, "9.8.8"))

    with pytest.raises(ReleaseVerificationError, match="wheel does not identify"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_build_tool_ignore_marker_in_dist_is_not_a_distribution(tmp_path: Path) -> None:
    # uv build writes dist/.gitignore containing "*" when it creates dist/.
    directory, _, bodies = _fixture(tmp_path)
    (directory / ".gitignore").write_bytes(b"*\n")
    verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_any_other_hidden_file_in_dist_is_still_refused(tmp_path: Path) -> None:
    directory, _, bodies = _fixture(tmp_path)
    (directory / ".gitignore").write_bytes(b"*.whl\n")
    with pytest.raises(ReleaseVerificationError, match="unexpected local distribution"):
        verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))


def test_publish_attestation_sidecars_are_not_distributions(tmp_path: Path) -> None:
    # pypa/gh-action-pypi-publish writes <file>.publish.attestation beside
    # each uploaded file before the post-publication verification runs.
    directory, _, bodies = _fixture(tmp_path)
    for name in list(bodies):
        filename = name.rsplit("/", 1)[-1]
        if filename.endswith((".whl", ".tar.gz")):
            (directory / f"{filename}.publish.attestation").write_bytes(b"{}")
    verify_pypi_release(directory, VERSION, fetch=_fetch(bodies))
