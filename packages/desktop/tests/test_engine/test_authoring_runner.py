"""Authoring mailbox claim, Allow-per-sub, wait=0 poll, and record_observed."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from engine.auth.store import load_authoring_lease
from engine.authoring_runner import (
    POLL_WAIT_S,
    AuthoringCoachOnly,
    AuthoringError,
    AuthoringRunner,
    project_observe,
)
from engine.config import EngineConfig

BIND = "oab_" + "A" * 43
PACK = "p.abcdefghijkl"
LEASE = "oals_" + "a" * 64
ORIGIN = "https://openadapt.ai"
URI = f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https%3A%2F%2Fopenadapt.ai"
SUB = "b" * 64
OTHER_SUB = "d" * 64
CLIENT = "c" * 64


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def log(self, event: str, **data: Any) -> None:
        self.events.append((event, data))


class FakeRecorder:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.observed: list[dict[str, Any]] = []
        self.typed: list[Any] = []
        self.finished = False

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def type_text(self, *args: Any, **kwargs: Any) -> None:
        self.typed.append((args, kwargs))
        raise AssertionError("Continue must not call type_text")

    def record_observed(self, **kwargs: Any) -> None:
        self.observed.append(kwargs)

    def finish(self) -> SimpleNamespace:
        self.finished = True
        return SimpleNamespace(ok=True)


def _nodes() -> list[dict[str, Any]]:
    return [
        {
            "provider_runtime_id": "ax-elem-1",
            "role": "button",
            "control_type": "button",
            "automation_id": "btnContinue",
            "enabled": True,
            "focused": False,
            "bounds": {"x": 0.72, "y": 0.88, "w": 0.14, "h": 0.05},
            "backend_pixels": {"x": 920, "y": 640, "w": 180, "h": 36},
            "value": "SSN-SECRET",
            "title": "Patient chart",
            "name": "Continue",
        },
        {
            "provider_runtime_id": "ax-note",
            "role": "text_input",
            "control_type": "edit",
            "automation_id": "note",
            "enabled": True,
            "focused": True,
            "bounds": {"x": 0.2, "y": 0.4, "w": 0.5, "h": 0.1},
            "backend_pixels": {"x": 200, "y": 400, "w": 500, "h": 40},
            "name": "note",
        },
    ]


def _mailbox(
    tmp_path: Path,
    *,
    claim_status: int = 201,
    claim_body: dict[str, Any] | None = None,
    poll_bodies: list[dict[str, Any] | None] | None = None,
) -> tuple[AuthoringRunner, list[httpx.Request], FakeRecorder, FakeAudit]:
    requests: list[httpx.Request] = []
    polls = list(poll_bodies or [])
    recorder = FakeRecorder()
    audit = FakeAudit()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/runner/claim"):
            if claim_status == 201:
                return httpx.Response(
                    201,
                    headers={"Cache-Control": "no-store"},
                    json=claim_body
                    or {"leaseSecret": LEASE, "lease_s": 900},
                )
            return httpx.Response(
                claim_status,
                headers={"Cache-Control": "no-store"},
                json={"error": "rejected"},
            )
        if path.endswith("/runner/poll"):
            body = json.loads(request.content)
            assert body["wait_seconds"] == 0
            assert body["lease_seconds"] == 900
            assert request.headers["Authorization"] == f"Bearer {LEASE}"
            if not polls:
                return httpx.Response(204)
            next_body = polls.pop(0)
            if next_body is None:
                return httpx.Response(204)
            return httpx.Response(
                200,
                headers={"Cache-Control": "no-store"},
                json=next_body,
            )
        if path.endswith("/runner/callback"):
            assert request.headers["Authorization"] == f"Bearer {LEASE}"
            return httpx.Response(
                202,
                headers={"Cache-Control": "no-store"},
                json={"accepted": True},
            )
        if path.endswith("/runner/allow"):
            assert request.headers["Authorization"] == f"Bearer {LEASE}"
            body = json.loads(request.content)
            assert body["command_id"]
            return httpx.Response(
                200,
                headers={"Cache-Control": "no-store"},
                json={"allowedAt": "2026-09-01T00:00:00Z", "client_display": "ChatGPT"},
            )
        return httpx.Response(404)

    client = httpx.Client(
        base_url=ORIGIN,
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    runner = AuthoringRunner(
        config,
        audit=audit,
        client=client,
        sleep=lambda _seconds: None,
        observe_nodes=_nodes,
        recorder_factory=lambda _out_dir: recorder,
        compile_recording=lambda _out_dir: SimpleNamespace(id="wf_mockmed"),
        playwright_launcher=lambda url: SimpleNamespace(url=url, cookies=lambda: []),
        text_value_at=lambda _pixels: "follow up in two weeks",
    )
    return runner, requests, recorder, audit


def _envelope(tool: str, *, sub: str = SUB, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.authoring.command/v1",
        "command_id": f"cmd_{tool}",
        "pack_id": PACK,
        "tool": tool,
        "args": args or {},
        "oauth_sub_sha256": sub,
        "client_id_sha256": CLIENT,
        "client_display": "ChatGPT",
    }


def test_claim_stores_lease_without_returning_the_secret(tmp_path: Path) -> None:
    runner, requests, _recorder, _audit = _mailbox(tmp_path)
    result = runner.claim_uri(URI, start_loop=False)
    assert result["bound"] is True
    assert "leaseSecret" not in result
    assert "lease_secret" not in result
    stored = load_authoring_lease(PACK)
    assert stored is not None
    assert stored["lease_secret"] == LEASE
    assert stored["origin"] == ORIGIN
    assert requests[0].url.path == f"/j/{PACK}/runner/claim"
    assert json.loads(requests[0].content) == {"bind": BIND}


@pytest.mark.parametrize("status", [409, 410, 404, 401])
def test_claim_maps_mailbox_failures(tmp_path: Path, status: int) -> None:
    runner, _requests, _recorder, _audit = _mailbox(tmp_path, claim_status=status)
    with pytest.raises(AuthoringError):
        runner.claim_uri(URI, start_loop=False)
    assert load_authoring_lease(PACK) is None


def test_poll_wait_is_zero_not_twenty_five(tmp_path: Path) -> None:
    assert POLL_WAIT_S == 0
    source = Path("engine/authoring_runner.py").read_text(encoding="utf-8")
    assert "DEFAULT_WAIT_S" not in source
    assert '"wait_seconds": POLL_WAIT_S' in source
    assert "from openadapt_flow.backends.win_agent" not in source
    assert "launch_agent(" not in source
    assert "import parallels_vm" not in source
    assert "hashlib.sha256(lease_secret" not in source
    assert "hashlib.sha256(stored[\"lease_secret\"]" not in source
    assert "bytes.fromhex(lease_secret[5:])" in source
    assert '"allow"' in source
    assert "PAUSE_PROMPT" in source
    runner, requests, _recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.poll_once()
    poll = next(item for item in requests if item.url.path.endswith("/poll"))
    assert json.loads(poll.content)["wait_seconds"] == 0


def test_bind_pack_allow_is_per_sub_and_required_for_halt(tmp_path: Path) -> None:
    runner, requests, recorder, _audit = _mailbox(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    runner.emit = lambda event, data: events.append((event, data))
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    assert any(
        event == "authoring_state" and data["status"] == "pending_allow"
        for event, data in events
    )
    runner.handle_envelope(_envelope("observe"))
    runner.handle_envelope(_envelope("halt"))
    denied = [
        json.loads(item.content)
        for item in requests
        if item.url.path.endswith("/callback")
    ]
    assert {item["result"]["error"] for item in denied} == {"not_allowed"}
    assert runner.allow()["allowed"] is True
    allow_req = next(item for item in requests if item.url.path.endswith("/allow"))
    assert json.loads(allow_req.content) == {"command_id": "cmd_bind_pack"}
    assert allow_req.headers["Authorization"] == f"Bearer {LEASE}"
    runner.handle_envelope(_envelope("observe"))
    observe = json.loads(requests[-1].content)["result"]
    assert observe["schema_version"] == "openadapt.authoring.observe/v1"
    assert "value" not in json.dumps(observe)
    assert "title" not in json.dumps(observe)
    assert "SSN-SECRET" not in json.dumps(observe)
    runner.handle_envelope(_envelope("halt", sub=OTHER_SUB))
    assert json.loads(requests[-1].content)["result"]["error"] == "not_allowed"
    runner.handle_envelope(_envelope("halt"))
    assert json.loads(requests[-1].content)["result"] == {"halted": True}
    assert recorder.clicks == []


def test_unsigned_stop_uses_lease_bearer_not_mcp_jwt(tmp_path: Path) -> None:
    runner, requests, _recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.operator_stop()
    callback = next(item for item in reversed(requests) if item.url.path.endswith("/callback"))
    body = json.loads(callback.content)
    assert body["halted"] is True
    assert callback.headers["Authorization"] == f"Bearer {LEASE}"


def test_continue_records_observed_on_pause_target_never_type_text(tmp_path: Path) -> None:
    runner, requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="web", url="https://openadapt.ai/mockmed")
    runner.handle_envelope(_envelope("start_record"))
    observe = runner._observe()
    note = next(node for node in observe["tree"] if node.get("automation_id") == "note")
    before = [item for item in requests if item.url.path.endswith("/callback")]
    runner.handle_envelope(
        _envelope("pause_for_input", args={"node_id": note["node_id"], "param": "note"})
    )
    after_pause = [item for item in requests if item.url.path.endswith("/callback")]
    assert after_pause == before
    assert runner.status_dict()["pause_prompt"] == (
        "Type in the application. Continue here when done."
    )
    runner.continue_pause()
    assert recorder.typed == []
    assert recorder.observed[0]["event"] == {"kind": "type"}
    assert recorder.observed[0]["text"] == "follow up in two weeks"
    assert "secret" not in recorder.observed[0] or recorder.observed[0].get("secret") is not True
    continued = json.loads(requests[-1].content)
    assert continued["result"] == {"recorded": True, "param": "note"}
    assert "text" not in continued["result"]
    assert "value" not in continued["result"]


def test_secret_continue_has_no_text_and_compile_refuses_if_missing(tmp_path: Path) -> None:
    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="macos", window_title_unique=True)
    runner.handle_envelope(_envelope("start_record"))
    observe = runner._observe()
    note = next(node for node in observe["tree"] if node.get("automation_id") == "note")
    runner.handle_envelope(
        _envelope(
            "pause_for_input",
            args={"node_id": note["node_id"], "param": "ssn", "secret": True},
        )
    )
    with pytest.raises(AuthoringError, match="secret_type_missing"):
        runner._compile()
    runner.continue_pause()
    assert "text" not in recorder.observed[0]
    assert recorder.observed[0]["secret"] is True
    compiled = runner._compile()
    assert compiled == {
        "status": "needs_human_admit",
        "workflow_id": "wf_mockmed",
        "recording_retained": True,
    }
    assert compiled.get("success") is not True


def test_click_uses_backend_pixels_and_stale_node_does_not_retry(tmp_path: Path) -> None:
    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="macos", window_title_unique=True)
    runner.handle_envelope(_envelope("start_record"))
    observe = runner._observe()
    button = next(node for node in observe["tree"] if node.get("automation_id") == "btnContinue")
    runner.handle_envelope(_envelope("click", args={"node_id": button["node_id"]}))
    assert recorder.clicks == [(1010, 658)]
    with pytest.raises(AuthoringError, match="stale_node"):
        runner._click({"node_id": "n_deadbeef"})
    runner._uncertain = True
    with pytest.raises(AuthoringError, match="RECONCILIATION_REQUIRED"):
        runner._click({"node_id": button["node_id"]})
    assert recorder.clicks == [(1010, 658)]


def test_linux_without_a_unique_title_is_coach_only(tmp_path: Path) -> None:
    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="linux", window_title_unique=False)
    observe = runner._observe()
    assert observe["coach_only"] is True
    with pytest.raises(AuthoringCoachOnly):
        runner._start_record()
    assert recorder.clicks == []


def test_windows_native_is_coach_only_and_does_not_start_recorder(tmp_path: Path) -> None:
    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="windows")
    observe = runner._observe()
    assert observe["coach_only"] is True
    assert observe["agent_drive"] is False
    assert observe["tree"] == []
    with pytest.raises(AuthoringCoachOnly):
        runner._start_record()
    with pytest.raises(AuthoringCoachOnly):
        runner._click({"node_id": "n_00000000"})
    assert recorder.clicks == []
    assert recorder.finished is False


def test_playwright_launcher_gets_the_desktop_url_not_an_mcp_string(tmp_path: Path) -> None:
    launched: list[str] = []
    runner, _requests, _recorder, _audit = _mailbox(tmp_path)
    runner._playwright_launcher = lambda url: launched.append(url) or SimpleNamespace(
        cookies=lambda: []
    )
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="web", url="https://openadapt.ai/mockmed")
    runner._start_record()
    assert launched == ["https://openadapt.ai/mockmed"]


def test_node_table_is_mode_0600(tmp_path: Path) -> None:
    runner, _requests, _recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner._observe()
    assert runner._node_table is not None
    mode = stat.S_IMODE(runner._node_table._path.stat().st_mode)
    assert mode == 0o600


def test_projector_drops_value_title_and_six_digit_names(tmp_path: Path) -> None:
    from engine.authoring_runner import NodeTable

    table = NodeTable(tmp_path / "nodes.json", b"key")
    payload = project_observe(
        backend="web",
        provider="playwright_ax",
        recording=False,
        agent_drive=True,
        coach_only=False,
        process_name="Chromium",
        raw_nodes=[
            {
                "provider_runtime_id": "x",
                "role": "field",
                "control_type": "edit",
                "automation_id": "ssn-123456",
                "name": "acct 1234567",
                "value": "000-00-0000",
                "title": "Secret",
                "bounds": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
                "backend_pixels": {"x": 1, "y": 1, "w": 2, "h": 2},
            }
        ],
        node_table=table,
    )
    dumped = json.dumps(payload)
    assert "000-00-0000" not in dumped
    assert "Secret" not in dumped
    assert "ssn-123456" not in dumped
    assert "acct 1234567" not in dumped
    assert "value" not in dumped
    assert payload["tree"][0]["node_id"].startswith("n_")


def test_dispatch_resume_and_stop_use_authoring_when_bound(tmp_path: Path) -> None:
    from engine.db import IndexDB
    from engine.dispatch import EngineDispatcher, EngineServices

    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="macos", window_title_unique=True)
    runner.handle_envelope(_envelope("start_record"))
    observe = runner._observe()
    note = next(node for node in observe["tree"] if node.get("automation_id") == "note")
    runner.handle_envelope(
        _envelope("pause_for_input", args={"node_id": note["node_id"], "param": "note"})
    )
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    disp = EngineDispatcher(
        runner.config,
        services=EngineServices(runner.config, db=db, authoring=runner, audit=FakeAudit()),
    )
    resumed = disp.dispatch("resume_recording", {})
    assert resumed["paused"] is False
    assert recorder.typed == []
    assert recorder.observed
    stopped = disp.dispatch("stop_recording", {})
    assert stopped["halted"] is True
    db.close()


def test_replace_allow_required_for_a_second_sub(tmp_path: Path) -> None:
    runner, _requests, _recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.handle_envelope(_envelope("bind_pack", sub=OTHER_SUB))
    assert runner.status()["status"] == "replace_allow"
    assert runner.allow()["allowed"] is True
    assert runner._allowed_sub == SUB
    runner.allow(replace=True)
    assert runner._allowed_sub == OTHER_SUB


def test_macos_without_unique_window_is_coach_only(tmp_path: Path) -> None:
    runner, _requests, recorder, _audit = _mailbox(tmp_path)
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    pinned = runner.pin_target(backend="macos", macos_window_title="Patient Jane Doe")
    assert pinned["coach_only"] is True
    observe = runner._observe()
    assert observe["coach_only"] is True
    assert "Patient Jane Doe" not in json.dumps(observe)
    with pytest.raises(AuthoringCoachOnly):
        runner._start_record()
    assert recorder.clicks == []


def test_use_frontmost_unique_window_stays_local(tmp_path: Path) -> None:
    runner, requests, _recorder, _audit = _mailbox(tmp_path)
    runner._unique_window = lambda: {
        "backend": "macos",
        "unique": True,
        "process_name": "Notes",
        "window_title": "Patient Jane Doe",
    }
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    pinned = runner.pin_target(use_frontmost=True)
    assert pinned == {"ok": True, "backend": "macos", "coach_only": False}
    assert "Patient Jane Doe" not in json.dumps(pinned)
    observe = runner._observe()
    assert observe["window"].get("process_name") == "Notes"
    assert "Patient Jane Doe" not in json.dumps(observe)
    assert all("Patient Jane Doe" not in item.content.decode("utf-8") for item in requests)


def test_playwright_rejects_nonempty_cookies(tmp_path: Path) -> None:
    runner, _requests, _recorder, _audit = _mailbox(tmp_path)
    runner._playwright_launcher = lambda _url: SimpleNamespace(cookies=lambda: [{"name": "sid"}])
    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="web", url="https://openadapt.ai/mockmed")
    with pytest.raises(AuthoringError, match="empty cookies"):
        runner._start_record()


def test_callback_strips_titles_and_never_retries_uncertain_click(tmp_path: Path) -> None:
    runner, requests, recorder, _audit = _mailbox(tmp_path)

    def _boom(_x: int, _y: int) -> None:
        raise RuntimeError("delivery unknown")

    runner.claim_uri(URI, start_loop=False)
    runner.handle_envelope(_envelope("bind_pack"))
    runner.allow()
    runner.pin_target(backend="macos", window_title_unique=True)
    runner.handle_envelope(_envelope("start_record"))
    observe = runner._observe()
    button = next(node for node in observe["tree"] if node.get("automation_id") == "btnContinue")
    recorder.click = _boom
    runner.handle_envelope(_envelope("click", args={"node_id": button["node_id"]}))
    body = json.loads(requests[-1].content)
    assert body["result"]["error"] == "RECONCILIATION_REQUIRED"
    assert "title" not in json.dumps(body)
    assert "backend_pixels" not in json.dumps(body)
    recorder.click = lambda x, y: recorder.clicks.append((x, y))
    with pytest.raises(AuthoringError, match="RECONCILIATION_REQUIRED"):
        runner._click({"node_id": button["node_id"]})
    assert recorder.clicks == []
