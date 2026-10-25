"""Every Flow command line Desktop builds parses under the Flow it bundles.

Needs the bundled Flow runtime (``uv sync --extra build``); the
qualification-contract job runs it. The plain test job skips it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

from scripts import flow_command_contract as contract


@pytest.fixture(scope="module")
def flow_parser():
    try:
        installed = version("openadapt-flow")
    except PackageNotFoundError:
        pytest.skip("the bundled openadapt-flow runtime is not installed (--extra build)")
    from openadapt_desktop.flow_bridge import REQUIRED_FLOW_VERSION

    assert installed == REQUIRED_FLOW_VERSION, (
        f"Desktop runs Flow {REQUIRED_FLOW_VERSION} but {installed} is installed"
    )
    from openadapt_flow.__main__ import build_parser

    return build_parser


def test_every_command_desktop_builds_parses(flow_parser, tmp_path) -> None:
    commands = contract.bridge_commands(tmp_path)
    assert len(commands) >= 6
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
