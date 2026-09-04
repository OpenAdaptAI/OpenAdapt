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
    assert "provide --bundles, --tutorial, or --authoring" in err
    assert "--allow-run" in err


def test_authoring_flag_does_not_require_bundles_or_imply_allow_run(tmp_path):
    args = build_parser().parse_args(
        ["serve", "--authoring", "--runs-dir", str(tmp_path / "runs")]
    )
    assert args.authoring is True
    assert args.bundles is None
    assert args.allow_run is False
    assert args.tutorial is False


def test_authoring_help_says_run_tools_stay_off_and_stdio_only(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["serve", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--authoring" in out
    assert "Does not enable run tools" in out
    assert "HTTP" in out


def test_authoring_does_not_imply_allow_run_without_bundles(capsys):
    result = main(["serve", "--authoring", "--allow-run"])
    assert result == 2
    err = capsys.readouterr().err
    assert "does not imply --allow-run" in err
    assert "requires --bundles" in err


def test_authoring_cannot_combine_with_tutorial(capsys):
    result = main(["serve", "--authoring", "--tutorial"])
    assert result == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_authoring_without_flow_session_fails_closed(capsys, monkeypatch):
    def missing(**kwargs):
        from openadapt_agent.authoring import AuthoringError

        raise AuthoringError("openadapt_flow.authoring is not available")

    monkeypatch.setattr("openadapt_agent.authoring.open_authoring_session", missing)
    result = main(["serve", "--authoring"])
    assert result == 2
    assert "openadapt_flow.authoring" in capsys.readouterr().err


def test_authoring_connect_parses_runner_link_and_url():
    args = build_parser().parse_args(
        [
            "authoring",
            "connect",
            "openadapt://runner?pack=p.abcdefghijkl&bind=oab_"
            + "A" * 43
            + "&origin=https://openadapt.ai",
            "--url",
            "https://example.invalid/app",
            "--headed",
        ]
    )
    assert args.authoring_command == "connect"
    assert args.url == "https://example.invalid/app"
    assert args.headed is True


def test_authoring_connect_runs_mailbox(monkeypatch):
    captured: dict = {}

    def fake_connect(target, **kwargs):
        captured["target"] = target
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("openadapt_agent.mailbox.connect_mailbox", fake_connect)
    result = main(
        [
            "authoring",
            "connect",
            "openadapt://runner?pack=p.abcdefghijkl&bind=oab_"
            + "A" * 43
            + "&origin=https://openadapt.ai",
        ]
    )
    assert result == 0
    assert captured["target"].startswith("openadapt://runner")


def test_authoring_connect_reports_mailbox_errors(monkeypatch, capsys):
    def fake_connect(target, **kwargs):
        from openadapt_agent.mailbox import MailboxError

        raise MailboxError("Bind token is malformed")

    monkeypatch.setattr("openadapt_agent.mailbox.connect_mailbox", fake_connect)
    result = main(["authoring", "connect", "https://openadapt.ai/j/p.abcdefghijkl"])
    assert result == 2
    assert "malformed" in capsys.readouterr().err


def test_authoring_serve_registers_probe_tools_without_run(monkeypatch, capsys):
    from test_authoring import FakeAuthoringSession

    captured: dict = {}

    def fake_session(**kwargs):
        captured["session_kwargs"] = kwargs
        return FakeAuthoringSession()

    def fake_serve(bridge, authoring=None):
        captured["bridge"] = bridge
        captured["authoring"] = authoring

    monkeypatch.setattr("openadapt_agent.authoring.open_authoring_session", fake_session)
    monkeypatch.setattr("openadapt_agent.mcp.serve", fake_serve)

    result = main(["serve", "--authoring"])
    assert result == 0
    assert captured["bridge"] is None
    authoring = captured["authoring"]
    names = [spec.name for spec in authoring.list_tool_specs()]
    assert names[:4] == ["observe", "start_record", "click", "halt"]
    assert "type" in names
    assert "admit" in names
    assert captured["session_kwargs"]["out_dir"].name == "authoring"
    err = capsys.readouterr().err
    assert "authoring tools enabled" in err
    assert "run tools disabled" in err
    assert "does not imply --allow-run" in err


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
