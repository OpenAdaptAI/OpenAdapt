"""Behavior contract for non-destructive, atomic bundle sealing."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import bundle_sealing, crypto
from openadapt_flow.__main__ import main
from openadapt_flow.bundle_sealing import BundleSealingError, seal_bundle
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.qualification import (
    EnvironmentBoundary,
    QualificationCertification,
    init_project,
    workflow_contract_sha256,
)

_KEY = "customer-controlled-test-key"
_CROP = b"\x89PNG\r\n\x1a\nsynthetic-target-crop"


def _source(tmp_path: Path) -> Path:
    bundle = tmp_path / "source"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "submit.png").write_bytes(_CROP)
    (bundle / "operator-notes.txt").write_text("complete-bundle-marker")
    Workflow(
        name="graph-ready",
        steps=[
            Step(
                id="submit",
                intent="Submit the qualified record",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/submit.png",
                    region=(10, 20, 30, 40),
                    click_point=(25, 40),
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Submitted",
                    )
                ],
            )
        ],
    ).save(bundle)
    return bundle


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _certify_source(source: Path) -> None:
    workflow = Workflow.load(source)
    project = init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Synthetic fixture",
            application_version="1",
            environment_digest="e" * 64,
            runtime_version="test",
        ),
    )
    project.last_certification = QualificationCertification(
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        policy_name="permissive",
        passed=True,
        report_sha256="f" * 64,
    )
    workflow.stamp_certification("permissive", passed=True)
    workflow.save(source)


def test_cli_seal_preserves_source_and_verifies_encrypted_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    before = _snapshot(source)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    assert main(["seal", str(source), "--out", str(destination)]) == 0

    assert _snapshot(source) == before
    assert (destination / "operator-notes.txt").read_text() == "complete-bundle-marker"
    assert not (destination / "workflow.json").exists()
    assert crypto.is_encrypted((destination / "workflow.json.enc").read_bytes())
    sealed_crop = destination / "templates" / "submit.png.enc"
    assert crypto.is_encrypted(sealed_crop.read_bytes())
    assert not (destination / "templates" / "submit.png").exists()
    loaded = Workflow.load(destination, key=_KEY, verify_integrity=True)
    assert loaded.encrypted
    assert loaded.decrypted_template("templates/submit.png") == _CROP
    assert loaded.manifest is not None
    # Production ordering is seal first, then evaluate the exact encrypted
    # artifact contract that will be deployed.
    assert main(["certify", str(destination), "--policy", "permissive"]) == 0
    output = capsys.readouterr().out
    assert f"Sealed bundle: {destination}" in output
    assert f"Content digest: sha256:{loaded.manifest.content_digest}" in output


def test_seal_invalidates_prior_certification_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    _certify_source(source)
    before = _snapshot(source)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    assert main(["seal", str(source), "--out", str(destination)]) == 0

    # The source remains the exact certified artifact the operator supplied.
    assert _snapshot(source) == before
    source_workflow = Workflow.load(source)
    assert source_workflow.manifest is not None
    assert source_workflow.manifest.provenance.certified is True
    assert source_workflow.qualification is not None
    assert source_workflow.qualification.last_certification is not None

    # Encryption changes the workflow contract, so the new artifact cannot
    # inherit either persisted certification result.
    sealed = Workflow.load(destination, key=_KEY, verify_integrity=True)
    assert sealed.manifest is not None
    provenance = sealed.manifest.provenance
    assert provenance.policy_name == "permissive"
    assert provenance.certified is False
    assert provenance.certification_status == "expired"
    assert provenance.certification_invalidated_at
    assert provenance.expires_at == provenance.certification_invalidated_at
    assert (
        provenance.certification_invalidation_reason
        == "at-rest sealing changed the workflow contract"
    )
    assert sealed.qualification is not None
    assert sealed.qualification.last_certification is None
    assert "Prior certification invalidated" in capsys.readouterr().out

    # A later certification clears the invalidation marker rather than leaving
    # contradictory provenance on the artifact.
    sealed.stamp_certification("permissive", passed=True)
    sealed.save(destination, encrypt=True, key=_KEY)
    recertified = Workflow.load(destination, key=_KEY, verify_integrity=True)
    assert recertified.manifest is not None
    assert recertified.manifest.provenance.certified is True
    assert recertified.manifest.provenance.certification_invalidated_at is None
    assert recertified.manifest.provenance.certification_invalidation_reason is None


def test_cli_seal_requires_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)

    assert main(["seal", str(source), "--out", str(destination)]) == 2
    assert not destination.exists()
    assert crypto.ENV_KEY in capsys.readouterr().out


@pytest.mark.parametrize("destination_kind", ["same", "existing"])
def test_cli_seal_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path)
    destination = source if destination_kind == "same" else tmp_path / "production"
    if destination_kind == "existing":
        destination.mkdir()
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    assert main(["seal", str(source), "--out", str(destination)]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_cli_seal_refuses_source_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    link = source / "templates" / "alias.png"
    try:
        link.symlink_to(source / "templates" / "submit.png")
    except OSError:
        pytest.skip("this host does not permit creating a test symlink")
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    assert main(["seal", str(source), "--out", str(tmp_path / "production")]) == 2
    assert not (tmp_path / "production").exists()


def test_seal_failure_removes_only_its_private_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    def fail_save(self, *args, **kwargs):
        raise RuntimeError("synthetic sealing failure")

    monkeypatch.setattr(Workflow, "save", fail_save)
    with pytest.raises(BundleSealingError, match="synthetic sealing failure"):
        seal_bundle(source, destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".production.seal-*")) == []
    assert source.exists()


def test_cli_seal_normalizes_destination_parent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    parent = tmp_path / "not-a-directory"
    parent.write_text("occupied")
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    assert main(["seal", str(source), "--out", str(parent / "production")]) == 2
    output = capsys.readouterr().out
    assert "seal REFUSED: bundle sealing failed:" in output


def test_cli_seal_normalizes_staging_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    def refuse_staging(**_kwargs):
        raise PermissionError("synthetic staging denial")

    monkeypatch.setattr(bundle_sealing.tempfile, "mkdtemp", refuse_staging)
    assert main(["seal", str(source), "--out", str(destination)]) == 2
    output = capsys.readouterr().out
    assert "seal REFUSED: bundle sealing failed: synthetic staging denial" in output
    assert not destination.exists()


def test_cli_seal_normalizes_publication_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)

    def refuse_publication(_staging: Path, _destination: Path) -> None:
        raise OSError("synthetic publication denial")

    monkeypatch.setattr(bundle_sealing, "_publish_no_replace", refuse_publication)
    assert main(["seal", str(source), "--out", str(destination)]) == 2
    output = capsys.readouterr().out
    assert "seal REFUSED: bundle sealing failed: synthetic publication denial" in output
    assert not destination.exists()
    assert list(tmp_path.glob(".production.seal-*")) == []


def test_atomic_publication_refuses_destination_created_after_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    publish = bundle_sealing._publish_no_replace

    def race(staging: Path, target: Path) -> None:
        target.mkdir()
        publish(staging, target)

    monkeypatch.setattr(bundle_sealing, "_publish_no_replace", race)
    with pytest.raises(BundleSealingError, match="destination appeared"):
        seal_bundle(source, destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert list(tmp_path.glob(".production.seal-*")) == []
    assert source.exists()


def test_sealed_template_tampering_fails_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "production"
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    seal_bundle(source, destination)
    crop = destination / "templates" / "submit.png.enc"
    payload = bytearray(crop.read_bytes())
    payload[-1] ^= 1
    crop.write_bytes(payload)

    with pytest.raises(crypto.DecryptionError):
        Workflow.load(destination, key=_KEY, verify_integrity=True)
