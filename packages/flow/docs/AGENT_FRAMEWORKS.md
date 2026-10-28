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
4. The system of record is read again, even when the call times out or is
   cancelled after the write may have been sent.

| The agent gets | Meaning |
| --- | --- |
| `"verified": true`, `receipt_sha256` | The system of record shows exactly the declared write. |
| `"verdict": "REFUTED"` | It shows a different write: a wrong value or a duplicate. |
| `"verdict": "RECONCILIATION_REQUIRED"` | It doesn't show the write, or couldn't be read. The write may still land. |
| `"verdict": "NOT_WRITTEN"` | Nothing was written: the store couldn't be read first, a value was missing or differed from a pinned one, or the same write is held. |
| `"not_sent_again": true` | The same write was already made, and its reply was lost to a timeout. This is the first result; the write wasn't sent twice. |

The tool's own return value is in `tool_output`.

## What `verified: true` proves

It proves the system of record shows exactly the write the call declared. It
doesn't prove the agent picked the right record. Values in the agent's tool
call are the agent's own. Every reply lists who declared each value
(`values_declared_by`: `operator` or `agent`), and the receipt binds that
split. A value that names the record or the tenant, such as a patient, an
account or a clinic, is under your control only when you pin it:

```python
verified(save_note, contract="contracts/note.yaml", pinned={"clinic": "north"})
```

## Wrap every tool that can write

`verified()` checks the tool you give it. A tool you don't wrap isn't
checked, and an agent with a shell, a file writer or an HTTP tool can reach
the same system around the wrapper. Give the agent only tools you've
wrapped, or tools that can't write.

Within one process, a refused write is held: the same values are refused
until a person checks the record. A write whose reply was lost to a timeout
isn't sent again: the retry gets the first result. Both are protections
against retries, not guarantees. A new process, or a value spelled
differently, starts fresh.

## Claude Agent SDK

```bash
pip install "openadapt-flow[claude-agent-sdk]"
```

```python
from claude_agent_sdk import create_sdk_mcp_server, tool
from openadapt_flow.integrations.agents.claude_sdk import only, verified

@tool("save_order", "Save a new order.", {"customer": str, "product": str, "units": str})
async def save_order(args):
    ...  # your write
    return {"content": [{"type": "text", "text": "saved"}]}

orders = create_sdk_mcp_server(
    "orders", tools=[verified(save_order, contract="contracts/order.yaml")]
)
options = only({"orders": orders})
```

Claude Code's built-in tools (Bash, Write, WebFetch and the rest) can write
too. `only()` builds `ClaudeAgentOptions` with all of them off and allows only
the servers you pass. Other `ClaudeAgentOptions` fields go in as keywords:
`only({"orders": orders}, model="claude-sonnet-5-5")`.

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

Every CrewAI release so far depends on a chromadb version with open critical
advisories that have no patched release yet
([GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c),
[GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf)).
So `openadapt-flow` doesn't install CrewAI for you. Check those advisories
against your deployment before you install it.

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

## Skill cache: run a browser task once with the agent, then as a program

When your agent does a browser task and the write is verified, the run can
become a program. The next time the same task comes in, the program runs
through MCP instead of the agent, with no model calls.

```python
from openadapt_flow.integrations.agents.skills import FrameworkAgent, SkillCache

def run_agent(task, tools):
    # tools.functions(): observe, click, type_text, press_key, scroll, finish.
    # Wrap them for your framework (function_tool, @tool, StructuredTool) and
    # run your agent on `task`. Return its token usage, or None.
    ...

cache = SkillCache(
    url="https://clinic.example/referrals",
    verify="verify.yaml",                     # reads the system of record
    expect={"patient_id": "p1"},              # the record the task writes
    on_screen=["Jane Sample", "MRN P1"],      # checked before every save
    offer=ask_the_operator,                   # sees the proposed checks
    admitter=operator_key,                    # a registered admitter key
    canary_params=[{"note": "Canary note"}],
)
agent = FrameworkAgent(run_agent, principal_id="agent:intake")

first = cache.run("Save this note for Jane Sample: {note}", {"note": "..."}, agent)
later = cache.run("Save this note for Jane Sample: {note}", {"note": "..."}, agent)
# first.path == "agent"; later.path == "skill", later.model_calls == 0
```

The first run goes through [compile-agent](COMPILE_AGENT.md):

1. The agent works through the tools. They show the page as a structural
   listing and check that the right record is on screen before every save.
2. The system of record must show exactly the record the task names. If it
   doesn't, nothing is compiled.
3. `offer` gets the proposed record checks and returns the name of the
   person who confirmed them, or None to leave the run as a draft.
4. Canary runs with new values, then admission by your admitter key.
5. The program is written out as an MCP server with one tool.

Every later call with the same task runs that tool over MCP. Each save is
read back, and the run gets a receipt. A run that doesn't end verified stops
and goes to the exception inbox. It is never handed back to the agent.
