from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from test_verification import VALUES, check_local


class BrowserSession:
    """Scripted injection object; no browser or model is created."""


@pytest.mark.parametrize("mode", ["correct", "wrong"])
def test_browser_use_typed_action_boundary(store, tmp_path, mode):
    from openadapt_verify.adapters.browser_use import verified

    session = BrowserSession()

    async def save_action(code: str, quantity: str, browser_session: BrowserSession):
        assert browser_session is session
        return await asyncio.to_thread(store.save, {"code": code, "quantity": quantity})

    store.mode = mode
    checked = verified(
        save_action, contract=store.contract(), workdir=tmp_path / "writes"
    )
    signature = inspect.signature(checked)
    assert list(signature.parameters) == ["code", "quantity", "browser_session"]
    assert signature.parameters["browser_session"].annotation is BrowserSession
    assert signature.return_annotation is str
    result = json.loads(asyncio.run(checked(**VALUES, browser_session=session)))
    assert result["verified"] is (mode == "correct") and store.posts == 1
    if mode == "correct":
        check_local(result)


@pytest.mark.parametrize("mode", ["correct", "wrong"])
def test_stagehand_single_act_boundary(store, tmp_path, mode):
    from openadapt_verify.adapters.stagehand import verified

    class Session:
        async def act(self, *, input, **options):
            assert input == {"description": "scripted save"}
            assert options == {"timeout": 10}
            await asyncio.to_thread(store.save, VALUES)
            return {"success": True, "message": "claimed saved"}

    session = Session()
    original = session.act
    store.mode = mode
    checked = verified(session, contract=store.contract(), workdir=tmp_path / "writes")
    result = asyncio.run(
        checked.act(values=VALUES, input={"description": "scripted save"}, timeout=10)
    )
    assert result["verified"] is (mode == "correct") and store.posts == 1
    assert session.act == original  # no SDK/session mutation
    assert result["tool_output"]["success"] is True
    if mode == "correct":
        check_local(result)


def test_openai_actual_tool_callback_without_provider(store, tmp_path):
    pytest.importorskip("agents")
    from agents import function_tool, set_tracing_disabled
    from agents.tool_context import ToolContext

    from openadapt_verify.adapters.openai_agents import verified

    set_tracing_disabled(True)

    @function_tool
    async def save_item(code: str, quantity: str) -> str:
        """Save the declared item."""
        return await asyncio.to_thread(store.save, {"code": code, "quantity": quantity})

    checked = verified(
        save_item, contract=store.contract(), workdir=tmp_path / "writes"
    )
    args = json.dumps(VALUES)
    context = ToolContext(
        context=None,
        tool_name=checked.name,
        tool_call_id="scripted-1",
        tool_arguments=args,
    )
    result = json.loads(asyncio.run(checked.on_invoke_tool(context, args)))
    assert result["verified"] is True and store.posts == 1
    check_local(result)


def test_claude_actual_tool_callback_without_provider(store, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import tool

    from openadapt_verify.adapters.claude_sdk import verified

    @tool("save_item", "Save the declared item", {"code": str, "quantity": str})
    async def save_item(args):
        await asyncio.to_thread(store.save, args)
        return {"content": [{"type": "text", "text": "claimed saved"}]}

    checked = verified(
        save_item, contract=store.contract(), workdir=tmp_path / "writes"
    )
    output = asyncio.run(checked.handler(VALUES))
    result = json.loads(output["content"][0]["text"])
    assert result["verified"] is True and output["is_error"] is False
    assert store.posts == 1
    check_local(result)
