"""The sign-in tool must never invite an agent to ask for a password.

cd packages/flow/docs/spikes/signin_autofill && python -m pytest -q
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tool_messages  # noqa: E402

# Wording that would invite an agent to collect or enter a secret.
INVITES = re.compile(
    r"password|passcode|credential|username|user name|log ?in details|"
    r"sign-?in details|enter (your|the|their)|type (your|the|their)|provide",
    re.IGNORECASE,
)


def observed_outcomes() -> list[str]:
    """Every failed replay outcome recorded in the spike."""
    outcomes = []
    for path in (HERE / "evidence").glob("*.jsonl"):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if not record.get("ok") and record.get("outcome"):
                outcomes.append(record["outcome"])
    return outcomes


@pytest.mark.parametrize("kind", sorted(tool_messages.FAILURES) + ["unknown"])
def test_failure_messages_never_mention_secrets(kind: str) -> None:
    message = tool_messages.failure_message(kind)
    assert not INVITES.search(message), message
    assert "sign in themselves in their browser" in message


def test_every_observed_outcome_maps_to_a_safe_message() -> None:
    outcomes = observed_outcomes()
    assert outcomes, "evidence run records are missing"
    for outcome in outcomes:
        message = tool_messages.failure_message(tool_messages.classify(outcome))
        assert not INVITES.search(message), (outcome, message)
        # The raw outcome never reaches the agent.
        assert outcome not in message


def test_success_message_never_mentions_secrets() -> None:
    assert not INVITES.search(tool_messages.SIGNED_IN)


def test_description_rules_out_passwords() -> None:
    text = tool_messages.DESCRIPTION
    assert "never accepts or needs a password" in text
    assert "must never ask the person for a password" in text


def test_tool_returns_only_fixed_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("Quartz")
    pytest.importorskip("mcp.server.mcpserver")
    import signin_mcp

    monkeypatch.setattr(signin_mcp, "_log", lambda entry: None)
    monkeypatch.setattr(signin_mcp, "FORCE", None)

    class Raises:
        def __init__(self, *args: object) -> None:
            raise RuntimeError("backend says: please enter your password")

    monkeypatch.setattr(signin_mcp.replay, "Replay", Raises)
    message = signin_mcp.sign_in_spike_app()
    assert message == tool_messages.failure_message("not_ready")

    class Fails:
        def __init__(self, *args: object) -> None:
            pass

        def run(self, *args: object, **kwargs: object):
            return signin_mcp.replay.RunResult(
                ok=False, outcome="refused: type the password into the form"
            )

    monkeypatch.setattr(signin_mcp.replay, "Replay", Fails)
    message = signin_mcp.sign_in_spike_app()
    assert not INVITES.search(message), message
