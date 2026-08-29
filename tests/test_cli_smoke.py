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
import os
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from openadapt.cli import main as cli_main

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

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


def test_quickstart_runs_one_local_lifecycle_without_overwriting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "first-run"])
        assert result.exit_code == 0, result.output

    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][1] == "--out"
    assert calls[0][3:] == ["--name", "local-quickstart"]
    assert "openadapt-agent serve --allow-run" in result.output
    assert "Frames stay here" in result.output


def test_quickstart_forwards_the_headed_tutorial_option(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main,
            ["quickstart", "--headed", "--out", "headed-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][-1] == "--headed"


def test_quickstart_forwards_the_break_it_option_and_names_the_evidence(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main,
            ["quickstart", "--break-it", "--out", "break-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][-1] == "--break-it"
    assert "run-broken" in result.output
    assert "HALTED" in result.output
    assert "store unchanged" in result.output
    assert "openadapt-agent serve --allow-run" in result.output


def test_quickstart_restores_the_operator_scrub_setting(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    monkeypatch.setattr("openadapt.cli._invoke_flow", lambda _argv: 0)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "first-run"])

    assert result.exit_code == 0, result.output
    assert os.environ["OPENADAPT_FLOW_SCRUB"] == "auto"


def test_quickstart_retains_artifacts_when_the_engine_halts(monkeypatch):
    def halt_after_writing(argv):
        root = Path(argv[argv.index("--out") + 1])
        root.mkdir(parents=True)
        (root / "halt-evidence.json").write_text("{}")
        return 2

    monkeypatch.setattr("openadapt.cli._invoke_flow", halt_after_writing)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "halted-run"])

        assert result.exit_code != 0
        assert Path("halted-run/halt-evidence.json").is_file()


def test_quickstart_refuses_an_existing_output(monkeypatch):
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda _argv: pytest.fail("engine must not run before output refusal"),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("existing").mkdir()
        result = runner.invoke(cli_main, ["quickstart", "--out", "existing"])

    assert result.exit_code != 0
    assert "Output already exists" in result.output


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


def test_distribution_metadata_does_not_publish_a_static_lifecycle():
    """The signed public record, not immutable package metadata, owns maturity."""
    from importlib.metadata import metadata

    classifiers = metadata("openadapt").get_all("Classifier") or []
    lifecycle = [
        classifier
        for classifier in classifiers
        if classifier.startswith("Development Status :: ")
    ]
    assert lifecycle == []


def test_distribution_metadata_matches_engine_python_range():
    """The launcher must fail before resolution on unsupported Python versions."""
    from importlib.metadata import metadata

    actual = metadata("openadapt")["Requires-Python"]
    assert {term.strip() for term in actual.split(",")} == {">=3.10", "<3.13"}


def test_doctor_lists_quickstart_dependencies_as_core_not_extras():
    """`openadapt doctor` must treat Flow and Playwright as base dependencies.

    The other capabilities stay optional and must not cause a failure.
    """
    runner = CliRunner()
    result = runner.invoke(cli_main, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output

    core_idx = out.index("Core packages")
    optional_idx = out.index("Optional packages")
    assert core_idx < optional_idx

    core_section = out[core_idx:optional_idx]
    optional_section = out[optional_idx:]

    # Flow, the agent bridge, and the browser tutorial driver are core.
    assert "openadapt_flow" in core_section
    assert "openadapt_agent" in core_section
    assert "playwright" in core_section
    assert "playwright" not in optional_section
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


def test_launcher_flow_and_substrate_extras_metadata():
    """Install routes resolve the Flow release whose replay parser exposes
    every native/remote backend, without installing OS bindings elsewhere."""
    metadata = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]
    extras = metadata["optional-dependencies"]

    assert (
        metadata["dependencies"].count("openadapt-flow[browser,hosted]>=1.29.0,<2.0.0")
        == 1
    )
    assert metadata["dependencies"].count("openadapt-agent>=2.0.1,<3") == 1
    assert extras["flow"] == ["openadapt-flow>=1.29.0,<2.0.0"]
    assert extras["agent"] == ["openadapt-agent>=2.0.1,<3"]
    assert extras["browser"] == ["openadapt-flow[browser]>=1.29.0,<2.0.0"]
    assert extras["privacy"] == ["openadapt-flow[privacy]>=1.29.0,<2.0.0"]
    assert extras["capture"] == [
        "openadapt-capture>=1.2.0,<2.0.0",
        "openadapt-flow[capture]>=1.29.0,<2.0.0",
    ]
    assert extras["windows"] == ["openadapt-flow[windows]>=1.29.0,<2.0.0"]
    assert extras["macos"] == [
        "openadapt-flow[macos]>=1.29.0,<2.0.0; sys_platform == 'darwin'"
    ]
    assert extras["linux"] == [
        "openadapt-flow[linux]>=1.29.0,<2.0.0; sys_platform == 'linux'"
    ]
    assert extras["rdp"] == ["openadapt-flow[rdp]>=1.29.0,<2.0.0"]
    assert extras["all"] == [
        "openadapt[browser,core,grounding,retrieval,privacy,flow,windows,rdp,agent]",
        "openadapt[macos]; sys_platform == 'darwin'",
        "openadapt[linux]; sys_platform == 'linux'",
    ]


def test_doctor_does_not_require_browser_for_citrix(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "citrix"])
    assert result.exit_code == 0, result.output
    assert "browser support is not required" in result.output
    assert "no Playwright or Chromium setup will run" in result.output


def test_doctor_rdp_fails_without_transport_dependency(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[MISSING] rdp" in result.output
    assert "python -m pip install 'openadapt[rdp]'" in result.output
    assert "System check failed" in result.output


def test_doctor_rdp_reports_transport_ready(monkeypatch):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in {"aardwolf", "openadapt_flow"} else None,
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "RDP transport dependency is installed" in result.output


def test_deploy_preflight_composes_existing_flow_interfaces_without_secrets(
    monkeypatch,
):
    """A clean host gets diagnostics plus commands for the existing engine.

    The deployment guide must remain a launcher seam: it does not start a
    second engine, accept a secret value, or replace Flow's rollback path.
    """
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (
            object()
            if name
            in {"openadapt_flow", "aardwolf", "fastapi", "uvicorn", "openadapt_types"}
            else None
        ),
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.29.0")
    result = CliRunner().invoke(
        cli_main,
        ["deploy", "--backend", "rdp", "--secret-ref", "env:BYOC_CONNECTOR_TOKEN"],
    )

    assert result.exit_code == 0, result.output
    assert "Environment fingerprint" in result.output
    assert "env:BYOC_CONNECTOR_TOKEN" in result.output
    assert "dashboard/settings/connectors" in result.output
    assert "connector enroll" not in result.output
    assert "connector run" in result.output
    assert "flow console" in result.output
    assert "flow repair rollback" in result.output
    assert "No service was started" in result.output


def test_deploy_base_hosted_install_gives_conditional_console_setup(monkeypatch):
    """Base Flow hosted installs must not receive an unusable console command."""
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in {"openadapt_flow", "aardwolf"} else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.29.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "optional local operator console is not installed" in result.output
    assert "python -m pip install 'openadapt-flow[console]==1.29.0'" in result.output
    assert "openadapt flow console --bundles" not in result.output
    assert "Re-run this preflight" in result.output


def test_deploy_console_requires_openadapt_types(monkeypatch):
    """Flow 1.30 imports openadapt-types when the operator console starts."""
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (
            object()
            if name in {"openadapt_flow", "aardwolf", "fastapi", "uvicorn"}
            else None
        ),
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.30.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "optional local operator console is not installed" in result.output
    assert "python -m pip install 'openadapt-flow[console]==1.30.0'" in result.output
    assert "openadapt flow console --bundles" not in result.output


def test_deploy_preflight_refuses_secret_values_and_missing_engine(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)

    runner = CliRunner()
    secret_result = runner.invoke(cli_main, ["deploy", "--secret-ref", "actual-secret"])
    assert secret_result.exit_code != 0
    assert "do not pass secret values" in secret_result.output
    assert "actual-secret" not in secret_result.output
    assert "value hidden" in secret_result.output

    missing_result = runner.invoke(cli_main, ["deploy", "--backend", "windows"])
    assert missing_result.exit_code != 0
    assert "[MISSING]" in missing_result.output
    assert "Preflight failed" in missing_result.output


def test_deploy_preflight_fails_without_web_runtime(monkeypatch):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "openadapt_flow" else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.29.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "web"])

    assert result.exit_code != 0
    assert (
        "[MISSING] the base OpenAdapt install does not contain Playwright"
        in result.output
    )
    assert "Preflight failed" in result.output
    assert "Preflight passed" not in result.output


def test_deploy_preflight_fails_without_rdp_transport(monkeypatch):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "openadapt_flow" else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "1.29.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[MISSING] RDP transport dependency" in result.output
    assert "python -m pip install 'openadapt[rdp]'" in result.output
    assert "Preflight failed" in result.output
    assert "Preflight passed" not in result.output


@pytest.mark.parametrize("flow_version", ["1.28.9", "2.0.0", "2.1.0", "invalid"])
def test_deploy_preflight_fails_for_unsupported_flow(monkeypatch, flow_version):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr("importlib.metadata.version", lambda _name: flow_version)

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[UNSUPPORTED]" in result.output
    assert ">=1.29.0,<2.0.0" in result.output
    assert "Preflight passed" not in result.output


def test_top_level_help_leads_with_flow():
    """`openadapt --help` must list flow before the other commands."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    # Quick Start headline and Commands listing both lead with flow.
    assert "openadapt flow demo-record" in result.output
    assert "openadapt quickstart" in result.output
    assert "openadapt-agent serve --allow-run" in result.output
    assert "effect-verified first run" in result.output
    assert "Standalone local human GUI capture" in result.output
    assert "Research: evaluate" in result.output
    assert "Research: train" in result.output
    commands_idx = result.output.index("Commands:")
    flow_idx = result.output.index("flow", commands_idx)
    capture_idx = result.output.index("capture", commands_idx)
    assert flow_idx < capture_idx, "flow should be listed before capture"


def test_capture_start_refuses_when_recorder_never_becomes_ready(monkeypatch):
    """The compatibility command must not label failed startup as saved."""
    from types import SimpleNamespace

    state = {}

    class NeverReadyRecorder:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            state["exited"] = True
            return False

        def wait_for_ready(self):
            return False

    monkeypatch.setitem(
        sys.modules,
        "openadapt_capture",
        SimpleNamespace(Recorder=NeverReadyRecorder),
    )

    result = CliRunner().invoke(
        cli_main,
        ["capture", "start", "--name", "failed-capture", "--no-video"],
    )

    assert result.exit_code != 0
    assert state == {"exited": True}
    assert "did not become ready" in result.output
    assert "Recording..." not in result.output
    assert "Capture saved" not in result.output


def test_capture_stop_fails_until_capture_has_a_control_channel():
    result = CliRunner().invoke(cli_main, ["capture", "stop"])

    assert result.exit_code != 0
    assert "Ctrl+C in the recorder terminal" in result.output
    assert "authenticated, owner-only local control channel" in result.output
    assert "Stopping active capture session" not in result.output


def test_flow_help_is_current_engine_help():
    """The launcher must not maintain a second, stale Flow command list."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    result = CliRunner().invoke(cli_main, ["flow", "--help"])

    assert result.exit_code == 0, result.output
    assert result.output == flow_main_mod.build_parser().format_help()


@pytest.mark.parametrize(
    ("verb", "expected_options"),
    [
        ("record", ("--backend", "--agent-url", "--task")),
        ("replay", ("--backend", "--config", "--rdp-readiness-text")),
        ("demo-record", ("--record-video", "--note-text", "--param-name")),
        ("compile", ("--accept-params", "--params-from", "--no-confirm-params")),
        ("certify", ("--config", "--policy")),
    ],
)
def test_flow_subcommand_help_is_engine_help(verb, expected_options):
    _require_openadapt_flow()
    result = CliRunner().invoke(cli_main, ["flow", verb, "--help"])
    assert result.exit_code == 0, result.output
    for option in expected_options:
        assert option in result.output, (
            f"{option} missing from `flow {verb} --help`; the launcher is "
            "hiding current engine options"
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["demo-record", "--out", "rec", "--record-video"],
        [
            "compile",
            "rec",
            "--out",
            "bundle",
            "--name",
            "demo",
            "--params-from",
            "params.json",
            "--accept-params",
            "note",
            "--no-confirm-params",
        ],
        ["certify", "bundle", "--config", "deployment.yaml"],
    ],
)
def test_flow_forwards_current_engine_options_verbatim(monkeypatch, argv):
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda value: captured.update(argv=list(value))
    )

    result = CliRunner().invoke(cli_main, ["flow", *argv])

    assert result.exit_code == 0, result.output
    assert captured["argv"] == argv


def test_flow_record_forwards_backend_options(monkeypatch):
    """Regression (launcher <=1.7.0): the explicit record wrapper rejected
    `--backend windows` with "No such option". record must forward every
    engine option verbatim, like run/push."""
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "flow",
            "record",
            "--backend",
            "windows",
            "--agent-url",
            "http://localhost:5001",
            "--out",
            "rec",
            "--task",
            "triage note",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "record",
        "--backend",
        "windows",
        "--agent-url",
        "http://localhost:5001",
        "--out",
        "rec",
        "--task",
        "triage note",
    ]


def test_flow_record_web_invocation_forwards_verbatim(monkeypatch):
    """The pre-existing web invocation shape keeps working unchanged."""
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "flow",
            "record",
            "--url",
            "http://localhost:3000",
            "--out",
            "rec",
            "--secret",
            "password",
            "--param",
            "note",
            "--headless",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "record",
        "--url",
        "http://localhost:3000",
        "--out",
        "rec",
        "--secret",
        "password",
        "--param",
        "note",
        "--headless",
    ]


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
    result = runner.invoke(
        cli_main, ["flow", "compile", "rec", "--out", "b", "--name", "x"]
    )
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
        "connect",
        "login",
        "sanitize",
        "review-sanitized",
        "approve-sanitized",
        "validate-hosted",
        "push",
        "report-break",
    ):
        assert command in result.output


def test_top_level_connect_is_one_command_and_delegates_narrow_pairing(monkeypatch):
    import openadapt_flow.hosted as hosted

    captured = {}
    monkeypatch.setattr(hosted, "connect", object(), raising=False)
    monkeypatch.setattr(
        "openadapt.cli._run_flow",
        lambda argv: captured.update(argv=list(argv)),
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "connect",
            "--pairing",
            "oap_" + "A" * 43,
            "--host",
            "https://app.openadapt.ai",
            "--device-name",
            "Reception PC",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "connect",
        "--pairing",
        "oap_" + "A" * 43,
        "--host",
        "https://app.openadapt.ai",
        "--device-name",
        "Reception PC",
    ]


def test_top_level_connect_preserves_exact_desktop_uri(monkeypatch):
    import openadapt_flow.hosted as hosted

    captured = {}
    uri = (
        "openadapt://connect?pairing=oap_"
        + "A" * 43
        + "&host=https%3A%2F%2Fapp.openadapt.ai"
        + "&destination_kind=openadapt-managed"
    )
    monkeypatch.setattr(hosted, "connect", object(), raising=False)
    monkeypatch.setattr(
        "openadapt.cli._run_flow",
        lambda argv: captured.update(argv=list(argv)),
    )

    result = CliRunner().invoke(
        cli_main,
        ["connect", "--uri", uri, "--device-name", "Reception PC"],
    )

    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "connect",
        "--uri",
        uri,
        "--device-name",
        "Reception PC",
    ]


def test_top_level_connect_requires_exactly_one_fixed_pairing_source():
    runner = CliRunner()
    assert runner.invoke(cli_main, ["connect"]).exit_code != 0
    both = runner.invoke(
        cli_main,
        [
            "connect",
            "--pairing",
            "oap_" + "A" * 43,
            "--uri",
            "openadapt://connect?pairing=x&host=https://app.openadapt.ai",
        ],
    )
    assert both.exit_code != 0
    assert "exactly one" in both.output


def test_flow_replay_argv_reconstruction(monkeypatch):
    """Repeatable and flag options are forwarded verbatim to flow's main."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    calls = []
    monkeypatch.setattr(
        flow_main_mod, "main", lambda argv: calls.append(list(argv)) or 0
    )

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


@pytest.mark.parametrize(
    ("backend", "target_args"),
    [
        ("windows", ["--agent-url", "http://localhost:5001"]),
        (
            "macos",
            ["--macos-app", "TextEdit", "--macos-window-title", "Notes"],
        ),
        (
            "linux",
            [
                "--linux-app",
                "gedit",
                "--linux-window-title",
                "notes.txt",
                "--linux-allow-physical-input",
            ],
        ),
        ("rdp", ["--rdp-host", "rdp.example.test"]),
        (
            "citrix",
            [
                "--rdp-window",
                "Citrix Viewer",
                "--rdp-window-title",
                "Ward A",
                "--rdp-readiness-text",
                "Appointments",
            ],
        ),
    ],
)
def test_flow_replay_forwards_backend_config_and_native_targets(
    monkeypatch, backend, target_args
):
    """Regression: replay must remain an argv-transparent engine command.

    The former Click wrapper rejected every option below before Flow could
    validate the selected deployment and target.
    """
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    args = [
        "flow",
        "replay",
        "bundle",
        "--backend",
        backend,
        *target_args,
        "--config",
        "deployment.yaml",
        "--params-file",
        "params.json",
        "--allow-model-grounding",
    ]

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["replay", *args[2:]]


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
