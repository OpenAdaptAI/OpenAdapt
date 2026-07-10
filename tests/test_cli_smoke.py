"""CLI smoke and seam-contract tests.

Issue #999: `openadapt serve` and `openadapt train start` were broken
for months while CI stayed green, because cli.py's imports of
openadapt-ml only execute inside command bodies and the broad
`except ImportError` handlers reported every failure as
"openadapt-ml not installed".

Three layers of defense here:

1. test_every_command_help — walks the whole Click tree and renders
   --help for every command, so any module-level wiring error fails CI.
2. Contract tests — monkeypatch the openadapt-ml entry points and
   assert cli.py calls them the way they're actually shaped today.
3. test_cmd_serve_reads_only_provided_args — parses the installed
   openadapt-ml's cmd_serve and asserts every `args.<attr>` it reads is
   provided by cli.py's Namespace, so the seam can't drift silently in
   either direction.

The openadapt-ml-dependent tests skip when it isn't installed; CI
installs it so they always run there.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from openadapt.cli import main as cli_main

# Namespace attributes cli.py's serve command provides to cmd_serve.
# Keep in sync with openadapt/cli.py::serve.
SERVE_NAMESPACE_ATTRS = {
    "port",
    "benchmark",
    "no_regenerate",
    "start_page",
    "quiet",
    "open",
}


def _iter_commands(group, prefix=()):
    yield prefix, group
    if isinstance(group, click.Group):
        for name, cmd in group.commands.items():
            yield from _iter_commands(cmd, prefix + (name,))


def test_every_command_help():
    """Render --help for every command in the tree."""
    runner = CliRunner()
    failures = []
    for path, _cmd in _iter_commands(cli_main):
        args = list(path) + ["--help"]
        result = runner.invoke(cli_main, args)
        if result.exit_code != 0:
            failures.append(f"{' '.join(args)!r} exited {result.exit_code}")
    assert not failures, "Commands whose --help failed:\n  " + "\n  ".join(failures)


def test_version_command():
    runner = CliRunner()
    result = runner.invoke(cli_main, ["version"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Seam contracts with openadapt-ml
# ---------------------------------------------------------------------------


def _require_openadapt_ml():
    return pytest.importorskip("openadapt_ml", reason="openadapt-ml not installed")


def test_train_start_calls_real_entry_point(monkeypatch, tmp_path):
    """`openadapt train start` must call scripts.train.main with kwargs
    that exist in its signature."""
    _require_openadapt_ml()
    import inspect

    from openadapt_ml.scripts import train as train_module

    real_params = set(inspect.signature(train_module.main).parameters)
    calls = []

    def fake_main(**kwargs):
        unknown = set(kwargs) - real_params
        assert not unknown, (
            f"cli.py passes kwargs {sorted(unknown)} that "
            f"openadapt_ml.scripts.train.main does not accept "
            f"(it takes {sorted(real_params)})"
        )
        calls.append(kwargs)

    monkeypatch.setattr(train_module, "main", fake_main)

    capture_dir = tmp_path / "my-capture"
    capture_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  name: test\n")

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "train",
            "start",
            "--capture",
            str(capture_dir),
            "--config",
            str(config),
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["config_path"] == str(config)
    assert kwargs["capture_path"] == str(capture_dir)
    assert kwargs["open_dashboard"] is False


def test_serve_calls_cmd_serve_with_expected_namespace(monkeypatch):
    """`openadapt serve` must call cmd_serve with the agreed Namespace."""
    _require_openadapt_ml()
    from openadapt_ml.cloud import local as oa_local

    received = []

    def fake_cmd_serve(args):
        received.append(args)
        return 0

    monkeypatch.setattr(oa_local, "cmd_serve", fake_cmd_serve)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["serve", "--port", "8123", "--no-open"])
    assert result.exit_code == 0, result.output
    assert len(received) == 1
    ns = received[0]
    assert ns.port == 8123
    assert ns.open is False  # --no-open passes through to cmd_serve
    for attr in SERVE_NAMESPACE_ATTRS:
        assert hasattr(ns, attr), f"Namespace missing {attr}"


def test_serve_honors_output_directory(monkeypatch, tmp_path):
    """--output must repoint openadapt-ml's TRAINING_OUTPUT."""
    _require_openadapt_ml()
    from openadapt_ml.cloud import local as oa_local

    monkeypatch.setattr(oa_local, "cmd_serve", lambda args: 0)

    runner = CliRunner()
    out = tmp_path / "runs"
    result = runner.invoke(cli_main, ["serve", "--output", str(out), "--no-open"])
    assert result.exit_code == 0, result.output
    assert Path(oa_local.TRAINING_OUTPUT) == out


def test_cmd_serve_reads_only_provided_args():
    """Every `args.<attr>` cmd_serve reads must be in cli.py's Namespace.

    This is the direction the contract can silently drift: openadapt-ml
    adds a new required Namespace attribute and cli.py doesn't provide
    it. Parse the installed cmd_serve and check.
    """
    ml = _require_openadapt_ml()
    local_path = Path(next(iter(ml.__path__))) / "cloud" / "local.py"
    tree = ast.parse(local_path.read_text(encoding="utf-8"))
    cmd_serve = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_serve"
        ),
        None,
    )
    assert cmd_serve is not None, "cmd_serve not found in openadapt-ml"

    args_param = cmd_serve.args.args[0].arg
    read_attrs = {
        node.attr
        for node in ast.walk(cmd_serve)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == args_param
    }
    missing = read_attrs - SERVE_NAMESPACE_ATTRS
    assert not missing, (
        f"openadapt-ml's cmd_serve reads args attributes "
        f"{sorted(missing)} that openadapt's serve command does not "
        f"provide; update openadapt/cli.py (and SERVE_NAMESPACE_ATTRS)"
    )


# ---------------------------------------------------------------------------
# Seam contracts with openadapt-panel
# ---------------------------------------------------------------------------


def _require_openadapt_panel():
    return pytest.importorskip(
        "openadapt_panel", reason="openadapt-panel not installed"
    )


def test_panel_calls_run_panel_with_valid_kwargs(monkeypatch):
    """`openadapt panel` must call run_panel with kwargs that exist in its
    signature (the seam that broke serve/train in #999)."""
    _require_openadapt_panel()
    import inspect

    import openadapt_panel

    real_params = set(inspect.signature(openadapt_panel.run_panel).parameters)
    calls = []

    def fake_run_panel(**kwargs):
        unknown = set(kwargs) - real_params
        assert not unknown, (
            f"cli.py passes kwargs {sorted(unknown)} that "
            f"openadapt_panel.run_panel does not accept "
            f"(it takes {sorted(real_params)})"
        )
        calls.append(kwargs)
        return 0

    # cli.py does `from openadapt_panel import run_panel` at call time, so
    # patching the attribute on the package is enough.
    monkeypatch.setattr(openadapt_panel, "run_panel", fake_run_panel)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["panel", "--port", "9000", "--no-open"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["port"] == 9000
    assert calls[0]["open_browser"] is False


def test_panel_system_report_imports_no_siblings():
    """The panel's system page must inspect state without importing siblings
    (mirrors doctor: openadapt-capture screenshots at import time)."""
    _require_openadapt_panel()
    from openadapt_panel.system import system_report

    sibling_prefixes = (
        "openadapt_capture",
        "openadapt_ml",
        "openadapt_evals",
        "openadapt_viewer",
        "openadapt_grounding",
        "openadapt_retrieval",
        "openadapt_privacy",
    )
    before = set(sys.modules)
    system_report()
    newly = [m for m in set(sys.modules) - before if m.startswith(sibling_prefixes)]
    assert not newly, f"system_report imported sibling packages: {sorted(newly)}"


def test_import_error_messages_not_masked(monkeypatch):
    """Internal ImportErrors must surface the real error, not claim
    openadapt-ml isn't installed."""
    _require_openadapt_ml()

    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "openadapt_ml.cloud" or name.startswith("openadapt_ml.cloud."):
            raise ImportError(
                "cannot import name 'definitely_phantom' from "
                "'openadapt_ml.cloud.local'"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.delitem(sys.modules, "openadapt_ml.cloud.local", raising=False)
    monkeypatch.delitem(sys.modules, "openadapt_ml.cloud", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["serve", "--no-open"])
    assert result.exit_code != 0
    assert "definitely_phantom" in result.output, (
        "The underlying ImportError must appear in the CLI output; "
        f"got: {result.output}"
    )
