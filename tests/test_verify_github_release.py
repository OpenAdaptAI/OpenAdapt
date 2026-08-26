import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_github_release.py"
SPEC = importlib.util.spec_from_file_location("verify_github_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def release_candidate(tmp_path: Path) -> tuple[Path, Path, dict]:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    expected.mkdir()
    downloaded.mkdir()

    wheel = expected / "openadapt-1.16.0-py3-none-any.whl"
    sdist = expected / "openadapt-1.16.0.tar.gz"
    wheel.write_bytes(b"exact wheel bytes")
    sdist.write_bytes(b"exact sdist bytes")
    for path in (wheel, sdist):
        (downloaded / path.name).write_bytes(path.read_bytes())

    metadata = {
        "tag_name": "v1.16.0",
        "draft": False,
        "prerelease": False,
        "author": {"login": "openadapt-release[bot]"},
        "assets": [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "digest": f"sha256:{_sha256(path)}",
            }
            for path in (wheel, sdist)
        ],
    }
    return expected, downloaded, metadata


def _verify(expected: Path, downloaded: Path, metadata: dict) -> None:
    MODULE.verify_release(
        metadata,
        expected_tag="v1.16.0",
        expected_author="openadapt-release[bot]",
        expected_dir=expected,
        downloaded_dir=downloaded,
    )


def test_exact_existing_release_is_idempotently_accepted(release_candidate):
    expected, downloaded, metadata = release_candidate

    _verify(expected, downloaded, metadata)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tag_name", "v1.15.0", "release tag mismatch"),
        ("draft", True, "is a draft"),
        ("prerelease", True, "is a prerelease"),
        ("author", {"login": "abrichr"}, "release author mismatch"),
    ],
)
def test_release_identity_or_state_mismatch_fails_closed(
    release_candidate, field, value, message
):
    expected, downloaded, metadata = release_candidate
    metadata[field] = value

    with pytest.raises(ValueError, match=message):
        _verify(expected, downloaded, metadata)


def test_missing_release_asset_fails_closed(release_candidate):
    expected, downloaded, metadata = release_candidate
    metadata["assets"].pop()

    with pytest.raises(ValueError, match="missing"):
        _verify(expected, downloaded, metadata)


def test_exact_partial_release_can_add_only_the_missing_asset(release_candidate):
    expected, downloaded, metadata = release_candidate
    missing_asset = metadata["assets"].pop()
    (downloaded / missing_asset["name"]).unlink()

    missing = MODULE.verify_release(
        metadata,
        expected_tag="v1.16.0",
        expected_author="openadapt-release[bot]",
        expected_dir=expected,
        downloaded_dir=downloaded,
        allow_missing=True,
    )

    assert missing == [missing_asset["name"]]

    metadata["assets"].append(missing_asset)
    source = expected / missing_asset["name"]
    (downloaded / missing_asset["name"]).write_bytes(source.read_bytes())
    _verify(expected, downloaded, metadata)


def test_unexpected_release_asset_fails_closed(release_candidate):
    expected, downloaded, metadata = release_candidate
    metadata["assets"].append(
        {"name": "unexpected.txt", "size": 1, "digest": "sha256:" + "0" * 64}
    )

    with pytest.raises(ValueError, match="unexpected"):
        _verify(expected, downloaded, metadata)


def test_release_asset_digest_mismatch_fails_closed(release_candidate):
    expected, downloaded, metadata = release_candidate
    metadata["assets"][0]["digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="digest mismatch"):
        _verify(expected, downloaded, metadata)


def test_downloaded_release_byte_mismatch_fails_closed(release_candidate):
    expected, downloaded, metadata = release_candidate
    name = metadata["assets"][0]["name"]
    path = downloaded / name
    original = (expected / name).read_bytes()
    path.write_bytes(b"X" + original[1:])

    with pytest.raises(ValueError, match="downloaded release asset bytes mismatch"):
        _verify(expected, downloaded, metadata)
