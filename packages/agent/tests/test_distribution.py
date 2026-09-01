"""Guard the distribution artifacts (server.json / manifest.json / llms.txt).

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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

from openadapt_agent import __version__
from openadapt_agent.copy import IDENTITY_SENTENCE, SKILL_WHEN_TO_USE, THREE_LINE_INSTALL

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = REPO_ROOT / "server.json"
MCPB_MANIFEST = REPO_ROOT / "manifest.json"
LLMS_TXT = REPO_ROOT / "llms.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
UV_LOCK = REPO_ROOT / "uv.lock"

# `uses: owner/repo@<revision>` with the trailing ` # vX.Y.Z` comment stripped.
USES_REVISION = re.compile(r"(?m)^\s*(?:-\s+)?uses:\s+(\S+)@([^\s#]+)")

REVERSE_DNS_NAME = "io.github.OpenAdaptAI/openadapt-agent"
PYPI_NAME = "openadapt-agent"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


def _server_json() -> dict:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))


def _mcpb_manifest() -> dict:
    return json.loads(MCPB_MANIFEST.read_text(encoding="utf-8"))


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
    """Registry, MCPB, package, and runtime versions must not drift."""
    doc = _server_json()
    assert doc["version"] == __version__
    assert doc["packages"][0]["version"] == __version__
    assert _mcpb_manifest()["version"] == __version__
    assert _pyproject_version() == __version__


def test_mcpb_lock_has_one_flow_and_one_opencv_provider() -> None:
    """The one-click MCPB must not install two distributions that own cv2."""
    packages = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]
    names = {package["name"] for package in packages}
    flow_packages = [package for package in packages if package["name"] == "openadapt-flow"]

    assert len(flow_packages) == 1
    assert isinstance(flow_packages[0]["version"], str)
    assert names & {"opencv-python", "opencv-python-headless"} in (
        {"opencv-python"},
        {"opencv-python-headless"},
    )


def test_release_archives_pin_supported_core_metadata() -> None:
    """Hatchling must not silently adopt metadata newer than Twine accepts."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    targets = project["tool"]["hatch"]["build"]["targets"]

    assert targets["wheel"]["core-metadata-version"] == "2.4"
    assert targets["sdist"]["core-metadata-version"] == "2.4"


def test_pypi_readme_proves_mcp_namespace_ownership() -> None:
    """The MCP registry validates the reverse-DNS name through PyPI metadata."""
    assert f"mcp-name: {REVERSE_DNS_NAME}" in README.read_text(encoding="utf-8")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert re.search(r'(?m)^readme\s*=\s*"README\.md"$', pyproject)


def test_release_workflow_runs_the_complete_archive_boundary() -> None:
    """The exact artifacts handed to publishers get both release guards."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert '- "scripts/check_dist.py"' in workflow
    assert "python scripts/check_release_artifacts.py dist" in workflow
    assert "python scripts/check_dist.py dist/*" in workflow


def test_release_actions_are_pinned_to_commits() -> None:
    """Every action on the publish path must be a full 40-char commit SHA.

    A floating ref (``@v7``, or worse ``@release/v1``, which is a *branch*)
    means whoever controls that ref decides what runs with this repo's PyPI
    Trusted Publishing OIDC identity, at a moment nobody is watching. Ported
    from openadapt-tray's test of the same name.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    revisions = [m.group(2) for m in USES_REVISION.finditer(workflow)]
    assert revisions
    unpinned = [rev for rev in revisions if not re.fullmatch(r"[0-9a-f]{40}", rev)]
    assert not unpinned, f"release.yml uses unpinned action revisions: {unpinned}"


def test_every_workflow_action_is_pinned_to_a_commit() -> None:
    """The same rule for the rest of the repo, so release.yml stays the norm.

    Each pin also carries a trailing ``# vX.Y.Z`` comment so the human-readable
    version is never lost to the SHA.
    """
    unpinned: list[str] = []
    uncommented: list[str] = []
    for workflow_file in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = workflow_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = USES_REVISION.match(line)
            if not match:
                continue
            action, revision = match.group(1), match.group(2)
            where = f"{workflow_file.name}: {action}@{revision}"
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                unpinned.append(where)
            elif not re.search(r"#\s*\S", line.split(revision, 1)[1]):
                uncommented.append(where)
    assert not unpinned, f"unpinned action revisions: {unpinned}"
    assert not uncommented, f"pinned actions missing a version comment: {uncommented}"


def test_serve_is_the_subcommand_and_bundles_is_required() -> None:
    args = _server_json()["packages"][0]["packageArguments"]
    positional = [a for a in args if a["type"] == "positional"]
    named = {a["name"]: a for a in args if a["type"] == "named"}
    # The published launch command is `openadapt-agent serve --bundles <dir>`.
    assert any(a.get("value") == "serve" for a in positional)
    assert "--bundles" in named
    assert named["--bundles"]["isRequired"] is True
    assert "--authoring" not in named
    assert not any(a.get("value") == "--authoring" for a in args)
    assert _server_json()["packages"][0]["transport"]["type"] == "stdio"


def test_registry_launch_is_read_only_by_default() -> None:
    """A one-click registry install must NOT auto-enable execution.

    --allow-run is deliberately absent from server.json defaults and false in
    the MCPB. Attended decisions have their own false-by-default switch.
    """
    args = _server_json()["packages"][0]["packageArguments"]
    assert not any(a.get("name") == "--allow-run" or a.get("value") == "--allow-run" for a in args)
    config = _mcpb_manifest()["user_config"]
    assert config["allow_run"]["default"] is False
    assert config["allow_attended_actions"]["default"] is False
    assert config["bundles_dir"]["required"] is True


def test_bundle_key_is_marked_secret_not_leaked() -> None:
    env = _server_json()["packages"][0].get("environmentVariables", [])
    key = next(e for e in env if e["name"] == "OPENADAPT_BUNDLE_KEY")
    assert key["isSecret"] is True
    assert key.get("isRequired", False) is False


def test_mcpb_is_local_uv_and_wires_all_operator_choices() -> None:
    manifest = _mcpb_manifest()
    assert manifest["manifest_version"] == "0.4"
    assert manifest["server"]["type"] == "uv"
    assert manifest["server"]["entry_point"] == "src/openadapt_agent/mcpb_entry.py"
    env = manifest["server"]["mcp_config"]["env"]
    for key in (
        "OPENADAPT_AGENT_BUNDLES_DIR",
        "OPENADAPT_AGENT_RUNS_DIR",
        "OPENADAPT_AGENT_ALLOW_RUN",
        "OPENADAPT_AGENT_ALLOW_ATTENDED_ACTIONS",
        "OPENADAPT_AGENT_DEPLOYMENT_CONFIG",
        "OPENADAPT_AGENT_HEADED",
        "OPENADAPT_AGENT_BUNDLE_KEY",
    ):
        assert key in env
    assert manifest["tools_generated"] is True


def test_llms_txt_lists_the_tool_surface() -> None:
    text = LLMS_TXT.read_text(encoding="utf-8")
    for token in (
        "list_workflows",
        "get_workflow",
        "get_run_report",
        "list_needs_attention",
        "get_attention_item",
        "run_workflow_<opaque-id>",
        "run_local_quickstart",
        "--tutorial",
        "--authoring",
        "observe",
        "start_record",
        "continue_attention",
        "skip_attention",
        "teach_attention",
        "escalate_attention",
        "docs.openadapt.ai",
        IDENTITY_SENTENCE,
    ):
        assert token in text


def _readme_first_paragraph() -> str:
    lines: list[str] = []
    started = False
    for line in README.read_text(encoding="utf-8").splitlines():
        if not started:
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped.startswith("[")
                or stripped.startswith("`")
            ):
                continue
            started = True
        if started:
            if not line.strip():
                break
            lines.append(line.strip())
    return " ".join(lines)


def test_identity_sentence_is_shared() -> None:
    # MCP registry server.json description maxLength is 100.
    assert len(IDENTITY_SENTENCE) <= 100
    assert _readme_first_paragraph() == IDENTITY_SENTENCE
    assert IDENTITY_SENTENCE == _server_json()["description"]
    assert IDENTITY_SENTENCE in LLMS_TXT.read_text(encoding="utf-8")
    skill = REPO_ROOT / "skills" / "openadapt-gui-write" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert f'description: "{IDENTITY_SENTENCE}"' in text
    assert SKILL_WHEN_TO_USE in text
    assert "name: computer-use" not in text.lower()
    assert THREE_LINE_INSTALL in README.read_text(encoding="utf-8")
    assert "serve --tutorial --allow-run" not in THREE_LINE_INSTALL
    assert "openadapt-agent serve --allow-run" in README.read_text(encoding="utf-8")
    assert "serve --authoring" in README.read_text(encoding="utf-8")
    assert "serve --authoring" in LLMS_TXT.read_text(encoding="utf-8")
    assert "authoring connect" in README.read_text(encoding="utf-8")
    assert "authoring connect" in LLMS_TXT.read_text(encoding="utf-8")
    assert "openadapt connect" in README.read_text(encoding="utf-8")
    assert "openadapt quickstart --break-it" in README.read_text(encoding="utf-8")
    assert "If the tool returns unsigned success, treat it as failure" in README.read_text(
        encoding="utf-8"
    )
    assert "Production success without a Seal is failure" in README.read_text(
        encoding="utf-8"
    )
    assert "name: computer-use" not in README.read_text(encoding="utf-8").lower()


def test_mcpb_checker_allows_skill_markdown_not_bundles() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_mcpb", REPO_ROOT / "scripts" / "check_mcpb.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._is_skill_markdown("skills/openadapt-gui-write/SKILL.md")
    assert not module._is_skill_markdown(
        "skills/openadapt-gui-write/bundle/workflow.json"
    )
