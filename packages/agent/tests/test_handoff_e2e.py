"""End to end: a program run hands its receipt to another agent over MCP and A2A.

The run goes through the governed runner with Flow's ``openadapt-flow run``
process stubbed (as everywhere in this suite), and then Flow's real receipt
writer (``write_receipt_record``) signs the run's report.json with a
throwaway key, exactly as Flow's run hook does after every run. The A2A side
uses the official ``a2a-sdk`` client against our server over real HTTP on
127.0.0.1.

Covered: a real run's receipt in the A2A artifact and the MCP tool result;
verification of a good, a tampered, an unknown-signer and a swapped-payload
handoff; the A2A and MCP gates refusing the same things; no browser Origin
or foreign Host; the extensions header echo; and a follow-up message that
can't approve a run waiting for a person.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
from conftest import FlowCliStub

pytest.importorskip("a2a.server.routes")
pytest.importorskip("uvicorn")
pytest.importorskip("cryptography")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from openadapt_types import receipt as r  # noqa: E402

import openadapt_agent.runner as runner_mod  # noqa: E402
from openadapt_flow import handoff as h  # noqa: E402
from openadapt_flow.agent import a2a_server  # noqa: E402
from openadapt_flow.agent.bridge import AgentBridge  # noqa: E402
from openadapt_flow.agent.handoff_tools import HandoffTools  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.receipt_record import write_receipt_record  # noqa: E402
from openadapt_flow.seal_keys import SoftwareSigner  # noqa: E402

WRITE_REPORT_RESULTS = [
    {"step_id": "s1", "intent": "Open the patient chart", "ok": True},
    {
        "step_id": "s2",
        "intent": "Type the triage note",
        "ok": True,
        "effect_verified": True,
        "postconditions_ok": True,
    },
]


class ReceiptWritingStub(FlowCliStub):
    """`openadapt-flow run`, stubbed; then Flow's real receipt writer runs."""

    def __init__(self, signer, **kwargs):
        super().__init__(**kwargs)
        self.signer = signer

    def __call__(self, cmd, *args, **kwargs):
        done = super().__call__(cmd, *args, **kwargs)
        run_dir = Path(cmd[cmd.index("--run-dir") + 1])
        if (run_dir / "report.json").is_file():
            write_receipt_record(run_dir, signer=self.signer, flow_version="3.4.0")
        return done


def _registry(*signers):
    return r.parse_registry(
        {
            "schema": r.REGISTRY_SCHEMA,
            "keys": [{**s.registry_entry(), "role": "this_computer"} for s in signers],
        }
    )


@pytest.fixture()
def signer():
    return SoftwareSigner(Ed25519PrivateKey.generate(), "generated")


@pytest.fixture()
def bridge(monkeypatch, bundles_root, runner_config, success_report, signer):
    report = dict(success_report, results=WRITE_REPORT_RESULTS, started_at="2026-11-21T01:02:03Z")
    monkeypatch.setattr(
        runner_mod, "_run_flow_process", ReceiptWritingStub(signer, exit_code=0, report=report)
    )
    bridge = AgentBridge(bundles_root, runner_config, allow_run=True, mode="production")
    bridge.handoffs = HandoffTools(registry=_registry(signer))
    return bridge


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def a2a_url(bridge):
    config = a2a_server.A2AConfig(port=_free_port())
    server = uvicorn.Server(
        uvicorn.Config(
            a2a_server.build_app(bridge, config),
            host=config.host,
            port=config.port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the A2A server did not start")
        time.sleep(0.05)
    try:
        yield config.base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _message(data, *, task_id=None, context_id=None):
    from a2a.helpers.proto_helpers import new_data_part
    from a2a.types import Message, Role

    message = Message(
        role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[new_data_part(data)]
    )
    if task_id:
        message.task_id = task_id
    if context_id:
        message.context_id = context_id
    return message


async def _send(url, data, **ids):
    """Send one message with the official SDK client; return the final task."""
    from a2a.client import ClientConfig, create_client
    from a2a.types import SendMessageRequest

    client = await create_client(url, client_config=ClientConfig(streaming=False))
    try:
        task = None
        async for event in client.send_message(
            SendMessageRequest(message=_message(data, **ids))
        ):
            if event.HasField("task"):
                task = event.task
        assert task is not None
        return task
    finally:
        await client.close()


def _handoff_from(task):
    from google.protobuf.json_format import MessageToDict

    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.HasField("raw") and part.media_type == h.MEDIA_TYPE:
                assert h.EXTENSION_URI in artifact.extensions
                return json.loads(part.raw.decode("utf-8"))
    raise AssertionError(f"no handoff part in {MessageToDict(task)}")


def _result_from(task):
    from google.protobuf.json_format import MessageToDict

    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.HasField("data"):
                return MessageToDict(part.data)
    raise AssertionError("no data part")


def _verdict_from(task):
    return _result_from(task)


def _tampered(doc):
    bad = json.loads(json.dumps(doc))
    statement = json.loads(base64.b64decode(bad["receipt"]["payload"]))
    statement["predicate"]["model_calls"] = 9
    bad["receipt"]["payload"] = base64.b64encode(r.canonical_json(statement).encode()).decode()
    return bad


def test_a2a_agent_card_declares_skills_interfaces_and_the_extension(a2a_url, bridge):
    card = httpx.get(a2a_url + "/.well-known/agent-card.json").json()
    assert card["name"] == "OpenAdapt"
    skills = {s["id"] for s in card["skills"]}
    assert "verify_handoff" in skills and next(iter(bridge.catalog)) in skills
    bindings = {(i["protocolBinding"], i["protocolVersion"]) for i in card["supportedInterfaces"]}
    assert bindings == {("JSONRPC", "1.0"), ("HTTP+JSON", "1.0")}
    (extension,) = card["capabilities"]["extensions"]
    assert extension["uri"] == h.EXTENSION_URI
    assert extension.get("required", False) is False
    assert extension["params"]["handoff_schema"] == h.SCHEMA
    # The card is built from the public projection: no step text, no paths.
    text = json.dumps(card)
    assert "Type the triage note" not in text and str(bridge.bundles_dir) not in text


def test_a2a_run_carries_its_receipt_and_verifies_good_tampered_unknown_and_swapped(
    a2a_url, bridge, signer
):
    workflow = next(iter(bridge.catalog))

    async def scenario():
        task = await _send(
            a2a_url,
            {"skill": workflow, "inputs": {"note": "x"}, "request_id": "ref-a2a-0001"},
        )
        from a2a.types import TaskState

        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        result = _result_from(task)
        assert result["outcome"] == "done"
        handoff = _handoff_from(task)
        assert handoff["schema"] == h.SCHEMA
        assert r.is_receipt_envelope(handoff["receipt"])
        assert "openings" not in json.dumps(handoff["receipt"])

        good = await _send(a2a_url, {"skill": "verify_handoff", "handoff": handoff})
        tampered = await _send(
            a2a_url, {"skill": "verify_handoff", "handoff": _tampered(handoff)}
        )
        restapled = h.make_handoff({"outcome": "done", "amount": 10**6}, handoff["receipt"])
        swapped = await _send(a2a_url, {"skill": "verify_handoff", "handoff": restapled})
        return handoff, _verdict_from(good), _verdict_from(tampered), _verdict_from(swapped)

    handoff, good, tampered, swapped = asyncio.run(scenario())
    assert good["integrity"] == "intact"
    assert good["signer_roles"] == ["this_computer"]
    assert good["outcome"] == "VERIFIED"
    assert good["what_this_proves"][0] == h.WHAT_A_SIGNATURE_PROVES
    assert tampered["integrity"] == "refused" and tampered["reason"] == "bad_signature"
    assert swapped["integrity"] == "intact" and swapped["payload"]["bound"] is False
    # Unknown signer: a receiver that pins other keys refuses the same handoff.
    other = SoftwareSigner(Ed25519PrivateKey.generate(), "generated")
    assert h.verify_handoff(handoff, _registry(other)).reason == "unknown_signer"
    # And the receiver's own check agrees with the server's.
    assert h.verify_handoff(handoff, _registry(signer)).ok


def test_a2a_unknown_signer_through_the_server(a2a_url, bridge, tmp_path):
    other = SoftwareSigner(Ed25519PrivateKey.generate(), "generated")
    run = tmp_path / "elsewhere"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"execution_outcome": "VERIFIED", "results": WRITE_REPORT_RESULTS})
    )
    write_receipt_record(run, signer=other, flow_version="3.4.0")
    doc = h.make_handoff({"outcome": "done"}, h.receipt_for_run(run))

    async def scenario():
        return _verdict_from(await _send(a2a_url, {"skill": "verify_handoff", "handoff": doc}))

    verdict = asyncio.run(scenario())
    assert verdict["integrity"] == "refused" and verdict["reason"] == "unknown_signer"
    assert verdict["signer_lines"] == [r.UNKNOWN_SIGNER_LINE]


def test_a2a_refuses_what_mcp_refuses(a2a_url, bridge):
    workflow = next(iter(bridge.catalog))

    async def scenario():
        from a2a.types import TaskState

        no_id = await _send(a2a_url, {"skill": workflow, "inputs": {"note": "x"}})
        attended = await _send(a2a_url, {"skill": "continue_attention", "attention_id": "a" * 24})
        approve = await _send(
            a2a_url,
            {"skill": "answer_approval_request", "request_id": "x", "approve": True},
        )
        for task in (no_id, attended, approve):
            assert task.status.state == TaskState.TASK_STATE_FAILED
        return [t.status.message.parts[0].text for t in (no_id, attended, approve)]

    no_id, attended, approve = asyncio.run(scenario())
    # The same schema refusal text MCP returns; nothing ran.
    assert "request_id" in no_id and "Nothing ran" in no_id
    # Attended and approval tools are never workflow names, so they are
    # refused as unknown workflows by the same run_workflow schema.
    assert "Nothing ran" in attended and "Nothing ran" in approve
    records = list((Path(bridge.runner_config.runs_dir) / "openadapt-agent").rglob("*.json"))
    assert not [p for p in records if "run-" in p.name]


def test_a2a_refuses_browsers_and_foreign_hosts_and_echoes_extensions(a2a_url):
    card = a2a_url + "/.well-known/agent-card.json"
    assert httpx.get(card, headers={"Origin": "https://evil.example"}).status_code == 403
    assert httpx.get(card, headers={"Host": "evil.example"}).status_code == 421
    echoed = httpx.get(card, headers={"A2A-Extensions": h.EXTENSION_URI})
    assert echoed.headers.get("a2a-extensions") == h.EXTENSION_URI
    assert "a2a-extensions" not in httpx.get(card).headers


def test_a2a_off_loopback_needs_a_token_and_hides_workflows(bridge, tmp_path):
    with pytest.raises(ValueError, match="needs --a2a-token-file"):
        a2a_server.A2AConfig(host="0.0.0.0")
    config = a2a_server.A2AConfig(host="0.0.0.0", port=_free_port(), token="t" * 40)
    public = a2a_server.build_agent_card(bridge, config)
    assert [s.id for s in public.skills] == ["verify_handoff"]
    assert public.capabilities.extended_agent_card is True
    assert "bearer" in public.security_schemes
    extended = a2a_server.build_agent_card(bridge, config, extended=True)
    assert next(iter(bridge.catalog)) in {s.id for s in extended.skills}

    from starlette.testclient import TestClient

    client = TestClient(a2a_server.build_app(bridge, config), base_url=f"http://0.0.0.0:{config.port}")
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetExtendedAgentCard", "params": {}}
    assert client.post("/a2a/jsonrpc", json=body).status_code == 401
    wrong = client.post("/a2a/jsonrpc", json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    ok = client.post(
        "/a2a/jsonrpc",
        json=body,
        headers={"Authorization": "Bearer " + "t" * 40, "A2A-Version": "1.0"},
    )
    assert ok.status_code == 200


def test_a2a_follow_up_cannot_approve_a_run_waiting_for_a_person(
    monkeypatch, bundles_root, runner_config, halt_report, signer
):
    monkeypatch.setattr(
        runner_mod, "_run_flow_process", ReceiptWritingStub(signer, exit_code=1, report=halt_report)
    )
    bridge = AgentBridge(bundles_root, runner_config, allow_run=True, mode="production")
    bridge.handoffs = HandoffTools(registry=_registry(signer))
    executor = a2a_server.OpenAdaptExecutor(bridge)
    result = a2a_server.call_remote_tool(
        bridge,
        "run_workflow",
        {"workflow": next(iter(bridge.catalog)), "inputs": {"note": "x"}, "request_id": "ref-hold-1"},
    )
    assert result["outcome"] == "needs_review"
    assert a2a_server.task_state_for(result["outcome"]) == "TASK_STATE_INPUT_REQUIRED"
    # A message on that task is mapped to get_run, whatever it says.
    executor.runs["task-1"] = result["run_id"]
    calls = []
    monkeypatch.setattr(
        a2a_server, "call_remote_tool", lambda b, name, args: calls.append((name, args)) or result
    )

    class Queue:
        events: list = []

        async def enqueue_event(self, event):
            self.events.append(event)

    class Context:
        task_id, context_id = "task-1", "ctx-1"
        current_task = object()
        message = _message({"skill": "answer_approval_request", "approve": True})

    asyncio.run(executor.execute(Context(), Queue()))
    assert [name for name, _ in calls if name != "get_handoff"] == ["get_run"]
    assert calls[0][1] == {"run_id": result["run_id"]}


def test_only_done_is_completed():
    assert a2a_server.task_state_for("done") == "TASK_STATE_COMPLETED"
    for outcome in ("not_sure_if_saved", "did_not_run", None, "anything"):
        assert a2a_server.task_state_for(outcome) == "TASK_STATE_FAILED"


def test_mcp_run_get_handoff_and_verify(bridge, signer, mcp_client):
    server = build_server(bridge)
    workflow = next(iter(bridge.catalog))

    async def scenario():
        async with mcp_client(server) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"get_handoff", "verify_handoff"} <= tools
            run = await client.call_tool(
                "run_workflow",
                {"workflow": workflow, "inputs": {"note": "x"}, "request_id": "ref-mcp-0001"},
            )
            run_id = json.loads(run.content[0].text)["run_id"]
            got = await client.call_tool("get_handoff", {"run_id": run_id})
            handoff = json.loads(got.content[0].text)
            good = await client.call_tool("verify_handoff", {"handoff": handoff})
            bad = await client.call_tool("verify_handoff", {"handoff": _tampered(handoff)})
            return handoff, json.loads(good.content[0].text), json.loads(bad.content[0].text)

    handoff, good, bad = asyncio.run(scenario())
    assert handoff["payload"]["content"]["outcome"] == "done"
    assert good["integrity"] == "intact" and good["signer_roles"] == ["this_computer"]
    assert bad["reason"] == "bad_signature"
    # The same handoff verifies the same way outside the server.
    assert h.verify_handoff(handoff, _registry(signer)).ok


def test_a2a_refuses_flags_that_need_a_person_or_export_protected_data(bundles_root, capsys):
    from openadapt_flow.agent.cli import main

    for flag in (["--allow-protected-export"], ["--authoring"]):
        assert main(["serve", "--bundles", str(bundles_root), "--a2a", *flag]) == 2
    err = capsys.readouterr().err
    assert "--allow-protected-export stays on local MCP stdio" in err
    assert "--authoring needs a person" in err
    assert main(["serve", "--a2a-token-file", "x"]) == 2


def test_handoff_tools_are_off_unless_asked(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config, allow_run=True, mode="production")
    names = {s.name for s in bridge.list_tool_specs()}
    assert not names & {"get_handoff", "verify_handoff"}


def test_two_party_twenty_handoffs_receiver_reads_its_own_record(a2a_url, bridge, signer):
    """The memo's kill criterion, measured.

    Party A (OpenAdapt over A2A) runs a program 20 times and hands party B a
    claim about each record with the run's receipt, POSTed to B's inbox. B
    pins A's key and reads its own record store (SQLite here) for every
    claim. 10 claims are true, 5 are fabricated (no such record) and 5
    differ from B's record; all 20 carry a valid receipt from a trusted
    signer, so the signature alone can't tell them apart. Pass: B accepts
    zero fabricated or differing handoffs, rejects zero true ones, and its
    check adds at most 2 s per handoff.
    """
    import sqlite3
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from openadapt_flow.cli_handoff import post_handoff

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("create table referral (ref text primary key, note text)")
    kinds = ["true"] * 10 + ["fabricated"] * 5 + ["differing"] * 5
    workflow = next(iter(bridge.catalog))

    def own_record(claim):
        row = db.execute("select note from referral where ref = ?", (claim["ref"],)).fetchone()
        if row is None:
            return h.ReceiverRecord("not_found")
        if row[0] != claim["fields"]["note"]:
            return h.ReceiverRecord("recorded_differently", ("note",))
        return h.ReceiverRecord("recorded_as_intended")

    # B pins A's key under A's name, never as "this computer".
    registry = r.parse_registry(
        {
            "schema": r.REGISTRY_SCHEMA,
            "keys": [{**signer.registry_entry(), "role": "customer_key", "org": "Party A"}],
        }
    )
    signer_lines: set[tuple[str, ...]] = set()
    decisions: list[tuple[str, str, float]] = []

    class Inbox(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            started = time.perf_counter()
            verdict = h.decide(body, registry, own_record)
            spent = time.perf_counter() - started
            kind = json.loads(body)["payload"]["content"]["kind"]
            decisions.append((kind, verdict.decision, spent))
            signer_lines.add(tuple(verdict.signer_lines))
            self.send_response(202)
            self.end_headers()

    inbox = HTTPServer(("127.0.0.1", 0), Inbox)
    thread = threading.Thread(target=inbox.serve_forever, daemon=True)
    thread.start()

    async def runs():
        out = []
        for i, kind in enumerate(kinds):
            task = await _send(
                a2a_url,
                {"skill": workflow, "inputs": {"note": f"n{i}"}, "request_id": f"ref-2p-{i:04d}"},
            )
            out.append(_handoff_from(task)["receipt"])
        return out

    try:
        receipts = asyncio.run(runs())
        for i, (kind, receipt) in enumerate(zip(kinds, receipts)):
            ref, note = f"ref-2p-{i:04d}", f"n{i}"
            # Stands in for the record party A's program wrote in B's system.
            if kind == "true":
                db.execute("insert into referral values (?, ?)", (ref, note))
            elif kind == "differing":
                db.execute("insert into referral values (?, ?)", (ref, note + "-changed"))
            claim = {"kind": kind, "ref": ref, "fields": {"note": note}}
            status = post_handoff(
                f"http://127.0.0.1:{inbox.server_address[1]}/inbox", h.make_handoff(claim, receipt)
            )
            assert status == 202
    finally:
        inbox.shutdown()

    assert len(decisions) == 20
    accepted_false = [d for d in decisions if d[0] != "true" and d[1] == "accept"]
    rejected_true = [d for d in decisions if d[0] == "true" and d[1] != "accept"]
    slowest = max(d[2] for d in decisions)
    print(
        f"two-party: 20 handoffs, accepted fabricated/differing {len(accepted_false)}, "
        f"rejected true {len(rejected_true)}, slowest check {slowest * 1000:.1f} ms"
    )
    assert accepted_false == [] and rejected_true == []
    assert signer_lines == {("Signed by Party A's key",)}
    assert slowest <= 2.0
    # Without its own read, the receiver never accepts on the signature.
    unchecked = h.decide(h.make_handoff({"ref": "x"}, receipts[0]), registry)
    assert unchecked.ok and unchecked.decision == "unchecked"
