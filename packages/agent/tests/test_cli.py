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


def _capture_serve(monkeypatch):
    captured: dict = {}

    def fake_serve(bridge, authoring=None):
        captured["bridge"] = bridge
        captured["tools"] = [spec.name for spec in bridge.list_tool_specs()]
        captured["listing"] = bridge.dispatch("list_workflows", {})

    monkeypatch.setattr("openadapt_agent.mcp.serve", fake_serve)
    return captured


def test_serve_with_no_flags_starts_the_sandbox(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENADAPT_AGENT_SANDBOX_DIR", str(tmp_path / "sandbox"))
    captured = _capture_serve(monkeypatch)
    assert main(["serve", "--sandbox-engine", "simulated"]) == 0
    bridge = captured["bridge"]
    assert bridge.mode == "sandbox"
    assert captured["tools"] == ["list_workflows", "run_workflow", "get_run"]
    assert captured["listing"]["workflows"][0]["name"] == "add_triage_note"
    assert bridge.runner_config.runs_dir == tmp_path / "sandbox"
    err = capsys.readouterr().err
    assert "sandbox mode" in err
    assert "Nothing real changes" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--tutorial"],
        ["serve", "--allow-run"],
        ["serve", "--mode", "sandbox"],
    ],
)
def test_older_sandbox_flags_are_aliases(monkeypatch, tmp_path, argv):
    captured = _capture_serve(monkeypatch)
    argv = [*argv, "--sandbox-engine", "simulated", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv) == 0
    assert captured["bridge"].mode == "sandbox"


@pytest.mark.parametrize(
    "argv, message",
    [
        (["serve", "--mode", "production"], "--mode production needs --bundles"),
        (["serve", "--mode", "attended"], "--mode attended needs --bundles"),
        (["serve", "--mode", "sandbox", "--bundles", "b"], "cannot be combined with --bundles"),
        (["serve", "--url", "https://app.example"], "--url and --config need"),
    ],
)
def test_mode_conflicts_fail_closed(argv, message, capsys):
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_mode_production_enables_runs_and_attended_adds_decisions(
    monkeypatch, bundles_root, tmp_path
):
    from contextlib import contextmanager

    @contextmanager
    def fake_attended(**kwargs):
        yield None

    monkeypatch.setattr("openadapt_agent.flow_service.open_attended_service", fake_attended)
    captured = _capture_serve(monkeypatch)
    runs = str(tmp_path / "runs")
    assert main(["serve", "--mode", "production", "--bundles", str(bundles_root), "--runs-dir", runs]) == 0
    assert captured["bridge"].mode == "production"
    assert "run_workflow" in captured["tools"]
    assert "reject_attention" not in captured["tools"]
    assert main(["serve", "--mode", "attended", "--bundles", str(bundles_root), "--runs-dir", runs]) == 0
    assert captured["bridge"].mode == "attended"
    assert "reject_attention" in captured["tools"]
    assert main(["serve", "--bundles", str(bundles_root), "--runs-dir", runs]) == 0
    assert "run_workflow" not in captured["tools"]
    assert captured["listing"]["run_tools_enabled"] is False
    # Older flags: decisions on paused runs without runs still label as attended.
    argv = ["serve", "--bundles", str(bundles_root), "--runs-dir", runs, "--allow-attended-actions"]
    assert main(argv) == 0
    assert captured["bridge"].mode == "attended"
    assert "run_workflow" not in captured["tools"]
    assert "reject_attention" in captured["tools"]


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
    assert "empty cookies" in out
    assert "pause_for_input" in out
    assert "already signed into" in out


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
