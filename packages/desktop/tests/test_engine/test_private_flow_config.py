"""Tests for private target/deployment staging."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from engine.private_flow_config import (
    PrivateFlowConfigError,
    flow_log_redactions,
    prepare_flow_config,
    private_flow_config,
    redact_flow_log,
    stage_private_yaml,
)
from engine.targets import ExecutionTarget


def test_merges_target_over_base_config_with_mode_0600_and_removes_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        """
backend:
  kind: rdp
  rdp_host: old.example
  rdp_username: operator
  rdp_password: from-private-source
effects:
  kind: onscreen
""".strip()
    )
    target = ExecutionTarget(
        backend="rdp",
        rdp_host="new.example",
        rdp_readiness_text="MRN 12345",
    )

    staged_path: Path | None = None
    with private_flow_config(
        tmp_path / "run",
        source=source,
        target=target,
    ) as staged:
        assert staged is not None
        staged_path = staged
        if os.name != "nt":
            assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        else:
            # Windows does not expose POSIX owner/group bits. mkstemp creates
            # inside Desktop's current-user run directory and inherits its ACL.
            assert staged.is_file()
        merged = yaml.safe_load(staged.read_text())
        assert merged["backend"] == {
            "kind": "rdp",
            "rdp_host": "new.example",
            "rdp_username": "operator",
            "rdp_password": "from-private-source",
            "rdp_readiness_text": "MRN 12345",
        }
        assert merged["effects"] == {"kind": "onscreen"}

    assert staged_path is not None
    assert not staged_path.exists()
    assert source.exists()


def test_target_only_config_uses_flow_backend_keys(tmp_path: Path) -> None:
    target = ExecutionTarget(
        backend="macos",
        macos_app="TextEdit",
        macos_window_title="Patient Jane Doe",
    )

    with private_flow_config(tmp_path, target=target) as staged:
        assert staged is not None
        assert yaml.safe_load(staged.read_text()) == {
            "backend": {
                "kind": "macos",
                "macos_app": "TextEdit",
                "macos_window_title": "Patient Jane Doe",
            }
        }


def test_config_only_preserves_native_backend_without_web_override(
    tmp_path: Path,
) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text("backend:\n  kind: linux\n  linux_app: gedit\n")

    with private_flow_config(tmp_path / "run", source=source) as staged:
        assert staged is not None
        merged = yaml.safe_load(staged.read_text())
        assert merged["backend"]["kind"] == "linux"
        assert merged["backend"]["linux_app"] == "gedit"


def test_log_redactions_cover_config_and_direct_selector_values(tmp_path: Path) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text("backend:\n  kind: rdp\n  rdp_password: super-secret\n")
    target = ExecutionTarget(
        backend="rdp",
        rdp_host="10.0.0.5",
        rdp_readiness_text="MRN 12345",
    )

    redactions = flow_log_redactions(source, target)
    safe = redact_flow_log(
        "MRN 12345 at 10.0.0.5 with super-secret",
        redactions,
    )

    assert safe == "[REDACTED] at [REDACTED] with [REDACTED]"


def test_rejects_non_mapping_config_without_echoing_values(tmp_path: Path) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text("- Patient Jane Doe\n- MRN 12345\n")

    try:
        with private_flow_config(tmp_path / "run", source=source):
            raise AssertionError("invalid config must not be staged")
    except PrivateFlowConfigError as exc:
        message = str(exc)

    assert "Jane Doe" not in message
    assert "12345" not in message


def test_selected_local_window_clears_stale_network_transport(tmp_path: Path) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        "backend:\n"
        "  kind: rdp\n"
        "  rdp_host: stale.example\n"
        "  rdp_username: stale-user\n"
        "  rdp_password: stale-password\n"
    )
    target = ExecutionTarget(
        backend="rdp",
        rdp_window="Microsoft Remote Desktop",
        rdp_window_title="Clinical Workspace",
    )

    with private_flow_config(tmp_path / "run", source=source, target=target) as staged:
        assert staged is not None
        backend = yaml.safe_load(staged.read_text())["backend"]

    assert backend["rdp_window"] == "Microsoft Remote Desktop"
    assert backend["rdp_window_title"] == "Clinical Workspace"
    assert not ({"rdp_host", "rdp_username", "rdp_password"} & backend.keys())


def test_selected_network_host_clears_stale_local_window_transport(tmp_path: Path) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        "backend:\n"
        "  kind: rdp\n"
        "  rdp_window: Microsoft Remote Desktop\n"
        "  rdp_window_title: Clinical Workspace\n"
    )
    target = ExecutionTarget(backend="rdp", rdp_host="fresh.example")

    with private_flow_config(tmp_path / "run", source=source, target=target) as staged:
        assert staged is not None
        backend = yaml.safe_load(staged.read_text())["backend"]

    assert backend["rdp_host"] == "fresh.example"
    assert not ({"rdp_window", "rdp_window_title"} & backend.keys())


def test_explicit_empty_network_mode_clears_stale_local_window_and_refuses(
    tmp_path: Path,
) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        "backend:\n"
        "  kind: rdp\n"
        "  rdp_window: Microsoft Remote Desktop\n"
        "  rdp_window_title: Clinical Workspace\n"
    )
    target = ExecutionTarget.model_validate({"backend": "rdp", "rdp_host": ""})

    with pytest.raises(
        PrivateFlowConfigError,
        match="requires a network host or local client window",
    ):
        prepare_flow_config(source, target)


def test_explicit_empty_window_mode_clears_stale_network_and_refuses(
    tmp_path: Path,
) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        "backend:\n"
        "  kind: rdp\n"
        "  rdp_host: stale.example\n"
        "  rdp_username: stale-user\n"
        "  rdp_password: stale-password\n"
    )
    target = ExecutionTarget.model_validate({"backend": "rdp", "rdp_window": ""})

    with pytest.raises(
        PrivateFlowConfigError,
        match="requires a network host or local client window",
    ):
        prepare_flow_config(source, target)


def test_prepare_and_stage_use_one_immutable_source_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "deployment.yaml"
    source.write_text(
        "backend:\n  kind: rdp\n  rdp_host: first.example\n  rdp_password: first-secret\n"
    )
    prepared = prepare_flow_config(source, None)
    assert prepared is not None

    # Changing the operator-owned file after preparation cannot change either
    # the staged values or their redaction set.
    source.write_text(
        "backend:\n  kind: rdp\n  rdp_host: second.example\n  rdp_password: second-secret\n"
    )
    with stage_private_yaml(tmp_path / "run", prepared=prepared) as staged:
        deployment = yaml.safe_load(staged.read_text())

    assert deployment["backend"]["rdp_host"] == "first.example"
    assert "first.example" in prepared.redactions
    assert "first-secret" in prepared.redactions
    assert "second.example" not in prepared.payload
    assert "second-secret" not in prepared.redactions


# --------------------------------------------------- the hosted decision lane


def test_remote_decisions_is_read_from_the_same_snapshot_as_the_payload(tmp_path):
    """One read decides both what Flow executes and whether the lane is on."""
    source = tmp_path / "deployment.json"
    source.write_text(
        json.dumps(
            {
                "human_decisions": {
                    "remote": {"enabled": True, "runner_id": " runner_exact_01 "}
                }
            }
        ),
        encoding="utf-8",
    )
    prepared = prepare_flow_config(source, None)
    assert prepared is not None
    assert prepared.remote_decisions is True
    assert prepared.remote_decision_runner_id == "runner_exact_01"


@pytest.mark.parametrize(
    "deployment",
    [
        {},
        {"human_decisions": {}},
        {"human_decisions": {"remote": {}}},
        {"human_decisions": {"remote": {"enabled": False}}},
        # Truthy is not True. An outbound lane carrying decision context is
        # never inferred from a stray string or a 1.
        {"human_decisions": {"remote": {"enabled": "yes"}}},
        {"human_decisions": {"remote": {"enabled": 1}}},
        {"human_decisions": "remote"},
    ],
)
def test_remote_decisions_defaults_to_off_and_is_never_inferred(tmp_path, deployment):
    source = tmp_path / "deployment.json"
    source.write_text(json.dumps(deployment), encoding="utf-8")
    prepared = prepare_flow_config(source, None)
    assert prepared is not None
    assert prepared.remote_decisions is False
