"""``config init`` writes a draft deploy.yaml that certify/run refuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from openadapt_flow.cli_config import (
    BACKENDS,
    bundle_digest,
    init_deploy_config,
    main,
)
from openadapt_flow.deployment import UNRESOLVED, load_deployment


def _tiny_bundle(
    root: Path, *, surface: str | None = "web", name: str = "trial"
) -> Path:
    bundle = root / "bundle"
    bundle.mkdir()
    payload = {"name": name, "steps": []}
    if surface is not None:
        payload["surface"] = surface
    (bundle / "workflow.json").write_text(json.dumps(payload, indent=2) + "\n")
    return bundle


def test_init_binds_digest_and_refuses_governed_load(tmp_path: Path) -> None:
    bundle = _tiny_bundle(tmp_path)
    out = tmp_path / "deploy.yaml"
    init_deploy_config(bundle, out, backend="web")

    text = out.read_text()
    assert "Do not put secrets in this file" in text
    assert "OPENADAPT_SOR_BEARER_TOKEN" in text
    assert "OPENADAPT_RECORD_ID" in text
    assert "OPENADAPT_IDEMPOTENCY_KEY" in text
    assert "rdp_password" not in text

    digest = hashlib.sha256((bundle / "workflow.json").read_bytes()).hexdigest()
    assert bundle_digest(bundle) == digest

    draft = load_deployment(out, allow_unresolved=True)
    assert draft.bundle_digest == digest
    assert draft.backend.kind == "web"
    assert draft.backend.url == UNRESOLVED
    assert draft.identity.record_id_env == "OPENADAPT_RECORD_ID"
    assert draft.idempotency.key_env == "OPENADAPT_IDEMPOTENCY_KEY"
    assert draft.effects.auth is not None
    assert draft.effects.auth.bearer_env == "OPENADAPT_SOR_BEARER_TOKEN"
    assert "backend.url" in draft.unresolved

    with pytest.raises(ValueError, match="incomplete"):
        load_deployment(out)


def test_completed_draft_loads_for_certify(tmp_path: Path) -> None:
    bundle = _tiny_bundle(tmp_path)
    out = tmp_path / "deploy.yaml"
    init_deploy_config(bundle, out, backend="web")
    data = yaml.safe_load(out.read_text())
    data["backend"]["url"] = "http://localhost:8080"
    data["effects"]["kind"] = "rest"
    data["effects"]["base_url"] = "http://localhost:8080"
    data["unresolved"] = []
    out.write_text(yaml.safe_dump(data, sort_keys=False))

    cfg = load_deployment(out)
    assert cfg.backend.url == "http://localhost:8080"
    assert cfg.effects.kind == "rest"
    assert cfg.unresolved == []


@pytest.mark.parametrize(
    "kind,expected_paths",
    [
        ("web", ("backend.url",)),
        ("windows", ("backend.agent_url",)),
        ("macos", ("backend.macos_app", "backend.macos_window_title")),
        ("linux", ("backend.linux_app", "backend.linux_window_title")),
        ("rdp", ("backend.rdp_host", "backend.rdp_username")),
        ("citrix", ("backend.rdp_window", "backend.rdp_window_title")),
    ],
)
def test_init_covers_each_backend(
    tmp_path: Path, kind: str, expected_paths: tuple[str, ...]
) -> None:
    bundle = _tiny_bundle(tmp_path, surface=kind)
    out = tmp_path / f"{kind}.yaml"
    init_deploy_config(bundle, out)
    draft = load_deployment(out, allow_unresolved=True)
    assert draft.backend.kind == kind
    for path in expected_paths:
        assert path in draft.unresolved
    raw = out.read_text()
    assert "rdp_password" not in raw
    assert "agent_token:" not in raw
    with pytest.raises(ValueError, match="incomplete"):
        load_deployment(out)


def test_init_uses_surface_when_backend_omitted(tmp_path: Path) -> None:
    bundle = _tiny_bundle(tmp_path, surface="citrix")
    out = tmp_path / "deploy.yaml"
    init_deploy_config(bundle, out)
    draft = load_deployment(out, allow_unresolved=True)
    assert draft.backend.kind == "citrix"


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    bundle = _tiny_bundle(tmp_path)
    out = tmp_path / "deploy.yaml"
    out.write_text("name: existing\n")
    with pytest.raises(SystemExit, match="already exists"):
        init_deploy_config(bundle, out)


def test_init_refuses_missing_bundle(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not a workflow bundle"):
        init_deploy_config(tmp_path / "missing", tmp_path / "deploy.yaml")


def test_main_writes_draft(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = _tiny_bundle(tmp_path, name="claims")
    out = tmp_path / "deploy.yaml"
    assert main(["init", str(bundle), "--out", str(out), "--backend", "web"]) == 0
    captured = capsys.readouterr().out
    assert str(out) in captured
    draft = load_deployment(out, allow_unresolved=True)
    assert draft.name == "claims"
    assert set(BACKENDS) == {
        "web",
        "windows",
        "macos",
        "linux",
        "rdp",
        "citrix",
    }


def test_second_init_is_byte_identical(tmp_path: Path) -> None:
    bundle = _tiny_bundle(tmp_path, surface="rdp")
    first = tmp_path / "a.yaml"
    second = tmp_path / "b.yaml"
    init_deploy_config(bundle, first, backend="rdp")
    init_deploy_config(bundle, second, backend="rdp")
    assert first.read_text() == second.read_text()
