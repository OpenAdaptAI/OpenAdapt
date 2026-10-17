"""Delegated approval tools. Made-up values only.

The agent can always read the approval mode. It can answer a waiting save
only when the operator started the server with --delegate-approvals, and
then only through Flow's item-approval command as decided_by automation
from the mcp route, naming the agent. Nothing changes the mode, and no
tool admits a check.
"""

from __future__ import annotations

import dataclasses

import pytest

from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.runner import FlowRunner

REQUEST_ID = "a" * 32
DIGEST = "sha256:" + "b" * 64


class _Runner(FlowRunner):
    def __init__(self, config, reply):
        super().__init__(config)
        self.reply = reply
        self.calls: list[tuple[str, str, list[str]]] = []

    def item_approval(self, verb, run_dir, args):
        self.calls.append((verb, str(run_dir), list(args)))
        return dict(self.reply)


def _bridge(bundles_root, runner_config, *, delegate=None, reply=None):
    config = dataclasses.replace(runner_config, delegate_approver_id=delegate)
    runner = _Runner(config, reply or {"ok": True})
    bridge = AgentBridge(bundles_root, config, allow_run=True, runner=runner)
    return bridge, runner, config


def _names(bridge):
    return {spec.name for spec in bridge.list_tool_specs()}


def test_mode_is_readable_and_answer_tools_are_off_by_default(bundles_root, runner_config):
    bridge, runner, _ = _bridge(bundles_root, runner_config)
    names = _names(bridge)
    assert "get_approval_mode" in names
    assert not names & {"list_approval_requests", "answer_approval_request"}
    view = bridge.dispatch("get_approval_mode", {})
    assert view == {
        "approval_mode": "flow_default",
        "delegated_approvals": False,
        "delegate_id": None,
    }
    with pytest.raises(BridgeError):
        bridge.dispatch(
            "answer_approval_request",
            {"run_id": "r1", "request_id": REQUEST_ID, "request_sha256": DIGEST, "decision": "approve"},
        )
    assert runner.calls == []


def test_no_tool_changes_the_mode_or_admits(bundles_root, runner_config):
    bridge, _, _ = _bridge(bundles_root, runner_config, delegate="agent:intake-bot")
    for name in _names(bridge):
        assert not name.startswith("set_approval")
        assert "admit" not in name


def test_an_answer_goes_to_flow_as_this_agent(bundles_root, runner_config):
    bridge, runner, config = _bridge(
        bundles_root,
        runner_config,
        delegate="agent:intake-bot",
        reply={"ok": True, "request_id": REQUEST_ID, "decision": "approve", "answer_sha256": "x"},
    )
    (config.runs_dir / "r1").mkdir(parents=True)
    result = bridge.dispatch(
        "answer_approval_request",
        {"run_id": "r1", "request_id": REQUEST_ID, "request_sha256": DIGEST, "decision": "approve"},
    )
    assert result["ok"] is True
    verb, _run_dir, args = runner.calls[-1]
    assert verb == "approve"
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["--decided-by"] == "automation"
    assert pairs["--route"] == "mcp"
    assert pairs["--delegate-kind"] == "agent"
    assert pairs["--delegate-id"] == "agent:intake-bot"


def test_flow_refusals_come_back_as_is(bundles_root, runner_config):
    bridge, _, config = _bridge(
        bundles_root,
        runner_config,
        delegate="agent:intake-bot",
        reply={"ok": False, "code": "approver_kind_not_allowed", "message": "m"},
    )
    (config.runs_dir / "r1").mkdir(parents=True)
    result = bridge.dispatch(
        "answer_approval_request",
        {
            "run_id": "r1",
            "request_id": REQUEST_ID,
            "request_sha256": DIGEST,
            "decision": "decline",
            "decline_reason": "wrong_values",
        },
    )
    assert result == {"ok": False, "code": "approver_kind_not_allowed", "message": "m"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"run_id": "r1", "request_id": "nope", "request_sha256": DIGEST, "decision": "approve"},
        {"run_id": "r1", "request_id": REQUEST_ID, "request_sha256": "x", "decision": "approve"},
        {"run_id": "r1", "request_id": REQUEST_ID, "request_sha256": DIGEST, "decision": "decline"},
        {"run_id": "../x", "request_id": REQUEST_ID, "request_sha256": DIGEST, "decision": "approve"},
    ],
)
def test_bad_answers_never_reach_flow(bundles_root, runner_config, arguments):
    bridge, runner, config = _bridge(bundles_root, runner_config, delegate="agent:intake-bot")
    (config.runs_dir / "r1").mkdir(parents=True)
    with pytest.raises(BridgeError):
        bridge.dispatch("answer_approval_request", arguments)
    assert runner.calls == []


def test_request_list_drops_everything_but_the_allow_list(bundles_root, runner_config):
    bridge, _, config = _bridge(
        bundles_root,
        runner_config,
        delegate="agent:intake-bot",
        reply={
            "schema": "openadapt.item-approval-list/v1",
            "run_dir": "/private/path",
            "requests": [
                {
                    "request_id": REQUEST_ID,
                    "request_sha256": DIGEST,
                    "status": "waiting",
                    "step_id": "save",
                    "step_class": "consequential",
                    "step_intent": "Type Zyxw Qopn's DOB",
                    "screenshot": "/private/path/shot.png",
                }
            ],
        },
    )
    (config.runs_dir / "r1").mkdir(parents=True)
    result = bridge.dispatch("list_approval_requests", {"run_id": "r1"})
    assert result["ok"] is True
    (row,) = result["requests"]
    assert set(row) == {"request_id", "request_sha256", "status", "step_id", "step_class"}
    assert "private" not in str(result)
