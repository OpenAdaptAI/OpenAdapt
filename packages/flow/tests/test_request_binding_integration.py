"""Normal wrapper/RunStore/verifier receipts over a public synthetic store.

The deterministic action is an explicit component fixture, not an admitted
program, a person, a model run or a claim of receipt-signing authority.
"""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from openadapt_flow.agent.runs import RunStore
from openadapt_flow.compile_agent import pipeline
from openadapt_flow.integrations.agents.skills import SkillRun
from openadapt_flow.request_binding import (
    BindingError,
    capture_receipt_binding,
    current_request_context,
    verify_receipt_binding,
)
from openadapt_flow.verify_run import capture_baseline
from openadapt_flow.wrap import WrappedTask
from tests.test_verify_run import _actor_writes
from tests.test_wrap_integration import mcp_session, payload

pytest_plugins = ("tests.test_verify_run", "tests.test_wrap_integration")


class ReceiptCache:
    """Real producer/verifier boundary with an explicit synthetic action."""

    def __init__(self, root, cfg, base):
        self.root, self.cfg, self.base = root, cfg, base
        self.root.mkdir()
        self.calls = 0
        self.contexts = []
        self.operation_kind = "run"

    def key(self, instruction, params):
        return {
            "instruction": instruction,
            "parameters": sorted(params),
            "verify": "fixed-test-contract",
        }

    def receipt_root(self, instruction, params):
        return self.root

    def run(self, instruction, params, agent):
        self.calls += 1
        self.contexts.append(current_request_context())
        producer = SimpleNamespace(
            dir=self.root,
            task=SimpleNamespace(key="synthetic-receipt-component"),
            _confirmed_config=lambda: self.cfg,
            _receipt_baseline=None,
        )

        def action():
            baseline = producer._pending[2]
            capture_baseline(self.cfg, params, baseline)
            producer._receipt_baseline = baseline
            _actor_writes(self.base, patient=params["patient_id"], note=params["note"])
            return SimpleNamespace(status="COMPONENT_COMPLETED")

        # The actual pipeline creates its receipt with the current request
        # context. Neither this fixture nor a caller constructs a pass receipt.
        result, receipt, path = pipeline.FirstRunCompiler._governed(
            producer, self.operation_kind, params, action
        )
        assert receipt is not None and path is not None
        outcome = receipt["receipt"]["outcome"].upper()
        return SkillRun(
            path="skill",
            status=result.status,
            verified=outcome == "VERIFIED",
            model_calls=0,
            cost_usd=0.0,
            seconds=0,
            usage=[],
            usage_known=True,
            replay={"receipt": str(path), "receipt_outcome": outcome},
        )


@pytest.fixture
def task_case(tmp_path, cfg, sor):
    base, _store = sor
    cache = ReceiptCache(tmp_path / "producer", cfg, base)
    task = WrappedTask(
        cache=cache,
        agent=None,
        instruction="Create {note}",
        parameters=("patient_id", "note"),
        runs_dir=tmp_path / "runs",
    )
    return task, cache, tmp_path / "runs"


def invoke(task, request_id, values, transport, monkeypatch, capsys):
    if transport == "sdk":
        return task.call(request_id, values)
    if transport == "cli":
        from openadapt_flow.wrap import cli

        with monkeypatch.context() as patch:
            patch.setattr(cli, "_task", lambda args, parser: task)
            args = ["--task", "test-owned-task", "--request-id", request_id]
            for key, value in values.items():
                args += ["--param", key + "=" + value]
            assert cli.main(args) == 0
        return json.loads(capsys.readouterr().out)

    async def steps(session):
        return payload(
            await session.call_tool(
                "run_task", {"request_id": request_id, "parameters": values}
            )
        )

    return anyio.run(mcp_session, task, steps)


@pytest.mark.parametrize("transport", ["sdk", "cli", "mcp"])
def test_two_genuine_requests_bind_and_same_outcome_receipt_swap_refuses(
    task_case, monkeypatch, capsys, transport
):
    task, cache, runs = task_case
    replies = [
        invoke(
            task,
            "request-" + letter,
            {"patient_id": "p1", "note": "synthetic " + letter},
            transport,
            monkeypatch,
            capsys,
        )
        for letter in ("a", "b")
    ]
    store = RunStore(runs)
    records = [store.read(reply["run_id"]) for reply in replies]
    assert all(record is not None for record in records)
    assert all(
        reply["verified"] is True and reply["receipt_outcome"] == "VERIFIED"
        for reply in replies
    )
    assert all(
        verify_receipt_binding(record, reply, cache.root)["status"] == "BOUND"
        for record, reply in zip(records, replies)
    )
    for letter, reply in zip(("a", "b"), replies):
        assert (
            task.verify_reply(
                "request-" + letter,
                {"patient_id": "p1", "note": "synthetic " + letter},
                reply,
            )["status"]
            == "BOUND"
        )
    with pytest.raises(BindingError, match="^terminal_request$"):
        task.verify_reply(
            "request-a", {"patient_id": "p1", "note": "synthetic a"}, replies[1]
        )
    with pytest.raises(BindingError, match="^terminal_workflow$"):
        task.verify_reply(
            "request-a", {"patient_id": "p1", "note": "changed arguments"}, replies[0]
        )
    assert cache.calls == 2 and cache.contexts[0] != cache.contexts[1]
    assert current_request_context() is None
    paths = [Path(reply["receipt"]) for reply in replies]
    original = paths[0].read_bytes()
    replacement = paths[1].read_bytes()
    try:
        paths[0].write_bytes(replacement)
        with pytest.raises(BindingError, match="^receipt_digest_mismatch$"):
            verify_receipt_binding(records[0], replies[0], cache.root)
        # Independently test context: account for replacement bytes in copies
        # of the consumer inputs, without altering genuine RunStore records.
        changed_record, changed_reply = deepcopy(records[0]), deepcopy(replies[0])
        changed_reply["receipt_binding"]["receipt_sha256"] = hashlib.sha256(
            replacement
        ).hexdigest()
        changed_reply["receipt_binding"]["receipt_bytes"] = len(replacement)
        changed_record["result"] = deepcopy(changed_reply)
        with pytest.raises(BindingError, match="^receipt_context_mismatch$"):
            verify_receipt_binding(changed_record, changed_reply, cache.root)
    finally:
        paths[0].write_bytes(original)
    assert (
        verify_receipt_binding(records[0], replies[0], cache.root)["status"] == "BOUND"
    )
    replay = invoke(
        task,
        "request-a",
        {"patient_id": "p1", "note": "synthetic a"},
        transport,
        monkeypatch,
        capsys,
    )
    assert replay["replayed"] is True and replay["run_id"] == replies[0]["run_id"]
    assert verify_receipt_binding(records[0], replay, cache.root)["status"] == "BOUND"
    assert cache.calls == 2


def test_changed_context_and_digest_independently_refuse(task_case):
    from openadapt_flow.request_binding import RequestContext

    task, cache, runs = task_case
    reply = task.call(
        "negative-fields", {"patient_id": "p1", "note": "synthetic field controls"}
    )
    record = RunStore(runs).read(reply["run_id"])
    assert record is not None
    assert verify_receipt_binding(record, reply, cache.root)["status"] == "BOUND"
    expected = RequestContext.from_record(record)
    for field, value in (
        ("run_id", "different-run"),
        ("request_ref", "sha256:" + "0" * 64),
        ("workflow", "wrap:" + "0" * 64),
        ("fingerprint", "0" * 64),
    ):
        changed = expected.to_dict()
        assert changed[field] != value
        changed[field] = value
        with pytest.raises(BindingError, match="^receipt_context_mismatch$"):
            capture_receipt_binding(reply, RequestContext.parse(changed), cache.root)
    for field, value in (("receipt_sha256", "0" * 64), ("receipt_bytes", 1)):
        changed_reply, changed_record = deepcopy(reply), deepcopy(record)
        assert changed_reply["receipt_binding"][field] != value
        changed_reply["receipt_binding"][field] = value
        changed_record["result"] = deepcopy(changed_reply)
        with pytest.raises(BindingError, match="^receipt_digest_mismatch$"):
            verify_receipt_binding(changed_record, changed_reply, cache.root)


def test_interrupted_real_write_retains_request_and_resets_context(task_case):
    task, cache, runs = task_case
    original_run = cache.run
    primary = KeyboardInterrupt()

    def interrupted(instruction, params, agent):
        original_run(instruction, params, agent)
        raise primary

    cache.run = interrupted
    with pytest.raises(KeyboardInterrupt) as caught:
        task.call("interrupted", {"patient_id": "p1", "note": "synthetic interrupted"})
    assert caught.value is primary and current_request_context() is None
    calls = cache.calls
    retry = task.call(
        "interrupted", {"patient_id": "p1", "note": "synthetic interrupted"}
    )
    assert retry["status"] == "RECONCILIATION_REQUIRED" and retry["verified"] is False
    assert cache.calls == calls
    record = RunStore(runs).read(retry["run_id"])
    assert record is not None
    with pytest.raises(BindingError, match="^terminal_unfinished$"):
        verify_receipt_binding(record, retry, cache.root)


def test_direct_sdk_run_has_no_fabricated_transport_binding(task_case):
    task, cache, _runs = task_case
    run = task.run({"patient_id": "p1", "note": "synthetic direct"})
    document = json.loads(Path(run.replay["receipt"]).read_text())
    assert "request_context" not in document["receipt"]
    assert "request_context" not in document["evidence"]
    assert cache.contexts == [None] and current_request_context() is None


def test_distinct_concurrent_sdk_calls_keep_their_reserved_context(task_case):
    task, cache, runs = task_case
    replies, errors = [], []
    ready = threading.Barrier(2)

    def worker(index):
        try:
            ready.wait(timeout=5)
            replies.append(
                task.call(
                    "concurrent-" + str(index),
                    {"patient_id": "p1", "note": "synthetic concurrent " + str(index)},
                )
            )
            assert current_request_context() is None
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert len(replies) == cache.calls == 2
    store = RunStore(runs)
    for reply in replies:
        assert (
            verify_receipt_binding(store.read(reply["run_id"]), reply, cache.root)[
                "status"
            ]
            == "BOUND"
        )
    assert len(set(cache.contexts)) == 2 and current_request_context() is None


def test_first_miss_record_check_does_not_gain_request_receipt_authority(component):
    task = component.task()
    reply = task.call("first-miss", {"note": "synthetic first record"})
    assert (
        reply["path"] == "agent"
        and reply["status"] == "admitted"
        and reply["verified"] is True
    )
    assert reply["receipt_binding"] == {
        "schema": "openadapt.receipt-binding/1",
        "status": "UNBOUND",
        "reason": "NO_RECEIPT",
    }
    record = RunStore(component.root / "requests").read(reply["run_id"])
    assert record is not None
    with pytest.raises(BindingError, match="^receipt_unbound$"):
        verify_receipt_binding(record, reply, component.root)


def test_canary_inside_request_scope_is_not_relabelled_as_request_effect(task_case):
    task, cache, runs = task_case
    cache.operation_kind = "canary"
    reply = task.call(
        "separate-canary", {"patient_id": "p1", "note": "synthetic canary"}
    )
    assert reply["verified"] is True  # Existing effect check, not request binding.
    document = json.loads(Path(reply["receipt"]).read_text())
    assert "request_context" not in document["receipt"]
    assert "request_context" not in document["evidence"]
    assert reply["receipt_binding"]["status"] == "REFUSED"
    record = RunStore(runs).read(reply["run_id"])
    with pytest.raises(BindingError, match="^receipt_unbound$"):
        verify_receipt_binding(record, reply, cache.root)
    assert current_request_context() is None


def test_receipt_read_failure_keeps_primary_when_descriptor_close_also_fails(
    task_case, monkeypatch
):
    import openadapt_flow.request_binding as model

    task, cache, runs = task_case
    reply = task.call("file-cleanup", {"patient_id": "p1", "note": "synthetic cleanup"})
    record = RunStore(runs).read(reply["run_id"])
    primary = KeyboardInterrupt()
    descriptor = []
    real_close = model.os.close

    def failed_read(fd, _count):
        descriptor.append(fd)
        raise primary

    def close_then_fail(fd):
        real_close(fd)
        if descriptor and fd == descriptor[0]:
            raise OSError("synthetic secondary close")

    with monkeypatch.context() as patch:
        patch.setattr(model.os, "read", failed_read)
        patch.setattr(model.os, "close", close_then_fail)
        with pytest.raises(KeyboardInterrupt) as caught:
            verify_receipt_binding(record, reply, cache.root)
    assert caught.value is primary and len(descriptor) == 1


class InnerMCPReceiptCache(ReceiptCache):
    """Actual emitted inner MCP/SDK boundary, with synthetic action only.

    The SDK's memory streams exercise wire messages without claiming native
    subprocess ownership or child-import provenance. The unchanged producer
    and independent store verifier still create every receipt.
    """

    def run(self, instruction, params, agent):
        import contextvars
        import importlib.util
        import uuid

        import openadapt_flow.compile_agent as api
        from openadapt_flow.compile_agent.emit import emit_mcp
        from openadapt_flow.request_binding import request_meta

        try:
            from mcp.server.mcpserver import MCPServer
        except ModuleNotFoundError as error:
            if error.name != "mcp.server.mcpserver":
                raise
            from mcp.server.fastmcp import FastMCP as MCPServer

            sdk_generation = 1
        else:
            sdk_generation = 2

        output = self.root / ("server-" + uuid.uuid4().hex + ".py")
        compiler = SimpleNamespace(
            task=SimpleNamespace(
                params=("patient_id", "note"),
                task_id="synthetic_binding",
                instruction=instruction,
            ),
            dir=self.root,
        )
        emit_mcp(compiler, output)
        spec = importlib.util.spec_from_file_location(
            "synthetic_binding_" + uuid.uuid4().hex, output
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        servers = [
            value for value in vars(module).values() if isinstance(value, MCPServer)
        ]
        assert len(servers) == 1
        lowlevel = (
            servers[0]._lowlevel_server
            if sdk_generation == 2
            else servers[0]._mcp_server
        )
        real_run = api.run_compiled_params

        def dispatch(task_dir, values):
            assert Path(task_dir) == self.root
            run = super(InnerMCPReceiptCache, self).run(instruction, values, agent)
            return pipeline.ReplayResult(
                status=run.status,
                verified=run.verified,
                model_calls=run.model_calls,
                seconds=run.seconds,
                run_dir=None,
                receipt_outcome=run.replay["receipt_outcome"],
                receipt=Path(run.replay["receipt"]),
            )

        async def call():
            from mcp import ClientSession
            from mcp.shared.memory import create_client_server_memory_streams

            metadata = request_meta()
            assert metadata is not None
            async with create_client_server_memory_streams() as (client, transport):
                async with anyio.create_task_group() as group:
                    # The receiver starts without the caller's ambient scope:
                    # only actual MCP _meta can carry the owned request join.
                    contextvars.Context().run(
                        group.start_soon,
                        lowlevel.run,
                        *transport,
                        lowlevel.create_initialization_options(),
                    )
                    async with ClientSession(*client) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        assert len(tools) == 1
                        result = payload(
                            await session.call_tool(
                                tools[0].name, dict(params), meta=metadata
                            )
                        )
                    group.cancel_scope.cancel()
            return result

        api.run_compiled_params = dispatch
        try:
            reply = anyio.run(call)
        finally:
            api.run_compiled_params = real_run
        return SkillRun(
            path="skill",
            status=reply["status"],
            verified=reply["verified"],
            model_calls=reply["model_calls"],
            cost_usd=0.0,
            seconds=reply["seconds"],
            replay=reply,
            usage=reply["usage"],
            usage_known=reply["usage_known"],
        )


def test_emitted_inner_mcp_carries_real_reserved_context_to_normal_producer(
    tmp_path, cfg, sor
):
    base, _store = sor
    cache = InnerMCPReceiptCache(tmp_path / "inner-producer", cfg, base)
    task = WrappedTask(
        cache=cache,
        agent=None,
        instruction="Create {note}",
        parameters=("patient_id", "note"),
        runs_dir=tmp_path / "inner-runs",
    )
    store = RunStore(tmp_path / "inner-runs")
    replies = [
        task.call(
            "inner-" + name, {"patient_id": "p1", "note": "synthetic inner " + name}
        )
        for name in ("a", "b")
    ]
    assert len(cache.contexts) == 2 and len(set(cache.contexts)) == 2
    for reply in replies:
        assert reply["verified"] is True
        record = store.read(reply["run_id"])
        assert record is not None
        assert verify_receipt_binding(record, reply, cache.root)["status"] == "BOUND"
    assert current_request_context() is None


def test_legacy_finished_reply_is_explicitly_unbound_without_rewriting_record(
    task_case,
):
    task, cache, runs = task_case
    values = {"patient_id": "p1", "note": "synthetic legacy"}
    identity = cache.key(task.instruction, values)
    workflow = (
        "wrap:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    )
    store = RunStore(runs)
    begin = store.begin(
        request_id="legacy", workflow=workflow, inputs=values, mode="skill-cache"
    )
    assert begin.kind == "new" and begin.record is not None
    old = {
        "run_id": begin.run_id,
        "request_id": "legacy",
        "replayed": False,
        "status": "VERIFIED",
        "verified": True,
    }
    store.finish(begin.record, old)
    reply = task.call("legacy", values)
    assert reply["receipt_binding"] == {
        "schema": "openadapt.receipt-binding/1",
        "status": "UNBOUND",
        "reason": "LEGACY_TERMINAL",
    }
    record = store.read(begin.run_id)
    assert record is not None and record["result"] == old and cache.calls == 0
    with pytest.raises(BindingError):
        verify_receipt_binding(record, reply, cache.root)


@pytest.mark.parametrize("transport", ["sdk", "cli", "mcp"])
def test_real_terminal_json_type_changes_never_equal_the_stored_reply(
    task_case, monkeypatch, capsys, transport
):
    task, cache, runs = task_case
    values = {"patient_id": "p1", "note": "synthetic typed terminal"}
    reply = invoke(task, "typed-terminal", values, transport, monkeypatch, capsys)
    record = RunStore(runs).read(reply["run_id"])
    assert type(record["result"]["usage"]) is list
    assert task.verify_reply("typed-terminal", values, reply)["status"] == "BOUND"
    variants = [
        ("usage", tuple(reply["usage"])),
        ("verified", 1),
        ("seconds", False),
        ("seconds", float("nan")),
    ]
    for field, replacement in variants:
        changed = deepcopy(reply)
        changed[field] = replacement
        with pytest.raises(BindingError):
            task.verify_reply("typed-terminal", values, changed)
    assert cache.calls == 1 and current_request_context() is None
    assert verify_receipt_binding(record, reply, cache.root)["status"] == "BOUND"


@pytest.mark.parametrize(
    "invalid",
    [None, {}, [], "context", True, 1],
    ids=("null", "empty-object", "array", "text", "boolean", "integer"),
)
def test_generated_mcp_rejects_invalid_present_namespace_before_dispatch(
    tmp_path, cfg, sor, monkeypatch, invalid
):
    import sys

    import mcp.shared.memory as memory

    import openadapt_flow.request_binding as model

    base, _store = sor
    cache = InnerMCPReceiptCache(tmp_path / "metadata-producer", cfg, base)
    task = WrappedTask(
        cache=cache,
        agent=None,
        instruction="Create {note}",
        parameters=("patient_id", "note"),
        runs_dir=tmp_path / "metadata-runs",
    )
    original_payload = payload
    actual_errors = []

    def observe_payload(result):
        # CallToolResult.isError is the actual pinned SDK transport result.
        assert type(result.isError) is bool
        actual_errors.append(result.isError)
        return original_payload(result)

    with monkeypatch.context() as patch:
        patch.setattr(sys.modules[__name__], "payload", observe_payload)
        warmup = {"patient_id": "p1", "note": "synthetic metadata warmup"}
        first = task.call("metadata-valid-before", warmup)
        assert first["verified"] is True
        assert (
            task.verify_reply("metadata-valid-before", warmup, first)["status"]
            == "BOUND"
        )
        assert cache.calls == 1 and actual_errors == [False]
        with monkeypatch.context() as malformed:
            wire_faults = []
            malformed.setattr(
                memory,
                "create_client_server_memory_streams",
                _memory_wire_namespace_fault(
                    memory.create_client_server_memory_streams,
                    model.MCP_META_KEY,
                    invalid,
                    wire_faults,
                ),
            )
            rejected = task.call(
                "metadata-invalid",
                {"patient_id": "p1", "note": "synthetic must not dispatch"},
            )
        assert wire_faults == [True]
        assert actual_errors == [False, True]
        assert cache.calls == 1 and len(cache.contexts) == 1
        assert rejected["verified"] is False
        with pytest.raises(BindingError):
            task.verify_reply(
                "metadata-invalid",
                {"patient_id": "p1", "note": "synthetic must not dispatch"},
                rejected,
            )
        assert current_request_context() is None
        values = {"patient_id": "p1", "note": "synthetic metadata valid after"}
        after = task.call("metadata-valid-after", values)
        assert after["verified"] is True
        assert (
            task.verify_reply("metadata-valid-after", values, after)["status"]
            == "BOUND"
        )
        assert cache.calls == 2 and len(cache.contexts) == 2
        assert actual_errors == [False, True, False]
    assert current_request_context() is None


def _memory_wire_namespace_fault(real_factory, namespace, invalid, observations):
    """Fault only an encoded tools/call request on genuine SDK memory streams."""
    from contextlib import asynccontextmanager

    import mcp.types as types
    from mcp.shared.message import SessionMessage

    @asynccontextmanager
    async def streams():
        async with real_factory() as (client, transport):
            send, read = anyio.create_memory_object_stream(1)
            async with send, read, anyio.create_task_group() as forwarding:

                async def relay():
                    async for message in transport[0]:
                        if (
                            isinstance(message, SessionMessage)
                            and isinstance(message.message.root, types.JSONRPCRequest)
                            and message.message.root.method == "tools/call"
                        ):
                            request = message.message.root
                            params = deepcopy(request.params)
                            assert (
                                type(params) is dict
                                and type(params.get("_meta")) is dict
                            )
                            assert namespace in params["_meta"]
                            params["_meta"][namespace] = invalid
                            changed = request.model_copy(update={"params": params})
                            # Match the receiver's actual JSONRPCRequest dump.
                            # Unlike a Meta model field, a raw dict entry must
                            # retain present null before model validation.
                            encoded = changed.model_dump(
                                by_alias=True, mode="json", exclude_none=True
                            )
                            observed = encoded["params"]["_meta"]
                            assert namespace in observed
                            assert (
                                type(observed[namespace]) is type(invalid)
                                and observed[namespace] == invalid
                            )
                            assert (
                                changed.id == request.id
                                and changed.method == request.method
                            )
                            assert (
                                changed.params["arguments"]
                                == request.params["arguments"]
                            )
                            assert {
                                key: value
                                for key, value in changed.params.items()
                                if key != "_meta"
                            } == {
                                key: value
                                for key, value in request.params.items()
                                if key != "_meta"
                            }
                            assert {
                                key: value
                                for key, value in changed.params["_meta"].items()
                                if key != namespace
                            } == {
                                key: value
                                for key, value in request.params["_meta"].items()
                                if key != namespace
                            }
                            message = SessionMessage(
                                message=types.JSONRPCMessage(changed),
                                metadata=message.metadata,
                            )
                            observations.append(True)
                        await send.send(message)

                forwarding.start_soon(relay)
                try:
                    yield client, (read, transport[1])
                finally:
                    forwarding.cancel_scope.cancel()

    return streams
