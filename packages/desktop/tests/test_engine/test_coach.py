"""Local-only coach payload: sanitize, omit unbound rings, never persist."""

from __future__ import annotations

from pathlib import Path

from engine.coach import (
    COACH_SCHEMA_VERSION,
    apply_coach_update,
    bind_coach_target,
    empty_coach,
    sanitize_hint,
)
from engine.config import EngineConfig
from engine.dispatch import EngineDispatcher, EngineServices
from engine.socket_server import _LOCAL_AGENT_COMMANDS, _TRAY_EVENTS, DesktopSocketServer

HMAC = "a" * 64


def test_sanitize_hint_keeps_playbook_copy_and_drops_identifiers() -> None:
    assert sanitize_hint("Open the claim screen") == "Open the claim screen"
    assert sanitize_hint("see https://openadapt.ai/j/1") is None
    assert sanitize_hint("mail jane@clinic.org") is None
    assert sanitize_hint("open record 123456") is None


def test_unbound_target_rect_is_omitted() -> None:
    assert (
        bind_coach_target(
            {
                "coordinate_space": "top_level_viewport_normalized",
                "rect": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            }
        )
        is None
    )
    bound = bind_coach_target(
        {
            "coordinate_space": "top_level_viewport_normalized",
            "rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            "binding": {
                "kind": "observation_hmac_sha256",
                "observation_hmac_sha256": HMAC,
            },
        }
    )
    assert bound is not None
    assert bound["binding"]["observation_hmac_sha256"] == HMAC


def test_apply_coach_update_drops_pack_url_and_screenshots() -> None:
    payload = apply_coach_update(
        empty_coach(),
        {
            "hint": "Open the claim screen",
            "turn": "your_turn",
            "pack_url": "https://openadapt.ai/j/secret",
            "screenshot": "data:image/png;base64,aaa",
        },
    )
    encoded = str(payload)
    assert payload["hint"] == "Open the claim screen"
    assert payload["schema_version"] == COACH_SCHEMA_VERSION
    assert "openadapt.ai" not in encoded
    assert "screenshot" not in encoded
    assert "pack_url" not in encoded


def test_dispatcher_set_coach_is_memory_only_and_emits_locally(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    disp = EngineDispatcher(
        config,
        services=EngineServices(config),
        emit=lambda event, data: events.append((event, data)),
    )
    result = disp.dispatch(
        "set_coach",
        {"hint": "Open the claim screen", "turn": "your_turn"},
    )
    assert result["hint"] == "Open the claim screen"
    assert disp.dispatch("get_coach", {})["hint"] == "Open the claim screen"
    assert events[-1][0] == "coach"
    assert events[-1][1]["hint"] == "Open the claim screen"
    cleared = disp.dispatch("clear_coach", {})
    assert cleared["hint"] is None
    assert disp.dispatch("get_coach", {})["hint"] is None


def test_coach_event_is_not_tray_vocabulary() -> None:
    assert "coach" not in _TRAY_EVENTS
    assert "set_coach" in _LOCAL_AGENT_COMMANDS


def test_socket_set_coach_replies_on_that_connection_only(tmp_path: Path) -> None:
    import json
    import socket
    import time

    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    disc = tmp_path / "desktop_ipc.json"
    srv = DesktopSocketServer(config, discovery_path=disc)
    srv.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect((srv.host, srv.port))
        time.sleep(0.2)
        sock.sendall(
            (
                json.dumps(
                    {
                        "type": "set_coach",
                        "token": srv.token,
                        "data": {"hint": "Open the claim screen", "turn": "your_turn"},
                    }
                )
                + "\n"
            ).encode()
        )
        buf = ""
        deadline = time.time() + 5.0
        while "\n" not in buf and time.time() < deadline:
            buf += sock.recv(4096).decode("utf-8")
        event = json.loads(buf.split("\n", 1)[0])
        assert event["type"] == "coach"
        assert event["data"]["hint"] == "Open the claim screen"
        srv._broadcast("coach", {"hint": "must not leak to tray"})
    finally:
        sock.close()
        srv.stop()


def test_presentation_export_does_not_consume_the_coach_channel() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "engine/presentation_export.py").read_text(encoding="utf-8")
    assert "overlay://coach" in source
    assert "openadapt.control-overlay-coach/v1" in source
    assert "TIMELINE_SCHEMA = \"openadapt.control-overlay-timeline/v2\"" in source
    assert "control-overlay-coach" not in source.split("TIMELINE_SCHEMA", 1)[1]
