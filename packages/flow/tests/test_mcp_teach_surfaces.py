"""The MCP teach tools on a remote session (RDP, Citrix), through the real backends.

MockMed runs in a browser page that Flow never reads: tests/surface_kiosk
shows it to ``RemoteDisplayBackend`` and ``CitrixWorkspaceBackend`` as a
client window, so Flow sees pixels only. A scripted MCP client teaches the
task with pixel clicks, as an agent reading the screen would.

What it checks:

* the observation is screen-only and the screen tool works when the operator
  turned it on;
* every click a person must approve in the client's form (the harness can't
  read which record a remote screen shows), and without a form nothing is
  clicked;
* run 1 compiles, passes its record check against the system of record, and
  a person confirms the check;
* admission fails closed with its reason: the canary is refused because a
  pixel-only click carries no verified record identity (docs/BACKEND_PARITY.md).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

pytest.importorskip("mcp")
pytest.importorskip("playwright.sync_api")

import anyio  # noqa: E402
import mcp.types as types  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from openadapt_flow.agent.bridge import AgentBridge  # noqa: E402
from openadapt_flow.agent.mcp import build_server  # noqa: E402
from openadapt_flow.agent.runner import RunnerConfig  # noqa: E402
from openadapt_flow.agent.teach import TeachBridge, TeachConfig  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.tutorial import _http_json, _records  # noqa: E402
from tests.surface_kiosk import PageWindowClient  # noqa: E402

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
SAVE = (134, 457)


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _open_backend(base: str):
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
        backend = build_backend(
            cfg, window_client=PageWindowClient(page, owner=cfg.rdp_window, title=TITLE)
        )

        def close() -> None:
            browser.close()
            pw.stop()

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


async def _call(session: ClientSession, name: str, args: Optional[dict] = None) -> dict:
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
    from openadapt_flow.jit.compile_agent import SurfaceApp

    section = SECTIONS[kind]
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n  kind: rest\n"
        f"  base_url: {mockmed}\n  records_path: /api/db\n  records_key: records\n"
        "  timeout_s: 2.0\n  poll_interval_s: 0.05\n"
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
            open_backend=_open_backend(mockmed),
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
        # A client with no form: the first click needs a person, so nothing
        # is clicked and run 1 keeps going.
        async with connect(server, person=None) as session:
            started = await _call(session, "teach_start", start)
            assert started["surface"] == kind and started["person_approves_saves"]
            assert "remote session" in started["observation"]
            screen = await _call(session, "teach_screen")
            assert len(screen["screen_png_base64"]) > 1000
            x, y = CLICKS[0]
            refused = await _call(
                session, "teach_act", {"action": "click", "x": x, "y": y}
            )
            assert "needs a person's approval" in refused["_error"]
            assert _records(mockmed) == []
            assert (await _call(session, "teach_abort"))["status"] == "aborted"

        async with connect(server, person="dana.reviewer") as session:
            started = await _call(session, "teach_start", start)
            assert started["status"] == "recording", started
            for x, y in CLICKS:
                done = await _call(
                    session, "teach_act", {"action": "click", "x": x, "y": y}
                )
                assert done["status"] == "ok", done
            typed = await _call(
                session, "teach_act", {"action": "type", "input": "note"}
            )
            assert typed["status"] == "ok", typed
            x, y = SAVE
            saved = await _call(
                session, "teach_act", {"action": "click", "x": x, "y": y}
            )
            assert saved["status"] == "ok", saved
            draft = await _call(session, "teach_finish")
            assert draft["status"] == "draft", draft
            assert draft["surface"] == kind
            assert draft["record_check"]["passed"]
            assert {g["decision"] for g in draft["save_gate"]} == {"approved_by_hold"}
            assert draft["parameters"]["lifted"] == ["note"]

            confirmed = await _call(
                session,
                "teach_confirm_checks",
                {"task_key": draft["task_key"], "drop_fields": ["key", "source"]},
            )
            assert confirmed["confirmed_by"] == "dana.reviewer", confirmed

            saved_rows = _records(mockmed)
            admitted = await _call(
                session,
                "teach_admit",
                {"task_key": draft["task_key"], "canary_inputs": [{"note": "Canary"}]},
            )
            assert admitted["status"] == "canary_failed", admitted
            assert "Identity coverage" in admitted["detail"]
            assert _records(mockmed) == saved_rows  # refused before any input
            assert not any(
                t.name.startswith("taught_") for t in (await session.list_tools()).tools
            )

    anyio.run(scenario)
