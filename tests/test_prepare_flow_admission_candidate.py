from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_flow_admission_candidate.py"
sys.path.insert(0, str(ROOT / "scripts"))
from release_platform_versions import COMPONENT_PACKAGES  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "prepare_flow_admission_candidate", SCRIPT
)
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


def _registry(entry: dict | None = None) -> dict:
    return {
        "$schema": "schemas/evidence-registry.schema.json",
        "schema_version": "openadapt.production-evidence-registry/v2",
        "repository": "OpenAdaptAI/.github",
        "repository_id": "858454062",
        "repository_owner_id": "132681217",
        "revision": 4,
        "previous_registry_head_sha256": "sha256:" + "a" * 64,
        "registry_head_sha256": "sha256:" + "b" * 64,
        "signer_registry": {},
        "signer_registry_history": [],
        "entries": [] if entry is None else [entry],
    }


def _admission_entry() -> dict:
    return {
        "registry_entry_sha256": "sha256:" + "1" * 64,
        "kind": "qualification-release",
        "object_schema_version": "openadapt.qualification-release/v2",
        "object_media_type": "application/vnd.openadapt.qualification-release+json;version=2",
        "object_path": "production-evidence/objects/sha256/22/"
        + "2" * 64
        + ".qualification-release.json",
        "object_sha256": "sha256:" + "2" * 64,
        "semantic_identity_sha256": "sha256:" + "3" * 64,
        "size_bytes": 1200,
        "subject_sha256": None,
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


def test_selects_the_last_registered_release_admission() -> None:
    older = _admission_entry()
    older["object_sha256"] = "sha256:" + "4" * 64
    latest = _admission_entry()
    registry = _registry()
    registry["entries"] = [older, latest]

    reference = MODULE.select_release_admission_reference(registry, "c" * 40)

    assert reference["schema_version"] == (
        "openadapt.production-evidence-object-reference/v2"
    )
    assert reference["registry_source_commit"] == "c" * 40
    assert reference["registry_revision"] == 4
    assert reference["object_sha256"] == latest["object_sha256"]


def test_refuses_a_registry_without_a_release_admission() -> None:
    with pytest.raises(ValueError, match="no registered Flow release admission"):
        MODULE.select_release_admission_reference(_registry(), "c" * 40)


def test_refuses_extra_release_admission_entry_fields() -> None:
    entry = _admission_entry()
    entry["registry_source_commit"] = "d" * 40

    with pytest.raises(ValueError, match="entry fields differ"):
        MODULE.select_release_admission_reference(_registry(entry), "c" * 40)


def test_github_outputs_are_single_line_canonical_json(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    MODULE._write_github_outputs(
        output,
        {
            "document": json.dumps(
                {"b": 2, "a": 1}, sort_keys=True, separators=(",", ":")
            )
        },
    )

    assert output.read_text(encoding="utf-8") == 'document={"a":1,"b":2}\n'


def test_refuses_multiline_github_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="line break"):
        MODULE._write_github_outputs(tmp_path / "output", {"bad": "one\ntwo"})
