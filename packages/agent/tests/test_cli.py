"""CLI keeps one exact Flow runtime across runs and attended decisions."""

from __future__ import annotations

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
