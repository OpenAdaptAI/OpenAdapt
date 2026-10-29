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


P_JANE = "Open the referral and save a Triage note for Jane Sample: {note}"
P_ALEX = "Open the referral and save a Triage note for Alex Testcase: {note}"


def _for_alex(jane: SkillCache) -> SkillCache:
    common = {k: v for k, v in jane.common.items()}
    common.update(
        expect={"patient_id": "p2", "type": "Triage"},
        on_screen=["Alex Testcase", "MRN P2"],
    )
    return SkillCache(
        **common,
        offer=jane.offer,
        admitter=jane.admitter,
        canary_params=jane.canary_params,
        drop_fields=jane.drop_fields,
        model_key=jane.model_key,
    )


def test_a_skill_never_serves_another_record(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's probe: another patient's task on the same cache."""

    reset = lambda: _http_json(f"{mockmed}/api/reset", method="POST", body={})  # noqa: E731
    reset()
    jane = _cache(tmp_path, mockmed, monkeypatch, [])
    jane.common.pop("task_id")
    first = jane.run(
        P_JANE,
        {"note": "Jane note"},
        _agent(ScriptedChat(patient="Jane Sample", note="Jane note")),
    )
    assert first.status == "admitted", first.detail
    assert jane.server(P_JANE, {"note": "x"}) is not None

    # Alex's task is another task: it goes to the agent and lands on p2.
    reset()
    alex = _for_alex(jane)
    alex_chat = ScriptedChat(patient="Alex Testcase", note="Alex note")
    second = alex.run(P_ALEX, {"note": "Alex note"}, _agent(alex_chat))
    assert second.path == "agent" and alex_chat.calls > 0
    # Its own record check (one new record with patient_id p2) passed.
    assert second.status == "admitted" and second.verified, second.detail
    assert second.record_check["passed"] and second.record_check["created"] == 1

    # Even a program served for Alex's task by a key collision is caught:
    # this task's record check fails, and the program is evicted.
    reset()
    poisoned = _for_alex(jane)
    jane_key = jane.key(P_JANE, {"note": "x"})
    monkeypatch.setattr(poisoned, "key", lambda prompt, params: jane_key)
    served = poisoned.run(
        P_JANE,
        {"note": "Alex again"},
        _agent(ScriptedChat(patient="Alex Testcase", note="x")),
    )
    assert served.path == "skill"
    assert served.replay.get("verified") is True  # its own checks pass on p1
    assert served.verified is False and served.status == "RECORD_CHECK_FAILED"
    assert "wrong record" in (served.detail or "")
    assert served.record_check["created"] == 0
    assert served.record_check["outside_run"]["created"] == 1
    assert jane.server(P_JANE, {"note": "x"}) is None  # evicted
    assert list((tmp_path / "cache" / "evicted").iterdir())


def test_the_cache_is_per_user_by_default(tmp_path: Path) -> None:
    verify = tmp_path / "verify.yaml"
    verify.write_text("verifier: {}\n")
    cache = SkillCache(
        url="http://127.0.0.1:1/",
        verify=verify,
        expect={"patient_id": "p1"},
        on_screen=["Jane Sample"],
        offer=lambda proposal: None,
        canary_params=[{"note": "n"}],
    )
    assert cache.common["cache_dir"] == Path("~/.openadapt/skills").expanduser()
    key = cache.key("Save {note}", {"note": "a"})
    assert set(key) == {
        "instruction",
        "params",
        "expect",
        "on_screen",
        "url",
        "verify_sha256",
        "policy",
        "environment",
    }


def test_another_users_write_elsewhere_does_not_fail_the_skill(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared system has other writers: a note saved on another chart
    while the skill runs is outside the run, not a failure."""

    from openadapt_flow.integrations.agents import skills

    _http_json(f"{mockmed}/api/reset", method="POST", body={})
    cache = _cache(tmp_path, mockmed, monkeypatch, [])
    first = cache.run(
        PROMPT, {"note": NOTE}, _agent(ScriptedChat(patient="Jane Sample", note=NOTE))
    )
    assert first.status == "admitted", first.detail
    _http_json(f"{mockmed}/api/reset", method="POST", body={})

    call_skill = skills._call_skill

    async def with_another_writer(python, server, params):
        _http_json(
            f"{mockmed}/api/encounter?fault=ok",
            method="POST",
            body={"patient_id": "p2", "type": "Triage", "note": "Another user"},
        )
        return await call_skill(python, server, params)

    monkeypatch.setattr(skills, "_call_skill", with_another_writer)
    second = cache.run(
        PROMPT,
        {"note": NEW_NOTE},
        _agent(ScriptedChat(patient="Jane Sample", note=NOTE)),
    )
    assert second.path == "skill" and second.verified, second.to_dict()
    assert second.record_check["created"] == 1
    assert second.record_check["outside_run"]["created"] == 1
    assert cache.server(PROMPT, {"note": NOTE}) is not None  # not evicted
    notes = {(r["patient_id"], r["note"]) for r in _records(mockmed)}
    assert {("p1", NEW_NOTE), ("p2", "Another user")} <= notes
