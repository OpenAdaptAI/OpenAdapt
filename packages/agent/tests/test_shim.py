"""openadapt-agent is a shim over openadapt_flow.agent: same objects, same command."""

import subprocess
import sys

import openadapt_flow.agent as agent


def test_top_level_is_the_agent_package() -> None:
    import openadapt_agent

    assert openadapt_agent is agent


def test_submodules_are_the_same_objects() -> None:
    from openadapt_agent.cli import main

    import openadapt_flow.agent.cli as new_cli

    assert main is new_cli.main
    assert sys.modules["openadapt_agent.cli"] is new_cli


def test_python_dash_m_still_starts() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_agent", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_console_script_entry_point_resolves() -> None:
    from importlib.metadata import entry_points

    (ep,) = [e for e in entry_points(group="console_scripts") if e.name == "openadapt-agent"]
    assert ep.value == "openadapt_agent.cli:main"
    assert ep.load() is agent.cli.main
