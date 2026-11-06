from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
import requests
from PIL import Image

from openadapt_flow.backends.win_agent import server as agent_server
from openadapt_flow.backends.win_agent.tls import generate_self_signed_cert
from tests.e2e import win_agent_capture_telemetry as capture_telemetry


def _png(rgb: tuple[int, int, int], *, compression: int) -> bytes:
    import io

    image = Image.new("RGB", (3, 2), rgb)
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=compression)
    return output.getvalue()


class _RunningServer:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        frames: list[bytes],
        *,
        sidecar: Path | None = None,
    ) -> None:
        self.cursor = 0
        self.frames = frames
        self.context_calls = 0
        self.input_calls: list[dict[str, Any]] = []
        self.processing_events: list[str] = []
        self.cert = generate_self_signed_cert(dirpath=str(tmp_path / "tls"))
        self.sidecar = sidecar or (tmp_path / "capture-sidecar.json")
        self.telemetry = capture_telemetry.CaptureTelemetry(self.sidecar)
        original_capture = self.telemetry.capture

        def tracked_capture(png: bytes) -> int | None:
            self.processing_events.append("capture")
            return original_capture(png)

        self.telemetry.capture = tracked_capture
        original_factory = agent_server.make_handler_class

        def tracked_factory(config, grab_fn, *args, **kwargs):
            handler = original_factory(config, grab_fn, *args, **kwargs)
            original_send_json = handler._send_json

            def tracked_send_json(instance, status, payload):
                result = original_send_json(instance, status, payload)
                if status == 409 and isinstance(payload, dict):
                    if payload.get("code") == "stale_frame":
                        self.processing_events.append("stale_response")
                return result

            handler._send_json = tracked_send_json
            return handler

        monkeypatch.setattr(
            agent_server,
            "make_handler_class",
            capture_telemetry.instrument_make_handler_class(
                tracked_factory, self.telemetry
            ),
        )

        def grab() -> bytes:
            index = min(self.cursor, len(self.frames) - 1)
            self.cursor += 1
            return self.frames[index]

        def context() -> dict[str, str]:
            self.context_calls += 1
            self.processing_events.append("context")
            return {
                "application": "fixture-app",
                "session": "fixture-session",
                "workflow_state": "fixture-state",
            }

        def input_action(payload: dict[str, Any]) -> dict[str, str]:
            self.input_calls.append(payload)
            self.processing_events.append("input")
            return {"status": "ok"}

        self.httpd = agent_server.create_server(
            agent_server.AgentConfig(
                host="127.0.0.1",
                port=0,
                token="synthetic-agent-token",
                certfile=self.cert.certfile,
                keyfile=self.cert.keyfile,
            ),
            grab_fn=grab,
            context_fn=context,
            input_fn=input_action,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"https://127.0.0.1:{self.httpd.server_port}"
        self.session = requests.Session()
        self.session.verify = self.cert.certfile
        self.session.headers.update(
            {"Authorization": "Bearer synthetic-agent-token"}
        )

    def close(self) -> None:
        self.session.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _guard(session: requests.Session, url: str, expected_png: bytes):
    return session.post(
        f"{url}/input/guarded",
        json={
            "expected_frame_sha256": hashlib.sha256(expected_png).hexdigest(),
            "expected_context": {
                "application": "fixture-app",
                "session": "fixture-session",
                "workflow_state": "fixture-state",
            },
            "input": {"action": "click", "x": 1, "y": 1},
        },
        timeout=5,
    )


@pytest.mark.parametrize(
    ("current", "expected_code"),
    [
        pytest.param(_png((10, 20, 30), compression=9), "encoding_only_change", id="same-rgb"),
        pytest.param(_png((90, 20, 30), compression=9), "decoded_raster_changed", id="changed-rgb"),
    ],
)
def test_guard_refusal_correlates_expected_and_exact_request_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    current: bytes,
    expected_code: str,
) -> None:
    expected = _png((10, 20, 30), compression=0)
    assert expected != current
    running = _RunningServer(monkeypatch, tmp_path, [expected, current])
    try:
        unauthorized = requests.get(
            f"{running.url}/screenshot", verify=running.cert.certfile, timeout=5
        )
        assert unauthorized.status_code == 401
        screenshot = running.session.get(f"{running.url}/screenshot", timeout=5)
        assert screenshot.status_code == 200 and screenshot.content == expected
        response = _guard(running.session, running.url, screenshot.content)
        assert response.status_code == 409
        assert response.json()["code"] == "stale_frame"
        assert running.context_calls == 0
        assert running.input_calls == []
        projection = capture_telemetry.safe_projection(running.sidecar)
        assert projection["status"] == "available"
        if os.name != "nt":
            assert stat.S_IMODE(running.sidecar.stat().st_mode) == 0o600
        stored = json.loads(running.sidecar.read_text(encoding="utf-8"))
        assert set(stored) == {"schema", "captures", "guarded_refusals"}
        assert set(stored["captures"][0]) == {
            "sequence",
            "png_sha256",
            "rgb_sha256",
            "width",
            "height",
        }
        (diagnosis,) = projection["diagnoses"]
        assert diagnosis["status"] == "classified"
        assert diagnosis["code"] == expected_code
        assert diagnosis["expected_dimensions"] == [3, 2]
        assert diagnosis["current_dimensions"] == [3, 2]
    finally:
        running.close()


def test_matching_frame_preserves_tls_auth_capture_count_and_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = _png((10, 20, 30), compression=6)
    running = _RunningServer(monkeypatch, tmp_path, [frame, frame])
    try:
        unauthorized = requests.get(
            f"{running.url}/screenshot", verify=running.cert.certfile, timeout=5
        )
        assert unauthorized.status_code == 401
        screenshot = running.session.get(f"{running.url}/screenshot", timeout=5)
        assert screenshot.status_code == 200 and screenshot.content == frame
        response = _guard(running.session, running.url, screenshot.content)
        assert response.status_code == 200
        assert running.cursor == 2
        assert running.context_calls == 1
        assert len(running.input_calls) == 1
        assert running.processing_events.index("input") < len(running.processing_events) - 1
        assert running.processing_events[-1] == "capture"
        assert capture_telemetry.safe_projection(running.sidecar) == {
            "status": "available",
            "diagnoses": [],
        }
    finally:
        running.close()


@pytest.mark.parametrize("scenario", ["missing", "ambiguous"], ids=["missing-provenance", "ambiguous-provenance"])
def test_projection_refuses_unproven_capture_correlation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    first = _png((10, 20, 30), compression=6)
    current = _png((90, 20, 30), compression=6)
    frames = [first, current] if scenario == "missing" else [first, first, current]
    running = _RunningServer(monkeypatch, tmp_path, frames)
    try:
        prior = 1 if scenario == "missing" else 2
        for _ in range(prior):
            response = running.session.get(f"{running.url}/screenshot", timeout=5)
            assert response.status_code == 200
        expected = _png((200, 1, 2), compression=6) if scenario == "missing" else first
        response = _guard(running.session, running.url, expected)
        assert response.status_code == 409
        assert response.json()["code"] == "stale_frame"
        assert running.context_calls == 0 and running.input_calls == []
        projection = capture_telemetry.safe_projection(running.sidecar)
        (diagnosis,) = projection["diagnoses"]
        assert diagnosis == {
            "phase": "input_guard_frame_compare",
            "status": "unknown",
            "code": "ambiguous_or_missing_provenance",
        }
    finally:
        running.close()


def test_sidecar_failure_does_not_change_capture_or_guard_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _png((10, 20, 30), compression=6)
    current = _png((90, 20, 30), compression=6)
    missing_parent = tmp_path / "absent" / "capture.json"
    running = _RunningServer(
        monkeypatch, tmp_path, [first, current], sidecar=missing_parent
    )
    try:
        response = running.session.get(f"{running.url}/screenshot", timeout=5)
        assert response.status_code == 200 and response.content == first
        refused = _guard(running.session, running.url, response.content)
        assert refused.status_code == 409
        assert refused.json()["code"] == "stale_frame"
        assert running.context_calls == 0 and running.input_calls == []
        assert running.processing_events.index("stale_response") < len(
            running.processing_events
        ) - 1
        assert running.processing_events[-1] == "capture"
        assert capture_telemetry.safe_projection(missing_parent)["status"] == "unavailable"
    finally:
        running.close()


def test_sidecar_update_refuses_to_replace_changed_foreign_content(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / "capture-sidecar.json"
    telemetry = capture_telemetry.CaptureTelemetry(sidecar)
    first = _png((10, 20, 30), compression=6)
    changed = _png((90, 20, 30), compression=6)

    assert telemetry.capture(first) == 1
    foreign_content = b"foreign sidecar state"
    sidecar.write_bytes(foreign_content)

    assert telemetry.capture(changed) is None
    assert sidecar.read_bytes() == foreign_content


def test_telemetry_exception_does_not_mask_guard_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _png((10, 20, 30), compression=6)
    current = _png((90, 20, 30), compression=6)
    running = _RunningServer(monkeypatch, tmp_path, [first, current])

    def fail_capture(_png_bytes: bytes) -> int:
        raise RuntimeError("synthetic telemetry failure")

    running.telemetry.capture = fail_capture  # type: ignore[method-assign]
    try:
        screenshot = running.session.get(f"{running.url}/screenshot", timeout=5)
        assert screenshot.status_code == 200 and screenshot.content == first
        refused = _guard(running.session, running.url, screenshot.content)
        assert refused.status_code == 409
        assert refused.json()["code"] == "stale_frame"
        assert running.context_calls == 0 and running.input_calls == []
    finally:
        running.close()


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "wrong", "captures": [], "guarded_refusals": []},
        {
            "schema": "openadapt.test-win-agent-capture-telemetry/v1",
            "captures": [],
            "guarded_refusals": [],
            "raw": "must-not-project",
        },
        {
            "schema": "openadapt.test-win-agent-capture-telemetry/v1",
            "captures": [
                {"sequence": True, "png_sha256": "0" * 64, "rgb_sha256": "0" * 64,
                 "width": 1, "height": 1}
            ],
            "guarded_refusals": [],
        },
    ],
)
def test_strict_projection_returns_only_fixed_unavailable_on_bad_sidecar(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    path = tmp_path / "sidecar.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert capture_telemetry.safe_projection(path) == {
        "status": "unavailable",
        "code": "invalid_capture_telemetry",
    }


def test_projection_rejects_oversized_capture_ring(tmp_path: Path) -> None:
    path = tmp_path / "sidecar.json"
    row = {
        "sequence": 1,
        "png_sha256": "0" * 64,
        "rgb_sha256": "1" * 64,
        "width": 1,
        "height": 1,
    }
    path.write_text(
        json.dumps(
            {
                "schema": "openadapt.test-win-agent-capture-telemetry/v1",
                "captures": [dict(row, sequence=sequence) for sequence in range(1, 34)],
                "guarded_refusals": [],
            }
        ),
        encoding="utf-8",
    )
    assert capture_telemetry.safe_projection(path) == {
        "status": "unavailable",
        "code": "invalid_capture_telemetry",
    }


def test_wrapper_main_strips_only_its_option_and_delegates_cli_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[list[str] | None] = []
    monkeypatch.setattr(
        agent_server,
        "main",
        lambda argv=None: observed.append(argv),
    )
    capture_telemetry.main(
        [
            "--capture-telemetry-sidecar",
            str(tmp_path / "telemetry.json"),
            "--host",
            "127.0.0.1",
            "--port",
            "43210",
            "--token",
            "synthetic-token",
            "--certfile",
            "synthetic-cert.pem",
            "--keyfile",
            "synthetic-key.pem",
        ]
    )
    assert observed == [
        [
            "--host",
            "127.0.0.1",
            "--port",
            "43210",
            "--token",
            "synthetic-token",
            "--certfile",
            "synthetic-cert.pem",
            "--keyfile",
            "synthetic-key.pem",
        ]
    ]
