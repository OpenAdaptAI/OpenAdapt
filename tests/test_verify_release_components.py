from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "verify_release_components", ROOT / "scripts" / "verify_release_components.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
from release_platform_versions import COMPONENT_PACKAGES  # noqa: E402

VERSIONS = {"flow": "2.0.0", "agent": "2.1.0"}


def _files(role: str) -> dict[str, bytes]:
    dist = MODULE.COMPONENTS[role][1]
    v = VERSIONS[role]
    return {
        f"{dist}-{v}-py3-none-any.whl": f"{role}-wheel".encode(),
        f"{dist}-{v}.tar.gz": f"{role}-sdist".encode(),
    }


def _setup(tmp_path: Path):
    components = {
        role: {"package": package, "version": f"1.0.{i}"}
        for i, (role, package) in enumerate(COMPONENT_PACKAGES.items(), start=1)
    }
    components["launcher"]["version"] = "1.16.0"
    blobs: dict[str, bytes] = {}
    pypi: dict[str, dict] = {}
    for role, (package, _dist, repository) in MODULE.COMPONENTS.items():
        version = VERSIONS[role]
        artifacts = []
        urls = []
        for name, raw in _files(role).items():
            url = f"https://files.pythonhosted.org/packages/aa/{name}"
            digest = hashlib.sha256(raw).hexdigest()
            blobs[url] = raw
            artifacts.append(
                {
                    "type": "sdist" if name.endswith(".tar.gz") else "bdist_wheel",
                    "filename": name,
                    "url": url,
                    "sha256": digest,
                }
            )
            urls.append(
                {"filename": name, "digests": {"sha256": digest}, "yanked": False}
            )
        components[role] = {
            "package": package,
            "version": version,
            "source": "pypi",
            "provenance": {
                "repository": repository,
                "release_ref": f"v{version}",
                "commit": "e" * 40,
            },
            "artifacts": artifacts,
        }
        pypi[f"https://pypi.org/pypi/{package}/{version}/json"] = {
            "info": {"version": version},
            "urls": urls,
        }
    document = {
        "manifest_kind": "openadapt-platform-release-manifest",
        "schema_version": "2.0.0",
        "components": components,
        "release_selection": {
            "mode": "explicit-published",
            "component_versions": {r: c["version"] for r, c in components.items()},
        },
    }
    (tmp_path / "platform-manifest.json").write_text(json.dumps(document))
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt"\nversion = "1.17.0"\n'
    )

    def fetch(url: str) -> bytes:
        if url in pypi:
            return json.dumps(pypi[url]).encode()
        return blobs[url]

    return document, pypi, blobs, fetch


def _run(tmp_path: Path, fetch) -> int:
    return MODULE.main(["--root", str(tmp_path)], fetch=fetch)


def test_exact_pypi_match_passes(tmp_path: Path, capsys) -> None:
    _, _, _, fetch = _setup(tmp_path)
    assert _run(tmp_path, fetch) == 0
    out = capsys.readouterr().out
    assert "openadapt-flow==2.0.0" in out and "openadapt-agent==2.1.0" in out


@pytest.mark.parametrize("role", ["flow", "agent"])
def test_pypi_digest_mismatch_fails(tmp_path: Path, role: str, capsys) -> None:
    document, pypi, _, fetch = _setup(tmp_path)
    package = MODULE.COMPONENTS[role][0]
    record = pypi[f"https://pypi.org/pypi/{package}/{VERSIONS[role]}/json"]
    record["urls"][0]["digests"]["sha256"] = "f" * 64
    assert _run(tmp_path, fetch) == 1
    assert "PyPI digest differs" in capsys.readouterr().err


def test_downloaded_bytes_mismatch_fails(tmp_path: Path, capsys) -> None:
    _, _, blobs, fetch = _setup(tmp_path)
    for url in blobs:
        blobs[url] = b"tampered"
    assert _run(tmp_path, fetch) == 1
    assert "downloaded bytes differ" in capsys.readouterr().err


def test_missing_or_yanked_pypi_file_fails(tmp_path: Path, capsys) -> None:
    _, pypi, _, fetch = _setup(tmp_path)
    record = pypi["https://pypi.org/pypi/openadapt-agent/2.1.0/json"]
    record["urls"][0]["yanked"] = True
    assert _run(tmp_path, fetch) == 1
    assert "yanked" in capsys.readouterr().err
    record["urls"] = []
    assert _run(tmp_path, fetch) == 1


def test_unpublished_version_fails(tmp_path: Path) -> None:
    _, pypi, _, fetch = _setup(tmp_path)
    pypi["https://pypi.org/pypi/openadapt-flow/2.0.0/json"]["info"]["version"] = "1.0.0"
    assert _run(tmp_path, fetch) == 1


def test_artifact_outside_pypi_files_fails(tmp_path: Path, capsys) -> None:
    document, _, _, fetch = _setup(tmp_path)
    document["components"]["flow"]["artifacts"][0]["url"] = (
        "https://example.com/openadapt_flow-2.0.0-py3-none-any.whl"
    )
    (tmp_path / "platform-manifest.json").write_text(json.dumps(document))
    assert _run(tmp_path, fetch) == 1
    assert "outside the exact PyPI file boundary" in capsys.readouterr().err


def test_dynamic_selection_fails(tmp_path: Path) -> None:
    document, _, _, fetch = _setup(tmp_path)
    document["release_selection"] = {
        "mode": "latest-published",
        "component_versions": {},
    }
    (tmp_path / "platform-manifest.json").write_text(json.dumps(document))
    assert _run(tmp_path, fetch) == 1


def test_network_error_fails_closed(tmp_path: Path) -> None:
    _setup(tmp_path)

    def broken(_url: str) -> bytes:
        raise OSError("offline")

    assert _run(tmp_path, broken) == 1


def test_monorepo_release_provenance_passes(tmp_path: Path) -> None:
    document, _, _, fetch = _setup(tmp_path)
    for role, (package, _dist, _repository) in MODULE.COMPONENTS.items():
        version = document["components"][role]["version"]
        document["components"][role]["provenance"] = {
            "repository": "OpenAdaptAI/OpenAdapt",
            "release_ref": f"{package}-v{version}",
            "commit": "e" * 40,
            "source_root": f"packages/{role}",
        }
    (tmp_path / "platform-manifest.json").write_text(json.dumps(document))
    assert _run(tmp_path, fetch) == 0


@pytest.mark.parametrize(
    "provenance",
    [
        # The monorepo tag in the standalone repository.
        {
            "repository": "OpenAdaptAI/openadapt-flow",
            "release_ref": "openadapt-flow-v2.0.0",
        },
        # A monorepo release without its package directory.
        {"repository": "OpenAdaptAI/OpenAdapt", "release_ref": "openadapt-flow-v2.0.0"},
        # Another package's directory.
        {
            "repository": "OpenAdaptAI/OpenAdapt",
            "release_ref": "openadapt-flow-v2.0.0",
            "source_root": "packages/agent",
        },
    ],
)
def test_mismatched_monorepo_provenance_fails(
    tmp_path: Path, provenance: dict, capsys
) -> None:
    document, _, _, fetch = _setup(tmp_path)
    document["components"]["flow"]["provenance"] = {**provenance, "commit": "e" * 40}
    (tmp_path / "platform-manifest.json").write_text(json.dumps(document))
    assert _run(tmp_path, fetch) == 1
    assert "flow provenance does not bind" in capsys.readouterr().err
