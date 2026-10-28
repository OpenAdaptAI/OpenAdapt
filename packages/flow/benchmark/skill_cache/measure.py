"""Measure the skill cache: run 1 by a LangGraph agent, runs 2..N by the
compiled skill over MCP.

    PYTHONPATH=. python benchmark/skill_cache/measure.py --app trac --runs 5

The agent is a LangGraph graph (``ToolNode``) on ``ChatAnthropic`` with the
skill cache's browser tools. Run 1 goes through compile-agent: the record
check against the system of record, the proposed checks confirmed through
``offer`` (here, by this script, as the operator), canaries, admission by a
registered admitter key, then the emitted MCP server. Every later run calls
that server's tool over stdio. Each row records the path, verified, model
calls, tokens, cost and wall time, with the host's load average before and
after. Rows go to ``results/<app>/runs.jsonl``; nothing in a row holds a
typed value.

Apps (see README.md for setting each one up):

* ``trac``: Trac 1.6 (BSD) on 127.0.0.1:9580, SQLite. Task: file a ticket.
* ``openemr``: the pinned OpenEMR fixture. Task: register a patient.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5-5"
TRACE: Path | None = None

SYSTEM = (
    "You operate a web application for the user with browser tools. observe "
    "returns the page as a structural listing: one line per heading, text, "
    "table row or control on screen, and controls carry refs such as [e3]. Act with "
    "click, type_text and scroll; each returns the new listing, and refs "
    "change, so use the refs in the latest listing. Before any save, the "
    "harness checks that the right record is on screen. Save by clicking "
    "the form's button. Do exactly the task, "
    "with the values given, and nothing else. When the task is done, or "
    "cannot be done, call finish with a one-line summary."
)

SUMMARIES = [
    "Printer on floor 3 jams on duplex",
    "Wiki search ignores page titles",
    "Nightly backup email arrives twice",
    "Timeline page loads slowly after upgrade",
    "Roadmap shows closed milestones",
    "Attachment upload fails above 2 MB",
    "Login page logo is blurry on retina screens",
    "Report export drops the last column",
]


def trac(run: int) -> dict[str, Any]:
    return {
        "summary": f"{SUMMARIES[run % len(SUMMARIES)]} (ref {secrets.token_hex(3)})",
        "description": f"Seen on build {1000 + run}; steps in the attached log.",
    }


APPS: dict[str, dict[str, Any]] = {
    "trac": {
        "prompt": (
            "Create a new ticket with summary {summary} and description "
            "{description}. Leave every other field as it is."
        ),
        "url": "http://127.0.0.1:9580/newticket",
        "verify": HERE / "apps" / "trac.verify.yaml",
        "expect": {"type": "defect", "status": "new"},
        # A new ticket has no earlier record to show; this names its form.
        "on_screen": ["Summary:"],
        "values": trac,
        # Trac's form carries no idempotency key, which clinical-write
        # requires for a save; permissive still requires the read-back.
        "policy": "permissive",
    },
}


def load_1m() -> float:
    return round(os.getloadavg()[0], 1)


def agent_for(chat: Any) -> Any:
    from langchain_core.messages import AIMessage, SystemMessage
    from langchain_core.tools import StructuredTool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    from openadapt_flow.compile_agent.agents import PRICES_PER_MTOK
    from openadapt_flow.integrations.agents.skills import FrameworkAgent

    def run(task: str, tools: Any) -> dict[str, Any]:
        # Click and type only: a key press compiles to a step whose risk an
        # operator must classify before the program can run.
        lc_tools = [
            StructuredTool.from_function(f)
            for f in tools.functions()
            if f.__name__ != "press_key"
        ]
        bound = chat.bind_tools(lc_tools)
        timings: list[float] = []

        def think(state: dict) -> dict:
            started = time.monotonic()
            reply = bound.invoke([SystemMessage(SYSTEM), *state["messages"]])
            timings.append(time.monotonic() - started)
            return {"messages": [reply]}

        def route(state: dict) -> str:
            if tools.summary is not None or tools.stopped is not None:
                return END
            return "agent"

        graph = StateGraph(MessagesState)
        graph.add_node("agent", think)
        graph.add_node("tools", ToolNode(lc_tools))
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", tools_condition, {"tools": "tools", END: END}
        )
        graph.add_conditional_edges("tools", route, {"agent": "agent", END: END})
        state = graph.compile().invoke(
            {"messages": [("user", task)]}, {"recursion_limit": 60}
        )
        calls = []
        replies = [m for m in state["messages"] if isinstance(m, AIMessage)]
        if TRACE is not None:
            # Private: the agent's tool calls hold the typed values.
            with TRACE.open("a") as handle:
                for reply in replies:
                    handle.write(json.dumps(reply.tool_calls, default=str) + "\n")
        for reply, seconds in zip(replies, timings):
            usage = reply.usage_metadata or {}
            details = usage.get("input_token_details") or {}
            read = int(details.get("cache_read") or 0)
            wrote = int(details.get("cache_creation") or 0)
            calls.append(
                {
                    "input_tokens": int(usage.get("input_tokens") or 0) - read - wrote,
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": wrote,
                    "seconds": round(seconds, 3),
                }
            )
        return {"calls": calls}

    return FrameworkAgent(
        run,
        principal_id="agent:langgraph-measure",
        model=MODEL,
        prices=PRICES_PER_MTOK[MODEL],
    )


def admitter_for(key_dir: Path) -> Any:
    from openadapt_flow.jit import authority

    os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, secrets.token_hex(16))
    pem = key_dir.parent / "keyfiles" / "operator.pem"
    if not pem.exists():
        authority.register_principal(
            key_dir,
            role="admitter_key",
            principal_id="operator:skill-cache-measure",
            public_key_hex=authority.generate_encrypted_key(pem),
        )
    return authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", choices=sorted(APPS), required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--work", type=Path, required=True, help="private state")
    args = parser.parse_args()
    global TRACE
    args.work.mkdir(parents=True, exist_ok=True)
    TRACE = args.work / "agent-tool-calls.jsonl"

    from langchain_anthropic import ChatAnthropic

    from openadapt_flow.integrations.agents.skills import SkillCache
    from openadapt_flow.jit.keys import load_customer_model_key

    app = APPS[args.app]
    out = HERE / "results" / args.app
    out.mkdir(parents=True, exist_ok=True)
    key_dir = args.work / "keys"
    offered: list[str] = []

    def offer(proposal: dict) -> str:
        # The operator reads the proposed checks and confirms them.
        offered.append(json.dumps(proposal, sort_keys=True, default=str)[:200])
        return "operator.measure"

    cache = SkillCache(
        url=app["url"],
        verify=app["verify"],
        expect=app["expect"],
        on_screen=app["on_screen"],
        offer=offer,
        admitter=admitter_for(key_dir),
        canary_params=[app["values"](100), app["values"](101)],
        task_id=f"{args.app}.skill_cache_measure",
        cache_dir=args.work / "cache",
        key_dir=key_dir,
        model_key=load_customer_model_key("anthropic"),
        policy=app.get("policy", "clinical-write"),
        on_warning=lambda text: None,
    )
    chat = ChatAnthropic(model=MODEL, max_tokens=1024)
    agent = agent_for(chat)
    rows = []
    for number in range(1, args.runs + 1):
        values = app["values"](number)
        before = load_1m()
        result = cache.run(app["prompt"], values, agent)
        row = {
            "app": args.app,
            "run": number,
            "path": result.path,
            "status": result.status,
            "verified": result.verified,
            "model_calls": result.model_calls,
            "cost_usd": round(result.cost_usd, 5),
            "seconds": result.seconds,
            "host_load_1m_before": before,
            "host_load_1m_after": load_1m(),
            "detail": result.detail,
        }
        if result.path == "agent":
            run1 = result.compile.get("run1") or {}
            ledger = run1.get("agent") or {}
            row["agent_seconds"] = ledger.get("wall_seconds")
            row["model_seconds"] = ledger.get("model_seconds")
            row["prompt_tokens"] = ledger.get("prompt_tokens")
            row["output_tokens"] = ledger.get("output_tokens")
            row["canaries"] = [
                c.get("status")
                for c in (result.compile.get("admission") or {}).get("canaries", [])
            ]
            row["admission"] = (result.compile.get("admission") or {}).get("mode")
        else:
            row["receipt_outcome"] = result.replay.get("receipt_outcome")
            row["program_seconds"] = result.replay.get("seconds")
        rows.append(row)
        print(json.dumps(row), flush=True)
        with (out / "runs.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        if result.path == "agent" and result.status != "admitted":
            print("run 1 was not admitted; stopping", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
