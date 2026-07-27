from __future__ import annotations

import pytest

from openadapt_agent.mcpb_entry import build_argv


def test_mcpb_defaults_to_local_read_only_server() -> None:
    assert build_argv({"OPENADAPT_AGENT_BUNDLES_DIR": "/workflows"}) == [
        "serve",
        "--bundles",
        "/workflows",
        "--runs-dir",
        "runs",
    ]


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
