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


def test_version_flag_matches_installed_metadata():
    """`openadapt --version` must print the real installed distribution
    version (importlib.metadata), never a hardcoded string that can drift
    from pyproject.toml. See openadapt/version.py."""
    from importlib.metadata import version as dist_version

    expected = dist_version("openadapt")

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--version"])
    assert result.exit_code == 0, result.output
    assert expected in result.output
    # And it must not be the old hardcoded value unless that is genuinely
    # the installed version.
    from openadapt.version import __version__

    assert __version__ == expected


def test_distribution_metadata_advertises_beta_lifecycle():
    """Published launcher metadata must match the documented Beta lifecycle."""
    from importlib.metadata import metadata

    classifiers = metadata("openadapt").get_all("Classifier") or []
    lifecycle = [
        classifier
        for classifier in classifiers
        if classifier.startswith("Development Status :: ")
    ]
    assert lifecycle == ["Development Status :: 4 - Beta"]


def test_doctor_lists_flow_as_core_not_extras():
    """`openadapt doctor` must treat openadapt-flow as core and the opt-in
    extras (capture/ml/evals/viewer/...) as optional, never flagging a
    missing extra as a failure."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output

    core_idx = out.index("Core packages")
    optional_idx = out.index("Optional packages")
    assert core_idx < optional_idx

    core_section = out[core_idx:optional_idx]
    optional_section = out[optional_idx:]

    # flow is core.
    assert "openadapt_flow" in core_section
    # The excluded-by-default extras must appear only in the optional
    # section and must not be reported as [MISSING].
    for extra_pkg in (
        "openadapt_capture",
        "openadapt_ml",
        "openadapt_evals",
        "openadapt_viewer",
    ):
        assert extra_pkg in optional_section
        assert extra_pkg not in core_section
    assert "[MISSING]" not in optional_section
    # Optional section tells the user how to install extras.
    assert "pip install openadapt[" in optional_section


# ---------------------------------------------------------------------------
# Flow command (the demonstration compiler — flagship path)
# ---------------------------------------------------------------------------

FLOW_VERBS = {"demo-record", "record", "compile", "replay", "lint", "certify"}


def test_launcher_requires_hosted_flow_release():
    """The base and compatibility extras must not resolve pre-hosted engines."""
    metadata = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()

    assert metadata.count('"openadapt-flow>=1.7.0,<2.0.0"') == 2
    assert metadata.count('"openadapt-flow[privacy]>=1.7.0,<2.0.0"') == 1
    assert "openadapt-flow>=1.6.0" not in metadata


def test_top_level_help_leads_with_flow():
    """`openadapt --help` must list flow before the other commands."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    # Quick Start headline and Commands listing both lead with flow.
    assert "Beta launcher" in result.output
    assert "openadapt flow demo-record" in result.output
    assert "Experimental native GUI capture" in result.output
    assert "Research: evaluate" in result.output
    assert "Research: train" in result.output
    commands_idx = result.output.index("Commands:")
    flow_idx = result.output.index("flow", commands_idx)
    capture_idx = result.output.index("capture", commands_idx)
    assert flow_idx < capture_idx, "flow should be listed before capture"


def test_flow_help_lists_verbs():
    """`openadapt flow --help` lists every mounted verb (no flow install
    needed — click renders help before importing openadapt-flow)."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["flow", "--help"])
    assert result.exit_code == 0, result.output
    for verb in FLOW_VERBS:
        assert verb in result.output, f"'{verb}' missing from `flow --help`"


def test_flow_subcommand_help_renders():
    """Each flow subcommand renders --help without importing flow."""
    runner = CliRunner()
    for verb in FLOW_VERBS:
        result = runner.invoke(cli_main, ["flow", verb, "--help"])
        assert result.exit_code == 0, f"`flow {verb} --help` failed: {result.output}"


def test_flow_missing_shows_install_hint(monkeypatch):
    """When openadapt-flow isn't installed, `openadapt flow <verb>` exits
    nonzero with a pip install hint instead of a traceback."""
    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "openadapt_flow.__main__" or name.startswith("openadapt_flow"):
            raise ImportError("No module named 'openadapt_flow'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.delitem(sys.modules, "openadapt_flow", raising=False)
    monkeypatch.delitem(sys.modules, "openadapt_flow.__main__", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["flow", "compile", "rec", "--out", "b", "--name", "x"])
    assert result.exit_code != 0
    assert "pip install --upgrade openadapt" in result.output
    assert "pip install openadapt-flow" in result.output


def _require_openadapt_flow():
    return pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")


def test_flow_delegates_to_flow_main(monkeypatch):
    """`openadapt flow <verb>` must reconstruct argv and call
    openadapt_flow.__main__.main so behavior matches `openadapt-flow <verb>`."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(flow_main_mod, "main", fake_main)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "compile",
            "my-rec",
            "--out",
            "my-bundle",
            "--name",
            "my-flow",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [["compile", "my-rec", "--out", "my-bundle", "--name", "my-flow"]]


def test_unwrapped_flow_command_delegates_to_engine(monkeypatch):
    captured = {}

    def fake_run(argv):
        captured["argv"] = argv

    monkeypatch.setattr("openadapt.cli._run_flow", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "sanitize",
            "recording",
            "--kind",
            "recording",
            "--out",
            "sanitized",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "sanitize",
        "recording",
        "--kind",
        "recording",
        "--out",
        "sanitized",
    ]


def test_flow_help_lists_delegated_launch_commands():
    result = CliRunner().invoke(cli_main, ["flow", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "run",
        "teach",
        "login",
        "sanitize",
        "review-sanitized",
        "approve-sanitized",
        "validate-hosted",
        "push",
        "report-break",
    ):
        assert command in result.output


def test_flow_replay_argv_reconstruction(monkeypatch):
    """Repeatable and flag options are forwarded verbatim to flow's main."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    calls = []
    monkeypatch.setattr(flow_main_mod, "main", lambda argv: calls.append(list(argv)) or 0)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "replay",
            "bundle",
            "--param",
            "note=hi",
            "--param",
            "id=7",
            "--headed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ["replay", "bundle", "--param", "note=hi", "--param", "id=7", "--headed"]
    ]


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
