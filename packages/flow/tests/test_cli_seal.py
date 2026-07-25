"""Behavior contract for non-destructive, atomic bundle sealing."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import bundle_sealing, crypto
from openadapt_flow.__main__ import main
from openadapt_flow.bundle_sealing import BundleSealingError, seal_bundle
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow

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
    output = capsys.readouterr().out
    assert f"Sealed bundle: {destination}" in output
    assert f"Content digest: sha256:{loaded.manifest.content_digest}" in output


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
