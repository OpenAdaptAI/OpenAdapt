# Verified writes in agent frameworks

Wrap an agent's write tool in one line. The wrapped tool keeps its name and
arguments. Each call reads the system of record before and after the write,
and the agent gets back a verified receipt or a refusal.

Each call goes through four steps:

1. Your contract (`verify.yaml`, see [VERIFY_WRITES.md](VERIFY_WRITES.md))
   says what must be in the system of record after this kind of write. The
   agent can't change it.
2. Before the write, the system of record is read. If it can't be read, the
   tool doesn't write.
3. Your tool writes, the same way it always did.
4. The system of record is read again. A refused write is held: the same call
   is not made again until a person checks the record.

| The agent gets | Meaning |
| --- | --- |
| `"verified": true`, `receipt_sha256` | The system of record shows exactly the declared write. |
| `"verdict": "REFUTED"` | It shows a different write: a wrong value or a duplicate. |
| `"verdict": "RECONCILIATION_REQUIRED"` | It doesn't show the write, or couldn't be read. The write may still land. |
| `"verdict": "NOT_WRITTEN"` | Nothing was written: the store couldn't be read first, a value was missing or differed from a pinned one, or the same write is held. |

The tool's own return value is in `tool_output`.

## Claude Agent SDK

```bash
pip install "openadapt-flow[claude-agent-sdk]"
```

```python
from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool
from openadapt_flow.integrations.agents.claude_sdk import verified

@tool("save_order", "Save a new order.", {"customer": str, "product": str, "units": str})
async def save_order(args):
    ...  # your write
    return {"content": [{"type": "text", "text": "saved"}]}

orders = create_sdk_mcp_server(
    "orders", tools=[verified(save_order, contract="contracts/order.yaml")]
)
options = ClaudeAgentOptions(mcp_servers={"orders": orders})
```

## OpenAI Agents SDK

```bash
pip install "openadapt-flow[openai-agents]"
```

```python
from agents import Agent, function_tool
from openadapt_flow.integrations.agents.openai_agents import verified

@function_tool
def save_order(customer: str, product: str, units: str) -> str:
    """Save a new order."""
    ...  # your write

agent = Agent(name="orders", tools=[verified(save_order, contract="contracts/order.yaml")])
```

## LangGraph

```bash
pip install "openadapt-flow[langgraph]"
```

```python
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
from openadapt_flow.integrations.agents.langgraph import verified

@tool
def save_order(customer: str, product: str, units: str) -> str:
    """Save a new order."""
    ...  # your write

tools = ToolNode([verified(save_order, contract="contracts/order.yaml")])
```

The wrapped tool is a LangChain `StructuredTool`, so it works anywhere a tool
does: `ToolNode`, `bind_tools` or a prebuilt agent.

## CrewAI

```bash
pip install openadapt-flow crewai
```

```python
from crewai import Agent
from crewai.tools import tool
from openadapt_flow.integrations.agents.crewai import verified

@tool("save_order")
def save_order(customer: str, product: str, units: str) -> str:
    """Save a new order."""
    ...  # your write

clerk = Agent(
    role="Order clerk", goal="Enter orders", backstory="Enters orders from email.",
    tools=[verified(save_order, contract="contracts/order.yaml")],
)
```

## Options

`verified(tool, contract=..., agent_id=..., pinned=..., workdir=...)`, in every
adapter:

- `contract`: a `verify.yaml` path, the same document as a dict, or a
  `WriteContract`.
- `pinned`: values you fix, such as `{"clinic": "north"}`. A call that names a
  different value is refused before anything is written.
- `agent_id`: the agent's name on the receipt.
- `workdir`: where each write's read-back and receipt are kept (default
  `~/.openadapt/verify`, private to the user).

The tool's argument names must match the contract's `{param: ...}` names.
Writes through one wrapped tool run one at a time, so each read-back sees only
its own write.

Without a framework, `WriteGuard` does the same around any function:

```python
from openadapt_flow.integrations.agents import WriteGuard

guard = WriteGuard("contracts/order.yaml", agent_id="orders-script")
reply = guard.run(order, lambda: my_api.create(**order))
```

From an MCP client, use `openadapt verify serve` instead
([VERIFY_WRITES.md](VERIFY_WRITES.md)).
