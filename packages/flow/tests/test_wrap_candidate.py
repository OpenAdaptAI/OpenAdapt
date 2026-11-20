"""Untrusted handoff identity, filesystem safety and absence of trust promotion."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from pathlib import Path

import pytest

from openadapt_flow import bundle_validation as bv
from openadapt_flow import registry
from openadapt_flow.ir import Anchor, Step, Workflow
from openadapt_flow.wrap import candidate


@pytest.fixture(autouse=True)
def _capture_platform(request):
    # The implementation deliberately refuses platforms lacking no-follow
    # directory-relative reads; its explicit refusal has a separate test.
    if request.node.name not in {
        "test_unsupported_safe_file_primitives_refuse",
        "test_mcp_refusal_is_before_capture_and_leaves_no_candidate",
    } and (
        not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.listdir not in os.supports_fd
    ):
        pytest.skip(
            "capture filesystem primitives unavailable; explicit refusal tested"
        )


def bundle(tmp_path: Path, *, sidecar: bool = False, assets: bool = True) -> Path:
    source = tmp_path / "input"
    source.mkdir()
    steps = [
        Step(
            id="click",
            intent="Select the synthetic target",
            action="click",
            anchor=Anchor(template="", region=(0, 0, 20, 20), click_point=(10, 10)),
        )
    ]
    if assets:
        (source / "templates").mkdir()
        (source / "templates/target.png").write_bytes(b"synthetic image bytes")
        steps[0].anchor.template = "templates/target.png"
    Workflow(name="Synthetic offline handoff", steps=steps).save(source)
    if sidecar:
        raw = json.loads((source / "workflow.json").read_text())
        manifest = raw.pop("manifest")
        (source / "manifest.json").write_text(json.dumps(manifest))
        (source / "workflow.json").write_text(json.dumps(raw))
    return source


@pytest.mark.parametrize(
    "sidecar,assets", [(False, False), (False, True), (True, True)]
)
def test_exact_untrusted_snapshot_and_reproducible_archive(tmp_path, sidecar, assets):
    source = bundle(tmp_path, sidecar=sidecar, assets=assets)
    before = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    first = candidate.export_candidate(source, tmp_path / "first")
    second = candidate.export_candidate(source, tmp_path / "second")
    assert first == second
    assert first["state"] == "untrusted-local-candidate"
    assert "mcp" in first and "not-emitted" in first["mcp"]
    assert not (tmp_path / "first/workflow.json").exists()
    for rel, digest in first["bundle"]["files"].items():
        copied = (tmp_path / "first/bundle" / rel).read_bytes()
        assert copied == before[rel]
        assert hashlib.sha256(copied).hexdigest() == digest
    archive = tmp_path / "first/bundle.tar.gz"
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest() == first["archive"]["sha256"]
    )
    with tarfile.open(archive) as tar:
        assert {m.name: tar.extractfile(m).read() for m in tar.getmembers()} == {
            rel: before[rel] for rel in first["bundle"]["files"]
        }
    assert (tmp_path / "first").stat().st_mode & 0o777 == 0o700
    assert all(
        p.stat().st_mode & 0o077 == 0
        for p in (tmp_path / "first").rglob("*")
        if p.is_file()
    )
    assert {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    } == before


def test_does_not_read_keys_admission_cache_private_tier_or_executable_source(
    tmp_path, monkeypatch
):
    source = bundle(tmp_path)
    extras = [
        "admission.json",
        "workflow.py",
        "verify.full.yaml",
        "verify.yaml",
        "faults.json",
        "keys/local.key",
        ".env",
        "skill.lock.json",
    ]
    for rel in extras:
        path = source / rel
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"DO NOT READ")
    reads = []
    original = candidate._read_file

    def observe(root, rel):
        assert rel not in extras
        reads.append(rel)
        return original(root, rel)

    monkeypatch.setattr(candidate, "_read_file", observe)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("execution or trust path entered")

    monkeypatch.setattr(Workflow, "load", forbidden)
    monkeypatch.setattr(bv, "remember_original_content", forbidden)
    from openadapt_flow.integrations.agents.skills import SkillCache

    monkeypatch.setattr(SkillCache, "admitted_program", forbidden)
    result = candidate.export_candidate(source, tmp_path / "out")
    assert set(reads) == {"workflow.json", "templates/target.png"}
    assert set(result["bundle"]["files"]) == set(reads)
    assert not (tmp_path / "out/server.py").exists()
    assert not list((tmp_path / "out").rglob("admission.json"))


@pytest.mark.parametrize(
    "fault",
    [
        "workflow",
        "asset",
        "missing_asset",
        "extra_asset",
        "no_seal",
        "bad_structure",
        "duplicate_json",
        "nonfinite_json",
        "overflow_json",
        "manifest",
        "encrypted",
        "sealed_set",
        "sealed_manifest",
    ],
)
def test_refuses_invalid_occurrence_before_output(tmp_path, fault):
    source = bundle(tmp_path)
    path = source / "workflow.json"
    raw = json.loads(path.read_text())
    if fault == "workflow":
        raw["name"] = "changed after seal"
    elif fault == "asset":
        (source / "templates/target.png").write_bytes(b"substituted")
    elif fault == "missing_asset":
        (source / "templates/target.png").unlink()
    elif fault == "extra_asset":
        (source / "templates/unsealed.png").write_bytes(b"extra")
    elif fault == "no_seal":
        raw.pop("manifest")
        (source / "manifest.json").unlink()
    elif fault == "bad_structure":
        raw["steps"].append(raw["steps"][0])
        raw["manifest"]["content_digest"] = hashlib.sha256(
            bv.source_canonical_payload(raw, raw["manifest"]["file_hashes"])
        ).hexdigest()
    elif fault == "manifest":
        raw["manifest"]["content_digest"] = "0" * 64
    elif fault == "encrypted":
        (source / "workflow.json.enc").write_bytes(b"never decrypt")
    elif fault == "sealed_set":
        (source / ".openadapt-sealed.json").write_bytes(b"never decrypt")
    elif fault == "sealed_manifest":
        raw["manifest"]["encrypted"] = True
    if fault == "duplicate_json":
        path.write_text('{"name":"duplicate",' + path.read_text()[1:])
    elif fault == "overflow_json":
        path.write_text(
            path.read_text().replace('"name":', '"unused":1e9999,"name":', 1)
        )
    elif fault == "nonfinite_json":
        path.write_text(path.read_text().replace('"name":', '"unused":NaN,"name":', 1))
    else:
        path.write_text(json.dumps(raw))
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_embedded_manifest_precedence_does_not_copy_ignored_sidecar(tmp_path):
    source = bundle(tmp_path)
    (source / "manifest.json").write_bytes(b"ignored, do not read")
    result = candidate.export_candidate(source, tmp_path / "out")
    assert "manifest.json" not in result["bundle"]["files"]


@pytest.mark.parametrize("link", ["root", "workflow", "templates", "asset"])
def test_links_refuse_without_reading_target(tmp_path, link):
    source = bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if link == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif link == "workflow":
        (source / "workflow.json").rename(outside / "workflow.json")
        (source / "workflow.json").symlink_to(outside / "workflow.json")
    elif link == "templates":
        (source / "templates").rename(outside / "templates")
        (source / "templates").symlink_to(
            outside / "templates", target_is_directory=True
        )
    else:
        (source / "templates/target.png").rename(outside / "target.png")
        (source / "templates/target.png").symlink_to(outside / "target.png")
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["occupied", "input", "input/nested"])
def test_output_is_new_disjoint_and_preserves_existing_data(tmp_path, name):
    source = bundle(tmp_path)
    target = tmp_path / name
    if name == "occupied":
        target.mkdir()
        (target / "retained").write_bytes(b"keep")
    before = (source / "workflow.json").read_bytes()
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, target)
    assert (source / "workflow.json").read_bytes() == before
    if name == "occupied":
        assert (target / "retained").read_bytes() == b"keep"


@pytest.mark.parametrize("rel", ["workflow.json", "templates/target.png"])
def test_changes_between_capture_observations_refuse(tmp_path, monkeypatch, rel):
    source = bundle(tmp_path)
    original = candidate._read_file
    reads = 0

    def changing(root, name):
        nonlocal reads
        data = original(root, name)
        if name == rel:
            reads += 1
            if reads == 1:
                (source / rel).write_bytes(data + b" ")
        return data

    monkeypatch.setattr(candidate, "_read_file", changing)
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_archive_cannot_borrow_hashes_from_another_payload(tmp_path, monkeypatch):
    source = bundle(tmp_path)
    build = registry.build_archive

    def replace(payload, out, **kwargs):
        (payload / "workflow.json").write_bytes(b"wrong program")
        return build(payload, out, **kwargs)

    monkeypatch.setattr(registry, "build_archive", replace)
    with pytest.raises(candidate.CandidateError, match="not produced"):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_snapshot_is_not_replaced_by_later_source_bytes(tmp_path, monkeypatch):
    source = bundle(tmp_path)
    original = (source / "workflow.json").read_bytes()
    build = registry.build_archive

    def source_changes(payload, out, **kwargs):
        (source / "workflow.json").write_bytes(b"later source occurrence")
        return build(payload, out, **kwargs)

    monkeypatch.setattr(registry, "build_archive", source_changes)
    candidate.export_candidate(source, tmp_path / "out")
    assert (tmp_path / "out/bundle/workflow.json").read_bytes() == original


def test_mcp_refusal_is_before_capture_and_leaves_no_candidate(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*_args):
        raise AssertionError("MCP request must not capture or emit")

    monkeypatch.setattr(candidate, "_capture", forbidden)
    assert (
        candidate.main(
            [
                "--bundle",
                str(tmp_path / "not-read"),
                "--out",
                str(tmp_path / "out"),
                "--emit-mcp",
            ]
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["state"] == "REFUSED"
    assert refusal["code"] == "qualification_required"
    assert "existing local certification" in refusal["detail"]
    assert not (tmp_path / "out").exists()


def test_wrap_export_dispatch_and_json_result(tmp_path, capsys):
    from openadapt_flow.wrap import cli

    source = bundle(tmp_path)
    assert (
        cli.main(["export", "--bundle", str(source), "--out", str(tmp_path / "out")])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "untrusted-local-candidate"


def test_unsupported_safe_file_primitives_refuse(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(candidate.CandidateError) as refusal:
        candidate.export_candidate(tmp_path / "input", tmp_path / "out")
    assert refusal.value.code == "unsupported_filesystem"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("limit", ["file", "total", "members"])
def test_shared_registry_capture_bounds_refuse(tmp_path, monkeypatch, limit):
    source = bundle(tmp_path)
    if limit == "file":
        monkeypatch.setattr(registry, "MAX_FILE_BYTES", 10)
    elif limit == "total":
        monkeypatch.setattr(registry, "MAX_ARCHIVE_BYTES", 10)
    else:
        monkeypatch.setattr(registry, "MAX_ARCHIVE_MEMBERS", 2)
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_special_file_refuses_without_blocking(tmp_path):
    source = bundle(tmp_path)
    (source / "templates/target.png").unlink()
    os.mkfifo(source / "templates/target.png")
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_candidate_claims_are_not_local_qualification_authority(tmp_path, monkeypatch):
    from openadapt_flow import qualification

    source = bundle(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate cannot manufacture a qualification decision")

    monkeypatch.setattr(qualification, "current_certification_matches", forbidden)
    result = candidate.export_candidate(source, tmp_path / "out")
    assert {
        "qualification",
        "admission",
        "publication",
        "permission-to-execute",
    }.issubset(result["not_granted"])
    assert "signature" not in result and "certification" not in result
    assert "registry" not in result


def test_cli_refusal_never_reports_artifact_content(tmp_path, capsys):
    source = bundle(tmp_path)
    secret_marker = "SYNTHETIC_PRIVATE_VALUE_UNDER_TEST"
    path = source / "workflow.json"
    raw = json.loads(path.read_text())
    raw["name"] = secret_marker
    path.write_text(json.dumps(raw))
    assert (
        candidate.main(["--bundle", str(source), "--out", str(tmp_path / "out")]) == 2
    )
    output = capsys.readouterr()
    assert secret_marker not in output.out + output.err
    assert json.loads(output.err)["code"] == "invalid_bundle"
    assert not (tmp_path / "out").exists()


def test_failed_cleanup_preserves_fixed_refusal(tmp_path, monkeypatch):
    source = bundle(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("SYNTHETIC_PRIVATE_ERROR_VALUE")

    monkeypatch.setattr(registry, "build_archive", fail)
    monkeypatch.setattr(candidate.shutil, "rmtree", fail)
    with pytest.raises(candidate.CandidateError) as refusal:
        candidate.export_candidate(source, tmp_path / "out")
    assert refusal.value.code == "export_failed"
    assert "incomplete" in str(refusal.value)
    assert "SYNTHETIC_PRIVATE_ERROR_VALUE" not in str(refusal.value)


def test_failure_never_deletes_a_replaced_output_directory(tmp_path, monkeypatch):
    source = bundle(tmp_path)
    out = tmp_path / "out"

    def replace(*_args, **_kwargs):
        out.rename(tmp_path / "retained-incomplete-output")
        out.mkdir()
        (out / "other-owner-data").write_bytes(b"must survive")
        raise OSError("replacement during export")

    monkeypatch.setattr(registry, "build_archive", replace)
    with pytest.raises(candidate.CandidateError):
        candidate.export_candidate(source, out)
    assert (out / "other-owner-data").read_bytes() == b"must survive"
    assert (tmp_path / "retained-incomplete-output/bundle/workflow.json").exists()
