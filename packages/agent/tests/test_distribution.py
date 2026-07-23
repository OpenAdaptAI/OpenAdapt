"""Guard the distribution artifacts (server.json / smithery.yaml / llms.txt).

These files are how the package is listed in MCP registries. The tests
pin them to the package's real identity so a version bump or a rename
cannot silently desync the registry manifests, and they encode the
security-relevant invariant that a registry-launched server is PHI-safe and
read-only by default. Workflow execution and attended decisions require
separate operator opt-ins.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from openadapt_agent import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = REPO_ROOT / "server.json"
SMITHERY_YAML = REPO_ROOT / "smithery.yaml"
LLMS_TXT = REPO_ROOT / "llms.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

REVERSE_DNS_NAME = "io.github.OpenAdaptAI/openadapt-agent"
PYPI_NAME = "openadapt-agent"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


def _server_json() -> dict:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))


def test_server_json_is_valid_and_well_formed() -> None:
    doc = _server_json()
    assert doc["$schema"].startswith("https://static.modelcontextprotocol.io/")
    assert doc["name"] == REVERSE_DNS_NAME
    assert doc["repository"]["url"].endswith("/openadapt-agent")
    assert doc["repository"]["source"] == "github"
    packages = doc["packages"]
    assert len(packages) == 1
    pkg = packages[0]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == PYPI_NAME
    assert pkg["runtimeHint"] == "uvx"
    assert pkg["transport"]["type"] == "stdio"


def test_version_is_consistent_everywhere() -> None:
    """server.json (twice), pyproject, and __version__ must not drift."""
    doc = _server_json()
    assert doc["version"] == __version__
    assert doc["packages"][0]["version"] == __version__
    assert _pyproject_version() == __version__


def test_release_workflow_runs_the_complete_archive_boundary() -> None:
    """The exact artifacts handed to publishers get both release guards."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert '- "scripts/check_dist.py"' in workflow
    assert "python scripts/check_release_artifacts.py dist" in workflow
    assert "python scripts/check_dist.py dist/*" in workflow


def test_serve_is_the_subcommand_and_bundles_is_required() -> None:
    args = _server_json()["packages"][0]["packageArguments"]
    positional = [a for a in args if a["type"] == "positional"]
    named = {a["name"]: a for a in args if a["type"] == "named"}
    # The published launch command is `openadapt-agent serve --bundles <dir>`.
    assert any(a.get("value") == "serve" for a in positional)
    assert "--bundles" in named
    assert named["--bundles"]["isRequired"] is True


def test_registry_launch_is_read_only_by_default() -> None:
    """A one-click registry install must NOT auto-enable execution.

    --allow-run is deliberately absent from server.json defaults and false in
    Smithery. Attended decisions have their own false-by-default switch.
    """
    args = _server_json()["packages"][0]["packageArguments"]
    assert not any(
        a.get("name") == "--allow-run" or a.get("value") == "--allow-run"
        for a in args
    )
    smithery = yaml.safe_load(SMITHERY_YAML.read_text(encoding="utf-8"))
    props = smithery["startCommand"]["configSchema"]["properties"]
    assert props["allowRun"]["default"] is False
    assert props["allowAttendedActions"]["default"] is False
    # --allow-run is not forced into the required config.
    assert "allowRun" not in smithery["startCommand"]["configSchema"].get(
        "required", []
    )


def test_bundle_key_is_marked_secret_not_leaked() -> None:
    env = _server_json()["packages"][0].get("environmentVariables", [])
    key = next(e for e in env if e["name"] == "OPENADAPT_BUNDLE_KEY")
    assert key["isSecret"] is True
    assert key.get("isRequired", False) is False


def test_smithery_stdio_command_wires_bundles_and_allow_run() -> None:
    smithery = yaml.safe_load(SMITHERY_YAML.read_text(encoding="utf-8"))
    start = smithery["startCommand"]
    assert start["type"] == "stdio"
    assert "bundlesDir" in start["configSchema"]["required"]
    command_fn = start["commandFunction"]
    assert "openadapt-agent" in command_fn
    assert "--bundles" in command_fn
    assert "--allow-run" in command_fn
    assert "--allow-attended-actions" in command_fn
    assert "--runs-dir" in command_fn
    assert "--config" in command_fn
    assert "--headed" in command_fn
    assert "OPENADAPT_BUNDLE_KEY" in command_fn


def test_llms_txt_lists_the_tool_surface() -> None:
    text = LLMS_TXT.read_text(encoding="utf-8")
    for token in (
        "list_workflows",
        "get_workflow",
        "get_run_report",
        "list_needs_attention",
        "get_attention_item",
        "run_workflow_<opaque-id>",
        "continue_attention",
        "skip_attention",
        "teach_attention",
        "escalate_attention",
        "docs.openadapt.ai",
    ):
        assert token in text
