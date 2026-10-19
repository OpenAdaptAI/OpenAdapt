"""The MCP teach loop on MockMed, driven by a scripted MCP client.

The client is the real MCP SDK session over its in-memory transport, talking
to the real server. It reads the listings the server returns and picks refs
from them, the way an agent would. No model is called anywhere.

It asserts:

* run 1 is recorded through teach_start / teach_act / teach_finish, passes
  its record check, and the draft report names the steps, the parameter, the
  proposed record check and what still needs a person;
* the agent can't admit before a person confirms the check, and a person
  confirms it in the client's form;
* a server with no admission policy refuses to admit and names the terminal
  command; a policy that does not allow self-admission refuses the recording
  agent before any canary runs;
* under a policy that allows it, the agent admits; the server sends
  tools/list_changed; the program is in list_workflows and has its own tool;
* calling that tool saves a new note, ends done, with zero model calls;
* a server started later lists the taught workflow at once;
* a run 1 whose save lands on the wrong record fails its record check and
  leaves nothing to admit.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import anyio
import pytest

pytest.importorskip("mcp")

import mcp.types as types  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow.agent.bridge import AgentBridge  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.agent.runner import RunnerConfig  # noqa: E402
from openadapt_flow.agent.teach import TeachBridge, TeachConfig  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.tutorial import _http_json, _records  # noqa: E402

TASK = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
NEW_NOTE = "Recheck knee in ten days"
AGENT_ID = "agent:mcp-teach-e2e"
PERSON = "dana.reviewer"


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _reset(base: str):
    return lambda: _http_json(f"{base}/api/reset", method="POST", body={})


def _verify_yaml(tmp_path: Path, base: str) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )
    return path


def _agent(
    key_dir: Path, base: str, monkeypatch: pytest.MonkeyPatch, *, allow_self: bool
) -> Any:
    """The registered agent the operator gives the server, under a policy a
    person signed."""

    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    files = key_dir.parent / "keyfiles"
    signer_pem, agent_pem = files / "signer.pem", files / "agent.pem"
    if not agent_pem.exists():
        for role, pid, pem in (
            ("policy_signer", "dana.signer", signer_pem),
            ("agent", AGENT_ID, agent_pem),
        ):
            authority.register_principal(
                key_dir,
                role=role,
                principal_id=pid,
                public_key_hex=authority.generate_encrypted_key(pem),
            )
    now = datetime.now(timezone.utc)
    policy = authority.AdmissionPolicy(
        policy_id="mockmed-teach" + ("" if allow_self else "-no-self"),
        signer="dana.signer",
        signed_at=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        allowed_hosts=(base.split("://", 1)[1],),
        allowed_actions=("click", "type"),
        allowed_effects=("record_written", "field_equals"),
        min_clean_canaries=2,
        agent_principals=(AGENT_ID,),
        allow_self_admission=allow_self,
    )
    signed = authority.sign_policy(policy, authority.load_encrypted_key(signer_pem))
    return authority.principal_from_key(
        authority.load_encrypted_key(agent_pem), key_dir, policy=signed
    )


def _server(
    tmp_path: Path, base: str, *, admitter: Any = None, fault: str = "ok"
) -> tuple[Any, TeachBridge, AgentBridge]:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    workflows = AgentBridge(
        None, RunnerConfig(runs_dir=runs), allow_run=True, mode="production"
    )
    teach = TeachBridge(
        TeachConfig(
            verify=_verify_yaml(tmp_path, base),
            cache_dir=tmp_path / "teach",
            url=f"{base}/?fault={fault}&idempotency=demo#tasks",
            key_dir=tmp_path / "keys",
            admitter=admitter,
            reset=_reset(base),
        ),
        workflows=workflows,
    )
    return build_server(workflows, teach=teach), teach, workflows


class Client:
    """A scripted MCP client: a person answers forms, an agent picks refs."""

    def __init__(self, session: ClientSession, notes: list[str]) -> None:
        self.session = session
        self.notes = notes
        self.list_changed = False

    async def call(self, name: str, arguments: Optional[dict] = None) -> dict:
        result = await self.session.call_tool(name, arguments or {})
        payload = json.loads(result.content[0].text)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            return {"_error": payload.get("error")}
        return payload

    async def tool_names(self) -> set[str]:
        return {t.name for t in (await self.session.list_tools()).tools}


@asynccontextmanager
async def connect(server: Any, *, person: Optional[str] = PERSON):
    notes: list[str] = []

    async def elicit(context: Any, params: Any) -> types.ElicitResult:
        if person is None:
            return types.ElicitResult(action="decline", content={})
        return types.ElicitResult(
            action="accept", content={"name": person, "confirmed": True}
        )

    async def on_message(message: Any) -> None:
        inner = getattr(message, "root", message)
        if isinstance(inner, types.ToolListChangedNotification):
            notes.append("tools/list_changed")

    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            options = server.create_initialization_options()
            tasks.start_soon(server.run, *transport, options)
            async with ClientSession(
                *client, elicitation_callback=elicit, message_handler=on_message
            ) as session:
                init = await session.initialize()
                client_ = Client(session, notes)
                tools = init.capabilities.tools
                client_.list_changed = bool(
                    tools
                    and (
                        getattr(tools, "list_changed", None)
                        or getattr(tools, "listChanged", None)
                    )
                )
                yield client_
            tasks.cancel_scope.cancel()


def _ref(listing: str, name: str, near: Optional[str] = None) -> str:
    for line in listing.splitlines():
        if near and near not in line:
            continue
        found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
        if found:
            return found.group(1)
    raise AssertionError(f"no {name!r} in listing:\n{listing}")


async def _teach_once(client: Client, patient: str = "Jane Sample") -> dict:
    """What an agent does: start, act on refs from each listing, finish."""

    started = await client.call(
        "teach_start",
        {
            "task": TASK,
            "inputs": {"note": NOTE},
            "record_fields": {"patient_id": "p1", "type": "Triage"},
            "on_screen": ["Jane Sample", "MRN P1"],
            "name": "triage_note",
        },
    )
    assert started.get("status") == "recording", started
    listing = started["observation"]
    for action in (
        {"action": "click", "ref": _ref(listing, "Open", patient)},
        {"action": "click", "name": "New Encounter"},
        {"action": "click", "name": "Triage"},
        {"action": "type", "input": "note", "name": "Note"},
        {"action": "click", "name": "Save Encounter"},
    ):
        if "name" in action:
            label = action.pop("name")
            action = {**action, "ref": _ref(listing, label)}
        result = await client.call("teach_act", action)
        if result.get("status") == "refused":
            break
        assert result.get("status") == "ok", result
        listing = result["observation"]
    return await client.call("teach_finish")


def test_teach_compile_admit_then_call_with_no_model_calls(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        # Run 1, recorded as the registered agent the operator configured,
        # whose policy does not allow self-admission.
        no_self = _agent(tmp_path / "keys", mockmed, monkeypatch, allow_self=False)
        server, teach, _ = _server(tmp_path, mockmed, admitter=no_self)
        async with connect(server) as client:
            assert client.list_changed
            tools = await client.tool_names()
            assert {"teach_start", "teach_act", "teach_finish", "teach_admit"} <= tools
            assert "teach_screen" not in tools  # the operator didn't turn it on

            draft = await _teach_once(client)
            assert draft["status"] == "draft", draft
            assert draft["record_check"]["passed"]
            assert draft["record_check"]["created"] == 1
            assert [g["decision"] for g in draft["save_gate"]] == ["passed_no_hold"]
            assert len(draft["steps"]) >= 5
            assert draft["parameters"]["inputs"] == ["note"]
            assert draft["parameters"]["lifted"] == ["note"]
            (check,) = draft["proposed_record_check"]
            assert check["kind"] == "record_written"
            assert check["match"]["patient_id"] == "p1"
            assert check["match"]["note"] == "{param: note}"
            assert len(draft["needs_person"]) == 1  # confirm the check
            task_key = draft["task_key"]
            admission = (
                teach.config.cache_dir
                / task_key
                / "program"
                / "bundle"
                / "admission.json"
            )

            # The agent can't admit before a person confirms the check.
            early = await client.call(
                "teach_admit",
                {"task_key": task_key, "canary_inputs": [{"note": "Canary"}]},
            )
            assert "confirm the record checks first" in early["_error"], early

            # A person confirms the check in the client's form.
            confirmed = await client.call(
                "teach_confirm_checks",
                {"task_key": task_key, "drop_fields": ["key", "source"]},
            )
            assert confirmed.get("confirmed_by") == PERSON, confirmed
            assert confirmed["record_check_confirmed"]

            # The recording agent may not admit its own draft: no canary runs.
            before = _records(mockmed)
            refused = await client.call(
                "teach_admit",
                {"task_key": task_key, "canary_inputs": [{"note": "Canary"}]},
            )
            assert refused["status"] == "needs_person", refused
            assert "self-admission" in (refused.get("detail") or "")
            assert refused["canaries"] == []
            assert _records(mockmed) == before
            assert not admission.exists()

        # No policy on the server: refused, with the command a person runs.
        server, teach, _ = _server(tmp_path, mockmed)
        async with connect(server) as client:
            refused = await client.call(
                "teach_admit",
                {"task_key": task_key, "canary_inputs": [{"note": "Canary"}]},
            )
            assert refused["status"] == "needs_person"
            assert "compile-agent admit" in " ".join(refused["needs_person"])
            assert not admission.exists()

        # Under a policy that allows self-admission, the agent admits.
        agent = _agent(tmp_path / "keys", mockmed, monkeypatch, allow_self=True)
        server, teach, workflows = _server(tmp_path, mockmed, admitter=agent)
        async with connect(server) as client:
            admitted = await client.call(
                "teach_admit",
                {
                    "task_key": task_key,
                    "canary_inputs": [{"note": "Canary test note"}],
                },
            )
            assert admitted["status"] == "admitted", admitted
            assert admitted["mode"] == "agent_policy"
            assert admitted["admitted_by"] == AGENT_ID
            assert [c["status"] for c in admitted["canaries"]] == ["VERIFIED"] * 2
            assert all(c["model_calls"] == 0 for c in admitted["canaries"])
            assert (Path(admitted["skill"]) / "SKILL.md").is_file()
            tool = admitted["tool"]
            assert client.notes == ["tools/list_changed"]
            assert tool in await client.tool_names()
            listed = await client.call("list_workflows")
            assert [w["name"] for w in listed["workflows"]] == [admitted["workflow"]]

            # Next time: the tool, a new note, no model calls, read back.
            ran = await client.call(
                tool, {"inputs": {"note": NEW_NOTE}, "request_id": "teach-e2e-0001"}
            )
            assert ran["outcome"] == "done", ran
            assert ran["model_calls"] == 0
            rows = _records(mockmed)
            assert [(r["patient_id"], r["type"], r["note"]) for r in rows] == [
                ("p1", "Triage", NEW_NOTE)
            ]

        # A server started later lists it at once.
        server, teach, workflows = _server(tmp_path, mockmed, admitter=agent)
        async with connect(server) as client:
            assert tool in await client.tool_names()
            again = await client.call(
                "teach_start",
                {
                    "task": TASK,
                    "inputs": {"note": NOTE},
                    "record_fields": {"patient_id": "p1", "type": "Triage"},
                    "on_screen": ["Jane Sample", "MRN P1"],
                    "name": "triage_note",
                },
            )
            assert again["started"] is False and again["status"] == "admitted"

    anyio.run(scenario)


def test_a_save_on_the_wrong_record_fails_the_read_back(
    mockmed: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        # MockMed files the note on another patient while the screen shows
        # Jane Sample: the gate passes, the read-back does not.
        server, teach, _ = _server(tmp_path, mockmed, fault="wrong_record")
        async with connect(server) as client:
            result = await _teach_once(client)
            assert result["status"] == "record_check_failed", result
            assert not result["record_check"]["passed"]
            assert "wrong record" in result["detail"]
            assert result["needs_person"] == []
            status = await client.call("teach_status", {"task_key": result["task_key"]})
            assert status["status"] == "no_draft"

    anyio.run(scenario)


def test_the_agent_cannot_confirm_the_check_without_a_person(
    mockmed: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        server, teach, _ = _server(tmp_path, mockmed)
        async with connect(server, person=None) as client:
            draft = await _teach_once(client)
            assert draft["status"] == "draft", draft
            # Extra arguments can't stand in for the person.
            forged = await client.call(
                "teach_confirm_checks",
                {"task_key": draft["task_key"], "person": PERSON},
            )
            assert "_error" in forged
            declined = await client.call(
                "teach_confirm_checks", {"task_key": draft["task_key"]}
            )
            assert "declined" in declined["_error"]
            status = await client.call("teach_status", {"task_key": draft["task_key"]})
            assert status["record_check_confirmed"] is False

    anyio.run(scenario)


def test_a_person_admits_at_a_terminal_and_the_server_lists_it(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """No policy: the agent teaches, a person confirms and admits at a terminal
    (``openadapt-flow compile-agent admit``), and the next server lists it."""

    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent import FirstRunCompiler
    from openadapt_flow.compile_agent.cli import cmd_compile_agent

    async def teach() -> dict:
        server, _, _ = _server(tmp_path, mockmed)
        async with connect(server) as client:
            return await _teach_once(client)

    draft = anyio.run(teach)
    assert draft["status"] == "draft", draft
    command = next(n for n in draft["needs_person"] if "compile-agent admit" in n)
    task_dir = tmp_path / "teach" / draft["task_key"]
    assert str(task_dir) in command

    # The CLI has no reset hook (a real app is never reset); MockMed's demo
    # idempotency key needs one before the canary.
    original = FirstRunCompiler._on_reset

    def reset_first(self: FirstRunCompiler) -> None:
        _reset(mockmed)()
        original(self)

    monkeypatch.setattr(FirstRunCompiler, "_on_reset", reset_first)
    # A person's admission needs a real terminal; pytest has none.
    monkeypatch.setattr("openadapt_flow.jit.lifecycle._has_terminal", lambda: True)
    answers = iter([PERSON, "ok", "ok", "ok"])
    args = build_parser().parse_args(
        [
            "compile-agent",
            "admit",
            str(task_dir),
            "--admit-as",
            PERSON,
            "--drop-field",
            "key",
            "--drop-field",
            "source",
            "--canary-param",
            "note=Canary test note",
            "--json",
        ]
    )
    code = cmd_compile_agent(
        args, ask=lambda _: next(answers), interactive=lambda: True
    )
    out = capsys.readouterr().out
    result = json.loads(out[out.index("\n{") + 1 :])
    assert code == 0 and result["status"] == "admitted", result
    assert result["admission"]["mode"] == "person"

    async def call() -> None:
        server, teach_bridge, _ = _server(tmp_path, mockmed)
        async with connect(server) as client:
            (tool,) = [t for t in await client.tool_names() if t.startswith("taught_")]
            ran = await client.call(
                tool, {"inputs": {"note": NEW_NOTE}, "request_id": "teach-e2e-0002"}
            )
            assert ran["outcome"] == "done" and ran["model_calls"] == 0, ran

    anyio.run(call)
