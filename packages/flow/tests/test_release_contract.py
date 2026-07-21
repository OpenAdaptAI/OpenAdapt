"""Guard the publication path against version and artifact drift."""

import hashlib
import io
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from scripts.check_release_consistency import (
    AGPL_CONTENT_SIGNATURES,
    FORBIDDEN_SDIST_PATHS,
    FORBIDDEN_SDIST_PREFIXES,
    PRIVATE_CORPUS_CONTENT_SIGNATURES,
    PRIVATE_DISTRIBUTION_EXACT_PATHS,
    PRIVATE_DISTRIBUTION_PATH_SEGMENTS,
    PRIVATE_DISTRIBUTION_PATH_TOKENS,
    PUBLIC_ARTIFACT_INVENTORY_PATH,
    PUBLIC_ARTIFACT_SUFFIXES,
    PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS,
    PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS,
    PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES,
    REPOSITORY_ONLY_EVALUATION_EXACT_PATHS,
    REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES,
    REPOSITORY_ONLY_EVALUATION_PATH_TOKENS,
    REQUIRED_SDIST_PATHS,
    WHEEL_ARTIFACT_INVENTORY_PATH,
    release_versions,
    sync_lock_version,
    validate_distribution_directory,
    validate_public_source_tree,
    validate_sdist_license_boundary,
    validate_wheel_license_boundary,
    write_public_artifact_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
MIT_LICENSE = (ROOT / "LICENSE").read_bytes()
SOURCE_BOUNDARY_EXCLUDES = {
    "/benchmark/reliability",
    "/docs/validation/IDENTITY_ROC.md",
    "/docs/validation/adversary_corpus_manifest.json",
    "/docs/validation/adversary_corpus_v2_manifest.json",
    "/docs/validation/adversary_corpus_v3_manifest.json",
    "/docs/validation/identity_roc.json",
    "/docs/validation/identity_roc.png",
    "/openadapt_flow/benchmark/reliability_corpus.py",
    "/openadapt_flow/validation/adversary_corpus.py",
    "/openadapt_flow/validation/adversary_corpus_v2.py",
    "/openadapt_flow/validation/adversary_corpus_v3.py",
    "/openadapt_flow/validation/identity_roc.py",
    "/scripts/reliability",
    "/tests/test_adversary_corpus.py",
    "/tests/test_adversary_corpus_v2.py",
    "/tests/test_adversary_corpus_v3.py",
    "/tests/test_identity_corpus_rates.py",
    "/tests/test_identity_out_of_corpus.py",
    "/tests/test_reliability.py",
}
EPHEMERAL_BUILD_EXCLUDES = {"/.hypothesis"}


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_semantic_release_stamps_lock_and_validates_artifacts() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build_command = pyproject["tool"]["semantic_release"]["build_command"]

    assert "uv==0.11.29" in build_command
    assert "check_release_consistency.py --sync" in build_command
    assert "git add uv.lock" in build_command
    assert "uv build --wheel --sdist" in build_command
    assert "check_release_consistency.py --require-dist" in build_command
    assert "uv lock" not in build_command


def test_required_wheel_job_builds_and_validates_actual_sdist() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    wheel_job = workflow[workflow.index("\n  wheel:") :]

    assert "python -m build\n" in wheel_job
    assert "python -m build --wheel" not in wheel_job
    assert "python scripts/check_release_consistency.py --require-dist" in wheel_job
    assert "import openadapt_flow.benchmark.reliability" in wheel_job
    assert "openadapt_flow.validation.adversary_corpus" in wheel_job
    assert "openadapt_flow.benchmark.reliability_corpus" in wheel_job


def test_wheel_and_sdist_exclude_repository_only_evidence() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    targets = pyproject["tool"]["hatch"]["build"]["targets"]

    assert SOURCE_BOUNDARY_EXCLUDES <= set(targets["wheel"]["exclude"])
    assert SOURCE_BOUNDARY_EXCLUDES <= set(targets["sdist"]["exclude"])
    assert EPHEMERAL_BUILD_EXCLUDES <= set(targets["wheel"]["exclude"])
    assert EPHEMERAL_BUILD_EXCLUDES <= set(targets["sdist"]["exclude"])

    # The archive guard is semantic, not an exact-file-only denylist: a future
    # corpus version or renamed reliability recipe must fail closed too.
    assert {"adversary_corpus", "identity_roc", "held_out_corpus"} <= set(
        PRIVATE_DISTRIBUTION_PATH_TOKENS
    )
    assert {"private", ".private"} <= PRIVATE_DISTRIBUTION_PATH_SEGMENTS
    assert {
        "tests/test_identity_corpus_rates.py",
        "tests/test_identity_out_of_corpus.py",
    } <= PRIVATE_DISTRIBUTION_EXACT_PATHS
    assert "reliability_corpus" in REPOSITORY_ONLY_EVALUATION_PATH_TOKENS
    assert "benchmark/reliability/" in REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES
    assert "tests/test_reliability.py" in REPOSITORY_ONLY_EVALUATION_EXACT_PATHS


def test_public_source_tree_excludes_private_data_and_recipes(tmp_path: Path) -> None:
    allowed = tmp_path / "benchmark/reliability"
    allowed.mkdir(parents=True)
    (allowed / "summary.json").write_text("{}\n")
    (allowed / "RELIABILITY.md").write_text("bounded aggregate\n")
    write_public_artifact_inventory(tmp_path)
    validate_public_source_tree(tmp_path)

    forbidden = tmp_path / "benchmark/reliability/results.json"
    forbidden.write_text("{}\n")
    with pytest.raises(ValueError, match="public artifact inventory"):
        validate_public_source_tree(tmp_path)

    assert "benchmark/reliability/results.json" in (
        PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS
    )
    assert "benchmark/reliability/summary.json" in (
        PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS
    )
    assert "benchmark/reliability/" in PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES
    assert "scripts/reliability/" in PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES

    forbidden.unlink()
    renamed_raw_rows = tmp_path / "benchmark/reliability/per-target-rows.json"
    renamed_raw_rows.write_text("{}\n")
    with pytest.raises(ValueError, match="public artifact inventory"):
        validate_public_source_tree(tmp_path)


def test_public_source_tree_inventory_fails_closed_on_neutral_renames(
    tmp_path: Path,
) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "openadapt_flow/runtime.py").write_text("RUNTIME = True\n")
    write_public_artifact_inventory(tmp_path)
    validate_public_source_tree(tmp_path)

    for relative in (
        "data/production_samples.json",
        "openadapt_flow/dist/corpus.json",
        "openadapt_flow/data/production_samples.json",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        with pytest.raises(ValueError, match="unregistered"):
            validate_public_source_tree(tmp_path)
        path.unlink()


def test_public_source_tree_rejects_private_segments_tokens_and_signatures(
    tmp_path: Path,
) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    write_public_artifact_inventory(tmp_path)

    for relative in (
        "openadapt_flow/private/corpus.py",
        "openadapt_flow/.private/corpus.py",
        "openadapt_flow/control_plane.py",
        "openadapt_flow/enterprise_productionized.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n")
        with pytest.raises(ValueError, match="public source tree contains private"):
            validate_public_source_tree(tmp_path)
        path.unlink()

    renamed = tmp_path / "openadapt_flow/neutral.py"
    renamed.write_bytes(PRIVATE_CORPUS_CONTENT_SIGNATURES[0])
    with pytest.raises(ValueError, match="public source tree contains private"):
        validate_public_source_tree(tmp_path)


def test_public_source_tree_rejects_symlinks_and_changed_inventory(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "schemas/workflow.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n")
    write_public_artifact_inventory(tmp_path)
    validate_public_source_tree(tmp_path)

    artifact.write_text('{"changed": true}\n')
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_public_source_tree(tmp_path)

    artifact.write_text("{}\n")
    link = tmp_path / "openadapt_flow/link.py"
    link.parent.mkdir()
    try:
        os.symlink(artifact, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ValueError, match="symlink/special"):
        validate_public_source_tree(tmp_path)


def test_public_artifact_inventory_covers_data_and_configuration_suffixes() -> None:
    assert {".json", ".jsonl", ".csv", ".yaml", ".yml", ".toml"} <= (
        PUBLIC_ARTIFACT_SUFFIXES
    )


def test_lock_sync_updates_only_editable_root_version(tmp_path: Path) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "1.7.1"\n'
    )
    (tmp_path / "openadapt_flow/__init__.py").write_text('__version__ = "1.7.1"\n')
    original_lock = (
        'version = 1\n\n[[package]]\nname = "openadapt-flow"\n'
        'version = "1.7.0"\nsource = { editable = "." }\n'
        '\n[[package]]\nname = "dependency"\nversion = "1.2.3"\n'
    )
    (tmp_path / "uv.lock").write_text(original_lock)

    assert sync_lock_version(tmp_path) == "1.7.1"
    updated_lock = (tmp_path / "uv.lock").read_text()
    assert updated_lock == original_lock.replace(
        'name = "openadapt-flow"\nversion = "1.7.0"',
        'name = "openadapt-flow"\nversion = "1.7.1"',
    )


def test_lock_sync_rejects_source_version_drift(tmp_path: Path) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "1.7.1"\n'
    )
    (tmp_path / "openadapt_flow/__init__.py").write_text('__version__ = "1.7.0"\n')
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openadapt-flow"\nversion = "1.7.0"\n'
        'source = { editable = "." }\n'
    )

    with pytest.raises(ValueError, match="source versions differ"):
        sync_lock_version(tmp_path)


def test_release_workflow_uses_pinned_actions() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    uses = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert "# v10.6.1" in workflow


def test_semantic_release_is_manual_and_waits_for_exact_head_ci() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    triggers = workflow[workflow.index("\non:\n") : workflow.index("\njobs:\n")]
    wait_index = workflow.index("- name: Wait for exact-head CI")
    release_index = workflow.index("- name: Python Semantic Release")

    assert "  push:" not in triggers
    assert "operation:" in triggers
    assert "- semantic-release" in triggers
    assert "- publish-existing-ref" in triggers
    assert "inputs.operation == 'semantic-release'" in workflow
    assert "actions: read # inspect the exact-head CI run before publishing" in workflow
    assert wait_index < release_index
    assert "actions/workflows/ci.yml/runs" in workflow
    assert '--raw-field head_sha="${GITHUB_SHA}"' in workflow
    assert "--raw-field event=push" in workflow
    assert "select(.head_sha == $sha)" in workflow
    assert 'if [ "$conclusion" = "success" ]' in workflow
    assert "Refusing to publish because exact-head CI concluded" in workflow


def test_manual_publish_uses_current_guard_and_exact_target_ci() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    manual = workflow[workflow.index("\n  manual-publish:") :]
    validate_index = manual.index("Independently validate publication artifacts")
    publish_index = manual.index("- name: Publish to PyPI")

    assert "github.ref == 'refs/heads/main'" in manual
    assert "inputs.operation == 'publish-existing-ref'" in manual
    assert "actions: read" in manual
    assert "git merge-base --is-ancestor" in manual
    assert 'head_sha="${TARGET_SHA}"' in manual
    assert "--validate-dist-dir target/dist" in manual
    assert "--license-file target/LICENSE" not in manual
    assert "packages-dir: target/dist/" in manual
    assert validate_index < publish_index


def _metadata_bytes(*, version: str = "1.0", license_name: str = "MIT") -> bytes:
    return (
        f"Metadata-Version: 2.4\n"
        f"Name: openadapt-flow\n"
        f"Version: {version}\n"
        f"License: {license_name}\n"
        f"License-File: LICENSE\n"
        f"\n"
    ).encode()


def _write_sdist(
    path: Path,
    members: set[str],
    *,
    license_bytes: bytes = MIT_LICENSE,
    metadata_bytes: bytes | None = None,
    payloads: dict[str, bytes] | None = None,
) -> None:
    archive_root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, mode="w:gz") as archive:
        for relative in sorted(members):
            if payloads and relative in payloads:
                payload = payloads[relative]
            elif relative == "LICENSE":
                payload = license_bytes
            elif relative == "PKG-INFO":
                payload = metadata_bytes or _metadata_bytes()
            elif relative == PUBLIC_ARTIFACT_INVENTORY_PATH:
                payload = (ROOT / PUBLIC_ARTIFACT_INVENTORY_PATH).read_bytes()
            else:
                payload = b"fixture"
            info = tarfile.TarInfo(f"{archive_root}/{relative}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_wheel(
    path: Path,
    members: set[str],
    *,
    license_bytes: bytes = MIT_LICENSE,
    metadata_bytes: bytes | None = None,
    payloads: dict[str, bytes] | None = None,
    include_inventory: bool = True,
) -> None:
    members = set(members)
    if include_inventory:
        members.add(WHEEL_ARTIFACT_INVENTORY_PATH)
    with zipfile.ZipFile(path, mode="w") as archive:
        for relative in sorted(members):
            if payloads and relative in payloads:
                payload = payloads[relative]
            elif relative.endswith(".dist-info/licenses/LICENSE"):
                payload = license_bytes
            elif relative.endswith(".dist-info/METADATA"):
                payload = metadata_bytes or _metadata_bytes()
            elif relative == WHEEL_ARTIFACT_INVENTORY_PATH:
                payload = (ROOT / PUBLIC_ARTIFACT_INVENTORY_PATH).read_bytes()
            else:
                payload = b"fixture"
            archive.writestr(relative, payload)


def test_wheel_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"
    clean = tmp_path / "clean.whl"
    _write_wheel(
        clean,
        {license_path, metadata_path, "openadapt_flow/__init__.py"},
    )
    validate_wheel_license_boundary(clean)

    missing_license = tmp_path / "missing-license.whl"
    _write_wheel(
        missing_license,
        {metadata_path, "openadapt_flow/__init__.py"},
    )
    with pytest.raises(ValueError, match="missing the MIT LICENSE"):
        validate_wheel_license_boundary(missing_license)

    changed_license = tmp_path / "changed-license.whl"
    _write_wheel(
        changed_license,
        {license_path, metadata_path},
        license_bytes=b"MIT License\n",
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_wheel_license_boundary(changed_license)

    changed_metadata = tmp_path / "changed-metadata.whl"
    _write_wheel(
        changed_metadata,
        {license_path, metadata_path},
        metadata_bytes=_metadata_bytes(license_name="AGPL-3.0-only"),
    )
    with pytest.raises(ValueError, match="License: MIT"):
        validate_wheel_license_boundary(changed_metadata)

    for index, forbidden in enumerate(
        (
            "benchmark/openimis_claims/compose.yml",
            "licenses/LICENSE-AGPL-3.0.md",
            "THIRD_PARTY_NOTICES.md",
            "fixtures/renamed-compose.yml",
        )
    ):
        mixed = tmp_path / f"mixed-{index}.whl"
        _write_wheel(
            mixed,
            {license_path, metadata_path, forbidden},
            payloads=(
                {forbidden: AGPL_CONTENT_SIGNATURES[0]}
                if forbidden == "fixtures/renamed-compose.yml"
                else None
            ),
        )
        with pytest.raises(ValueError, match="outside the MIT package boundary"):
            validate_wheel_license_boundary(mixed)


def test_sdist_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.tar.gz"
    clean_members = {*REQUIRED_SDIST_PATHS, "PKG-INFO"}
    _write_sdist(clean, clean_members)
    validate_sdist_license_boundary(clean)

    missing_license = tmp_path / "missing-license.tar.gz"
    _write_sdist(missing_license, {"PKG-INFO"})
    with pytest.raises(ValueError, match="missing required files.*LICENSE"):
        validate_sdist_license_boundary(missing_license)

    changed_license = tmp_path / "changed-license.tar.gz"
    _write_sdist(
        changed_license,
        clean_members,
        license_bytes=b"MIT License\n",
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_sdist_license_boundary(changed_license)

    changed_metadata = tmp_path / "changed-metadata.tar.gz"
    _write_sdist(
        changed_metadata,
        clean_members,
        metadata_bytes=_metadata_bytes(license_name="AGPL-3.0-only"),
    )
    with pytest.raises(ValueError, match="License: MIT"):
        validate_sdist_license_boundary(changed_metadata)

    forbidden_members = {
        *FORBIDDEN_SDIST_PATHS,
        f"{FORBIDDEN_SDIST_PREFIXES[0]}compose.yml",
        "fixtures/openimis_claims/conf/nginx.conf",
        "fixtures/renamed-compose.yml",
    }
    for index, forbidden in enumerate(sorted(forbidden_members)):
        mixed = tmp_path / f"mixed-{index}.tar.gz"
        _write_sdist(
            mixed,
            {*clean_members, forbidden},
            payloads=(
                {forbidden: AGPL_CONTENT_SIGNATURES[0]}
                if forbidden == "fixtures/renamed-compose.yml"
                else None
            ),
        )
        with pytest.raises(ValueError, match="repository-only openIMIS"):
            validate_sdist_license_boundary(mixed)


def test_wheel_refuses_private_corpus_material(tmp_path: Path) -> None:
    """The wheel must reject private, deployment-derived hardening artifacts.

    The public synthetic baseline (benchmark/vision_hardening/) is allowed; only
    the private grown-corpus / tuned-adversary / real-EMR class -- detected by
    path token OR provenance banner even after a rename -- is refused.
    """
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"

    # A public synthetic corpus path is NOT private -> still passes.
    ok = tmp_path / "public-corpus-ok.whl"
    _write_wheel(
        ok,
        {
            license_path,
            metadata_path,
            "openadapt_flow/benchmark/reliability.py",
            "openadapt_flow/validation/hardening/corpus.py",
            "openadapt_flow/validation/hardening/harness.py",
        },
    )
    validate_wheel_license_boundary(ok)

    for index, forbidden in enumerate(
        (
            "openadapt_flow/validation/hardening/grown_corpus/deploy.json",
            "openadapt_flow/tuned_adversary/weights.json",
            "openadapt_flow/data/real_emr/patients.json",
            "openadapt_flow/validation/adversary_corpus_v4.py",
            "openadapt_flow/validation/identity_roc.py",
            "tests/test_identity_out_of_corpus.py",
            "private/identity/operating-point.json",
            "openadapt_flow/renamed-neutral.json",  # caught by banner, not path
        )
    ):
        mixed = tmp_path / f"private-{index}.whl"
        _write_wheel(
            mixed,
            {license_path, metadata_path, forbidden},
            payloads=(
                {forbidden: PRIVATE_CORPUS_CONTENT_SIGNATURES[0]}
                if forbidden.endswith("renamed-neutral.json")
                else None
            ),
        )
        with pytest.raises(ValueError, match="private, deployment-derived"):
            validate_wheel_license_boundary(mixed)


def test_sdist_refuses_private_corpus_material(tmp_path: Path) -> None:
    """The sdist must reject private, deployment-derived hardening artifacts."""
    clean_members = {*REQUIRED_SDIST_PATHS, "PKG-INFO"}

    # The committed public synthetic corpus is allowed in the sdist.
    ok = tmp_path / "public-corpus-ok.tar.gz"
    _write_sdist(
        ok,
        {
            *clean_members,
            "benchmark/vision_hardening/corpus.json",
            "docs/validation/VALIDATION.md",
            "openadapt_flow/benchmark/reliability.py",
            "openadapt_flow/validation/hardening/harness.py",
        },
        payloads={
            "benchmark/vision_hardening/corpus.json": (
                ROOT / "benchmark/vision_hardening/corpus.json"
            ).read_bytes()
        },
    )
    validate_sdist_license_boundary(ok)

    for index, forbidden in enumerate(
        (
            "benchmark/vision_hardening/grown_corpus/deploy.json",
            "vendor/openadapt-corpus/thresholds.json",
            "benchmark/effect_oracle_recipe/openemr.yml",
            "docs/validation/adversary_corpus_v4_manifest.json",
            "docs/validation/identity_roc.json",
            "tests/test_identity_corpus_rates.py",
            "private/identity/operating-point.json",
            "benchmark/renamed-neutral.json",  # caught by banner, not path
        )
    ):
        mixed = tmp_path / f"private-{index}.tar.gz"
        _write_sdist(
            mixed,
            {*clean_members, forbidden},
            payloads=(
                {forbidden: PRIVATE_CORPUS_CONTENT_SIGNATURES[0]}
                if forbidden.endswith("renamed-neutral.json")
                else None
            ),
        )
        with pytest.raises(ValueError, match="private, deployment-derived"):
            validate_sdist_license_boundary(mixed)


def test_archives_refuse_neutral_unregistered_and_modified_artifacts(
    tmp_path: Path,
) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"
    wheel_base = {license_path, metadata_path}
    sdist_base = {*REQUIRED_SDIST_PATHS, "PKG-INFO"}

    for index, relative in enumerate(
        (
            "data/production_samples.json",
            "openadapt_flow/dist/corpus.json",
            "openadapt_flow/data/production_samples.json",
        )
    ):
        wheel = tmp_path / f"unregistered-{index}.whl"
        _write_wheel(wheel, {*wheel_base, relative})
        with pytest.raises(ValueError, match="artifact inventory/provenance"):
            validate_wheel_license_boundary(wheel)

        sdist = tmp_path / f"unregistered-{index}.tar.gz"
        _write_sdist(sdist, {*sdist_base, relative})
        with pytest.raises(ValueError, match="artifact inventory/provenance"):
            validate_sdist_license_boundary(sdist)

    known = "benchmark/vision_hardening/corpus.json"
    changed = tmp_path / "changed-known.tar.gz"
    _write_sdist(changed, {*sdist_base, known}, payloads={known: b"{}\n"})
    with pytest.raises(ValueError, match="artifact inventory/provenance"):
        validate_sdist_license_boundary(changed)


def test_archive_uses_its_embedded_historical_inventory_not_current_root(
    tmp_path: Path,
) -> None:
    known = "benchmark/vision_hardening/corpus.json"
    historical_payload = b'{"historical": true}\n'
    manifest = json.loads((ROOT / PUBLIC_ARTIFACT_INVENTORY_PATH).read_text())
    for row in manifest["artifacts"]:
        if row["path"] == known:
            row["sha256"] = hashlib.sha256(historical_payload).hexdigest()
            break
    else:  # pragma: no cover - the committed manifest contract catches this first
        raise AssertionError(f"expected {known} in public artifact inventory")
    historical_manifest = (json.dumps(manifest, indent=2) + "\n").encode()

    sdist = tmp_path / "historical.tar.gz"
    _write_sdist(
        sdist,
        {*REQUIRED_SDIST_PATHS, "PKG-INFO", known},
        payloads={
            PUBLIC_ARTIFACT_INVENTORY_PATH: historical_manifest,
            known: historical_payload,
        },
    )
    validate_sdist_license_boundary(sdist)


def test_archives_require_a_valid_embedded_inventory(tmp_path: Path) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"

    wheel_missing = tmp_path / "missing-inventory.whl"
    _write_wheel(
        wheel_missing,
        {license_path, metadata_path},
        include_inventory=False,
    )
    with pytest.raises(ValueError, match="missing its embedded"):
        validate_wheel_license_boundary(wheel_missing)

    wheel_tampered = tmp_path / "tampered-inventory.whl"
    _write_wheel(
        wheel_tampered,
        {license_path, metadata_path},
        payloads={WHEEL_ARTIFACT_INVENTORY_PATH: b"{}\n"},
    )
    with pytest.raises(ValueError, match="unexpected top-level schema"):
        validate_wheel_license_boundary(wheel_tampered)

    sdist_missing = tmp_path / "missing-inventory.tar.gz"
    _write_sdist(sdist_missing, {"LICENSE", "PKG-INFO"})
    with pytest.raises(ValueError, match="missing required files"):
        validate_sdist_license_boundary(sdist_missing)

    sdist_tampered = tmp_path / "tampered-inventory.tar.gz"
    _write_sdist(
        sdist_tampered,
        {*REQUIRED_SDIST_PATHS, "PKG-INFO"},
        payloads={PUBLIC_ARTIFACT_INVENTORY_PATH: b"not-json"},
    )
    with pytest.raises(ValueError, match="could not parse"):
        validate_sdist_license_boundary(sdist_tampered)


def test_archives_refuse_nested_private_segments(tmp_path: Path) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"
    for index, relative in enumerate(
        (
            "openadapt_flow/private/corpus.py",
            "openadapt_flow/.private/corpus.py",
        )
    ):
        wheel = tmp_path / f"private-segment-{index}.whl"
        _write_wheel(wheel, {license_path, metadata_path, relative})
        with pytest.raises(ValueError, match="private, deployment-derived"):
            validate_wheel_license_boundary(wheel)

        sdist = tmp_path / f"private-segment-{index}.tar.gz"
        _write_sdist(sdist, {*REQUIRED_SDIST_PATHS, "PKG-INFO", relative})
        with pytest.raises(ValueError, match="private, deployment-derived"):
            validate_sdist_license_boundary(sdist)


def test_wheel_refuses_repository_only_evaluation_data(tmp_path: Path) -> None:
    """The generic reliability harness ships; its curated corpus does not."""
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"
    clean_members = {
        license_path,
        metadata_path,
        "openadapt_flow/benchmark/reliability.py",
    }
    clean = tmp_path / "reliability-harness-ok.whl"
    _write_wheel(clean, clean_members)
    validate_wheel_license_boundary(clean)

    for index, forbidden in enumerate(
        (
            "openadapt_flow/benchmark/reliability_corpus.py",
            "openadapt_flow/benchmark/reliability_corpus_v2.py",
            "benchmark/reliability/results.json",
            "scripts/reliability/run.py",
            "tests/test_reliability.py",
        )
    ):
        mixed = tmp_path / f"repository-only-{index}.whl"
        _write_wheel(mixed, {*clean_members, forbidden})
        with pytest.raises(ValueError, match="repository-only evaluation"):
            validate_wheel_license_boundary(mixed)


def test_sdist_refuses_repository_only_evaluation_data(tmp_path: Path) -> None:
    """Public-source recipes/results stay out of the installation archive."""
    clean_members = {
        *REQUIRED_SDIST_PATHS,
        "PKG-INFO",
        "openadapt_flow/benchmark/reliability.py",
    }
    clean = tmp_path / "reliability-harness-ok.tar.gz"
    _write_sdist(clean, clean_members)
    validate_sdist_license_boundary(clean)

    for index, forbidden in enumerate(
        (
            "openadapt_flow/benchmark/reliability_corpus.py",
            "benchmark/reliability/corpus.json",
            "benchmark/reliability/results.json",
            "scripts/reliability/run.py",
            "tests/test_reliability.py",
        )
    ):
        mixed = tmp_path / f"repository-only-{index}.tar.gz"
        _write_sdist(mixed, {*clean_members, forbidden})
        with pytest.raises(ValueError, match="repository-only evaluation"):
            validate_sdist_license_boundary(mixed)


def test_distribution_directory_refuses_extra_or_multiple_artifacts(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / ".gitignore").write_text("*\n")
    wheel_members = {
        "openadapt_flow-1.0.dist-info/licenses/LICENSE",
        "openadapt_flow-1.0.dist-info/METADATA",
        "openadapt_flow/__init__.py",
    }
    _write_wheel(dist / "openadapt_flow-1.0-py3-none-any.whl", wheel_members)
    _write_sdist(
        dist / "openadapt_flow-1.0.tar.gz",
        {*REQUIRED_SDIST_PATHS, "PKG-INFO"},
    )
    validate_distribution_directory(dist, version="1.0")

    _write_wheel(dist / "openadapt_flow-1.0-cp312-any.whl", wheel_members)
    with pytest.raises(ValueError, match="exactly one wheel and one sdist"):
        validate_distribution_directory(dist, version="1.0")


def test_distribution_directory_requires_matching_embedded_inventories(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = json.loads((ROOT / PUBLIC_ARTIFACT_INVENTORY_PATH).read_text())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    different_valid_manifest = (json.dumps(manifest, indent=2) + "\n").encode()

    _write_wheel(
        dist / "openadapt_flow-1.0-py3-none-any.whl",
        {
            "openadapt_flow-1.0.dist-info/licenses/LICENSE",
            "openadapt_flow-1.0.dist-info/METADATA",
        },
        payloads={WHEEL_ARTIFACT_INVENTORY_PATH: different_valid_manifest},
    )
    _write_sdist(
        dist / "openadapt_flow-1.0.tar.gz",
        {*REQUIRED_SDIST_PATHS, "PKG-INFO"},
    )

    with pytest.raises(ValueError, match="embed different"):
        validate_distribution_directory(dist, version="1.0")


def test_sdist_refuses_other_roots_traversal_and_duplicates(tmp_path: Path) -> None:
    def write_members(path: Path, names: list[str]) -> None:
        with tarfile.open(path, mode="w:gz") as archive:
            for name in names:
                if name.endswith("/LICENSE"):
                    payload = MIT_LICENSE
                elif name.endswith("/PKG-INFO"):
                    payload = _metadata_bytes()
                else:
                    payload = b"fixture"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    other_root = tmp_path / "openadapt_flow-1.0.tar.gz"
    write_members(
        other_root,
        [
            "openadapt_flow-1.0/LICENSE",
            "openadapt_flow-1.0/PKG-INFO",
            "other-root/openimis.yml",
        ],
    )
    with pytest.raises(ValueError, match="outside its single"):
        validate_sdist_license_boundary(other_root)

    traversal = tmp_path / "traversal.tar.gz"
    write_members(traversal, ["traversal/../neutral.yml"])
    with pytest.raises(ValueError, match="non-canonical"):
        validate_sdist_license_boundary(traversal)

    duplicate = tmp_path / "duplicate.tar.gz"
    write_members(duplicate, ["duplicate/LICENSE", "duplicate/LICENSE"])
    with pytest.raises(ValueError, match="duplicate member"):
        validate_sdist_license_boundary(duplicate)


def test_wheel_refuses_traversal_and_duplicates(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.whl"
    _write_wheel(traversal, {"../neutral.py"})
    with pytest.raises(ValueError, match="non-canonical"):
        validate_wheel_license_boundary(traversal)

    duplicate = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(duplicate, mode="w") as archive:
        archive.writestr("neutral.py", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("neutral.py", b"second")
    with pytest.raises(ValueError, match="duplicate member"):
        validate_wheel_license_boundary(duplicate)
