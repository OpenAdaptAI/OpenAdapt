"""Every Flow command line Desktop builds parses under the Flow it bundles.

The qualification job validates the bundled pin. The current-source unit lane
names its mode explicitly and validates the same checkout's Flow origin and
version before exercising its genuine parser and CLI.
"""

from __future__ import annotations

import os
import subprocess
import sysconfig
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

from scripts import flow_command_contract as contract


@pytest.fixture(scope="module")
def flow_parser():
    try:
        installed = version("openadapt-flow")
    except PackageNotFoundError:
        pytest.skip("the bundled openadapt-flow runtime is not installed (--extra build)")
    from openadapt_desktop.flow_bridge import REQUIRED_FLOW_VERSION

    mode = os.environ.get("OPENADAPT_DESKTOP_FLOW_CONTRACT", "bundled")
    assert mode in {"bundled", "source"}, "unknown Flow command contract mode"
    if mode == "bundled":
        assert installed == REQUIRED_FLOW_VERSION, (
            f"Desktop runs Flow {REQUIRED_FLOW_VERSION} but {installed} is installed"
        )
    else:
        import openadapt_flow

        source = Path(__file__).resolve().parents[2] / "flow"
        expected = source / "openadapt_flow" / "__init__.py"
        assert Path(openadapt_flow.__file__).resolve() == expected.resolve()
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        assert installed == project["project"]["version"]
    from openadapt_flow.__main__ import build_parser

    return build_parser


def test_every_command_desktop_builds_parses(flow_parser, tmp_path) -> None:
    commands = contract.bridge_commands(tmp_path)
    assert len(commands) >= 5
    refused = {}
    for name, argv in commands.items():
        try:
            flow_parser().parse_args(argv)
        except SystemExit as exc:
            refused[name] = (exc.code, argv)
    assert not refused, f"the bundled Flow refuses these Desktop commands: {refused}"


def test_the_answer_lines_carry_the_pin_and_the_pipe(tmp_path) -> None:
    """The recorder really exercises the flags that broke 0.24.0."""
    commands = contract.bridge_commands(tmp_path)
    assert "--config-sha256" in commands["run"]
    assert "--config-sha256" in commands["qualify run-case"]
    signed = commands["item-approval approve, signed in the app"]
    assert {"--approver-root", "--approver-head", "--approver-signer", "stdio"} <= set(signed)
    assert "--approver-key" in commands["item-approval decline, with a key file"]


def test_every_flag_in_the_bridge_is_a_flow_flag(flow_parser) -> None:
    known = contract.parser_option_strings(flow_parser())
    unknown = sorted(contract.bridge_flag_literals() - known)
    assert not unknown, f"flow_bridge.py sends flags the bundled Flow doesn't have: {unknown}"


@pytest.mark.parametrize(
    "flag,probe", sorted(contract.CAPABILITY_PROBED_FLAGS.items())
)
def test_optional_flag_probe_matches_the_actual_installed_cli(
    flow_parser, flag, probe
) -> None:
    from openadapt_desktop.flow_bridge import FlowBridge

    executable = Path(sysconfig.get_path("scripts")) / (
        "openadapt-flow.exe" if os.name == "nt" else "openadapt-flow"
    )
    assert executable.is_file()
    calls = []

    def observe(command, **kwargs):
        calls.append(list(command))
        return subprocess.run(command, **kwargs)

    bridge = FlowBridge(flow_bin=str(executable), runner=observe)
    method, help_args = probe
    supported = getattr(bridge, method)()
    assert supported is (flag in contract.parser_option_strings(flow_parser()))
    assert len(calls) == 1 and calls[0] == [str(executable), *help_args]
    assert getattr(bridge, method)() is supported
    assert len(calls) == 1


def test_missing_no_input_capability_refuses_before_any_replay(
    flow_parser, tmp_path
) -> None:
    from openadapt_desktop.flow_bridge import FlowBridge, FlowNotAvailableError

    executable = Path(sysconfig.get_path("scripts")) / (
        "openadapt-flow.exe" if os.name == "nt" else "openadapt-flow"
    )
    assert executable.is_file()
    calls = []

    def observe(command, **kwargs):
        calls.append(list(command))
        return subprocess.run(command, **kwargs)

    bridge = FlowBridge(flow_bin=str(executable), runner=observe)
    bundle, output = tmp_path / "missing-bundle", tmp_path / "not-started"
    supported = "--stop-before-effect" in contract.parser_option_strings(flow_parser())
    if not supported:
        with pytest.raises(FlowNotAvailableError, match="dry-run refused"):
            bridge.replay(bundle, out_dir=output, stop_before_effect=True)
        assert calls == [[str(executable), "replay", "--help"]]
    else:
        result = bridge.replay(bundle, out_dir=output, stop_before_effect=True)
        assert calls[0] == [str(executable), "replay", "--help"]
        assert len(calls) == 2 and "--stop-before-effect" in calls[1]
        assert result.ok is False and result.returncode != 0
        assert not contract.argparse_refused(result.stderr)
    assert not bundle.exists() and not output.exists()
