"""The platform manifest binds components released from the monorepo.

Since the fold, Flow, Types, Agent and Desktop publish from OpenAdaptAI/OpenAdapt
under ``packages/<dir>`` and tag each release ``<package>-v<version>``
(``openadapt-flow-v2.2.0``). Releases from before the fold stay in the frozen
standalone repositories under ``v<version>``. The generator must bind whichever
tag exists, read schema sources and evidence under the package directory, and
the validator must accept both forms and nothing else.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_platform_manifest as gen  # noqa: E402
from generate_platform_manifest import (  # noqa: E402
    MONOREPO_REPOSITORY,
    SCHEMA_PROBES,
    DriftError,
    ReleaseSource,
    _desktop_lock_url,
    _evidence_location,
    _release_sources,
    _schema_compatibility,
    _source_path,
    _source_provenance,
)
from validate_platform_manifest import Report, check_structure  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "platform-manifest.json"
COMMIT = "a" * 40
TREE = "b" * 40


@pytest.fixture()
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def _commit_doc() -> dict:
    return {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}


def _monorepo_flow(manifest: dict, version: str = "2.3.0") -> dict:
    """The committed manifest with Flow rebound to a monorepo release."""

    moved = deepcopy(manifest)
    flow = moved["components"]["flow"]
    flow["version"] = version
    flow["provenance"] = {
        "repository": MONOREPO_REPOSITORY,
        "release_ref": f"openadapt-flow-v{version}",
        "commit": COMMIT,
        "tree": TREE,
        "url": (
            f"https://github.com/{MONOREPO_REPOSITORY}/tree/"
            f"openadapt-flow-v{version}/packages/flow"
        ),
        "source_root": "packages/flow",
    }
    for evidence in moved["qualification_evidence"]:
        if evidence.get("source_component") != "flow":
            continue
        package_path = evidence["path"].removeprefix("packages/flow/")
        evidence["repository"] = MONOREPO_REPOSITORY
        evidence["path"] = f"packages/flow/{package_path}"
        evidence["source_commit"] = COMMIT
        kind = "blob" if evidence["source_object_type"] == "blob" else "tree"
        evidence["url"] = (
            f"https://github.com/{MONOREPO_REPOSITORY}/{kind}/{COMMIT}/"
            f"{evidence['path']}"
        )
    for schema in moved["schema_compatibility"].values():
        if schema["source_component"] == "flow":
            schema["source_commit"] = COMMIT
    return moved


def _provenance_and_evidence_errors(report: Report) -> list[str]:
    return [
        error
        for error in report.errors
        if "provenance" in error or "qualification evidence" in error
    ]


def test_folded_components_look_in_the_monorepo_first() -> None:
    assert _release_sources("flow", "2.3.0") == (
        ReleaseSource(MONOREPO_REPOSITORY, "openadapt-flow-v2.3.0", "packages/flow"),
        ReleaseSource("OpenAdaptAI/openadapt-flow", "v2.3.0", ""),
    )
    assert _release_sources("desktop", "0.22.0") == (
        ReleaseSource(
            MONOREPO_REPOSITORY, "openadapt-desktop-v0.22.0", "packages/desktop"
        ),
        ReleaseSource("OpenAdaptAI/openadapt-desktop", "desktop-v0.22.0", ""),
        ReleaseSource("OpenAdaptAI/openadapt-desktop", "v0.22.0", ""),
    )
    # The launcher was always this repository, tagged v<version>.
    assert _release_sources("launcher", "1.18.0") == (
        ReleaseSource(MONOREPO_REPOSITORY, "v1.18.0", ""),
    )


def test_a_monorepo_release_binds_its_package_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fetch(url: str, **_kwargs):
        seen.append(url)
        return _commit_doc() if "/OpenAdapt/commits/openadapt-flow-v" in url else None

    monkeypatch.setattr(gen, "_fetch_json_optional", fetch)
    provenance = _source_provenance("flow", "2.3.0")

    assert provenance == {
        "repository": MONOREPO_REPOSITORY,
        "release_ref": "openadapt-flow-v2.3.0",
        "commit": COMMIT,
        "tree": TREE,
        "url": (
            "https://github.com/OpenAdaptAI/OpenAdapt/tree/"
            "openadapt-flow-v2.3.0/packages/flow"
        ),
        "source_root": "packages/flow",
    }
    assert len(seen) == 1


def test_a_release_from_before_the_fold_stays_in_its_standalone_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fetch(url: str, **_kwargs):
        return _commit_doc() if "/openadapt-flow/commits/v2.0.0" in url else None

    monkeypatch.setattr(gen, "_fetch_json_optional", fetch)
    provenance = _source_provenance("flow", "2.0.0")

    assert provenance["repository"] == "OpenAdaptAI/openadapt-flow"
    assert provenance["release_ref"] == "v2.0.0"
    assert (
        provenance["url"] == "https://github.com/OpenAdaptAI/openadapt-flow/tree/v2.0.0"
    )
    # Byte-identical to manifests generated before monorepo support.
    assert "source_root" not in provenance


def test_a_version_tagged_nowhere_names_every_tag_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen, "_fetch_json_optional", lambda _url, **_kwargs: None)
    with pytest.raises(DriftError) as raised:
        _source_provenance("types", "0.24.0")
    message = str(raised.value)
    assert "OpenAdaptAI/OpenAdapt@openadapt-types-v0.24.0" in message
    assert "OpenAdaptAI/openadapt-types@v0.24.0" in message


def test_paths_are_read_under_the_package_directory() -> None:
    monorepo = {"repository": MONOREPO_REPOSITORY, "source_root": "packages/flow"}
    standalone = {"repository": "OpenAdaptAI/openadapt-flow"}

    assert _source_path(monorepo, "openadapt_flow/ir.py") == (
        "packages/flow/openadapt_flow/ir.py"
    )
    assert _source_path(standalone, "openadapt_flow/ir.py") == "openadapt_flow/ir.py"
    evidence = {"path": "public-demo/evidence-packs"}
    assert _evidence_location(evidence, monorepo) == (
        MONOREPO_REPOSITORY,
        "packages/flow/public-demo/evidence-packs",
    )
    assert _evidence_location(evidence, standalone) == (
        "OpenAdaptAI/openadapt-flow",
        "public-demo/evidence-packs",
    )


def test_the_desktop_sidecar_lock_comes_from_its_release_tag() -> None:
    assert _desktop_lock_url(
        {
            "repository": MONOREPO_REPOSITORY,
            "release_ref": "openadapt-desktop-v0.22.0",
            "source_root": "packages/desktop",
        }
    ) == (
        "https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/"
        "openadapt-desktop-v0.22.0/packages/desktop/uv.lock"
    )
    assert _desktop_lock_url(
        {"repository": "OpenAdaptAI/openadapt-desktop", "release_ref": "v0.16.0"}
    ) == (
        "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-desktop/"
        "v0.16.0/uv.lock"
    )


def test_schema_sources_are_read_from_the_package_in_the_monorepo(
    manifest: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefixes = {
        str(probe["schema_prefix"])
        for probe in SCHEMA_PROBES.values()
        if "schema_prefix" in probe
    }
    source = (
        "SCHEMA_VERSION = 3\n"
        + 'data.get("schema_version", 3)\n'
        + "".join(
            f'X{index} = "{prefix}1"\n' for index, prefix in enumerate(sorted(prefixes))
        )
    )
    requested: list[tuple[str, str]] = []

    def fake_source(repository: str, commit: str, path: str, *, optional=False):
        requested.append((repository, path))
        return source, {"path": path, "git_blob": "c" * 40, "sha256": "d" * 64}

    monkeypatch.setattr(gen, "_github_source_file", fake_source)
    components = _monorepo_flow(manifest)["components"]
    schemas = _schema_compatibility(components)

    flow_reads = [path for repository, path in requested if "openadapt_flow/" in path]
    assert flow_reads
    assert all(path.startswith("packages/flow/openadapt_flow/") for path in flow_reads)
    assert {repository for repository, path in requested if path in flow_reads} == {
        MONOREPO_REPOSITORY
    }
    assert schemas["workflow_bundle"]["source_commit"] == COMMIT
    # Every other component is read where its own selected release lives.
    types = components["types"]["provenance"]
    assert (
        types["repository"],
        _source_path(types, "openadapt_types/human_decision.py"),
    ) in requested


def test_validator_accepts_a_monorepo_release(manifest: dict) -> None:
    report = Report()
    check_structure(_monorepo_flow(manifest), report)
    assert _provenance_and_evidence_errors(report) == []


def test_validator_accepts_the_releases_already_pinned(
    manifest: dict,
) -> None:
    report = Report()
    check_structure(manifest, report)
    assert _provenance_and_evidence_errors(report) == []


@pytest.mark.parametrize(
    "mutate",
    [
        # The monorepo tag with the standalone repository.
        lambda flow: flow["provenance"].update(repository="OpenAdaptAI/openadapt-flow"),
        # Another package's directory.
        lambda flow: flow["provenance"].update(source_root="packages/types"),
        # A monorepo release read as if it were the repository root.
        lambda flow: flow["provenance"].pop("source_root"),
        # A tag for another version.
        lambda flow: flow["provenance"].update(release_ref="openadapt-flow-v2.2.0"),
    ],
)
def test_validator_refuses_a_mismatched_monorepo_provenance(
    manifest: dict, mutate
) -> None:
    moved = _monorepo_flow(manifest)
    mutate(moved["components"]["flow"])
    report = Report()
    check_structure(moved, report)
    assert any("component flow provenance" in error for error in report.errors)


def test_validator_refuses_evidence_outside_the_package_directory(
    manifest: dict,
) -> None:
    moved = _monorepo_flow(manifest)
    evidence = next(
        row for row in moved["qualification_evidence"] if row.get("source_component")
    )
    evidence["path"] = evidence["path"].removeprefix("packages/flow/")
    report = Report()
    check_structure(moved, report)
    assert any(
        f"qualification evidence {evidence['id']!r} path drift" in error
        for error in report.errors
    )
