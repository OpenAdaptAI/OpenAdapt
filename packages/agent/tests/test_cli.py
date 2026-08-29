"""CLI keeps one exact Flow runtime across runs and attended decisions."""

from __future__ import annotations

import pytest

from openadapt_agent.cli import build_parser, main


def test_attended_flags_parse_as_server_fixed_configuration(tmp_path):
    args = build_parser().parse_args(
        [
            "serve",
            "--bundles",
            str(tmp_path / "bundles"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--allow-run",
            "--allow-attended-actions",
            "--allow-protected-export",
            "--allow-synthetic-recorded-defaults",
            "--config",
            "deployment.yaml",
            "--headed",
        ]
    )
    assert args.allow_run is True
    assert args.allow_attended_actions is True
    assert args.allow_protected_export is True
    assert args.allow_synthetic_recorded_defaults is True
    assert args.config == "deployment.yaml"
    assert args.headed is True


def test_attended_help_names_the_no_config_reject_capability(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["serve", "--help"])

    assert exc_info.value.code == 0
    assert "Reject/Teach/Escalate" in capsys.readouterr().out


def test_custom_flow_cli_is_refused_when_attended_actions_are_enabled(tmp_path, capsys):
    result = main(
        [
            "serve",
            "--bundles",
            str(tmp_path / "bundles"),
            "--allow-attended-actions",
            "--flow-cli",
            "different-openadapt-flow",
        ]
    )
    assert result == 2
    assert "cannot select a different runtime" in capsys.readouterr().err


def test_synthetic_recorded_defaults_require_run_authority(tmp_path, capsys):
    result = main(
        [
            "serve",
            "--bundles",
            str(tmp_path / "bundles"),
            "--allow-synthetic-recorded-defaults",
        ]
    )
    assert result == 2
    assert "requires --allow-run" in capsys.readouterr().err


def test_tutorial_flag_does_not_require_bundles(tmp_path):
    args = build_parser().parse_args(
        ["serve", "--tutorial", "--runs-dir", str(tmp_path / "runs")]
    )
    assert args.tutorial is True
    assert args.bundles is None
    assert args.allow_run is False


def test_serve_requires_tutorial_or_bundles(capsys):
    result = main(["serve"])
    assert result == 2
    err = capsys.readouterr().err
    assert "provide --bundles or --tutorial" in err
    assert "--allow-run" in err


def test_tutorial_rejects_private_bundle_path(tmp_path, capsys):
    result = main(
        ["serve", "--tutorial", "--bundles", str(tmp_path / "bundles")]
    )
    assert result == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_tutorial_serve_uses_slug_tool_and_standard_profile(monkeypatch, tmp_path, capsys):
    from contextlib import contextmanager
    from pathlib import Path

    from openadapt_agent.tutorial import TutorialSession
    from openadapt_flow.ir import ActionKind, Step, Workflow

    captured: dict = {}
    work = tmp_path / "runs" / "synthetic-tutorial"
    bundle = work / "bundle"
    bundle.mkdir(parents=True)
    Workflow(
        name="local-quickstart",
        params={"note": "Synthetic follow-up in two weeks"},
        steps=[Step(id="s1", intent="Save the synthetic note", action=ActionKind.CLICK)],
    ).save(bundle)
    config = work / "deployment.yaml"
    config.write_text("name: synthetic-tutorial\n", encoding="utf-8")

    def fake_prepare(work_dir, *, headed=False, reuse_bundle=True):
        def close():
            captured["closed"] = True

        return TutorialSession(
            bundle_dir=bundle,
            url="http://127.0.0.1:9/?fault=ok&idempotency=demo#tasks",
            deployment_config=config,
            work_dir=Path(work_dir),
            close=close,
        )

    def fake_serve(bridge):
        captured["bridge"] = bridge

    @contextmanager
    def fake_attended(**kwargs):
        captured["attended"] = kwargs
        yield None

    monkeypatch.setattr("openadapt_agent.tutorial.prepare_tutorial_session", fake_prepare)
    monkeypatch.setattr("openadapt_agent.mcp.serve", fake_serve)
    monkeypatch.setattr("openadapt_agent.flow_service.open_attended_service", fake_attended)

    result = main(
        [
            "serve",
            "--tutorial",
            "--allow-run",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert result == 0
    bridge = captured["bridge"]
    assert bridge.public_synthetic is True
    names = [spec.name for spec in bridge.list_tool_specs() if spec.name.startswith("run_")]
    assert names == ["run_local_quickstart"]
    assert "--profile" in bridge.runner_config.extra_run_args
    assert "standard" in bridge.runner_config.extra_run_args
    assert "--approve-unverified-writes" not in bridge.runner_config.extra_run_args
    assert captured.get("closed") is True
    assert "tutorial enabled" in capsys.readouterr().err


def test_serve_allow_run_without_bundles_starts_the_synthetic_tutorial(
    monkeypatch, tmp_path, capsys
):
    from contextlib import contextmanager
    from pathlib import Path

    from openadapt_agent.tutorial import TutorialSession
    from openadapt_flow.ir import ActionKind, Step, Workflow

    captured: dict = {}
    work = tmp_path / "runs" / "synthetic-tutorial"
    bundle = work / "bundle"
    bundle.mkdir(parents=True)
    Workflow(
        name="local-quickstart",
        params={"note": "Synthetic follow-up in two weeks"},
        steps=[Step(id="s1", intent="Save the synthetic note", action=ActionKind.CLICK)],
    ).save(bundle)
    config = work / "deployment.yaml"
    config.write_text("name: synthetic-tutorial\n", encoding="utf-8")

    def fake_prepare(work_dir, *, headed=False, reuse_bundle=True):
        def close():
            captured["closed"] = True

        return TutorialSession(
            bundle_dir=bundle,
            url="http://127.0.0.1:9/?fault=ok&idempotency=demo#tasks",
            deployment_config=config,
            work_dir=Path(work_dir),
            close=close,
        )

    def fake_serve(bridge):
        captured["bridge"] = bridge

    @contextmanager
    def fake_attended(**kwargs):
        captured["attended"] = kwargs
        yield None

    monkeypatch.setattr("openadapt_agent.tutorial.prepare_tutorial_session", fake_prepare)
    monkeypatch.setattr("openadapt_agent.mcp.serve", fake_serve)
    monkeypatch.setattr("openadapt_agent.flow_service.open_attended_service", fake_attended)

    result = main(
        [
            "serve",
            "--allow-run",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert result == 0
    bridge = captured["bridge"]
    assert bridge.public_synthetic is True
    names = [spec.name for spec in bridge.list_tool_specs() if spec.name.startswith("run_")]
    assert names == ["run_local_quickstart"]
    assert "--approve-unverified-writes" not in bridge.runner_config.extra_run_args
    assert captured.get("closed") is True
    assert "tutorial enabled" in capsys.readouterr().err
