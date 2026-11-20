"""Protected MCP teaching refuses pixel-only RDP and Citrix capture.

The factory constructs the real remote backends over an owned headless kiosk.
They expose pixels and input, not credential-field observation. A person's
save approval cannot add that missing capability: teach_start refuses before
recording or actor input, closes its backend and releases its app lock.

Ordinary trusted compile_agent capture is a separate contract, exercised by
both surfaces in test_jit_surface_pixels.py. This fixture is not a native
RDP, ICA or HDX qualification.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.tutorial import _http_json, _records
from tests._rest_read_contract_fixtures import mockmed_effects_yaml
from tests.surface_kiosk import PageWindowClient

# The MCP SDK is imported inside the tests, never at collection: other tests
# in the same process check that emitting a server does not import it.
pytestmark = pytest.mark.skipif(
    find_spec("mcp") is None or find_spec("playwright") is None,
    reason="needs the agent extra and Playwright",
)

TITLE = "MockMed kiosk"
SECTIONS = {
    "rdp": {
        "kind": "rdp",
        "rdp_window": "mstsc",
        "rdp_window_title": TITLE,
        "rdp_max_frame_age_s": 30.0,
    },
    "citrix": {
        "kind": "citrix",
        "rdp_window": "Citrix Viewer",
        "rdp_window_title": TITLE,
        "rdp_readiness_text": "MockMed",
        "rdp_max_frame_age_s": 30.0,
    },
}
TASK = "Open the first referral, start a Triage encounter, and save this note: {note}"
NOTE = "Synthetic follow-up in two weeks"
AGENT_ID = "agent:mcp-teach-surface"
#: Where MockMed's controls are at 1280x800, as an agent reading the screen
#: would find them (the same points tests/fixtures/jit/anthropic_add_note.json
#: clicks).
CLICKS = [(775, 186), (114, 159), (85, 214), (344, 350)]


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _open_backend(
    base: str, clients: list[PageWindowClient], built: list[Any], closed: list[Any]
):
    """Build each backend through the factory, with a fresh kiosk page as the
    client window. Each call opens its own browser on the calling thread."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_setup import launch_chromium
    from openadapt_flow.backends.factory import build_backend

    def open_backend(cfg: Any) -> Any:
        pw = sync_playwright().start()
        try:
            browser = launch_chromium(pw.chromium, headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser on this host
            pw.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(
            viewport={"width": 1280, "height": 800}, device_scale_factor=1
        )
        page.goto(f"{base}/?fault=ok&idempotency=demo#tasks")
        page.wait_for_timeout(300)
        client = PageWindowClient(page, owner=cfg.rdp_window, title=TITLE)
        clients.append(client)
        backend = build_backend(cfg, window_client=client)
        built.append(backend)

        def close() -> None:
            browser.close()
            pw.stop()
            closed.append(backend)

        backend.close = close  # type: ignore[attr-defined]
        return backend

    return open_backend


def _admitter(key_dir: Path, origin: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "surface passphrase")
    files = key_dir.parent / "keyfiles"
    for role, pid, name in (
        ("policy_signer", "dana.signer", "signer"),
        ("agent", AGENT_ID, "agent"),
    ):
        authority.register_principal(
            key_dir,
            role=role,
            principal_id=pid,
            public_key_hex=authority.generate_encrypted_key(files / f"{name}.pem"),
        )
    now = datetime.now(timezone.utc)
    policy = authority.AdmissionPolicy(
        policy_id="kiosk",
        signer="dana.signer",
        signed_at=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        allowed_hosts=(origin,),
        allowed_actions=("click", "type"),
        allowed_effects=("record_written", "field_equals"),
        min_clean_canaries=1,
        agent_principals=(AGENT_ID,),
        allow_self_admission=True,
    )
    signed = authority.sign_policy(
        policy, authority.load_encrypted_key(files / "signer.pem")
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(files / "agent.pem"), key_dir, policy=signed
    )


@asynccontextmanager
async def connect(server: Any, *, person: Optional[str]):
    import anyio
    import mcp.types as types
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def elicit(context: Any, params: Any) -> types.ElicitResult:
        if person is None:
            return types.ElicitResult(action="decline", content={})
        return types.ElicitResult(
            action="accept", content={"name": person, "confirmed": True}
        )

    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            kwargs = {"elicitation_callback": elicit} if person else {}
            async with ClientSession(*client, **kwargs) as session:
                await session.initialize()
                yield session
            tasks.cancel_scope.cancel()


async def _call(session: Any, name: str, args: Optional[dict] = None) -> dict:
    result = await session.call_tool(name, args or {})
    payload = json.loads(result.content[0].text)
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        return {"_error": payload.get("error")}
    return payload


@pytest.mark.timeout(600)
@pytest.mark.parametrize("kind", ["rdp", "citrix"])
def test_teach_on_a_remote_window(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import anyio

    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.mcp import build_server
    from openadapt_flow.agent.runner import RunnerConfig
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig
    from openadapt_flow.backends.citrix_workspace import CitrixWorkspaceBackend
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend
    from openadapt_flow.jit.compile_agent import SurfaceApp
    from openadapt_flow.secret_inputs import CredentialObservationUnsupported

    clients: list[PageWindowClient] = []
    built: list[Any] = []
    closed: list[Any] = []
    section = SECTIONS[kind]
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        + mockmed_effects_yaml(mockmed)
        + "  timeout_s: 2.0\n  poll_interval_s: 0.05\n"
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    workflows = AgentBridge(
        None, RunnerConfig(runs_dir=runs), allow_run=True, mode="production"
    )
    teach = TeachBridge(
        TeachConfig(
            verify=verify,
            cache_dir=tmp_path / "teach",
            surface=section,
            key_dir=tmp_path / "keys",
            admitter=_admitter(
                tmp_path / "keys", SurfaceApp(section).origin, monkeypatch
            ),
            screens="unscrubbed",
            reset=lambda: _http_json(f"{mockmed}/api/reset", method="POST", body={}),
            open_backend=_open_backend(mockmed, clients, built, closed),
        ),
        workflows=workflows,
    )
    server = build_server(workflows, teach=teach)
    start = {
        "task": TASK,
        "inputs": {"note": NOTE},
        "record_fields": {"patient_id": "p1", "type": "Triage"},
        "on_screen": ["Jane Sample", "MRN P1"],
    }

    async def scenario() -> None:
        # Both a declined form and a willing person have the same unsupported
        # observation boundary. Retrying also proves the failed run releases it.
        for index, person in enumerate((None, "dana.reviewer"), start=1):
            async with connect(server, person=person) as session:
                started = await _call(session, "teach_start", start)
                assert isinstance(started.get("_error"), str), started
                assert CredentialObservationUnsupported.code in started["_error"]
                assert "cannot observe credential fields" in started["_error"]
                assert "status" not in started and "surface" not in started

                run = teach._run
                assert run is not None and run.done.wait(5)
                assert run._thread is not None
                run._thread.join(timeout=5)
                assert not run._thread.is_alive()
                assert isinstance(run.error, CredentialObservationUnsupported)
                assert not run.agent.ready.is_set() and run.agent.performed == 0
                assert teach.config.app_lock.acquire(blocking=False)
                teach.config.app_lock.release()

                expected = (
                    CitrixWorkspaceBackend if kind == "citrix" else RemoteDisplayBackend
                )
                assert len(built) == len(closed) == len(clients) == index
                assert type(built[-1]) is expected and closed[-1] is built[-1]
                assert not callable(
                    getattr(built[-1], "focused_field_credential", None)
                )
                assert all(client.inputs == 0 for client in clients)

                # No tool can act on the failed run or expose a recorded frame.
                x, y = CLICKS[0]
                for tool, args in (
                    ("teach_act", {"action": "click", "x": x, "y": y}),
                    ("teach_act", {"action": "type", "input": "note"}),
                    ("teach_screen", {}),
                    ("teach_abort", {}),
                ):
                    refused = await _call(session, tool, args)
                    assert "no run 1 is in progress" in refused.get("_error", ""), (
                        refused
                    )
                finished = await _call(session, "teach_finish")
                assert finished["status"] == "capture_failed", finished
                assert CredentialObservationUnsupported.code in finished["detail"]
                assert not any(
                    tool.name.startswith("taught_")
                    for tool in (await session.list_tools()).tools
                )
                assert all(client.inputs == 0 for client in clients)
                assert _records(mockmed) == []
                cache = tmp_path / "teach"
                for pattern in (
                    "candidate.json",
                    "workflow.json",
                    "events.jsonl",
                    "*.png",
                ):
                    assert not list(cache.rglob(pattern))
                assert all(
                    NOTE.encode() not in path.read_bytes()
                    for path in cache.rglob("*")
                    if path.is_file()
                )

    anyio.run(scenario)
