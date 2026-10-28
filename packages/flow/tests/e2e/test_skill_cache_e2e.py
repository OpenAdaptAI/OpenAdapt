"""Skill cache end to end on MockMed: a LangGraph agent's verified run 1 is
compiled, and the next matching task runs the compiled skill over MCP.

The agent is a real LangGraph graph (``ToolNode``) whose chat model is
scripted: it reads the structural listing in each tool result and asks for
the next tool the way a model would. The browser is headless Chromium, and
the compiled skill is called through its emitted MCP server over stdio.
It asserts:

* run 1 is the agent's, its save is read back from the system of record, a
  person confirms the checks through ``offer``, canaries pass, a registered
  key admits it, and the MCP server is emitted;
* run 2, with a new note, goes through the skill: VERIFIED, zero model
  calls, and the scripted model is not called;
* an agent that opens the wrong patient is refused at the save; nothing is
  saved, compiled or cached.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("mcp")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode, tools_condition  # noqa: E402

from openadapt_flow.integrations.agents.skills import (  # noqa: E402
    FrameworkAgent,
    SkillCache,
)
from openadapt_flow.jit.keys import CustomerModelKey  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.tutorial import _http_json, _records  # noqa: E402

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
NEW_NOTE = "Recheck knee in ten days"
TEST_KEY = CustomerModelKey("anthropic", "env:TEST_ONLY", "not-a-real-key")


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


class ScriptedChat(BaseChatModel):
    """Answers each listing with the tool call a model would make."""

    patient: str
    note: str
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    @staticmethod
    def _ref(listing: str, name: str, near: Optional[str] = None) -> str:
        for line in listing.splitlines():
            if near and near not in line:
                continue
            found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
            if found:
                return found.group(1)
        raise AssertionError(f"no {name!r} in listing:\n{listing}")

    def _next(self, listing: str) -> tuple[str, dict]:
        if not listing or listing == "Finished.":
            return ("observe", {}) if not listing else ("", {})
        if listing.startswith("Stopped:"):
            return "finish", {"summary": "stopped"}
        if "Encounter saved" in listing:
            return "finish", {"summary": "saved"}
        if "# Referral Tasks" in listing:
            return "click", {"ref": self._ref(listing, "Open", self.patient)}
        if "# New Encounter" in listing:
            if not re.search(r'"Triage" \([^)]*selected', listing):
                return "click", {"ref": self._ref(listing, "Triage")}
            if f'value="{self.note}"' not in listing:
                return "type_text", {
                    "text": self.note,
                    "ref": self._ref(listing, "Note"),
                }
            return "click", {"ref": self._ref(listing, "Save Encounter")}
        if "New Encounter" in listing:
            return "click", {"ref": self._ref(listing, "New Encounter")}
        raise AssertionError(f"unexpected listing:\n{listing}")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        last = messages[-1]
        listing = str(last.content) if isinstance(last, ToolMessage) else ""
        if isinstance(last, HumanMessage):
            listing = ""
        name, args = self._next(listing)
        if not name:
            message = AIMessage(content="done")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": f"c{self.calls}"}],
            )
        usage = {"input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020}
        message.usage_metadata = usage
        return ChatResult(generations=[ChatGeneration(message=message)])


def _agent(chat: ScriptedChat) -> FrameworkAgent:
    def run(task: str, tools) -> dict:
        lc_tools = [StructuredTool.from_function(f) for f in tools.functions()]
        bound = chat.bind_tools(lc_tools)
        graph = StateGraph(MessagesState)
        graph.add_node("agent", lambda s: {"messages": [bound.invoke(s["messages"])]})
        graph.add_node("tools", ToolNode(lc_tools))
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", tools_condition, {"tools": "tools", END: END}
        )
        graph.add_edge("tools", "agent")
        state = graph.compile().invoke(
            {"messages": [("user", task)]}, {"recursion_limit": 40}
        )
        return {
            "calls": [
                dict(m.usage_metadata)
                for m in state["messages"]
                if isinstance(m, AIMessage) and m.usage_metadata
            ]
        }

    return FrameworkAgent(run, principal_id="agent:langgraph-e2e", model="scripted")


def _cache(tmp_path: Path, base: str, monkeypatch: pytest.MonkeyPatch, offered: list):
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    key_dir = tmp_path / "keys"
    pem = tmp_path / "keyfiles" / "ci.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:skill-cache",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    admitter = authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )

    def offer(proposal: dict) -> str:
        offered.append(proposal)
        return "dana.reviewer"

    return SkillCache(
        url=f"{base}/?fault=ok&idempotency=demo#tasks",
        verify=verify,
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "MRN P1"],
        offer=offer,
        admitter=admitter,
        canary_params=[{"note": "Canary test note"}],
        drop_fields=["key", "source"],
        task_id="mockmed.skill_cache",
        cache_dir=tmp_path / "cache",
        key_dir=key_dir,
        model_key=TEST_KEY,
        reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
        on_warning=lambda text: None,
    )


def test_run_1_is_compiled_and_run_2_calls_the_skill(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered: list = []
    cache = _cache(tmp_path, mockmed, monkeypatch, offered)
    chat = ScriptedChat(patient="Jane Sample", note=NOTE)
    first = cache.run(PROMPT, {"note": NOTE}, _agent(chat))
    assert first.path == "agent" and first.status == "admitted", first.detail
    assert first.verified and first.model_calls == chat.calls > 0
    assert len(offered) == 1
    assert Path(first.compile["emitted"]["mcp_server"]).is_file()
    calls_after_run1 = chat.calls

    # MockMed's demo idempotency key makes a second note on p1 a duplicate;
    # reset the test app between tasks (the MCP tool has no reset hook).
    _http_json(f"{mockmed}/api/reset", method="POST", body={})
    second = cache.run(PROMPT, {"note": NEW_NOTE}, _agent(chat))
    assert second.path == "skill", second.to_dict()
    assert second.verified and second.status == "VERIFIED", second.to_dict()
    assert second.model_calls == 0 and second.cost_usd == 0.0
    assert second.replay["receipt_outcome"] == "VERIFIED"
    assert chat.calls == calls_after_run1
    notes = [r["note"] for r in _records(mockmed) if r.get("patient_id") == "p1"]
    assert notes[-1] == NEW_NOTE


def test_a_wrong_patient_is_refused_and_not_cached(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _http_json(f"{mockmed}/api/reset", method="POST", body={})
    offered: list = []
    cache = _cache(tmp_path, mockmed, monkeypatch, offered)
    chat = ScriptedChat(patient="Alex Testcase", note=NOTE)
    first = cache.run(PROMPT, {"note": NOTE}, _agent(chat))
    assert first.path == "agent" and first.status == "refused_wrong_record"
    assert not first.verified and offered == []
    assert _records(mockmed) == []
    assert cache.server(PROMPT, {"note": NOTE}) is None
