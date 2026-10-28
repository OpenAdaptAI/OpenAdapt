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
* ``mockmed``: MockMed's fault server (synthetic, in this process). Task: a
  triage note on Jane Sample's referral.
* ``openemr``: the pinned OpenEMR fixture.
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


NOTES = [
    "Recheck knee in ten days",
    "Start physio twice a week",
    "Follow up on the blood panel",
    "Repeat the X-ray in six weeks",
    "Refer to dietitian next visit",
    "Reduce the dose and review in a month",
]


def mockmed(run: int) -> dict[str, Any]:
    return {"note": f"{NOTES[run % len(NOTES)]} (ref {secrets.token_hex(3)})"}


def mockmed_setup(work: Path) -> dict[str, Any]:
    """MockMed's fault server, in this process; verify.yaml reads /api/db."""

    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.tutorial import _http_json

    base, _db, _stop = serve()
    base = base.rstrip("/")
    verify = work / "mockmed.verify.yaml"
    verify.write_text(
        "verifier:\n  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n  records_key: records\n"
        "  timeout_s: 2.0\n  poll_interval_s: 0.05\n"
    )

    def reset() -> None:
        _http_json(f"{base}/api/reset", method="POST", body={})

    def oracle(values: dict[str, Any]) -> bool:
        """Independent of the receipt: the store holds exactly this note on p1."""

        rows = _http_json(f"{base}/api/db")["records"]
        mine = [r for r in rows if r.get("note") == values["note"]]
        return len(mine) == 1 and mine[0].get("patient_id") == "p1"

    return {
        "url": f"{base}/?fault=ok&idempotency=demo#tasks",
        "verify": verify,
        "reset": reset,
        "oracle": oracle,
    }


APPS["mockmed"] = {
    "prompt": (
        "Open the referral for Jane Sample, start a Triage encounter, and save "
        "this note: {note}"
    ),
    "expect": {"patient_id": "p1", "type": "Triage"},
    "on_screen": ["Jane Sample", "MRN P1"],
    "values": mockmed,
    "setup": mockmed_setup,
    "drop_fields": ["key", "source"],
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

    pem = key_dir.parent / "keyfiles" / "operator.pem"
    secret = key_dir.parent / "keyfiles" / "passphrase"
    if authority.KEY_PASSPHRASE_ENV not in os.environ:
        # A rerun on the same work folder (a draft waiting for admission)
        # opens the same key.
        if not secret.exists():
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text(secrets.token_hex(16))
            secret.chmod(0o600)
        os.environ[authority.KEY_PASSPHRASE_ENV] = secret.read_text()
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

    app = dict(APPS[args.app])
    if "setup" in app:
        app.update(app.pop("setup")(args.work))
    reset = app.get("reset")
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
        drop_fields=app.get("drop_fields", ()),
        reset=reset,
        on_warning=lambda text: None,
    )
    chat = ChatAnthropic(model=MODEL, max_tokens=1024)
    agent = agent_for(chat)
    rows = []
    for number in range(1, args.runs + 1):
        values = app["values"](number)
        if reset is not None:
            # A test app: MockMed's demo idempotency key makes a second note
            # on p1 a duplicate, so it is reset before every task.
            reset()
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
        if app.get("oracle") is not None:
            row["store_shows_record"] = app["oracle"](values)
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
