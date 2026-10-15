from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "flow_admission.py"
sys.path.insert(0, str(ROOT / "scripts"))
from release_platform_versions import COMPONENT_PACKAGES  # noqa: E402

SPEC = importlib.util.spec_from_file_location("flow_admission", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _manifest(tmp_path: Path, payloads: dict[str, bytes]) -> Path:
    components = {
        role: {"package": package, "version": f"1.0.{index}"}
        for index, (role, package) in enumerate(
            COMPONENT_PACKAGES.items(),
            start=1,
        )
    }
    components["launcher"]["version"] = "2.0.0"
    components["flow"] = {
        "package": "openadapt-flow",
        "version": "1.35.0",
        "source": "pypi",
        "provenance": {
            "repository": "OpenAdaptAI/openadapt-flow",
            "release_ref": "v1.35.0",
            "commit": "e" * 40,
        },
        "artifacts": [
            {
                "type": "bdist_wheel",
                "filename": name,
                "url": f"https://files.pythonhosted.org/packages/aa/{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in payloads.items()
            if name.endswith(".whl")
        ]
        + [
            {
                "type": "sdist",
                "filename": name,
                "url": f"https://files.pythonhosted.org/packages/bb/{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in payloads.items()
            if name.endswith(".tar.gz")
        ],
    }
    document = {
        "manifest_kind": "openadapt-platform-release-manifest",
        "schema_version": "2.0.0",
        "components": components,
        "release_selection": {
            "mode": "explicit-published",
            "component_versions": {
                role: component["version"] for role, component in components.items()
            },
        },
    }
    path = tmp_path / "platform-manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _payloads() -> dict[str, bytes]:
    return {
        "openadapt_flow-1.35.0-py3-none-any.whl": b"wheel-bytes",
        "openadapt_flow-1.35.0.tar.gz": b"sdist-bytes",
    }


def test_stages_exact_flow_bytes_and_closed_inventory(tmp_path: Path) -> None:
    payloads = _payloads()
    candidate = MODULE.load_flow_candidate(_manifest(tmp_path, payloads), "2.0.0")
    by_url = {artifact.url: payloads[artifact.name] for artifact in candidate.artifacts}
    root = tmp_path / "candidate"

    inventory = MODULE.stage_artifacts(
        candidate, root, fetch_bytes=lambda url: by_url[url]
    )

    assert candidate.version == "1.35.0"
    assert candidate.tag == "v1.35.0"
    assert candidate.source_commit == "e" * 40
    assert sorted(path.name for path in root.iterdir()) == sorted(payloads)
    assert inventory["target"] == "flow"
    assert inventory["claim_scope"] == "production_flow"
    assert [item["kind"] for item in inventory["artifacts"]] == [
        "python-sdist",
        "python-wheel",
    ]
    assert all(
        item["publish_destinations"] == ["github-release", "pypi"]
        for item in inventory["artifacts"]
    )


def test_refuses_dynamic_platform_selection(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _payloads())
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["release_selection"] = {
        "mode": "latest-published",
        "component_versions": {},
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="explicit-published"):
        MODULE.load_flow_candidate(manifest, "2.0.0")


def test_refuses_a_flow_artifact_outside_files_pythonhosted(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _payloads())
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["components"]["flow"]["artifacts"][0]["url"] = (
        "https://example.com/openadapt_flow-1.35.0-py3-none-any.whl"
    )
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="outside the exact PyPI file boundary"):
        MODULE.load_flow_candidate(manifest, "2.0.0")


def test_refuses_downloaded_bytes_that_differ_from_the_manifest(tmp_path: Path) -> None:
    candidate = MODULE.load_flow_candidate(_manifest(tmp_path, _payloads()), "2.0.0")

    with pytest.raises(ValueError, match="bytes differ"):
        MODULE.stage_artifacts(
            candidate,
            tmp_path / "candidate",
            fetch_bytes=lambda _url: b"different",
        )


def _signed_root(tmp_path: Path):
    """A launcher root with a manifest, a fresh test key, and a signed admission."""

    root = tmp_path / "launcher"
    root.mkdir(parents=True)
    manifest = _manifest(root, _payloads())
    candidate = MODULE.load_flow_candidate(manifest, "2.0.0")
    digests = {item.name: item.sha256 for item in candidate.artifacts}
    key = tmp_path / "test-admission-key.pem"
    MODULE.sign_admission(root, candidate, key, pypi_digests=lambda _v: digests)
    return root, candidate, key


def test_sign_then_verify_round_trip(tmp_path: Path) -> None:
    root, candidate, key = _signed_root(tmp_path)

    assert oct(key.stat().st_mode & 0o777) == "0o600"
    signer = MODULE.verify_admission(root, candidate)
    keys = json.loads((root / "release-admission/trusted-keys.json").read_text())
    assert [entry["key_id"] for entry in keys["keys"]] == [signer]
    assert "PRIVATE" not in (root / "release-admission/trusted-keys.json").read_text()


def test_resigning_with_the_same_key_does_not_duplicate_it(tmp_path: Path) -> None:
    root, candidate, key = _signed_root(tmp_path)
    digests = {item.name: item.sha256 for item in candidate.artifacts}
    MODULE.sign_admission(root, candidate, key, pypi_digests=lambda _v: digests)
    keys = json.loads((root / "release-admission/trusted-keys.json").read_text())
    assert len(keys["keys"]) == 1


def test_refuses_a_missing_admission(tmp_path: Path) -> None:
    root, candidate, _ = _signed_root(tmp_path)
    (root / "release-admission/flow-1.35.0.json").unlink()
    with pytest.raises(ValueError, match="no signed admission"):
        MODULE.verify_admission(root, candidate)


def test_refuses_when_no_trusted_key_is_committed(tmp_path: Path) -> None:
    root, candidate, _ = _signed_root(tmp_path)
    (root / "release-admission/trusted-keys.json").write_text(
        json.dumps({"schema_version": MODULE.TRUSTED_KEYS_SCHEMA, "keys": []})
    )
    with pytest.raises(ValueError, match="no trusted admission key"):
        MODULE.verify_admission(root, candidate)


def test_refuses_an_admission_for_different_bytes(tmp_path: Path) -> None:
    root, _, _ = _signed_root(tmp_path)
    manifest = root / "platform-manifest.json"
    document = json.loads(manifest.read_text())
    document["components"]["flow"]["artifacts"][0]["sha256"] = "f" * 64
    manifest.write_text(json.dumps(document))
    other = MODULE.load_flow_candidate(manifest, "2.0.0")
    with pytest.raises(ValueError, match="does not match the exact Flow release"):
        MODULE.verify_admission(root, other)


def test_refuses_a_tampered_signature(tmp_path: Path) -> None:
    root, candidate, _ = _signed_root(tmp_path)
    path = root / "release-admission/flow-1.35.0.json"
    document = json.loads(path.read_text())
    raw = bytearray(base64.b64decode(document["signature"]["value"]))
    raw[0] ^= 1
    document["signature"]["value"] = base64.b64encode(bytes(raw)).decode()
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="does not verify"):
        MODULE.verify_admission(root, candidate)


def test_refuses_a_signature_from_an_untrusted_key(tmp_path: Path) -> None:
    root, candidate, _ = _signed_root(tmp_path)
    other_root, _, _ = _signed_root(tmp_path / "other")
    # Same payload, signed by a different key that this root does not trust.
    (root / "release-admission/flow-1.35.0.json").write_text(
        (other_root / "release-admission/flow-1.35.0.json").read_text()
    )
    with pytest.raises(ValueError, match="not trusted"):
        MODULE.verify_admission(root, candidate)


def test_sign_refuses_when_pypi_digests_differ(tmp_path: Path) -> None:
    root = tmp_path / "launcher"
    root.mkdir()
    candidate = MODULE.load_flow_candidate(_manifest(root, _payloads()), "2.0.0")
    with pytest.raises(ValueError, match="PyPI digest differs"):
        MODULE.sign_admission(
            root, candidate, tmp_path / "k.pem", pypi_digests=lambda _v: {}
        )
    assert not (tmp_path / "k.pem").exists()


def test_verify_cli_fails_closed_without_an_admission(tmp_path: Path, capsys) -> None:
    root = tmp_path / "launcher"
    root.mkdir()
    _manifest(root, _payloads())
    (root / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    code = MODULE.main(
        ["verify", "--root", str(root), "--artifact-root", str(tmp_path / "a")]
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().err
