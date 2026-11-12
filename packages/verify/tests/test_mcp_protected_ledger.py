"""The installed Verify MCP server preserves protected state across restart."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import anyio
from test_cli_protected_ledger import (
    _AUTHORITY_ENV,
    _assert_no_new_evidence,
    _child_env,
    _configure_authority,
    _installed_verify,
    _protected_attempt,
    _retained_evidence,
)
from test_cli_verdicts import _files
from test_verification import VALUES


@asynccontextmanager
async def _installed_session(tmp_path, contracts, state, env, monkeypatch):
    from mcp import ClientSession
    from mcp.client import stdio

    launcher = _installed_verify()
    arguments = ["serve", "--contracts", str(contracts), "--state", str(state)]
    settings = stdio.StdioServerParameters(
        command=str(launcher), args=arguments, env=env, cwd=tmp_path
    )
    children = []
    native_spawn = stdio._create_platform_compatible_process

    async def observe_spawn(command, args, **kwargs):
        assert Path(command).resolve() == launcher and args == arguments
        child = await native_spawn(command, args, **kwargs)
        children.append(child)
        return child

    forced_cleanup = False
    with monkeypatch.context() as scoped, open(os.devnull, "w") as errlog:
        scoped.setattr(stdio, "_create_platform_compatible_process", observe_spawn)
        try:
            with anyio.fail_after(30):
                async with stdio.stdio_client(settings, errlog=errlog) as streams:
                    assert len(children) == 1
                    child = children[0]
                    assert child.pid > 0 and child.returncode is None
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        yield session, child
        finally:
            # Keep the exact SDK handle, never look up or signal a process by
            # name. Native stdio owns normal shutdown and has already unwound.
            with anyio.CancelScope(shield=True):
                for child in children:
                    if child.returncode is None:
                        forced_cleanup = True
                        with suppress(ProcessLookupError):
                            child.kill()
                    with anyio.fail_after(5):
                        await child.wait()
    assert len(children) == 1 and not forced_cleanup
    assert children[0].returncode == 0


def _is_error(response):
    return getattr(response, "is_error", getattr(response, "isError", None))


def _json_reply(response):
    assert _is_error(response) is False
    assert len(response.content) == 1
    return json.loads(response.content[0].text)


def test_installed_mcp_restart_retains_owed_protected_attempt(
    store, tmp_path, monkeypatch
):
    authority = _configure_authority(tmp_path, monkeypatch, enroll=True)
    env = _child_env(tmp_path)
    contracts, state = tmp_path / "contracts", tmp_path / "mcp-state"
    contracts.mkdir()
    contract, _ = _files(store, contracts, 3600)
    request = {
        "contract": contract.stem,
        "values": VALUES,
        "write_key": "synthetic-protected-write",
    }

    async def exercise():
        async with _installed_session(tmp_path, contracts, state, env, monkeypatch) as (
            session,
            first_child,
        ):
            begun = _json_reply(await session.call_tool("verify_begin", request))
            assert begun["ok_to_write"] is True
            write_id = begun["write_id"]
            await anyio.to_thread.run_sync(store.save, VALUES)
            first = _json_reply(
                await session.call_tool("verify_check", {"write_id": write_id})
            )
            assert first["verified"] is True and first["provisional"] is True
            saved = Path(first["receipt_path"]).parent / "recheck.json"
            assert saved.is_file()

        initial = _protected_attempt(authority, write_id)
        assert initial.recheck_owed == first["recheck_due_at"]
        assert initial.resolved is not None
        assert initial.resolved["receipt_sha256"] == first["receipt_sha256"]
        async with _installed_session(tmp_path, contracts, state, env, monkeypatch) as (
            session,
            restarted_child,
        ):
            assert restarted_child is not first_child and first_child.returncode == 0
            retry = _json_reply(await session.call_tool("verify_begin", request))
            assert retry["ok_to_write"] is False and "write_id" not in retry
            earlier = retry["earlier_result"]
            assert earlier["write_id"] == write_id and earlier["not_sent_again"] is True
            assert earlier["receipt_sha256"] == first["receipt_sha256"]
            assert earlier["provisional"] is True
            owed = await session.call_tool("verify_recheck", {"write_id": write_id})
            assert _is_error(owed) is True
            assert "the ledger shows its recheck owed" in owed.content[0].text

        retained = _protected_attempt(authority, write_id)
        assert retained.begun == initial.begun and retained.resolved == initial.resolved
        assert retained.recheck_owed == initial.recheck_owed
        assert retained.rechecked is None and saved.is_file()

    anyio.run(exercise)
    assert store.posts == 1 and store.records() == [{"id": 1, **VALUES}]


def test_installed_mcp_missing_authority_refuses_write(store, tmp_path, monkeypatch):
    authority = _configure_authority(tmp_path, monkeypatch, enroll=False)
    env = _child_env(tmp_path)
    contracts, state = tmp_path / "contracts", tmp_path / "mcp-state"
    contracts.mkdir()
    contract, _ = _files(store, contracts)

    async def exercise():
        async with _installed_session(tmp_path, contracts, state, env, monkeypatch) as (
            session,
            _,
        ):
            response = await session.call_tool(
                "verify_begin", {"contract": contract.stem, "values": VALUES}
            )
            reply = _json_reply(response)
            assert reply["ok_to_write"] is False
            assert reply["hold_reason"] == "ledger_authority_absent"
            assert "write_id" not in reply and "earlier_result" not in reply

    anyio.run(exercise)
    assert store.posts == 0 and store.records() == []
    _assert_no_new_evidence(authority, state)


def test_installed_mcp_restart_requires_authority_after_enrolled_write(
    store, tmp_path, monkeypatch
):
    authority = _configure_authority(tmp_path, monkeypatch, enroll=True)
    env = _child_env(tmp_path)
    contracts, state = tmp_path / "contracts", tmp_path / "mcp-state"
    contracts.mkdir()
    contract, _ = _files(store, contracts, 3600)
    request = {
        "contract": contract.stem,
        "values": VALUES,
        "write_key": "synthetic-protected-write",
    }

    async def exercise():
        async with _installed_session(tmp_path, contracts, state, env, monkeypatch) as (
            session,
            first_child,
        ):
            begun = _json_reply(await session.call_tool("verify_begin", request))
            assert begun["ok_to_write"] is True
            write_id = begun["write_id"]
            await anyio.to_thread.run_sync(store.save, VALUES)
            first = _json_reply(
                await session.call_tool("verify_check", {"write_id": write_id})
            )
            assert first["verified"] is True and first["provisional"] is True

        attempt = _protected_attempt(authority, write_id)
        assert attempt.recheck_owed == first["recheck_due_at"]
        receipt = Path(first["receipt_path"])
        outputs = (receipt, receipt.parent / "recheck.json")
        evidence = _retained_evidence(authority, state, *outputs)

        monkeypatch.delenv(_AUTHORITY_ENV)
        del env[_AUTHORITY_ENV]
        async with _installed_session(tmp_path, contracts, state, env, monkeypatch) as (
            session,
            restarted_child,
        ):
            assert restarted_child is not first_child and first_child.returncode == 0
            checked = await session.call_tool("verify_recheck", {"write_id": write_id})
            assert _is_error(checked) is True and len(checked.content) == 1
            assert (
                "the write ledger is held (ledger_protected_requires_authority)"
                in checked.content[0].text
            )
            refused = _json_reply(await session.call_tool("verify_begin", request))
            assert refused["ok_to_write"] is False
            assert refused["hold_reason"] == "ledger_protected_requires_authority"
            assert "write_id" not in refused and "earlier_result" not in refused

        assert _retained_evidence(authority, state, *outputs) == evidence
        assert _protected_attempt(authority, write_id) == attempt

    anyio.run(exercise)
    assert store.posts == 1 and store.records() == [{"id": 1, **VALUES}]
