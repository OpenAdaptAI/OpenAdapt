"""Unit tests for the top-level ``openadapt connect`` launcher command."""

from __future__ import annotations

import builtins
import sys

from click.testing import CliRunner

from openadapt import cli


def test_connect_default_delegates_to_connector_run(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(argv) or 0)

    result = CliRunner().invoke(cli.main, ["connect", "--token", "tok-123"])
    assert result.exit_code == 0
    assert calls == [
        [
            "connector",
            "run",
            "--control-plane-url",
            "https://app.openadapt.ai",
            "--token",
            "tok-123",
        ]
    ]


def test_connect_custom_server_and_once_and_extra_args(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(argv) or 0)

    result = CliRunner().invoke(
        cli.main,
        [
            "connect",
            "--server",
            "https://staging.openadapt.ai",
            "--once",
            "--profile",
            "deployment.yaml",
        ],
    )
    assert result.exit_code == 0
    assert calls == [
        [
            "connector",
            "run",
            "--control-plane-url",
            "https://staging.openadapt.ai",
            "--once",
            "--profile",
            "deployment.yaml",
        ]
    ]


def test_connect_subcommand_delegation(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(argv) or 0)

    result = CliRunner().invoke(
        cli.main, ["connect", "--name", "clinic-east", "enroll", "--org-id", "org-1"]
    )
    assert result.exit_code == 0
    assert calls == [
        [
            "connector",
            "enroll",
            "--control-plane-url",
            "https://app.openadapt.ai",
            "--name",
            "clinic-east",
            "--org-id",
            "org-1",
        ]
    ]


def test_connect_print_service_outputs_templates_without_leaking_token():
    runner = CliRunner()
    for target, expected in (
        ("windows", "openadapt flow connector service install"),
        ("systemd", "[Service]"),
        ("docker", "docker run -d"),
    ):
        result = runner.invoke(
            cli.main,
            [
                "connect",
                "--print-service",
                target,
                "--server",
                "https://app.openadapt.ai",
                "--name",
                "runner-1",
                "--token",
                "super-secret-value",
            ],
        )
        assert result.exit_code == 0
        assert expected in result.stdout
        assert "super-secret-value" not in result.stdout


def test_connect_missing_openadapt_flow_reports_clean_error(monkeypatch):
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openadapt_flow" or name.startswith("openadapt_flow."):
            raise ModuleNotFoundError("No module named 'openadapt_flow'")
        return real_import(name, *args, **kwargs)

    for mod in list(sys.modules):
        if mod == "openadapt_flow" or mod.startswith("openadapt_flow."):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = CliRunner().invoke(cli.main, ["connect", "--token", "tok-123"])
    assert result.exit_code != 0
    assert "openadapt-flow" in result.output.lower()
