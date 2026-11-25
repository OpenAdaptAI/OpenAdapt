"""Tests for openadapt-kernel wiring in the openadapt launcher and package."""

from __future__ import annotations

import builtins
import types
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import openadapt
from openadapt import cli


def test_kernel_cli_works_on_python_314_without_flow_python_cap(monkeypatch):
    calls: list[list[str]] = []

    def fake_kernel_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(cli, "_python_minor", lambda: (3, 14))
    monkeypatch.setattr(
        cli, "import_module", lambda name: SimpleNamespace(main=fake_kernel_main)
    )
    result = CliRunner().invoke(cli.main, ["kernel", "serve-mcp"])
    assert result.exit_code == 0, result.output
    assert calls == [["serve-mcp"]]


def test_kernel_cli_missing_package_prints_install_hint(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(name="openadapt_kernel")

    monkeypatch.setattr(cli, "import_module", missing)
    result = CliRunner().invoke(cli.main, ["kernel", "run"])
    assert result.exit_code == 2
    assert "openadapt-kernel is not installed" in result.output


def test_openadapt_lazy_exports_kernel_symbols(monkeypatch):
    sentinel = object()
    fake_mod = types.ModuleType("openadapt_kernel")
    for name in (
        "CommitInterceptor",
        "ObligationLedger",
        "PortalPack",
        "ShadowObserver",
        "run_portal_pack",
    ):
        setattr(fake_mod, name, sentinel)
    monkeypatch.setitem(__import__("sys").modules, "openadapt_kernel", fake_mod)
    assert openadapt.ObligationLedger is sentinel
    assert openadapt.CommitInterceptor is sentinel
    assert openadapt.PortalPack is sentinel
    assert openadapt.ShadowObserver is sentinel
    assert openadapt.run_portal_pack is sentinel


def test_openadapt_lazy_export_missing_kernel_raises_helpful_error(monkeypatch):
    monkeypatch.delitem(__import__("sys").modules, "openadapt_kernel", raising=False)
    real_import = builtins.__import__

    def block_kernel(name, *args, **kwargs):
        if name == "openadapt_kernel" or name.startswith("openadapt_kernel."):
            raise ImportError("No module named 'openadapt_kernel'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_kernel)
    with pytest.raises(ImportError, match="requires openadapt-kernel"):
        getattr(openadapt, "ObligationLedger")


def test_legacy_plan_and_proxy_help_point_to_openadapt_kernel(monkeypatch):
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    for cmd in ("plan", "proxy"):
        help_text = cli.main.commands[cmd].help or ""
        assert "openadapt kernel" in help_text
