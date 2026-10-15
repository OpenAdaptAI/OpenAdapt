from __future__ import annotations

import pytest

from openadapt_agent.mcpb_entry import build_argv


def test_mcpb_defaults_to_local_read_only_server(monkeypatch, tmp_path) -> None:
    from openadapt_agent.runner import default_runs_dir

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENADAPT_AGENT_RUNS_DIR", raising=False)
    argv = build_argv({"OPENADAPT_AGENT_BUNDLES_DIR": "/workflows"})
    assert argv == [
        "serve",
        "--bundles",
        "/workflows",
        "--runs-dir",
        str(default_runs_dir()),
    ]
    # B082: never a folder relative to wherever the client started us.
    runs_dir = argv[argv.index("--runs-dir") + 1]
    assert runs_dir != "runs"
    assert runs_dir.startswith(str(tmp_path / "home"))


def test_mcpb_maps_full_governed_attended_configuration() -> None:
    argv = build_argv(
        {
            "OPENADAPT_AGENT_BUNDLES_DIR": "/workflows",
            "OPENADAPT_AGENT_RUNS_DIR": "/evidence",
            "OPENADAPT_AGENT_ALLOW_RUN": "true",
            "OPENADAPT_AGENT_ALLOW_ATTENDED_ACTIONS": "1",
            "OPENADAPT_AGENT_DEPLOYMENT_CONFIG": "/deployment.yaml",
            "OPENADAPT_AGENT_HEADED": "yes",
        }
    )
    assert argv == [
        "serve",
        "--bundles",
        "/workflows",
        "--runs-dir",
        "/evidence",
        "--allow-run",
        "--allow-attended-actions",
        "--config",
        "/deployment.yaml",
        "--headed",
    ]


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "Choose a workflow bundle directory"),
        (
            {
                "OPENADAPT_AGENT_BUNDLES_DIR": "/workflows",
                "OPENADAPT_AGENT_ALLOW_RUN": "sometimes",
            },
            "OPENADAPT_AGENT_ALLOW_RUN must be true or false",
        ),
    ],
)
def test_mcpb_rejects_incomplete_or_ambiguous_configuration(
    environment: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_argv(environment)


# -- B086: the MCPB passes the certifying policy and ties env to user config ---


def test_mcpb_passes_the_certifying_policy() -> None:
    argv = build_argv(
        {"OPENADAPT_AGENT_BUNDLES_DIR": "/b", "OPENADAPT_AGENT_POLICY": "clinical-write"}
    )
    index = argv.index("--policy")
    assert argv[index + 1] == "clinical-write"
    assert "--policy" not in build_argv(
        {"OPENADAPT_AGENT_BUNDLES_DIR": "/b", "OPENADAPT_AGENT_POLICY": "  "}
    )


def test_every_mcpb_variable_is_wired_to_user_config() -> None:
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    env = manifest["server"]["mcp_config"]["env"]
    user_config = manifest["user_config"]
    source = (root / "src" / "openadapt_agent" / "mcpb_entry.py").read_text(encoding="utf-8")
    read = set(re.findall(r'"(OPENADAPT_AGENT_[A-Z_]+)"', source))
    assert "OPENADAPT_AGENT_POLICY" in read
    for name in read:
        assert name in env, name
        match = re.fullmatch(r"\$\{user_config\.([a-z_]+)\}", env[name])
        assert match is not None, name
        assert match.group(1) in user_config, name
    assert "form elicitation" in user_config["allow_attended_actions"]["description"]
