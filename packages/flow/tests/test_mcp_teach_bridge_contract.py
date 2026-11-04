"""Every teach bridge takes the MCP layer's ``hold_needed(arguments, tool=...)``.

``build_server`` duck-types its ``teach`` bridge. When the MCP layer started
passing ``tool=``, one bridge lacked the keyword, and only an e2e test found
the TypeError. This builds each real bridge into the real server, calls its
teach tools through an in-memory MCP client, and checks that ``hold_needed``
was called with ``tool=`` and that no call failed inside the server.
No app, browser or model is used.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow.agent.browser_teach import BrowserTeachBridge  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.agent.teach import TeachBridge, TeachConfig  # noqa: E402
from openadapt_flow.agent.verify_tools import VerifyBridge  # noqa: E402
from openadapt_flow.guard_demo import verify_yaml  # noqa: E402

BASE = "http://127.0.0.1:9"
# The MCP layer's answer when a call raised something other than BridgeError,
# such as a TypeError from a hold_needed without ``tool``.
FAILED = "Local tool execution failed safely"
# Schema-valid arguments; each call fails cleanly because no run is going.
ARGS = {
    "teach_act": {"action": "click", "ref": "e1"},
    "teach_run": {"script": "tree"},
}


def _teach(tmp_path: Path) -> TeachBridge:
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n  kind: rest\n"
        f"  base_url: {BASE}\n  records_path: /api/db\n  records_key: records\n"
    )
    return TeachBridge(
        TeachConfig(verify=verify, cache_dir=tmp_path / "teach", url=f"{BASE}/")
    )


def _browser(tmp_path: Path) -> BrowserTeachBridge:
    return BrowserTeachBridge(tmp_path / "tools", tmp_path / "files")


def _verify(tmp_path: Path) -> VerifyBridge:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "referral.yaml").write_text(yaml.safe_dump(verify_yaml(BASE)))
    return VerifyBridge(contracts, tmp_path / "state")


BRIDGES = {"teach": _teach, "browser_teach": _browser, "verify": _verify}


async def _calls(server: Any, names: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            async with ClientSession(*client) as session:
                await session.initialize()
                listed = {tool.name for tool in (await session.list_tools()).tools}
                for name in names:
                    if name not in listed:
                        continue
                    result = await session.call_tool(name, ARGS[name])
                    out[name] = json.loads(result.content[0].text)
            tasks.cancel_scope.cancel()
    return out


@pytest.mark.parametrize("kind", sorted(BRIDGES))
def test_hold_needed_takes_the_tool_name(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = BRIDGES[kind](tmp_path)
    # The signature the MCP layer calls, for both tool names.
    for tool in ARGS:
        inspect.signature(bridge.hold_needed).bind({}, tool=tool)

    # Record the real method's calls; it still runs as written.
    seen: list[dict[str, Any]] = []
    real = bridge.hold_needed

    def recording(arguments: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(arguments, **kwargs)

    monkeypatch.setattr(bridge, "hold_needed", recording)
    server = build_server(teach=bridge)
    results = anyio.run(_calls, server, list(ARGS))

    for name, payload in results.items():
        assert FAILED not in str(payload.get("error")), (name, payload)
    held = [name for name in ARGS if name in results and bridge.handles(name)]
    assert seen == [{"tool": name} for name in held]
    if kind == "teach":
        # The web teach bridge offers both, and the MCP layer asks it to hold both.
        assert held == ["teach_act", "teach_run"]
    elif kind == "browser_teach":
        assert held == ["teach_act"] and "teach_run" not in results
    else:
        assert results == {}
