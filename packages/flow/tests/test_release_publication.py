import hashlib
import json
from pathlib import Path

import pytest

import scripts.verify_release_publication as publication
from scripts.verify_release_publication import PublicationError, artifact_digests


def test_artifact_digests_requires_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "openadapt_flow-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "openadapt_flow-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    assert artifact_digests(tmp_path) == {
        wheel.name: hashlib.sha256(b"wheel").hexdigest(),
        sdist.name: hashlib.sha256(b"sdist").hexdigest(),
    }


def test_artifact_digests_rejects_an_extra_publishable_file(tmp_path: Path) -> None:
    (tmp_path / "openadapt_flow-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "openadapt_flow-1.0.0.tar.gz").write_bytes(b"sdist")
    (tmp_path / "openadapt_flow-1.0.0-extra.whl").write_bytes(b"extra")

    with pytest.raises(PublicationError, match="exactly one wheel and one sdist"):
        artifact_digests(tmp_path)


def test_pypi_verification_requires_the_exact_artifact_digest(monkeypatch) -> None:
    expected = {"package.whl": "a" * 64}
    payload = {"urls": [{"filename": "package.whl", "digests": {"sha256": "a" * 64}}]}
    monkeypatch.setattr(
        publication,
        "_request",
        lambda *_args, **_kwargs: json.dumps(payload).encode(),
    )
    assert publication.verify_pypi("package", "1.0.0", expected)

    payload["urls"][0]["digests"]["sha256"] = "b" * 64
    with pytest.raises(PublicationError, match="PyPI artifacts differ"):
        publication.verify_pypi("package", "1.0.0", expected)


def test_github_verification_requires_a_final_exact_release(monkeypatch) -> None:
    expected = {"package.whl": "a" * 64}
    payload = {
        "tag_name": "v1.0.0",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": "package.whl", "digest": f"sha256:{'a' * 64}"}],
    }
    monkeypatch.setattr(
        publication,
        "_request",
        lambda *_args, **_kwargs: json.dumps(payload).encode(),
    )
    assert publication.verify_github_release(
        "OpenAdaptAI/package",
        "v1.0.0",
        expected,
        token="app-token",
    )

    payload["draft"] = True
    with pytest.raises(PublicationError, match="draft or prerelease"):
        publication.verify_github_release(
            "OpenAdaptAI/package",
            "v1.0.0",
            expected,
            token="app-token",
        )
